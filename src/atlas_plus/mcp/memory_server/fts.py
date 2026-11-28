"""SQLite FTS5 index over .memory/ for memory.search.

Index lives at <ATLAS_STATE_DIR>/search.sqlite (its own file since 2026-11-09), table memory_fts.
Auto-rebuilds when any .memory/ file is newer than the index timestamp.

Re-ranking blends three signals (all normalised 0-1):
  BM25 relevance   -- FTS5 rank, 65% weight
  mtime recency    -- file modification age vs 1-year window, 20% weight
  confirmation age -- last_confirmed frontmatter field vs 1-year window, 15% weight

Scope authority multiplier: 1.2x for feedback/ and identity/ files.
"""

import functools
import json
import logging
import os
import re
import sqlite3
import sys
import unicodedata
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import yaml
from atlas_plus._yaml import load_mapping_or_none

# libyaml when present: the pure-Python loader was most of the rebuild time (AG23, sweep 12).
_YAML_LOADER = getattr(yaml, "CSafeLoader", yaml.SafeLoader)

from .config import EXCLUDED_DIRS, FTS_DB, MEMORY_ROOT

log = logging.getLogger("atlas.mcp.fts")

_CREATE_META = """
CREATE TABLE IF NOT EXISTS atlas_meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""

# unicode61 folds case and, with remove_diacritics, accents: 'zurich' finds "Zürich" (AG8,
# sweep 10; the old 'porter ascii' tokenizer did not). A changed tokenizer is detected at
# connect time and the index is rebuilt, since the virtual table bakes the tokenizer in.
# unicode61 treats every combining mark as a separator, so a Devanagari word with vowel signs was indexed as its
# bare consonants and `दिन` matched `दान` (MN49). The marks of the scripts whose words carry them are listed as
# token characters; Arabic and Hebrew points are folded away before indexing instead (a plain query must meet a
# vocalised note). A changed string here rebuilds every index (the tokenizer tag).
_INDIC_MARK_RANGES = (
    (0x0900, 0x0903), (0x093A, 0x093C), (0x093E, 0x094F), (0x0951, 0x0957), (0x0962, 0x0963), (0x0981, 0x0983), (0x09BC, 0x09C4),
    (0x09C7, 0x09C8), (0x09CB, 0x09CD), (0x0A01, 0x0A03), (0x0A3C, 0x0A51), (0x0A70, 0x0A71), (0x0A81, 0x0A83), (0x0ABC, 0x0ACD),
    (0x0B01, 0x0B03), (0x0B3C, 0x0B57), (0x0B82, 0x0B82), (0x0BBE, 0x0BCD), (0x0C00, 0x0C04), (0x0C3E, 0x0C56), (0x0C81, 0x0C83),
    (0x0CBC, 0x0CCD), (0x0D00, 0x0D03), (0x0D3E, 0x0D4D), (0x0DCA, 0x0DDF), (0x0E31, 0x0E31), (0x0E34, 0x0E3A), (0x0E47, 0x0E4E),
    (0x0EB1, 0x0EB1), (0x0EB4, 0x0EBC), (0x0EC8, 0x0ECD), (0x0F71, 0x0F84), (0x0F86, 0x0F87), (0x102B, 0x103E), (0x1056, 0x1059),
    (0x105E, 0x1060), (0x1062, 0x1064), (0x1067, 0x106D), (0x1071, 0x1074), (0x1082, 0x108D), (0x108F, 0x108F), (0x109A, 0x109D),
    (0x17B4, 0x17D3), (0x17DD, 0x17DD),
    # AG63: Sinhala anusvara/visarga, the Malayalam/Bengali/Tamil/Kannada length marks, Tibetan subjoined letters and signs;
    # the joiners (U+200C/D) are NOT token characters: Persian `کتاب‌ها` must still meet `کتاب` and `राम` must meet `रा\u200dम` (AG61)
    (0x0D81, 0x0D83), (0x0D57, 0x0D57), (0x09D7, 0x09D7), (0x0BD7, 0x0BD7), (0x0CD5, 0x0CD6),
    (0x0F18, 0x0F19), (0x0F35, 0x0F35), (0x0F37, 0x0F37), (0x0F39, 0x0F39), (0x0F3E, 0x0F3F), (0x0F90, 0x0FBC),
)
# AG83: emoji, pictographs, dingbats, arrows, regional indicators, keycap and tag characters were separators to
# unicode61, so `🚀`, `👨‍👩‍👧` and `🇯🇵` found nothing at all. They are token characters now; the index and the query
# put a separator around every emoji cluster (`_emoji_split`), so `rocket🚀fuel` stays three tokens and `a→b` two.
_EMOJI_RANGES = (
    (0x1F000, 0x1FAFF), (0x2600, 0x27BF), (0x2300, 0x23FF), (0x2B00, 0x2BFF), (0x2190, 0x21FF), (0x25A0, 0x25FF),
    (0x00A9, 0x00A9), (0x00AE, 0x00AE), (0x3030, 0x3030), (0x303D, 0x303D), (0x3297, 0x3297), (0x3299, 0x3299),
    (0x20E3, 0x20E3), (0xE0020, 0xE007F),
)
_TOKENCHARS = "".join(chr(c) for a, b in _INDIC_MARK_RANGES + _EMOJI_RANGES for c in range(a, b + 1))
_TOKENIZER = "porter unicode61 remove_diacritics 2 tokenchars ''" + _TOKENCHARS + "''"
_TOKENIZER_FALLBACK = "porter unicode61 remove_diacritics 1 tokenchars ''" + _TOKENCHARS + "''"   # SQLite before 3.27
# Index text format tag, stored with the tokenizer name in atlas_meta: a change here rebuilds
# every existing index once (the format is baked into the stored text, like the tokenizer).
_INDEX_TAG = " +cjk2 +fmu"   # +fmu: the raw frontmatter_json column is stored, not tokenised (AG99: its KEYS matched `source`)
_MAX_FM_CHARS = 262_144         # frontmatter larger than this is not parsed (AG22, sweep 12)
_MAX_FM_INDEX_CHARS = 65_536    # at most this much frontmatter text is indexed per note

# unicode61 has no notion of CJK word boundaries: a run of ideographs between spaces was ONE
# token, so `東京都` never matched `東京` and `人口` found nothing inside a sentence (AG17,
# sweep 11). The index stores a WORD JOINER (U+2060, a separator to unicode61 and invisible on
# screen) between adjacent CJK characters, and a query turns each CJK run into a phrase of those
# characters, so consecutive-character matching works the way CJK readers expect. Text is also
# NFKC-folded on both sides (full-width ABC, half-width katakana).
_CJK_SEP = "\u2060"
_CJK_RE = re.compile(
    "[\u1100-\u11ff\u2e80-\u2fdf\u3040-\u30ff\u3130-\u318f\u31f0-\u31ff\u3400-\u4dbf"
    "\u4e00-\u9fff\ua960-\ua97f\uac00-\ud7ff\uf900-\ufaff\U00020000-\U0003134f"
    "\u0e00-\u0e7f\u0e80-\u0eff\u1000-\u109f\u1780-\u17ff]")  # Thai, Lao, Burmese, Khmer: no word spaces either; a mid-run Thai word was unsearchable (AG52)
_CJK_RUN_RE = re.compile(_CJK_RE.pattern + "{2,}")
_CJK_RUN_OR_CHAR_RE = re.compile(_CJK_RE.pattern + "+")
# A CJK character glued to a Latin letter or digit fused into one token: `2025年`, `Python编程`
# and `iPhone15在` were unsearchable (MN13, MN17, sweep 11). The separator also goes on those
# boundaries, in the index and in the query.
_CJK_ASCII_BOUNDARY_RE = re.compile(
    "(?<=" + _CJK_RE.pattern + ")(?=[0-9A-Za-z])|(?<=[0-9A-Za-z])(?=" + _CJK_RE.pattern + ")")
# A phrase of consecutive characters matched ACROSS a full stop (`河北。京剧` answered `北京`,
# MN17): sentence punctuation and line breaks become a private-use token (a token character to
# unicode61, never typed by a user) that breaks adjacency. Stripped from snippets.
_SENTINEL = "\ue000"
_BREAK_RE = re.compile("[\n\u3001\u3002\uff0c\uff0e\uff1b\uff1a\uff01\uff1f\u300c\u300d\u300e\u300f"
                       "\uff08\uff09\u3010\u3011]+|[.,;:!?]+(?=\\s|$)")   # `\\s`: a SyntaxWarning on first run (AG24)


def _cjk_split(text: str) -> str:
    """Insert the separator between adjacent CJK characters and on CJK/ASCII boundaries."""
    if _CJK_RE.search(text) is None:
        # Both substitutions need a CJK character to match; one scan instead of two on the common
        # case (SEX-046: 10 of 113 s of a 20,000-note rebuild went here on text with none).
        return text
    text = _CJK_RUN_RE.sub(lambda m: _CJK_SEP.join(m.group(0)), text)
    return _CJK_ASCII_BOUNDARY_RE.sub(_CJK_SEP, text)


_MARKS_RE = re.compile("[\u064b-\u0652\u0670\u0640\u0591-\u05bd\u05bf\u05c1\u05c2\u05c4\u05c5\u05c7]")  # Arabic harakat and tatweel, Hebrew points (AG52); maqaf, paseq and sof pasuq are punctuation and stay (MN43: `בית־ספר` lost `ספר`)

# Combining-mark ranges of the scripts the corpus meets (Latin, Cyrillic, Hebrew, Arabic, the Indic scripts, Thai, Lao,
# Tibetan, Myanmar, Khmer, the generic blocks, kana voicing marks, variation selectors). `\w` stops at every one of
# them, so a word with vowel signs shattered into fragments; deleting them merged different words (दिन/दान: AG60).
_MARK_RANGES = (
    (0x0300, 0x036F), (0x0483, 0x0489), (0x0591, 0x05BD), (0x05BF, 0x05BF), (0x05C1, 0x05C2), (0x05C4, 0x05C5), (0x05C7, 0x05C7),
    (0x0610, 0x061A), (0x064B, 0x065F), (0x0670, 0x0670), (0x06D6, 0x06DC), (0x06DF, 0x06E4), (0x06E7, 0x06E8), (0x06EA, 0x06ED),
    (0x0900, 0x0903), (0x093A, 0x093C), (0x093E, 0x094F), (0x0951, 0x0957), (0x0962, 0x0963), (0x0981, 0x0983), (0x09BC, 0x09C4),
    (0x09C7, 0x09C8), (0x09CB, 0x09CD), (0x0A01, 0x0A03), (0x0A3C, 0x0A51), (0x0A70, 0x0A71), (0x0A81, 0x0A83), (0x0ABC, 0x0ACD),
    (0x0B01, 0x0B03), (0x0B3C, 0x0B57), (0x0B82, 0x0B82), (0x0BBE, 0x0BCD), (0x0C00, 0x0C04), (0x0C3E, 0x0C56), (0x0C81, 0x0C83),
    (0x0CBC, 0x0CCD), (0x0D00, 0x0D03), (0x0D3E, 0x0D4D), (0x0DCA, 0x0DDF), (0x0E31, 0x0E31), (0x0E34, 0x0E3A), (0x0E47, 0x0E4E),
    (0x0EB1, 0x0EB1), (0x0EB4, 0x0EBC), (0x0EC8, 0x0ECD), (0x0F71, 0x0F84), (0x0F86, 0x0F87), (0x102B, 0x103E), (0x1056, 0x1059),
    (0x105E, 0x1060), (0x1062, 0x1064), (0x1067, 0x106D), (0x1071, 0x1074), (0x1082, 0x108D), (0x108F, 0x108F), (0x109A, 0x109D),
    (0x17B4, 0x17D3), (0x17DD, 0x17DD), (0x1AB0, 0x1AFF), (0x1DC0, 0x1DFF), (0x20D0, 0x20F0), (0x302A, 0x302F), (0x3099, 0x309A),
    (0xFE00, 0xFE0F), (0xFE20, 0xFE2F),
    (0x0D81, 0x0D83), (0x0D57, 0x0D57), (0x09D7, 0x09D7), (0x0BD7, 0x0BD7), (0x0CD5, 0x0CD6), (0x0F90, 0x0FBC), (0x0F3E, 0x0F3F),  # AG63
)
_MARK_CLASS = "".join(chr(a) + "-" + chr(b) if a != b else chr(a) for a, b in _MARK_RANGES)
_WORD_RE = re.compile("[\\w" + _MARK_CLASS + "]+")  # a word of any script with its marks attached (AG60)


_INDIC_JOINER_RE = re.compile("(?<=[\u0900-\u0dff])[\u200c\u200d]+(?=[\u0900-\u0dff])")


_GREEK_ETC = str.maketrans({
    "\u03ac": "\u03b1", "\u03ad": "\u03b5", "\u03ae": "\u03b7", "\u03af": "\u03b9", "\u03cc": "\u03bf", "\u03cd": "\u03c5", "\u03ce": "\u03c9",
    "\u03ca": "\u03b9", "\u03cb": "\u03c5", "\u0390": "\u03b9", "\u03b0": "\u03c5",
    "\u0386": "\u0391", "\u0388": "\u0395", "\u0389": "\u0397", "\u038a": "\u0399", "\u038c": "\u039f", "\u038e": "\u03a5", "\u038f": "\u03a9",
    "\u03aa": "\u0399", "\u03ab": "\u03a5", "\u1e9e": "ss", "\u0259": "e", "\u018f": "E",
    # MN74: letters with a stroke or a slash have no decomposition, so remove_diacritics kept them and `da nang`,
    # `duoc`, `lodz`, `kobenhavn`, `gudrun` found nothing (Vietnamese is very often typed without its marks)
    "\u0111": "d", "\u0110": "D", "\u0142": "l", "\u0141": "L", "\u00f8": "o", "\u00d8": "O",
    "\u00f0": "d", "\u00d0": "D", "\u00fe": "th", "\u00de": "TH",
    # MN78: the other stroke letters (Maltese h, Sami t) and the ae/oe ligatures have no decomposition either:
    # `hamrun` (Hamrun with a stroke), `aeroskobing`, `coeur` found nothing
    "\u0127": "h", "\u0126": "H", "\u0167": "t", "\u0166": "T",
    "\u00e6": "ae", "\u00c6": "AE", "\u0153": "oe", "\u0152": "OE",
})


# the characters _GREEK_ETC folds (Greek U+0386..U+03CE and the letters of its table), as one compiled class (MN87)
_GREEK_ETC_RE = re.compile("[\u0386-\u03ce\u1e9e\u0259\u018f\u0111\u0110\u0142\u0141\u00f8\u00d8\u00f0\u00d0\u00fe\u00de"
                           "\u0127\u0126\u0167\u0166\u00e6\u00c6\u0153\u0152]")


def _recompose(text: str) -> str:
    """NFKC decomposes Thai sara am (U+0E33) into nikhahit + sara aa and Lao am (U+0EB3) likewise; the index and
    the snippets then showed `ทํา` for `ทำ` and `น้ำ` met `นา` (MN52). Put them back."""
    if "\u0e4d\u0e32" in text or "\u0ecd\u0eb2" in text:
        text = text.replace("\u0e4d\u0e32", "\u0e33").replace("\u0ecd\u0eb2", "\u0eb3")
    if "\u200c" in text or "\u200d" in text:
        text = _INDIC_JOINER_RE.sub("", text)  # ZWJ/ZWNJ between Indic letters are typed or not by keyboard: `राम` must meet `रा\u200dम` (MN55); between Persian letters the ZWNJ stays a separator
    if _GREEK_ETC_RE.search(text):  # one C-level scan: a Python generator over every character was 40% of a rebuild (MN87)
        text = text.translate(_GREEK_ETC)  # Greek tonos/dialytika, capital ẞ, Azeri ə: unicode61 folds none of them (MN62)
    if "\u0130" in text or "\u0131" in text:
        text = text.replace("\u0130", "i").replace("\u0131", "i")  # Turkish dotted I and dotless i: `Diyarbak\u0131r` met neither `diyarbakir` nor `D\u0130YARBAKIR` (AG72)
    if "\u0451" in text or "\u0401" in text or "\u00df" in text:
        text = text.replace("\u0451", "\u0435").replace("\u0401", "\u0415").replace("\u00df", "ss")  # ё is typed as е and ß as ss (MN58: `елка` missed `ёлка`, `Strasse` missed `Straße`)
    if ("\u05f4" in text or '"' in text or "\u201c" in text or "\u201d" in text) and _HE_ACRONYM_RE.search(text):
        text = _HE_ACRONYM_RE.sub("", text)  # AG89: a Hebrew acronym is one word (`צה״ל` = `צה"ל` = `צהל`), so a prefix splits off it
    return text


# AG89: gershayim (or the ASCII quote, or a curly quote typed for it) between two Hebrew letters marks an acronym, not
# a phrase quote: `בצה״ל` was the tokens `בצה` + `ל`, so `צה״ל` never found it, and the ASCII form became the FTS5
# phrase `"ל` that bracketed every ל-prefixed word. Index, query and every fallback drop the mark (points may sit before
# it); the snippet is cut from the note's own text, so it still shows the gershayim.
_HE_ACRONYM_RE = re.compile('(?<=[\u05d0-\u05ea\u0591-\u05c7])["\u05f4\u201c\u201d](?=[\u05d0-\u05ea])')


def _fold_marks(text: str) -> str:
    return _MARKS_RE.sub("", text)


_EMOJI_CLASS = "".join(chr(a) + "-" + chr(b) if a != b else chr(a) for a, b in _EMOJI_RANGES)
# one cluster: a flag (two regional indicators), a keycap, or a pictograph with its presentation selector, skin tone,
# tag sequence and ZWJ continuations; every lone emoji code point is a cluster of its own
_EMOJI_RE = re.compile(
    "[\U0001F1E6-\U0001F1FF]{2}"
    "|[0-9#*][\ufe0e\ufe0f]?\u20e3"
    "|[" + _EMOJI_CLASS + "][\ufe0e\ufe0f]?[\U0001F3FB-\U0001F3FF]?[\U000E0020-\U000E007F]*"
    "(?:\u200d[" + _EMOJI_CLASS + "][\ufe0e\ufe0f]?[\U0001F3FB-\U0001F3FF]?[\U000E0020-\U000E007F]*)*")
_EMOJI_DROP = str.maketrans("", "", "\ufe0e\ufe0f\u200d")
_SKIN_RE = re.compile("(?<=.)(?=[\U0001F3FB-\U0001F3FF]$)")


def _emoji_canon(cluster: str) -> str:
    """No VS15/VS16 and no ZWJ (`❤️` meets `❤`; the family sequence and a tag-sequence flag are one token each); a
    lone emoji's skin tone is a token of its own right after it, so `👍` finds `👍🏽` and `👍🏽` asks for both."""
    c = cluster.translate(_EMOJI_DROP)
    if "\u200d" in cluster:
        # MN74: a toned ZWJ sequence split at its tone into two tokens, so the untoned sequence never found it and
        # the snippet closed mid-sequence; the untoned sequence is the token and each tone follows as its own
        tones = [ch for ch in c if "\U0001F3FB" <= ch <= "\U0001F3FF"]
        base = "".join(ch for ch in c if not "\U0001F3FB" <= ch <= "\U0001F3FF")
        return _CJK_SEP.join([base] + tones) if tones and base else c
    return _SKIN_RE.sub(_CJK_SEP, c) if len(c) == 2 else c


def _emoji_split(text: str, sep: str = _CJK_SEP) -> str:
    """Each emoji cluster becomes one token in its canonical form, with a separator on each side (AG83)."""
    if text.isascii():
        return text
    return _EMOJI_RE.sub(lambda m: sep + _emoji_canon(m.group(0)) + sep, text)


# AG83: Arabic and Hebrew glue the article, the conjunctions and the one-letter prepositions to the word, so `الخادم`
# never met `خادم` and `השרת` never met `שרת`. The index keeps the prefix as its own token behind an invisible
# separator (`ال\u2060خادم`: the snippet still reads `الخادم`) and an unquoted query word drops it, so both spellings
# meet; a quoted phrase keeps it (exact form). A prefix is split only when the stem left behind is long enough:
# `الله`, `بيت`, `בית`, `מלך` stay whole (measured on 50k-word frequency lists: see tests/test_ag83.py).
_AR_LETTERS = "\u0621-\u063a\u0641-\u064a\u0671-\u06d3\u06fa-\u06fc"
# only the words a rule can change: an Arabic-script word that starts with a prefix letter or ends in ها, a Hebrew word
# that starts with one of its prefix letters (a callback per word was most of the cost: AG83 measured 2.5x rebuilds)
_AR_WORD_RE = re.compile("(?<![" + _AR_LETTERS + "])(?:(?=[\u0648\u0641\u0628\u0643\u0644\u0627])|(?=[" + _AR_LETTERS
                         + "]*\u0647\u0627))[" + _AR_LETTERS + "]{4,}(?![" + _AR_LETTERS + "])")
_HE_WORD_RE = re.compile("(?<![\u05d0-\u05ea])[\u05d5\u05d4\u05d1\u05dc\u05de][\u05d0-\u05ea]{3,}(?![\u05d0-\u05ea])")
_RTL_RE = re.compile("[\u05d0-\u05ea\u0621-\u06fc]")
_AR_ARTICLES = ("\u0648\u0628\u0627\u0644", "\u0641\u0628\u0627\u0644", "\u0648\u0643\u0627\u0644",
                "\u0648\u0644\u0644", "\u0641\u0644\u0644", "\u0648\u0627\u0644", "\u0641\u0627\u0644",
                "\u0628\u0627\u0644", "\u0643\u0627\u0644", "\u0644\u0644", "\u0627\u0644")  # وبال فبال وكال ولل فلل وال فال بال كال لل ال
_AR_MIN_STEM = 3        # after an article: `الله` (stem 2) stays whole
_AR_BARE_MIN_STEM = 4   # after a bare و or ب: `وقت`, `بيت`, `ورقة` stay whole
_HE_PREFIXES = "\u05d5\u05d4\u05d1\u05dc\u05de"  # ו ה ב ל מ
_HE_MIN_STEM = 3
_FA_PLURALS = ("\u0647\u0627\u06cc\u06cc", "\u0647\u0627\u06cc", "\u0647\u0627")  # هایی های ها
_FA_MIN_STEM = 3        # `تنها` (alone) and `رها` stay whole


def _ar_split(word: str) -> tuple:
    pre, stem = "", word
    for art in _AR_ARTICLES:
        if word.startswith(art) and len(word) - len(art) >= _AR_MIN_STEM:
            pre, stem = art, word[len(art):]
            break
    else:
        if word[0] in "\u0648\u0628" and len(word) - 1 >= _AR_BARE_MIN_STEM:
            pre, stem = word[0], word[1:]
    suf = ""
    for pl in _FA_PLURALS:   # Persian plural written without its ZWNJ: `کتابها` is `کتاب‌ها`
        if stem.endswith(pl) and len(stem) - len(pl) >= _FA_MIN_STEM:
            stem, suf = stem[:-len(pl)], pl
            break
    return pre, stem, suf


def _he_split(word: str) -> tuple:
    """At most two prefix letters, the second only after ו (`ובבית`, `והשרת`); מ only as מה (`מהבית`): a bare מ is
    the participle prefix far more often than `from` (`מרגיש`, `מאמין`, `מסיבה` merged with other words: measured);
    a stem never starts with ו (`הולך`, `בואו`, `מוכן` stay whole)."""
    k = 0
    if word[:2] == "ומ" or word[0] == "מ":
        k0 = 1 if word[0] == "ו" else 0
        if word[k0 + 1:k0 + 2] == "ה" and len(word) - k0 - 2 >= _HE_MIN_STEM and word[k0 + 2] != "ו":
            return word[:k0 + 2], word[k0 + 2:], ""
        if k0 == 0:
            return "", word, ""
    while k < 2 and word[k] in _HE_PREFIXES and word[k] != "מ":
        if k == 1 and word[0] != "ו":
            break
        if len(word) - k - 1 < _HE_MIN_STEM or word[k + 1] == "ו":
            break
        k += 1
    return word[:k], word[k:], ""


# MN80: an unquoted query word stands for every token a note's spelling of it can index to. Its stem alone missed the
# notes where the query word IS the stem behind another prefix (`לילה` is `ל`+`ילה` to the split, but `בלילה` indexes
# `ב`+`לילה`; `برنامج` is `ب`+`رنامج`, but `البرنامج` indexes `ال`+`برنامج`), and the stem's own split missed the
# bare word (`בלילה` searched `לילה`, which `לילה` never indexes). The query side marks such a word as alternatives
# (stem, whole word, the bare stem in its index form) between these markers; each query path renders them as an OR group.
_ALT_OPEN, _ALT_MID, _ALT_CLOSE = "\ue011", "\ue012", "\ue013"   # no note or query keeps them (stripped from queries)
_ALT_RE = re.compile("\ue011([^\ue013]*)\ue013(\\*?)")
_ALT_STRIP = {ord(c): " " for c in (_ALT_OPEN, _ALT_MID, _ALT_CLOSE)}


def _rtl_split(word: str) -> tuple:
    return (_he_split if "\u05d0" <= word[0] <= "\u05ea" else _ar_split)(word)


@functools.lru_cache(maxsize=65536)
def _affix_word(word: str, keep_prefix: bool, alts: bool = False) -> str:
    pre, stem, suf = _rtl_split(word)
    tail = _CJK_SEP + suf if suf else ""
    if alts and pre and not keep_prefix:
        forms = [stem, pre + stem]
        # the bare stem as the index holds it, a two-token phrase (`בלילה` adds `ל SEP ילה`: the note's bare `לילה`);
        # the stem's stem as a lone token matched `בילה` for `בלילה`, `קשה` for `בבקשה`, `בין` for `להבין` (MN80)
        if (_HE_WORD_RE if "\u05d0" <= stem[0] <= "\u05ea" else _AR_WORD_RE).fullmatch(stem):
            p2, s2, f2 = _rtl_split(stem)
            if p2 and not f2:
                forms.append(p2 + _CJK_SEP + s2)
        return _ALT_OPEN + _ALT_MID.join(dict.fromkeys(f + tail for f in forms)) + _ALT_CLOSE
    out = (pre + _CJK_SEP if pre and keep_prefix else "") + stem
    return out + tail


def _affix_split(text: str, keep_prefix: bool = True, alts: bool = False) -> str:
    """Index form (keep_prefix): `prefix SEP stem SEP suffix`; unquoted query form: `stem SEP suffix`, or with `alts`
    the marked alternatives a query path renders as an OR group (MN80)."""
    if text.isascii() or not _RTL_RE.search(text):
        return text
    text = _AR_WORD_RE.sub(lambda m: _affix_word(m.group(0), keep_prefix, alts), text)
    return _HE_WORD_RE.sub(lambda m: _affix_word(m.group(0), keep_prefix, alts), text)


def _alt_group(body: str, star: str = "") -> str:
    """One marked word as an FTS5 OR group of quoted forms (MN80)."""
    return "(" + " OR ".join('"' + f.replace('"', '""') + '"' + star for f in body.split(_ALT_MID) if f) + ")"


def _alts_to_groups(q: str) -> str:
    """The main query form: every marked word becomes its OR group, with an explicit AND to a neighbouring term
    (FTS5 refuses an implicit AND next to a parenthesised group, MN36). An operator or a parenthesis next to it is
    left alone, so `a OR לילה`, `NOT בדיקה` and the writer's own groups keep their meaning."""
    ops = ("AND", "OR", "NOT")

    def rep(m: "re.Match") -> str:
        left, right = m.string[:m.start()].rstrip(), m.string[m.end():].lstrip()
        lw, rw = (left.split() or [""])[-1], (right.split() or [""])[0]
        pre = "" if not left or left.endswith("(") or lw in ops else "AND "
        post = "" if not right or right.startswith((")", _ALT_OPEN)) or rw in ops else " AND"
        return " " + pre + _alt_group(m.group(1), m.group(2)) + post + " "
    return " ".join(_ALT_RE.sub(rep, q).split())


def _quoted_term(t: str, cjk: bool = False) -> str:
    """One whitespace token as a single quoted FTS5 operand (the syntax fallback and the bigram path); a marked word
    inside it becomes its OR group, AND-ed with the token's other letters (MN80)."""
    if _ALT_OPEN not in t:
        return '"' + (_cjk_split(t) if cjk else t).replace('"', '""') + '"'
    parts: list = []
    pos = 0
    for m in _ALT_RE.finditer(t):
        rest = t[pos:m.start()]
        if any(c.isalnum() for c in rest):
            parts.append('"' + (_cjk_split(rest) if cjk else rest).replace('"', '""') + '"')
        parts.append(_alt_group(m.group(1)))
        pos = m.end()
    rest = t[pos:]
    if any(c.isalnum() for c in rest):
        parts.append('"' + (_cjk_split(rest) if cjk else rest).replace('"', '""') + '"')
    return parts[0] if len(parts) == 1 else "(" + " AND ".join(parts) + ")"


_LATIN_FOLD: dict = {}


def _latin_fold_table() -> dict:
    """AG91: search's tokenizer strips the diacritics of Latin letters (remove_diacritics 2), so `Göteborg` and `Goteborg`,
    `päivää` and `paivaa` are one word to search; the duplicate scans kept them apart (a Swedish or Finnish fact retyped
    without its letters was never a repeat, while Danish `ø`, folded by `_recompose`, was). One table, built once."""
    if not _LATIN_FOLD:
        for c in list(range(0x00C0, 0x0250)) + list(range(0x1E00, 0x1F00)):
            ch = chr(c)
            base = "".join(x for x in unicodedata.normalize("NFD", ch) if not unicodedata.combining(x))
            if base != ch and base:
                _LATIN_FOLD[c] = base
    return _LATIN_FOLD


def _dup_fold(text: str) -> str:
    """Search's word folds for the duplicate scans (AG89): an Arabic/Hebrew prefix dropped the way an unquoted query
    drops it (`השרת` is `שרת`, `الخادم` is `خادم`) and every emoji cluster in its untoned canonical form between spaces
    (`👍🏽` is `👍`, `❤️` is `❤`). Run it after `_recompose` and `_fold_marks`; ASCII text comes back unchanged."""
    if text.isascii():
        return text
    text = unicodedata.normalize("NFC", text).translate(_latin_fold_table())  # AG91: diacritics as search folds them
    text = _EMOJI_RE.sub(lambda m: " " + _emoji_canon(m.group(0)).split(_CJK_SEP)[0] + " ", text)
    return _affix_split(text, keep_prefix=False).replace(_CJK_SEP, " ")


# AG91: Danes, Norwegians, Swedes and Germans type å/ø/ö/ä as aa/oe/oe/ae when the keyboard lacks them, and the official
# spelling is often the digraph (`Aalborg`, `Aarhus`). An unquoted query word that HOLDS one of the letters also matches its
# digraph spelling: `Ålborg` finds `Aalborg`, `København` finds `Koebenhavn`. Only that direction: folding a typed `aa`/`oe`
# onto the letter merged 1,736 of the 7,430 Finnish `aa` words with another word (`saada`/`sada`, 5.1% of running text) and
# `oe` merged `poet`/`pot`, `does`/`dos` (measured on wordfreq's top-50,000 lists; docs/USAGE.md, CHANGELOG AG91).
_NORDIC_LETTERS = "\u00e5\u00c5\u00f8\u00d8\u00f6\u00d6\u00e4\u00c4\u00fc\u00dc"   # AG96: German ü/Ü joined (`München`/`Muenchen`)
_NORDIC_LETTER_RE = re.compile("[\\w*]*[" + _NORDIC_LETTERS + "][\\w*]*")
_NORDIC_DIGRAPHS = str.maketrans({"\u00e5": "aa", "\u00c5": "Aa", "\u00f8": "oe", "\u00d8": "Oe", "\u00f6": "oe", "\u00d6": "Oe",
                                  "\u00e4": "ae", "\u00c4": "Ae", "\u00fc": "ue", "\u00dc": "Ue"})
_NORDIC_OPEN, _NORDIC_MID, _NORDIC_CLOSE = "\ue014", "\ue015", "\ue016"   # private: turned into the MN80 alternatives after the folds
_ALIAS_MARK = "\ue017"   # private: opens a note's digraph-alias tail in the index (AG96)
_NORDIC_STRIP = {ord(c): " " for c in (_NORDIC_OPEN, _NORDIC_MID, _NORDIC_CLOSE, _ALIAS_MARK)}
_NORDIC_TO_ALT = str.maketrans({_NORDIC_OPEN: _ALT_OPEN, _NORDIC_MID: _ALT_MID, _NORDIC_CLOSE: _ALT_CLOSE})

# AG96: the other direction, on the index side. A note's word holding å/ø/ö/ä/ü is also indexed in its digraph spelling, so a
# typed `aarhus`, `oere`, `goeteborg`, `muenchen` finds `Århus`, `øre`, `Göteborg`, `München`. The aliases go in a tail after the
# note's text (behind the sentence sentinel and a private mark, once per spelling): an alias placed beside its word would have
# broken every quoted phrase through it (`"Århus kommune"`). A typed `aa`/`oe` is still never folded onto the letter, so `saada`
# and `sada`, `poet` and `pot` stay apart; an alias only adds a spelling to a note that holds the letter. Measured on wordfreq's
# top-300 da/nb/sv/fi/nl words, a real query word newly hitting another real word: tests/test_ag96.py, CHANGELOG (AG96).
_ALIAS_WORD_RE = re.compile("\\w*[" + _NORDIC_LETTERS + "]\\w*")
_ALIAS_HEAD = _SENTINEL + " " + _ALIAS_MARK + " "


def _alias_of(word: str) -> str:
    """A word's digraph spelling as the index holds it (NFKC-composed input)."""
    return _recompose(word.translate(_NORDIC_DIGRAPHS)).casefold()


def _digraph_aliases(text: str) -> str:
    """The note's digraph spellings, each once, space separated; '' when the note holds none of the letters."""
    if text.isascii():
        return ""
    text = unicodedata.normalize("NFKC", text)
    if not _ALIAS_WORD_RE.search(text):
        return ""
    return " ".join(dict.fromkeys(_alias_of(m.group(0)) for m in _ALIAS_WORD_RE.finditer(text)))


def _alias_snippet(orig: str, terms: list, width: int = 60):
    """A snippet around the note's word whose digraph alias a query term matched (the tail itself is never shown); None
    when no word of the note has that alias."""
    want = [t.casefold().replace(_CJK_SEP, "") for t in terms]
    want = [t for t in want if t]
    if not want or not orig or orig.isascii():
        return None
    hit = part = None
    for m in _WORD_RE.finditer(orig):
        w = m.group(0)
        if w.isascii():
            continue
        a = _alias_of(unicodedata.normalize("NFKC", w))
        if a in want:
            hit = (m.start(), m.end())
            break
        if part is None and any(a.startswith(t) for t in want):   # a prefix query (`aarh*`)
            part = (m.start(), m.end())
    hit = hit or part
    if hit is None:
        return None
    i, e = hit
    a, b = max(0, i - width), min(len(orig), e + width)
    out, last = [], a
    for m in _WORD_RE.finditer(orig, a, b):   # every word of the shown text that a term matched (`Soeren Koebenhavn`)
        w = m.group(0)
        if (m.start(), m.end()) == hit or (not w.isascii() and any(_alias_of(unicodedata.normalize("NFKC", w)).startswith(t) for t in want)):
            out += [orig[last:m.start()], "[", w, "]"]
            last = m.end()
    out.append(orig[last:b])
    return ("..." if a else "") + "".join(out) + ("..." if b < len(orig) else "")


def _nordic_mark(q: str) -> str:
    """Mark every unquoted word with a Nordic letter as (word, digraph spelling); NFKC-composed input expected."""
    q = q.translate(_NORDIC_STRIP)
    if q.isascii() or not _NORDIC_LETTER_RE.search(q):
        return q

    def mark(m: "re.Match") -> str:
        w = m.group(0)
        star = "*" if w.endswith("*") else ""
        core = w.rstrip("*")
        if "*" in core or not core:
            return w
        return _NORDIC_OPEN + core + _NORDIC_MID + core.translate(_NORDIC_DIGRAPHS) + _NORDIC_CLOSE + star
    parts = q.split('"')
    return '"'.join(part if i % 2 else _NORDIC_LETTER_RE.sub(mark, part) for i, part in enumerate(parts))


def _nordic_finish(q: str) -> str:
    return q.translate(_NORDIC_TO_ALT) if _NORDIC_OPEN in q else q


def _query_words(q: str) -> str:
    """The query side of the AG83 transforms: emoji clusters as separate terms, prefixes dropped outside quotes and
    kept inside them (a quoted phrase is matched in the indexed form, prefix token included)."""
    q = _emoji_split(q.translate(_ALT_STRIP), " ")
    if '"' not in q:
        return _affix_split(q, keep_prefix=False, alts=True)
    parts = q.split('"')
    return '"'.join(_affix_split(part, keep_prefix=bool(i % 2), alts=not i % 2) for i, part in enumerate(parts))


_BREAK_REPL = " " + _SENTINEL + " "   # no backslash and no group reference: a plain replacement string


def _norm_index_text(text: str) -> str:
    if text.isascii():
        # SEX-046 (2026-11-25): the sentinel is not ASCII, so after the break step every later step lost
        # its own `isascii()` shortcut and scanned the whole text (10 of 15 s of a 20,000-note rebuild,
        # 19 notes in 20 being ASCII). For ASCII text those steps change nothing: NFKC is the identity,
        # and the folds, the emoji clusters (a keycap needs U+20E3), the affix split and the CJK split
        # each need a character outside ASCII. tests/test_sex046_norm_fast.py holds the two paths equal.
        return _BREAK_RE.sub(_BREAK_REPL, text)
    broken = _BREAK_RE.sub(_BREAK_REPL, text)
    return _cjk_split(_affix_split(_emoji_split(_fold_marks(_recompose(unicodedata.normalize("NFKC", broken))))))


def _fm_values(obj) -> list[str]:
    """Every string value in a parsed frontmatter mapping, recursively."""
    out: list[str] = []
    if isinstance(obj, dict):
        for v in obj.values():
            out.extend(_fm_values(v))
    elif isinstance(obj, list):
        for v in obj:
            out.extend(_fm_values(v))
    elif isinstance(obj, str):
        out.append(obj)
    elif obj is not None and not isinstance(obj, bool):
        out.append(str(obj))
    return out


# Provenance is metadata, not text (AG99, sweep 26): `human` and `source` matched every remembered note through
# the `<!-- source:human-direct -->` tag a write appends and the frontmatter fields writers stamp. The tag and
# the fields stay in the file (curation, prime, versions read them); only the text handed to the index drops them.
_PROV_COMMENT_RE = re.compile(r"[ \t]*<!--[ \t]*(?:end[ \t]+)?(?:source|session|task):[^\n]*?-->")
_PROV_KEYS = frozenset({"source_session", "source_task", "confidence", "first_observed", "updated", "last_confirmed",
                        "imported_from", "imported_at", "imported_utc", "original_path", "import_vault", "imported_hash"})
_PROV_SOURCE_RE = re.compile(r"[a-z0-9][a-z0-9._:-]*\Z")   # `source: human-direct` is a stamp; `source: Field notes, p. 4` is the writer's


def strip_provenance(body: str) -> str:
    """A note body without its provenance tags (`<!-- source:x session:y task:z -->`, `<!-- session:... -->`)."""
    return _PROV_COMMENT_RE.sub(" ", body) if "<!--" in body else body


def _fm_index_values(fm) -> list[str]:
    """The frontmatter values search indexes: every value but the provenance stamps."""
    if isinstance(fm, dict):
        fm = {k: v for k, v in fm.items()
              if k not in _PROV_KEYS and not (k == "source" and isinstance(v, str) and _PROV_SOURCE_RE.match(v.strip()))}
    return _fm_values(fm)


def _index_body(frontmatter_json: str, body: str) -> str:
    """The indexed body: the normalised note body followed by the frontmatter's values, so a
    CJK title or tag is searchable (MN16: `大阪城の歴史` in a title found nothing) while the
    stored frontmatter_json stays byte-for-byte parseable for the topics tool."""
    if _ALIAS_MARK in body:
        body = body.replace(_ALIAS_MARK, " ")   # only the alias tail holds the mark (AG96)
    body = strip_provenance(body)
    text = _norm_index_text(body)
    try:
        values = _fm_index_values(json.loads(frontmatter_json))
    except (ValueError, TypeError):
        values = []
    fm_text = "\n".join(values)[:_MAX_FM_INDEX_CHARS] if values else ""
    if values:
        text += "\n" + _norm_index_text(fm_text)
    aliases = _digraph_aliases(body + "\n" + fm_text)   # AG96: last, so no phrase of the note runs through them
    if aliases:
        text += "\n" + _ALIAS_HEAD + _cjk_split(aliases)
    return text


_SNIP_OPEN, _SNIP_CLOSE, _SNIP_ELL = "\ue001", "\ue002", "\ue003"   # snippet() markers no note contains
_FOLD_MEMO: dict = {}


def _letter_fold(ch: str) -> str:
    """One character as search sees it, letters, digits and symbols only: case, compatibility forms, diacritics,
    the _recompose folds; punctuation, spaces, marks, joiners, the separator and the sentinel give ''."""
    f = _FOLD_MEMO.get(ch)
    if f is None:
        d = unicodedata.normalize("NFKD", _recompose(ch)).casefold()
        f = "".join(c for c in d if unicodedata.category(c)[0] in "LNS" and not "\U0001F3FB" <= c <= "\U0001F3FF")  # a skin tone is a tail (MN74)
        if len(_FOLD_MEMO) < 65536:
            _FOLD_MEMO[ch] = f
    return f


def _fold_map(text: str) -> tuple:
    """(folded letters, index in `text` of the character each folded letter came from)."""
    parts: list = []
    pos: list = []
    memo = _FOLD_MEMO
    for k, ch in enumerate(text):
        f = memo.get(ch)
        if f is None:
            f = _letter_fold(ch)
        if f:
            parts.append(f)
            if len(f) == 1:
                pos.append(k)
            else:
                pos.extend([k] * len(f))
    return "".join(parts), pos


def _is_tail(ch: str) -> bool:
    return unicodedata.category(ch)[0] == "M" or ch in "\u200d\ufe0e\ufe0f" or "\U000E0020" <= ch <= "\U000E007F" or "\U0001F3FB" <= ch <= "\U0001F3FF"


def _word_edge(orig: str, i: int, step: int) -> int:
    """Widen a bracket to the whole word in the note (`ال[خادم]` -> `[الخادم]`, `[کتاب]ها` -> `[کتابها]`); never into
    CJK or Thai-like runs, whose words are single characters to the index."""
    def ok(c: str) -> bool:
        return (c.isalnum() or unicodedata.category(c)[0] == "M") and not _CJK_RE.match(c)
    if step < 0:
        while 0 < i < len(orig) and ok(orig[i - 1]) and ok(orig[i]):
            i -= 1
    else:
        while 0 < i < len(orig) and ok(orig[i]) and ok(orig[i - 1]):
            i += 1
    return i


def _render_in_original(orig: str, window: str, marks: list, lead: bool, trail: bool, hint: float = 0.0):
    """Re-cut a snippet of the indexed (folded) text out of the note's own text: `Straße` and `İSTANBUL` instead of
    `Strasse` and `iSTANBUL`, punctuation and joiners kept (AG83). `window` is the indexed-text window, `marks` the
    (offset in window, is_open) bracket positions. None when the window cannot be located (the caller keeps the
    folded snippet)."""
    wl = _fold_map(window)[0]
    if not wl or not orig:
        return None
    ol, opos = _fold_map(orig)
    found, j = [], ol.find(wl)
    while j >= 0 and len(found) < 64:
        found.append(j)
        j = ol.find(wl, j + 1)
    if not found:
        return None
    m = min(found, key=lambda x: abs(x / max(len(ol), 1) - hint))

    def _end(k: int) -> int:
        e = opos[k] + 1
        while e < len(orig) and _is_tail(orig[e]):
            e += 1
        return e

    a, b = opos[m], _end(m + len(wl) - 1)
    cuts = []
    for off, is_open in marks:
        c = len(_fold_map(window[:off])[0])   # folded letters of the window before this mark
        if is_open:
            if c < len(wl):
                cuts.append((_word_edge(orig, opos[m + c], -1), "["))
        elif c > 0:
            cuts.append((_word_edge(orig, _end(m + c - 1), 1), "]"))
    a = min([a] + [k for k, _ in cuts])
    b = max([b] + [k for k, _ in cuts])
    # one bracket per place, and `][` at one place (two adjacent tokens of one word or emoji) closes nothing
    cuts = sorted(set(cuts), key=lambda x: (x[0], x[1] == "["))
    at = {}
    for k, t in cuts:
        at.setdefault(k, set()).add(t)
    cuts = [(k, t) for k, t in cuts if at[k] != {"[", "]"}]
    depth, kept = 0, []
    for k, t in cuts:
        if (t == "[") == (depth == 0):
            kept.append((k, t))
            depth = 1 - depth
    if depth:
        kept.append((b, "]"))
    out, last = [], a
    for k, t in kept:
        out.append(orig[last:k])
        out.append(t)
        last = k
    out.append(orig[last:b])
    return ("..." if lead else "") + "".join(out) + ("..." if trail else "")


def _original_text(rel: str, indexed: str) -> str:
    """The text a note's index row was built from, read back from the corpus: the body, then its frontmatter values
    (the order `_index_body` uses). '' when the file cannot be read."""
    from atlas_plus._fsutil import is_link
    try:
        path = MEMORY_ROOT / rel
        if is_link(path):   # a note swapped for a link after indexing: never show another file's text (AG16)
            return ""
        from atlas_plus._readahead import read_note_text
        raw = read_note_text(path, "utf-8-sig")   # a UTF-16 note as the indexer read it (MN96)
    except (OSError, ValueError):
        return ""
    frontmatter_json, body, _ = _parse_md(raw)
    try:
        values = _fm_index_values(json.loads(frontmatter_json))
    except (ValueError, TypeError):
        values = []
    body = strip_provenance(body)   # the text the row was built from (AG99)
    return body + ("\n" + "\n".join(values)[:_MAX_FM_INDEX_CHARS] if values else "")


_HTML_COMMENT = re.compile(r"<!--.*?-->", re.S)
_OPEN_COMMENT_TAIL = re.compile(r"<!--(?:(?!-->)[^\n])*?(\.\.\.)?$")
_CLOSE_COMMENT_HEAD = re.compile(r"^(\.\.\.)?[^\s<>]*(?:source|session|task|direct|agent|telegram|facts)[^\s<>]*"
                                 r"(?:\s+(?:session|task):[^\s<>]*)*\s*-->")


def strip_snippet_comments(snip: str) -> str:
    """Drop HTML comments (the `<!-- source:... -->` provenance tag) from a snippet, including one the snippet window
    cut open at either end: the dashboard, `sextant search` and the MCP tool all showed them (AG97, sweep 25)."""
    if not snip or ("<!--" not in snip and "-->" not in snip):
        return snip
    s = _HTML_COMMENT.sub(" ", snip)
    s = _OPEN_COMMENT_TAIL.sub(lambda m: m.group(1) or "", s)
    s = _CLOSE_COMMENT_HEAD.sub(lambda m: m.group(1) or "", s)
    return re.sub(r"[ \t]{2,}", " ", s).strip()


def _snippet_from_fts(raw: str, indexed: str, orig: str) -> str:
    """Turn snippet()'s marked text into the bracketed snippet, in the note's own letters when it can be placed."""
    lead, trail = raw.startswith(_SNIP_ELL), raw.endswith(_SNIP_ELL)
    core = raw[len(_SNIP_ELL) if lead else 0: len(raw) - (len(_SNIP_ELL) if trail else 0)]
    window, marks = [], []
    for ch in core:
        if ch == _SNIP_OPEN or ch == _SNIP_CLOSE:
            marks.append((len(window), ch == _SNIP_OPEN))
        else:
            window.append(ch)
    window_s = "".join(window)
    tail_at = indexed.rfind(_ALIAS_HEAD) if _ALIAS_MARK in indexed else -1
    if tail_at >= 0:
        # AG96: the window reaches the digraph-alias tail: the note's own word is shown for an alias hit, never the tail
        k = window_s.find(_ALIAS_MARK)
        if k >= 0:
            j = window_s.rfind(_SENTINEL, 0, k)
            cut = j if j >= 0 else k
        else:
            at = indexed.find(window_s)
            cut = 0 if at > tail_at else len(window_s)
        if cut < len(window_s):
            spans, open_at = [], None
            for off, is_open in marks:
                if is_open:
                    open_at = off
                elif open_at is not None:
                    spans.append((open_at, off))
                    open_at = None
            body_spans = [(a, min(b, cut)) for a, b in spans if a < cut]
            terms = [window_s[a:b] for a, b in spans if a >= cut]
            if not body_spans:
                out = _alias_snippet(orig, terms) if terms else None
                if out is not None:
                    return out
            window_s = window_s[:cut].rstrip()
            marks = [(o, op) for a, b in body_spans for o, op in ((a, True), (b, False))]
            trail = False   # only the tail followed: the note's text ends here
            pieces, last = [], 0
            for off, is_open in marks:
                pieces += [window_s[last:off], _SNIP_OPEN if is_open else _SNIP_CLOSE]
                last = off
            raw = (_SNIP_ELL if lead else "") + "".join(pieces) + window_s[last:]
    if orig:
        at = indexed.find(window_s)
        hint = at / max(len(indexed), 1) if at >= 0 else 0.0
        out = _render_in_original(orig, window_s, marks, lead, trail, hint)
        if out is not None:
            return out
    return raw.replace(_SNIP_OPEN, "[").replace(_SNIP_CLOSE, "]").replace(_SNIP_ELL, "...")


def _cheap_pick(orig: str, low: str, pos: list, needle: str, cap: int = 4096) -> int:
    """Where the term sits in the folded note text: the first occurrence that ends a word (a whole word, or a stem behind
    an Arabic/Hebrew prefix), else the first one. The first substring bracketed another word: `cat` showed
    `[concatenate]`, `strasse` showed `[Straßenbahn]` while the hit was further down (MN74)."""
    first = j = low.find(needle)
    n = 0
    while j >= 0 and n < cap:
        e = pos[j + len(needle) - 1] + 1
        while e < len(orig) and _is_tail(orig[e]):
            e += 1
        if _word_edge(orig, e, 1) == e:
            return j
        j = low.find(needle, j + 1)
        n += 1
    return first


def _cheap_snippet(conn, rowid, query: str, width: int = 90, orig: str = "") -> str:
    """A snippet for a note too large for FTS5's snippet(): the first query term found, in brackets, with context.
    With `orig` (the note's own text) the term is found and shown there (AG83), else in the indexed text."""
    terms = [t for t in re.findall(r"[^\s\"()*:^+]+", query) if t.upper() not in ("AND", "OR", "NOT", "NEAR")]
    if orig:
        low, pos = _fold_map(orig)
        for t in terms:
            needle = _fold_map(t)[0]
            j = _cheap_pick(orig, low, pos, needle) if needle else -1
            if j >= 0:
                i, e = _word_edge(orig, pos[j], -1), pos[j + len(needle) - 1] + 1
                while e < len(orig) and _is_tail(orig[e]):
                    e += 1
                e = _word_edge(orig, e, 1)
                a, b = max(0, i - width), min(len(orig), e + width)
                return ("..." if a else "") + orig[a:i] + "[" + orig[i:e] + "]" + orig[e:b] + ("..." if b < len(orig) else "")
        out = _alias_snippet(orig, terms, width)   # AG96: a digraph spelling of a word the note holds (`aarhus` for `Århus`)
        if out is not None:
            return out
    try:
        body = conn.execute("SELECT body FROM memory_fts WHERE rowid = ?", (rowid,)).fetchone()[0] or ""
    except Exception:  # noqa: BLE001
        return ""
    if _ALIAS_MARK in body:
        body = body[:body.rfind(_ALIAS_HEAD)] if _ALIAS_HEAD in body else body.replace(_ALIAS_MARK, "")   # never show the alias tail (AG96)
    # `low` is the folded text and `pos` the body index of each folded character: casefold lengthens
    # some letters (U+01F0 -> 2) and the index keeps U+2060 between CJK characters, so an index found in
    # `low` was not an index into `body` (AG73: 400 such letters gave `...[]`; a CJK term never bracketed)
    low_parts: list = []
    pos: list = []
    for k, ch in enumerate(body):
        if ch == _CJK_SEP:
            continue
        f = ch.casefold()
        low_parts.append(f)
        pos.extend([k] * len(f))
    low = "".join(low_parts)
    for t in terms:
        needle = t.casefold().replace(_CJK_SEP, "")
        j = low.find(needle) if needle else -1
        if j >= 0:
            i, e = pos[j], pos[j + len(needle) - 1] + 1
            a, b = max(0, i - width), min(len(body), e + width)
            return ("..." if a else "") + body[a:i] + "[" + body[i:e] + "]" + body[e:b] + ("..." if b < len(body) else "")
    return body[: 2 * width] + ("..." if len(body) > 2 * width else "")


def _row_body(conn, rowid) -> str:
    try:
        row = conn.execute("SELECT body FROM memory_fts WHERE rowid = ?", (rowid,)).fetchone()
    except sqlite3.Error:
        return ""
    return (row[0] or "") if row else ""


def _bigram_fallback(raw: str) -> str:
    """A looser query for a CJK sentence that matched nothing as phrases: every CJK run of three
    characters or more becomes its overlapping bigrams OR-ed together (ranked by how many the
    note shares), Latin terms stay as they are. A natural Japanese sentence found nothing
    because each run had to match whole (MN23). Empty string when it does not apply."""
    q = _nordic_finish(_query_words(_fold_marks(_recompose(_nordic_mark(unicodedata.normalize("NFKC", raw)))).replace(_SENTINEL, " ")))  # the index holds recomposed sara am: decomposed bigrams never matched (MN62)
    if not _CJK_RUN_RE.search(q):
        return ""
    long_runs = [m.group(0) for m in _CJK_RUN_RE.finditer(q) if len(m.group(0)) >= 3]
    if not long_runs:
        return ""

    def _group(run: str) -> str:
        pairs = [run[i:i + 2] for i in range(len(run) - 1)]
        return "(" + " OR ".join('"' + _CJK_SEP.join(p) + '"' for p in pairs) + ")"

    # FTS5 refuses an implicit AND before a parenthesised group (`Sextant ("a" OR "b")` is a syntax
    # error, MN36), so every Latin word is quoted and the parts are joined with AND
    parts: list[str] = []
    pos = 0
    for m in _CJK_RUN_OR_CHAR_RE.finditer(q):
        parts += [_quoted_term(w) for w in q[pos:m.start()].split()]
        run = m.group(0)
        parts.append(_group(run) if len(run) >= 3 else '"' + _CJK_SEP.join(run) + '"')
        pos = m.end()
    parts += [_quoted_term(w) for w in q[pos:].split()]
    return " AND ".join(parts)


_LOOSE_CAP = 0.35            # a loose match is 'low' confidence at most (MN25)
_LOOSE_FILL_WORDS = 4        # a short strict result is topped up only for a sentence-like query
_FTS_SYNTAX_RE = re.compile(r'["*^]|(?<!\S)(?:AND|OR|NOT|NEAR)(?!\S)')


def _any_word_fallback(raw: str, min_words: int = 2) -> str:
    """A looser query for a sentence whose words are not all in any one note: the words OR-ed, so
    BM25 ranks the notes by how many of them they share and how rare those are. A question typed
    the way a person asks it ("what was the first issue I had with my new car?") found nothing,
    because every word had to match (LongMemEval run 2026-11-20: 406 of 470 questions came back
    empty). Empty string when it does not apply: fewer than `min_words` distinct words, or a
    query written in FTS5 syntax (a quoted phrase, a prefix star, AND/OR/NOT/NEAR), which means
    what it says."""
    if _FTS_SYNTAX_RE.search(raw):
        return ""
    q = _nordic_finish(_query_words(_fold_marks(_recompose(_nordic_mark(unicodedata.normalize("NFKC", raw)))).replace(_SENTINEL, " ")))
    q = re.sub(r"[():]", " ", q)
    tokens = list(dict.fromkeys(t for t in q.split() if any(c.isalnum() for c in t)))
    if len(tokens) < min_words:
        return ""
    return " OR ".join(_quoted_term(t, cjk=True) for t in tokens)


# Words that say nothing about what a question is about. A note that shares only these with a
# query is not a match for it (code review 2026-11-23: "what is my dentist's name" matched any
# note holding "what", "is" or "my", and reported low confidence instead of starved).
_LOOSE_STOPWORDS = frozenset("""
a about after again all also am an and any are as at be because been before being both but by can
could did do does doing down during each for from further had has have having he her here hers him
his how i if in into is it its just me more most my no nor not now of off on once only or other our
ours out over own same she should so some such than that the their theirs them then there these they
this those through to too under until up very was we were what when where which while who whom why
will with would you your yours
""".split())
_LOOSE_MAX_TERMS = 24
_LOOSE_MIN_COVERAGE = 0.4    # share of the question's content words a partial match must hold to be 'low'
_STARVED_BELOW = 0.2         # the confidence bands' starved threshold (tools/search.confidence_band)


def _content_terms(raw: str) -> list[str]:
    """The query's words that carry meaning, as FTS terms: no stopwords, no bare punctuation."""
    q = _nordic_finish(_query_words(_fold_marks(_recompose(_nordic_mark(unicodedata.normalize("NFKC", raw)))).replace(_SENTINEL, " ")))
    q = re.sub(r"[():]", " ", q)
    seen: dict[str, None] = {}
    for t in q.split():
        word = t.strip("'’.,;!?").lower()
        if word.endswith(("'s", "’s")):
            word = word[:-2]
        if any(c.isalnum() for c in t) and word not in _LOOSE_STOPWORDS:
            seen.setdefault(t)
    return list(seen)[:_LOOSE_MAX_TERMS]


def _coverage(terms: list[str], rows: dict[int, str]) -> dict[str, float]:
    """For each note (`rows`: index rowid -> path), the share of the query's content terms it
    holds: an absolute measure of how much of the question a loose match answers, where the BM25
    score is only relative to the other matches. Asked by rowid, which the index answers without
    reading a note's text (filtering on `path` read every candidate and doubled search time)."""
    paths = list(rows.values())
    if not terms or not rows:
        return {p: 0.0 for p in paths}
    held = dict.fromkeys(rows, 0)
    marks = ",".join("?" * len(rows))
    try:
        conn = _connect()
    except sqlite3.DatabaseError:
        return {p: 0.0 for p in paths}
    try:
        for term in terms:
            try:
                found = conn.execute(
                    # bandit B608 justification: `marks` is a literal "?,?,..." string; every value is bound
                    f"SELECT rowid FROM memory_fts WHERE memory_fts MATCH ? AND rowid IN ({marks})",  # nosec B608
                    [_quoted_term(term, cjk=True), *rows]).fetchall()
            except sqlite3.DatabaseError:
                continue
            for (rowid,) in found:
                if rowid in held:
                    held[rowid] += 1
    finally:
        conn.close()
    return {rows[i]: held[i] / len(terms) for i in rows}


def _loose_results(raw: str, scope: str, limit: int, min_words: int, ceiling: float = _LOOSE_CAP) -> list[dict]:
    """The any-word matches for `raw`, best first and flagged. Order is BM25's. The size of the
    score is the share of the query's content words the best match holds, under `ceiling`: a note
    holding most of the question lands in the low band, one that shares a word or two stays
    starved, and a query made only of stopwords matches nothing."""
    loose = _any_word_fallback(raw, min_words)
    terms = _content_terms(raw)
    if not loose or not terms:
        return []
    found = search(loose, scope=scope, limit=limit, _fallback_stage=4, _raw=raw)
    covered = _coverage(terms, {r.pop("_rowid"): r["path"] for r in found if "_rowid" in r})
    found = [r for r in found if covered.get(r["path"], 0.0) > 0]      # shares only stopwords: not a match
    top = max((float(r.get("score", 0) or 0) for r in found), default=0.0)
    best = max((covered[r["path"]] for r in found), default=0.0)
    # Low band: some note shares at least two of the question's content words and 40% of them.
    # One shared word is never enough ("dentist's name" against a note that holds "name").
    if best * len(terms) >= 1.999 and best >= _LOOSE_MIN_COVERAGE:
        size = ceiling * min(1.0, 0.6 + best)          # 0.4 -> the full ceiling
    else:
        size = min(ceiling, _STARVED_BELOW * 0.95) * best
    for r in found:
        r["score"] = round(size * float(r.get("score", 0) or 0) / top, 4) if top > 0 else 0.0
        r["loose_match"] = True
        r["coverage"] = round(covered[r["path"]], 3)
    return found


def _prepare_query(query: str) -> str:
    """NFKC-fold the query and turn every CJK run into a phrase of its characters; a run glued
    to Latin text becomes its own term (`2025年` -> `2025 "年"`)."""
    q = _nordic_finish(_query_words(_fold_marks(_recompose(_nordic_mark(unicodedata.normalize("NFKC", query)))).replace(_SENTINEL, " ")))
    q = re.sub(r"(?<=\S):(?=\S)", " ", q)  # `alpha:beta` is two words, not an FTS5 column filter (AG46: a silent miss)
    if _CJK_RE.search(q):
        q = " ".join(_CJK_RUN_OR_CHAR_RE.sub(lambda m: ' "' + _CJK_SEP.join(m.group(0)) + '" ', q).split())
    return _alts_to_groups(q) if _ALT_OPEN in q else q
_CREATE_FTS = """
CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts
USING fts5(path, frontmatter_json UNINDEXED, body, tokenize='{tokenizer}');
"""

_CREATE_FILES = """
CREATE TABLE IF NOT EXISTS memory_files (
    path            TEXT PRIMARY KEY,
    mtime           REAL,
    indexed         REAL,
    last_confirmed  REAL
);
"""

_MIGRATE_LAST_CONFIRMED = """
ALTER TABLE memory_files ADD COLUMN last_confirmed REAL;
"""

# Decay window for last_confirmed scoring: confirmed within 30d = full boost
_CONFIRMED_FULL_BOOST_SECS = 30 * 24 * 3600
# Confirmed older than 1y = no boost (neutral 0.5 used for unconfirmed)
_CONFIRMED_MAX_AGE_SECS = 365 * 24 * 3600
# Unconfirmed files get a neutral score so they are not penalised
_CONFIRMED_NEUTRAL = 0.5


def _parse_confirmed_ts(fm: dict) -> float | None:
    """Return unix timestamp for last_confirmed frontmatter field, or None."""
    raw = fm.get("last_confirmed")
    if not raw:
        return None
    try:
        s = str(raw)[:10]  # accept full ISO or YYYY-MM-DD
        dt = datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:
        return None


def _confirmed_score(last_confirmed_ts: float | None, now_ts: float) -> float:
    """Map last_confirmed timestamp to a 0-1 contribution score.

    None (no last_confirmed) -> _CONFIRMED_NEUTRAL (no penalty, no boost)
    Confirmed within 30d     -> 1.0
    Confirmed between 30d-1y -> linear decay from 1.0 to 0.0
    Confirmed >1y ago        -> 0.0
    """
    if last_confirmed_ts is None:
        return _CONFIRMED_NEUTRAL
    age_secs = now_ts - last_confirmed_ts
    if age_secs <= 0:
        return 1.0
    if age_secs <= _CONFIRMED_FULL_BOOST_SECS:
        return 1.0
    if age_secs >= _CONFIRMED_MAX_AGE_SECS:
        return 0.0
    # Linear decay in [30d, 1y] range
    span = _CONFIRMED_MAX_AGE_SECS - _CONFIRMED_FULL_BOOST_SECS
    return 1.0 - (age_secs - _CONFIRMED_FULL_BOOST_SECS) / span


def fts5_available() -> bool:
    """Whether this interpreter's SQLite carries the FTS5 module keyword search needs
    (AG8, sweep 10: without it every search silently returned nothing)."""
    try:
        conn = sqlite3.connect(":memory:")
        try:
            conn.execute("CREATE VIRTUAL TABLE t USING fts5(x)")
        finally:
            conn.close()
        return True
    except sqlite3.Error:
        return False


# One lock wait for the connect and the PRAGMA: the PRAGMA silently replaced a shorter connect timeout
# (AG101: a test that shrank the wait to 0.1 s still waited 30 s per locked search).
_BUSY_TIMEOUT_S = 30.0


def _connect() -> sqlite3.Connection:
    try:
        FTS_DB.parent.mkdir(parents=True, exist_ok=True)
    except FileExistsError:
        # The state dir is a FILE (AG10, sweep 10): a named OperationalError reaches search's
        # "not corruption" branch as one log line instead of a traceback.
        raise sqlite3.OperationalError(
            f"state directory {FTS_DB.parent} is a file, not a directory; move it aside")
    conn = sqlite3.connect(str(FTS_DB), timeout=_BUSY_TIMEOUT_S)
    # If setup raises (a trashed file fails at the WAL pragma, which reads
    # the header), CLOSE before re-raising: sqlite3.connect() has already
    # opened the file, the caller never receives the object, and on Windows
    # the leaked handle blocks the quarantine rename with WinError 32
    # (found 2026-10-09 hardening the file-level-corruption path).
    try:
        # busy_timeout FIRST so the journal_mode switch waits on a lock
        # instead of failing instantly with "database is locked".
        conn.execute(f"PRAGMA busy_timeout={int(_BUSY_TIMEOUT_S * 1000)}")  # embed-index bulk writes vs interactive search
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(_CREATE_META)
        conn.execute(_CREATE_FILES)
        row = conn.execute("SELECT value FROM atlas_meta WHERE key='tokenizer'").fetchone()
        if row is None or row[0] not in (_TOKENIZER + _INDEX_TAG, _TOKENIZER_FALLBACK + _INDEX_TAG):
            # First run on an index built with another tokenizer: drop it and forget every
            # file's mtime so the next update_incremental rebuilds from the corpus.
            # One transaction: DDL does not open an implicit one in sqlite3, so a crash between
            # the DROP and the commit left no table and a full memory_files (MN8, sweep 10).
            if not conn.in_transaction:
                conn.execute("BEGIN IMMEDIATE")
            conn.execute("DROP TABLE IF EXISTS memory_fts")
            conn.execute("DELETE FROM memory_files")
            if row is not None:
                log.info("FTS index format changed (%s -> %s): the keyword index is rebuilt from the "
                         "corpus on the next search; the corpus itself is untouched",
                         row[0], _TOKENIZER + _INDEX_TAG)
            chosen = _TOKENIZER
            try:
                conn.execute(_CREATE_FTS.format(tokenizer=_TOKENIZER))
            except sqlite3.OperationalError as exc:
                if "no such module" in str(exc).lower():
                    raise
                chosen = _TOKENIZER_FALLBACK
                conn.execute(_CREATE_FTS.format(tokenizer=_TOKENIZER_FALLBACK))
            conn.execute("INSERT OR REPLACE INTO atlas_meta(key,value) VALUES ('tokenizer',?)",
                         (chosen + _INDEX_TAG,))
        else:
            conn.execute(_CREATE_FTS.format(tokenizer=row[0].removesuffix(_INDEX_TAG)))
        # Safe migration: add last_confirmed column if it doesn't exist yet
        existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(memory_files)")}
        if "last_confirmed" not in existing_cols:
            conn.execute(_MIGRATE_LAST_CONFIRMED)
        if "size" not in existing_cols:
            conn.execute("ALTER TABLE memory_files ADD COLUMN size INTEGER")
        if "fts_rowid" not in existing_cols:
            conn.execute("ALTER TABLE memory_files ADD COLUMN fts_rowid INTEGER")  # AG42: delete by rowid, not by a scan
        conn.commit()
    except BaseException:
        conn.close()
        raise
    return conn


def _utf8_name(name: str) -> bool:
    try:
        name.encode("utf-8")
        return True
    except UnicodeEncodeError:
        return False


def _walk_memory():
    """Yield (relative_path_str, absolute_path) for every .md file in MEMORY_ROOT.

    Skips EXCLUDED_DIRS and the reference/research mirror -- that subtree mirrors the
    standalone research library (~54% of the corpus) and has its own dedicated RAG
    (research_search_tool), so indexing it into the general memory FTS only dilutes
    results and bloats the rebuild. (Single source of truth: curation.scan.)
    """
    from atlas_plus.curation.scan import is_excluded_mirror
    from atlas_plus._fsutil import is_link
    # Links are never followed: a symlinked `creds.md` put a file from outside the corpus into
    # search results while the read tool refused it (AG16, sweep 11).
    for dirpath, dirnames, filenames in os.walk(MEMORY_ROOT, followlinks=False):
        d = Path(dirpath)
        try:
            rel_parts = d.relative_to(MEMORY_ROOT).parts
        except ValueError:
            dirnames[:] = []
            continue
        keep = []
        for name in sorted(dirnames):
            parts = rel_parts + (name,)
            if (not rel_parts and name in EXCLUDED_DIRS) or is_excluded_mirror(parts) or is_link(d / name):
                continue
            keep.append(name)
        dirnames[:] = keep
        for name in sorted(filenames):
            if not name.lower().endswith(".md") or name == "_README.md":  # `UPPER.MD` was imported but never indexed (MN72)
                continue   # _README.md: sextant-init scaffolding notes, not memories (persona pass 2026-11-10)
            if not _utf8_name(name):
                continue   # a legacy-encoded name crashed every search at the sqlite bind (MN17)
            p = d / name
            if is_link(p):
                continue
            yield "/".join(rel_parts + (name,)), p


def _walk_memory_stats():
    """Yield (relative_path_str, mtime, size) for every indexed .md file.

    The freshness-sweep fast path (quality loop v2 run 1): ``rglob`` +
    ``Path.stat()`` costs one extra syscall per file and descends into
    excluded subtrees before filtering; ``os.scandir`` entries carry
    ``st_mtime`` for free (Windows fills it from the directory read) and
    excluded directories are PRUNED, never entered. Measured at 2,000
    files: sweep 219.6 ms -> see commit message for the after number.
    Same exclusion semantics as ``_walk_memory`` (parity-tested).
    """
    from atlas_plus.curation.scan import is_excluded_mirror
    # The relative path rides the stack as a prefix: the previous version
    # called os.path.relpath() once per directory ENTRY (including non-.md
    # files), and relpath's normalization dominated the sweep -- 6,756 calls
    # / 141ms of the 244ms walk on a 2,220-file corpus (profiled 2026-10-09).
    # Non-.md files are now skipped before any path work at all.
    # A path is excluded by a mirror prefix of its own length only: every shorter prefix was its
    # ancestor's check, and the walk never enters an excluded directory. So the mirror rule runs at
    # the depths a prefix has, and the top-level rule at the top level (AG92: per-entry cost at 20,000
    # notes). Unknown prefixes (another scan module): check everywhere, as before.
    from atlas_plus.curation import scan as _scan
    _mirror_depths = {len(p) for p in getattr(_scan, "_MIRROR_PREFIXES", ())} or None
    stack: list[tuple[str, str, int]] = [(str(MEMORY_ROOT), "", 1)]
    while stack:
        d, prefix, depth = stack.pop()
        try:
            entries = os.scandir(d)
        except OSError:
            continue
        check_top = depth == 1
        check_mirror = _mirror_depths is None or depth in _mirror_depths
        with entries:
            for entry in entries:
                name = entry.name
                try:
                    is_dir = entry.is_dir(follow_symlinks=False)
                    st0 = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                if entry.is_symlink() or getattr(st0, "st_reparse_tag", 0) & 0x20000000:
                    continue   # links are never followed (parity with _walk_memory)
                if not is_dir and not name.lower().endswith(".md"):
                    continue
                if not name.isascii() and not _utf8_name(name):
                    continue   # parity with _walk_memory (MN17)
                rel = prefix + name
                if check_top and name in EXCLUDED_DIRS:
                    continue
                if check_mirror and is_excluded_mirror(rel.split("/")):
                    continue
                if not is_dir and name == "_README.md":
                    continue
                if is_dir:
                    stack.append((entry.path, rel + "/", depth + 1))
                else:
                    yield rel, st0.st_mtime, st0.st_size   # the listing's stat, read once


INDEX_FORMAT = "11"  # bump when _norm_index_text changes: every note is re-indexed (2 = Thai-like runs split, Arabic/Hebrew marks folded; 3 = Hebrew punctuation kept, and an index stamped 2 by the search path without a rebuild; 4 = Turkish dotted/dotless i folded, AG72; 5 = Greek tonos, capital sharp s, Azeri schwa, MN62; 6 = emoji clusters, Arabic/Hebrew prefixes, Persian plurals, AG83; 7 = stroke letters folded, toned ZWJ sequences, MN74; 8 = the remaining stroke letters and the ae/oe ligatures, MN78; 9 = Hebrew acronyms joined across the gershayim, AG89; 10 = digraph-alias tail for å/ø/ö/ä/ü words, AG96; 11 = provenance tags and stamps left out, AG99)
# What _parse_md and _index_body make of a note, without a format bump (AG121, lesson LES-092): raise it when a
# change alters the row any note stores (tests/test_guardrails.py G21 holds the snapshot). An index whose stamp
# is older heals on the next incremental update: only the notes whose row would differ are re-indexed, once.
# 1 = a top block that is not key/value pairs is text (AG119). 2 = a note saved as UTF-16 with a byte-order
# mark is decoded, where it was indexed as replacement characters (MN96).
PARSE_REVISION = "2"


def _parse_stamp(last_indexed) -> str:
    """The `parse_revision` value: the revision tied to the `last_indexed` it was written with. A build that
    does not know the key (0.1.0) leaves it in place but rewrites `last_indexed` on every pass that writes
    rows, so its old-style rows never pass as healed (AG121, MN93 F3)."""
    return f"{PARSE_REVISION}@{last_indexed}"


def _needs_rebuild() -> bool:
    if not FTS_DB.exists():
        return True
    conn = _connect()
    try:
        fmt = conn.execute("SELECT value FROM atlas_meta WHERE key='index_format'").fetchone()
        if not fmt or fmt[0] != INDEX_FORMAT:
            return True
        row = conn.execute(
            "SELECT value FROM atlas_meta WHERE key='last_indexed'"
        ).fetchone()
        if not row:
            return True
        if _parse_stamp_stale(conn):
            return True   # rows an older parser wrote: `--if-stale` said "already fresh" (AG121, MN93 F1)
        last_indexed = float(row[0])
        # A newer mtime is an edit; a path the index does not hold is an added or renamed note (a rename and a
        # copy keep the mtime); fewer notes than the index holds is a removal. `--if-stale` answered "already
        # fresh" to the last three (AG108).
        recorded = {r[0] for r in conn.execute("SELECT path FROM memory_files")}
        seen = 0
        for rel, mtime, _size in _walk_memory_stats():
            if mtime > last_indexed or rel not in recorded:
                return True
            seen += 1
        return seen != len(recorded)
    finally:
        conn.close()


_rebuild_lock = threading.RLock()  # re-entrant since AG60: the format pre-check holds it while calling rebuild()


def _index_format_tag():
    """The index's format tag: '' when the row is missing (an index from before the tag), None when it cannot be read
    (the search path then fails open to the incremental update instead of forcing a rebuild: AG60)."""
    try:
        conn = _connect()
        try:
            row = conn.execute("SELECT value FROM atlas_meta WHERE key='index_format'").fetchone()
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        return None
    return row[0] if row else ""


def without_scaffold(raw: str) -> str | None:
    """A note with `sextant init`'s identity template removed, or None when it does not start with it.
    The template's instructions ("write it the way you would brief a sharp new collaborator") are not a
    memory and came back as a search hit (SEX-044 finding 6f, AG111); facts `remember --scope identity`
    appended under it stay. Only the exact template is removed, never a line the user rewrote. The
    keyword index and the embeddings both use this."""
    if "Introduce yourself to your own memory" not in raw[:2048]:
        return None
    from atlas_plus.init_cmd import _IDENTITY_TEMPLATE
    text = raw.lstrip("\ufeff").replace("\r\n", "\n").lstrip()
    tpl = _IDENTITY_TEMPLATE.strip()
    if not text.startswith(tpl):
        return None
    head = tpl[: tpl.find("\n---", 3) + 4]   # the frontmatter stays: its title is still the display name
    return head + "\n" + text[len(tpl):].lstrip("\n")


def is_untouched_scaffold(raw: str) -> bool:
    """True when a note is the identity template and nothing else (AG111)."""
    rest = without_scaffold(raw)
    return rest is not None and not _parse_md(rest)[1]


def _parse_md(raw: str) -> tuple[str, str, float | None]:
    """Split a memory file into (frontmatter_json, body, confirmed_ts)."""
    _rest = without_scaffold(raw)
    if _rest is not None:
        raw = _rest   # the init template's instructions are not a memory; appended facts stay (AG111)
    frontmatter_json = "{}"
    body = raw
    confirmed_ts = None
    if raw.startswith("---"):
        end = raw.find("\n---", 3)
        if end != -1:
            fm_str = raw[3:end].strip()
            body = raw[end + 4:].strip()
            try:
                if len(fm_str) > _MAX_FM_CHARS:
                    raise ValueError("frontmatter too large to parse")   # a 50 MB tag list cost a minute per search (AG22)
                # libyaml accepts `key:\tvalue`, the pure loader every reader uses refuses it
                # (FN6): parse such frontmatter the way the readers do, so index and read agree.
                fm = load_mapping_or_none(fm_str, fast="	" not in fm_str)
                if fm is None:   # a rule line over a sentence or a list: the block is text (AG119)
                    fm, body = {}, raw.strip()
                confirmed_ts = _parse_confirmed_ts(fm)
            except Exception:
                fm = {}
            try:
                frontmatter_json = json.dumps(fm, default=str, ensure_ascii=False)   # `大阪城` in a title was `\u5927...` (MN16)
            except Exception:
                pass
    return frontmatter_json, body, confirmed_ts


def _fts_delete(conn: sqlite3.Connection, rel: str, rowid) -> None:
    """Drop a note's index row. `DELETE ... WHERE path=?` on an FTS5 column is a full scan (0.1 s
    per note at 5,000 notes), so a corpus whose mtimes all changed (a pull, a sync, a restore, a
    copy) stalled the first search for minutes (AG42, sweep 15). The rowid memory_files recorded
    is used when it still names this path; otherwise the scan runs once and records a fresh one."""
    if rowid is not None:
        try:
            row = conn.execute("SELECT path FROM memory_fts WHERE rowid=?", (int(rowid),)).fetchone()
        except (sqlite3.Error, TypeError, ValueError):
            row = None
        if row and row[0] == rel:
            conn.execute("DELETE FROM memory_fts WHERE rowid=?", (int(rowid),))
            return
    conn.execute("DELETE FROM memory_fts WHERE path=?", (rel,))


_NOTICE_MIN_NOTES = 200   # fewer changed notes than this re-index in well under a second: no notice
_LAST_INDEX_WORK: dict = {}  # what the last search's freshness step did, when it was a long one (AG84)


def _notice(msg: str) -> None:
    """One line on stderr (the CLI's terminal, the MCP server's and the dashboard's log): the first search
    on a large corpus waited a minute in silence (AG84: 53 s on 10,000 notes, nothing printed)."""
    log.info(msg)
    try:
        if sys.stderr is not None:
            sys.stderr.write(msg + "\n")
            sys.stderr.flush()
    except (OSError, ValueError):
        pass


def _noticed_rebuild(reason: str) -> int:
    """rebuild(force=True) from the search path, announced when the corpus is large enough to wait for."""
    try:
        total = sum(1 for _ in _walk_memory_stats())  # a stat walk: a fraction of the rebuild's reads
    except OSError:
        total = 0
    loud = total >= _NOTICE_MIN_NOTES
    if loud:
        _notice(f"Sextant: building the search index for {total:,} notes ({reason}); this runs once and can take a minute.")
    t0 = time.monotonic()
    n = max(rebuild(force=True), 0)
    secs = round(time.monotonic() - t0, 1)
    if loud:
        _LAST_INDEX_WORK.update(kind="rebuild", reason=reason, notes=n, seconds=secs)
        _notice(f"Sextant: search index built: {n:,} notes in {secs} s.")
    return n


def last_index_work() -> dict:
    """{kind, reason, notes, seconds} when the last search rebuilt the index or re-indexed many notes, else {}."""
    return dict(_LAST_INDEX_WORK)


def _index_state():
    """(empty, format tag) of the index on one connection: `empty` when it holds no note; the tag is '' when
    the row is missing and None when it cannot be read or the index is empty (the search path then fails open
    to the incremental update instead of forcing a rebuild: AG60)."""
    conn = _connect()
    try:
        empty = conn.execute("SELECT COUNT(*) FROM memory_files").fetchone()[0] == 0
        fmt = None
        if not empty:
            try:
                row = conn.execute("SELECT value FROM atlas_meta WHERE key='index_format'").fetchone()
                fmt = row[0] if row else ""
            except Exception:  # noqa: BLE001 - unreadable: fail open to the incremental update (AG60)
                fmt = None
    finally:
        conn.close()
    return empty, fmt


def update_incremental() -> int:
    """Bring the index up to date by touching ONLY changed files.

    Diffs the filesystem against memory_files by mtime: new and modified
    files are (re)indexed, deleted files are dropped, untouched files are
    never re-read. Returns the number of rows changed (0 = already fresh).

    This is the search-path freshness mechanism (ROADMAP item 4): the old
    path ran a FULL rebuild whenever one file changed, which is the
    documented cliff at tens-of-thousands-of-files corpus size. The full
    rebuild() remains for the CLI / recovery path.
    """
    empty, _fmt = _index_state()
    if empty:
        # Re-read under the lock (SEX-056): a rebuild clears the tables in one commit and fills them in the
        # next, so a search arriving in between saw an empty index, waited for the lock and then built
        # everything a second time. The format tag below was already re-read this way (AG60).
        with _rebuild_lock:
            empty, _fmt = _index_state()
            if empty:
                return _noticed_rebuild("the index is new")
    resuming = _fmt == _BUILDING + INDEX_FORMAT
    if _fmt is not None and _fmt != INDEX_FORMAT and not resuming:
        with _rebuild_lock:  # re-entrant: a concurrent search may have rebuilt while we waited (AG60: four searches made four full rebuilds)
            _tag = _index_format_tag()
            if _tag != INDEX_FORMAT and _tag != _BUILDING + INDEX_FORMAT:
                return _noticed_rebuild("this version indexes text differently")  # a normalisation change re-indexes every note once; the tag was read by the rebuild command only (MN46)
            resuming = _tag == _BUILDING + INDEX_FORMAT
    with _rebuild_lock:
        return _index_pass(resuming)


_BUILDING = "building:"   # index_format while a full build runs: every row present is right, the set is not yet complete (AG99)
_BATCH_NOTES = 500        # a build commits every this many notes ...
_BATCH_SECS = 2.0         # ... or this many seconds: a killed 20,000-note first index kept nothing and started over (MN87, AG99)


_SCAFFOLD_NOTE = "identity/user.md"
_SCAFFOLD_MARK = "Introduce yourself to your own memory"


def _stale_scaffold_rows(conn, todo, seen, db_state, rowids) -> list:
    """The identity note when its index row still holds the init template's instructions although the
    note itself is unchanged (SEX-052 item 9): an index built before AG111 kept the template as a search
    hit until the note changed. Queued once with a force flag; after the re-index the row no longer holds
    the mark, so a healed or never-affected index costs one rowid lookup and writes nothing. A note the
    user rewrote (the mark kept on purpose, not the template) is never forced, so it never re-indexes on
    every search."""
    rel = _SCAFFOLD_NOTE
    if rel not in seen or rel not in db_state or any(t[0] == rel for t in todo):
        return []
    try:
        rid = rowids.get(rel)
        row = None
        if rid is not None:
            row = conn.execute("SELECT path, body FROM memory_fts WHERE rowid=?", (int(rid),)).fetchone()
        if row is None or row[0] != rel:
            row = conn.execute("SELECT path, body FROM memory_fts WHERE path=?", (rel,)).fetchone()
    except (sqlite3.Error, TypeError, ValueError):
        return []
    if row is None or _SCAFFOLD_MARK not in (row[1] or ""):
        return []
    try:
        raw = (MEMORY_ROOT / rel).read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError):
        return []
    if without_scaffold(raw) is None or _SCAFFOLD_MARK in _parse_md(raw)[1]:
        return []   # the user's own text holds the phrase: indexed as written, never re-forced
    mtime, size = db_state[rel]
    return [(rel, mtime, size)]


_HEAL_STATS: dict = {}   # what the last healing pass did: notes read, notes queued (AG121; read by tests)
_HEAL_NOTICE_MIN_NOTES = 2000   # the pass reads and parses every note once: 35 s at 20,000 notes, warm (2026-11-25)


def _parse_heal_rows(conn, todo, seen, db_state, rowids, resuming: bool = False) -> list:
    """The unchanged notes whose index row the current parser would write differently, when the index's
    parse stamp is missing or older than PARSE_REVISION (AG121, MN93 F1: an index 0.1.0 built kept a
    rule-line top block out of the body until a forced rebuild). Each candidate is read once and parsed;
    only a differing row is queued. A stamped index costs two meta lookups."""
    try:
        got = dict(conn.execute("SELECT key, value FROM atlas_meta WHERE key IN ('parse_revision','last_indexed')"))
    except sqlite3.Error:
        return []
    if "last_indexed" in got and got.get("parse_revision") == _parse_stamp(got["last_indexed"]):
        return []
    if resuming and got.get("parse_revision") == _parse_stamp("building"):
        return []   # a build this code started: every row it kept is right (AG99)
    queued = {t[0] for t in todo}
    cands = [rel for rel in db_state if rel in seen and rel not in queued]
    _HEAL_STATS.update(read=len(cands), queued=0)
    if not cands:
        return []
    if len(cands) >= _HEAL_NOTICE_MIN_NOTES:   # the first search after an update waited in silence
        _notice(f"Sextant: checking {len(cands):,} notes against the search index after an update; "
                "this runs once and can take a minute.")
    from atlas_plus import _readahead
    out = []
    for rel, (_p, raw) in zip(cands, _readahead.read_texts([MEMORY_ROOT / r for r in cands], encoding="utf-8-sig")):
        if isinstance(raw, OSError):
            continue
        fm_json, body, _c = _parse_md(raw)
        want = (fm_json, _index_body(fm_json, body))
        try:
            rid = rowids.get(rel)
            row = conn.execute("SELECT path, frontmatter_json, body FROM memory_fts WHERE rowid=?", (int(rid),)).fetchone() if rid is not None else None
            if row is None or row[0] != rel:
                row = conn.execute("SELECT path, frontmatter_json, body FROM memory_fts WHERE path=?", (rel,)).fetchone()
        except (sqlite3.Error, TypeError, ValueError):
            continue
        if row is None or (row[1], row[2]) != want:
            mtime, size = db_state[rel]
            out.append((rel, mtime, size))
    _HEAL_STATS["queued"] = len(out)
    return out


def _parse_stamp_stale(conn) -> bool:
    try:
        got = dict(conn.execute("SELECT key, value FROM atlas_meta WHERE key IN ('parse_revision','last_indexed')"))
    except sqlite3.Error:
        return False
    return "last_indexed" in got and got.get("parse_revision") != _parse_stamp(got["last_indexed"])


def _index_pass(resuming: bool = False) -> int:
    """Index every note whose (mtime, size) differs from memory_files and drop the rows of vanished notes.

    Invariant (AG99): a note's FTS row and its memory_files row are committed in the same transaction, in
    batches, so after a kill at any point every committed note is indexed exactly as its recorded version and
    the next pass picks up the rest from where the kill left it. `last_indexed` and `index_format` advance only
    when a pass completes; a full build carries `building:<format>` until then, which the next search resumes
    (never restarts) and which `_needs_rebuild` reports as not built. Each note is re-checked under the write lock,
    so two processes resuming the same build never index a note twice."""
    conn = _connect()
    try:
        db_state = {}
        rowids = {}
        for row in conn.execute("SELECT path, mtime, size, fts_rowid FROM memory_files"):  # one scan, not two (AG92)
            db_state[row[0]] = (row[1], row[2])
            rowids[row[0]] = row[3]
        now = time.time()
        changed = 0
        seen: set[str] = set()
        # (mtime, size) pair: mtime alone misses a same-mtime rewrite on coarse-timestamp mounts (SMB, some
        # Docker volumes) -- expert review 2026-09-27. Size catches the common case; a same-size same-mtime
        # rewrite remains theoretically invisible until the next real change.
        todo = []
        for rel, mtime, size in _walk_memory_stats():
            seen.add(rel)
            if db_state.get(rel) != (mtime, size):
                todo.append((rel, mtime, size))
        forced = _stale_scaffold_rows(conn, todo, seen, db_state, rowids)
        todo.extend(forced)
        healing = _parse_heal_rows(conn, todo, seen, db_state, rowids, resuming)   # a parse change heals old rows (AG121)
        todo.extend(healing)
        forced = set(forced) | set(healing)
        restamp = not resuming and _parse_stamp_stale(conn)
        _t0 = time.monotonic()
        if len(todo) >= _NOTICE_MIN_NOTES:  # a pull, sync or restore re-indexes thousands of notes: say so before the wait (AG84)
            if resuming and db_state:
                _notice(f"Sextant: resuming the interrupted search index build: {len(db_state):,} notes kept, {len(todo):,} to go.")
            elif not resuming:
                _notice(f"Sextant: re-indexing {len(todo):,} changed notes for search; this runs once.")
        # A note with no recorded rowid (a new note) made _fts_delete scan the whole FTS table once per note:
        # 1,500 notes added beside an index of one took 22.9 s, 20,000 more than 22 minutes (MN87). The paths
        # the table holds are read once, and a path it does not hold has no row to delete.
        fts_paths = None
        if any(rowids.get(rel) is None for rel, _m, _s in todo):
            fts_paths = {r[0] for r in conn.execute("SELECT path FROM memory_fts")}
        batch, batch_t0 = 0, time.monotonic()
        # The notes are read ahead on threads, in this order (SEX-046: a cold open costs ~7.4 ms a note on
        # Windows, 15 of a 23 s first build at 2,000 notes); a note another process indexed meanwhile is read
        # and dropped, an unreadable one is skipped as before.
        from atlas_plus import _readahead
        texts = _readahead.read_texts([MEMORY_ROOT / rel for rel, _m, _s in todo], encoding="utf-8-sig")
        for (rel, mtime, size), (_path, raw) in zip(todo, texts):
            if not conn.in_transaction:
                conn.execute("BEGIN IMMEDIATE")   # the write lock before the re-check below
            cur_row = conn.execute("SELECT mtime, size, fts_rowid FROM memory_files WHERE path=?", (rel,)).fetchone()
            if cur_row is not None and (cur_row[0], cur_row[1]) == (mtime, size) and (rel, mtime, size) not in forced:
                continue   # another process indexed this version since the scan above
            rid = cur_row[2] if cur_row is not None else None
            if isinstance(raw, OSError):
                continue
            frontmatter_json, body, confirmed_ts = _parse_md(raw)
            if rid is not None or fts_paths is None or rel in fts_paths:
                _fts_delete(conn, rel, rid)
            cur = conn.execute(
                "INSERT INTO memory_fts(path, frontmatter_json, body) VALUES (?,?,?)",
                (rel, frontmatter_json, _index_body(frontmatter_json, body)),
            )
            conn.execute(
                "INSERT OR REPLACE INTO memory_files(path, mtime, indexed, last_confirmed, size, fts_rowid)"
                " VALUES (?,?,?,?,?,?)",
                (rel, mtime, now, confirmed_ts, size, cur.lastrowid),
            )
            changed += 1
            batch += 1
            if batch >= _BATCH_NOTES or time.monotonic() - batch_t0 >= _BATCH_SECS:
                conn.commit()   # durable progress: a kill from here on keeps these notes (AG99)
                batch, batch_t0 = 0, time.monotonic()
        for rel in set(db_state) - seen:
            _fts_delete(conn, rel, rowids.get(rel))
            conn.execute("DELETE FROM memory_files WHERE path=?", (rel,))
            changed += 1
        if changed or resuming or conn.in_transaction or restamp:
            conn.execute(
                "INSERT OR REPLACE INTO atlas_meta(key,value) VALUES ('index_format','" + INDEX_FORMAT + "'), ('last_indexed',?),"
                " ('parse_revision',?)",
                (str(now), _parse_stamp(str(now))),
            )
            conn.commit()
            log.info("FTS incremental update: %d row(s) changed", changed)
        if len(todo) >= _NOTICE_MIN_NOTES:
            _LAST_INDEX_WORK.update(kind="update", reason="notes changed since the last search",
                                    notes=len(todo), seconds=round(time.monotonic() - _t0, 1))
        return changed
    finally:
        conn.close()


# Messages sqlite emits for a damaged FTS5 index or file, across versions: 3.42 reports
# trashed shadow rows as "vtable constructor failed: memory_fts" where 3.50 says "malformed"
# (CI matrix 2026-11-09). A LOCKED/busy database matches none of these and is never corrupt.
_CORRUPTION_MARKERS = ("malformed", "not a database", "vtable constructor failed",
                       "database disk image", "fts5: corrupt", "no such table: memory_fts")


_LAST_CONNECT_ERROR: dict[str, str] = {"text": ""}


def _open_failure(exc: Exception) -> str:
    """A label for an index that could not be opened: a state directory that is a file is not a busy
    database (SEX-048 1b: the log said "db busy" and "locked" around the real cause)."""
    if "is a file, not a directory" in str(exc):
        return "the state directory cannot be used; Next: move that file aside, or point ATLAS_STATE_DIR at a folder"
    return "the index database is busy or locked"


def last_connect_error() -> str:
    """Why the last search returned nothing although the index could not be opened (disk full,
    read-only state dir, locked); '' when the last connect succeeded (AG36: 'No hits' hid it)."""
    return _LAST_CONNECT_ERROR["text"]


def _is_corruption(exc: sqlite3.Error) -> bool:
    """True only for genuine corruption. A LOCKED/busy database is never corrupt."""
    msg = str(exc).lower()
    if isinstance(exc, sqlite3.OperationalError) and ("locked" in msg or "busy" in msg):
        return False
    return any(marker in msg for marker in _CORRUPTION_MARKERS)


def _heal_malformed() -> bool:
    """Recover from FTS5 shadow-table corruption (run 39, found live).

    Drop + recreate the FTS tables in place -- this preserves co-tenant
    tables in the same DB file (memory_vectors) -- then rebuild from the
    corpus. If even DROP fails, the FILE is corrupt: quarantine it
    (rename, never delete -- archive-not-delete) and rebuild fresh,
    sacrificing co-tenants (they are derived too; embed-index refills).
    Returns True when a rebuild completed.
    """
    try:
        conn = _connect()
        try:
            conn.execute("DROP TABLE IF EXISTS memory_fts")
            conn.execute("DROP TABLE IF EXISTS memory_files")
            conn.commit()
        finally:
            conn.close()
    except sqlite3.DatabaseError:
        try:
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            FTS_DB.rename(FTS_DB.with_name(FTS_DB.name + f".corrupt-{stamp}"))
            log.error("FTS DB quarantined as %s.corrupt-%s (file-level corruption)",
                      FTS_DB.name, stamp)
            # Keep the three newest quarantines; they accumulated forever (MN3, 2026-11-11).
            olds = sorted(FTS_DB.parent.glob(FTS_DB.name + ".corrupt-*"))
            for stale in olds[:-3]:
                try:
                    stale.unlink()
                except OSError:
                    pass
        except OSError as exc:
            log.error("could not quarantine corrupt FTS DB: %s", exc)
            return False
    try:
        rebuild(force=True)
        return True
    except sqlite3.DatabaseError as exc:
        log.error("rebuild after corruption failed: %s", exc)
        return False


def rebuild(force: bool = False) -> int:
    """Rebuild the FTS index. Returns count of indexed files."""
    if not force and not _needs_rebuild():
        return -1  # no-op

    with _rebuild_lock:
        # Re-check after acquiring lock -- another thread may have rebuilt while we waited
        if not force and not _needs_rebuild():
            return -1

        conn = _connect()
        try:
            row = conn.execute("SELECT value FROM atlas_meta WHERE key='index_format'").fetchone()
            if not row or row[0] != _BUILDING + INDEX_FORMAT:
                # A fresh build: clear and mark it unfinished in one transaction. An interrupted build of
                # this format resumes instead (its rows are right; AG99), here and on the search path.
                if not conn.in_transaction:
                    conn.execute("BEGIN IMMEDIATE")
                # Drop and re-create instead of deleting row by row: on a built index of 20,000 notes
                # the delete took 10 s and the drop 1.6 s (SEX-052). Same transaction, same tokenizer
                # (the one _connect recorded), so a crash here leaves the old index whole.
                tok = conn.execute("SELECT value FROM atlas_meta WHERE key='tokenizer'").fetchone()
                if tok and tok[0]:
                    conn.execute("DROP TABLE IF EXISTS memory_fts")
                    conn.execute(_CREATE_FTS.format(tokenizer=tok[0].removesuffix(_INDEX_TAG)))
                else:
                    conn.execute("DELETE FROM memory_fts")
                conn.execute("DELETE FROM memory_files")
                conn.execute("DELETE FROM atlas_meta WHERE key='last_indexed'")
                conn.execute("INSERT OR REPLACE INTO atlas_meta(key,value) VALUES ('index_format',?), ('parse_revision',?)",
                             (_BUILDING + INDEX_FORMAT, _parse_stamp("building")))
                conn.commit()
        finally:
            conn.close()
        _index_pass(resuming=True)
        conn = _connect()
        try:
            count = conn.execute("SELECT COUNT(*) FROM memory_files").fetchone()[0]
        finally:
            conn.close()
        log.info("FTS index rebuilt: %d files indexed into %s", count, FTS_DB)
        return count


def main() -> None:
    """One line on a full disk or a read-only state dir instead of a traceback (AG36)."""
    try:
        _main_guarded()
    except sqlite3.OperationalError as exc:
        print(f"sextant-fts-rebuild: could not open the search index: {exc}. The notes are intact; "
              "the state dir is full, read-only or locked (free space or fix permissions, then run this again).", file=sys.stderr)
        raise SystemExit(1)


def _main_guarded() -> None:
    """Console entry point for ``atlas-fts-rebuild`` / ``sextant-fts-rebuild``.

    The bare ``rebuild()`` is unsuitable as an entry point: console_scripts run
    ``sys.exit(func())``, and rebuild() returns the indexed-file COUNT (a nonzero
    "failure" exit code) -- or -1 (a no-op) when the index is already fresh. This
    wrapper forces a full rebuild by default, prints the count, and exits 0.
    """
    from atlas_plus._console import utf8_stdout_when_piped
    utf8_stdout_when_piped()
    import argparse

    from atlas_plus import _next_step  # usage errors name --help (LES-054)
    parser = _next_step.ArgumentParser(
        prog=__import__("atlas_plus.branding", fromlist=["prog"]).prog("atlas-fts-rebuild"),
        description="Rebuild the .memory FTS index (full rebuild by default).",
    )
    parser.add_argument(
        "--if-stale", action="store_true",
        help="only rebuild when the index is out of date (default: force a full rebuild)",
    )
    args = parser.parse_args()

    if not MEMORY_ROOT.is_dir():
        # before init it built an empty index (and the state dir) and said "rebuilt: 0 files" (AG102);
        # the sibling of `sextant search` before init (AG25): create nothing, name the step.
        raise SystemExit(_next_step.say(f"{parser.prog}: no corpus yet at {MEMORY_ROOT} (nothing was built).",
                                        f"run {_next_step.command('init')} first."))
    count = rebuild(force=not args.if_stale)
    if count == -1:
        print("FTS index already fresh -- nothing to do.")
    else:
        print(f"FTS index rebuilt: {count} files indexed into {FTS_DB}")


# Freshness debounce (measured, quality loop run 27): the incremental sweep
# is a full scandir mtime walk and cost 130ms of every 200ms search at ~2,200
# files -- paid again by every query in a burst. Within this window, skip the
# sweep; mark_dirty() (called by every in-process corpus write path) forces
# the next search to sweep, so write-then-search never misses its own write.
# Cross-process writers are covered by the window's expiry (worst case:
# results up to ATLAS_FTS_FRESHNESS_WINDOW seconds stale, default 10).
#
# Expert pass 2026-10-05 hardening: a GENERATION counter instead of zeroing
# the timestamp -- a sweep that finishes after a concurrent mark_dirty()
# could otherwise stamp the window and hide that write for a full window
# (searches run in threads via the web app's to_thread). The sweep captures
# the generation BEFORE sweeping and only counts as covering up to it.
# _last_fresh_sweep starts at -inf (0.0 skipped the first sweep when the
# process started within `window` seconds of boot -- monotonic() is small).
_last_fresh_sweep: float = float("-inf")
_dirty_gen: int = 0
_swept_gen: int = -1


def _freshness_window() -> float:
    raw = os.environ.get("ATLAS_FTS_FRESHNESS_WINDOW", "10")
    try:
        val = float(raw)
    except ValueError:
        return 10.0
    if val != val:  # NaN
        return 10.0
    return min(max(0.0, val), 3600.0)  # cap: 'inf' must not disable freshness


def mark_dirty() -> None:
    """Force the next search to run the freshness sweep (in-process writes)."""
    global _dirty_gen
    _dirty_gen += 1


def search(query: str, scope: str = "", limit: int = 10,
           _healed: bool = False, _fallback_stage: int = 0, _raw: str | None = None,
           loose: bool = False) -> list[dict]:
    """FTS search. Returns list of {path, snippet, score}.

    `loose=True` is for a caller answering a person's or an agent's question (the search tool,
    the CLI): when no note holds every word of the query, notes sharing some of its content words
    are returned, flagged `loose_match`, low band at most. Every other caller (duplicate checks,
    session priming, bots) gets strict matching, as before, and none of the extra work."""
    if _raw is None:
        if _ALIAS_MARK in query:   # every query path, the literal-phrase fallback included: the mark matched every aliased note (AG96)
            query = query.replace(_ALIAS_MARK, " ")
        # `fox fox fox ...` x1000 took 44 s: a repeated word adds nothing, so each word once, and
        # at most 64 of them (MN29)
        _words = list(dict.fromkeys(query.split()))
        if len(_words) < len(query.split()) or len(_words) > 64:
            query = " ".join(_words[:64])
        _raw = query
        query = _prepare_query(query)
    # Freshness on search: incremental -- only changed files are
    # re-read/re-indexed (a stat sweep when nothing changed). The old
    # full-rebuild-on-any-change was the large-corpus cliff (ROADMAP 4).
    # Fail open (expert review 2026-09-27): if another process holds the DB
    # past busy_timeout, serve the possibly-slightly-stale index instead of
    # letting OperationalError escape a SEARCH call.
    global _last_fresh_sweep, _swept_gen
    now = time.monotonic()
    if not _healed and not _fallback_stage:
        _LAST_INDEX_WORK.clear()  # per search: the field names this search's wait only
    if _swept_gen != _dirty_gen or now - _last_fresh_sweep >= _freshness_window():
        gen_before = _dirty_gen
        try:
            update_incremental()
            _last_fresh_sweep = time.monotonic()
            _swept_gen = gen_before
        except sqlite3.OperationalError as exc:
            log.warning("freshness update skipped (%s): %s -- serving current index", _open_failure(exc), exc)
            if (_index_format_tag() or "").startswith(_BUILDING):   # never a silent half-index (AG99)
                _notice("Sextant: the search index is still being built by another process; results may miss notes.")
        except sqlite3.DatabaseError as exc:
            # Malformed index: let the query below trigger the self-heal.
            log.error("freshness update hit a corrupt index: %s", exc)

    try:
        conn = _connect()
    except sqlite3.DatabaseError as exc:
        # File-level corruption ("file is not a database") raises at CONNECT
        # time -- the WAL pragma reads the header -- so the query-level
        # self-heal below never saw it and search CRASHED on a truly trashed
        # DB file (found 2026-10-09 when the quarantine test's corruption
        # became genuinely file-level). Same contract as below: heal once,
        # retry once, never raise out of a search call.
        if not _is_corruption(exc):
            if "no such module" in str(exc).lower():
                log.error("keyword search unavailable: this Python's SQLite has no FTS5 module (%s); "
                          "install a Python whose sqlite3 was built with FTS5 (python.org, uv and the "
                          "distro packages all have it) -- returning empty results", exc)
                return []
            log.warning("FTS connect failed (not corruption: %s): %s -- returning empty results",
                        _open_failure(exc), exc)
            _LAST_CONNECT_ERROR["text"] = str(exc)
            return []
        if not _healed and _heal_malformed():
            log.error("FTS DB was corrupt at connect (%s) -- self-healed; retrying query once", exc)
            return search(query, scope=scope, limit=limit, _healed=True, _raw=_raw, loose=loose)
        log.error("FTS connect failed on a corrupt DB and self-heal did not recover: %s", exc)
        return []
    _LAST_CONNECT_ERROR["text"] = ""
    try:
        if scope:
            # Escape LIKE wildcards in scope so a scope like "project_%foo"
            # doesn't match unexpected paths.
            # Use | as the LIKE escape character (unambiguous, never appears in memory paths).
            # Original code used ESCAPE '\' but Python string processing in triple-quoted
            # strings converts \' to ', making the ESCAPE clause an empty string which
            # SQLite rejects with "ESCAPE expression must be a single character".
            scope_safe = scope.rstrip("/").replace("|", "||").replace("%", "|%").replace("_", "|_")
            # a scope that names a file (write-side grammar) matches that file only
            like_pattern = scope_safe if scope.lower().endswith(".md") else f"{scope_safe}/%"
            rows = conn.execute(
                """
                SELECT path,
                       CASE WHEN length(body) <= 60000 THEN snippet(memory_fts, 2, '\ue001', '\ue002', '\ue003', 20) END AS snippet,
                       rank, rowid
                FROM memory_fts
                WHERE memory_fts MATCH ?
                  AND path LIKE ? ESCAPE '|'
                ORDER BY rank
                LIMIT ?
                """,
                (query, like_pattern, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT path,
                       CASE WHEN length(body) <= 60000 THEN snippet(memory_fts, 2, '\ue001', '\ue002', '\ue003', 20) END AS snippet,
                       rank, rowid
                FROM memory_fts
                WHERE memory_fts MATCH ?
                ORDER BY rank
                LIMIT ?
                """,
                (query, limit),
            ).fetchall()

        # Deduplicate by path: keep the highest-scoring row per file.
        # FTS5 can match the same file multiple times when a query term appears
        # in both the frontmatter and body columns.
        seen: dict[str, dict] = {}
        for r in rows:
            p, snip, sc = r[0], r[1], r[2]
            if p in seen and sc >= seen[p]["score"]:
                continue
            indexed = _row_body(conn, r[3])
            orig = _original_text(p, indexed) if indexed else ""
            if snip is None:  # a huge note: snippet() walked every hit (MN66: minutes for a common word in a 4 MB note)
                snip = _cheap_snippet(conn, r[3], query, orig=orig)
            else:
                snip = _snippet_from_fts(snip, indexed, orig)   # in the note's own letters (AG83)
            snip = (snip or "").replace(_CJK_SEP, "").replace(" " + _SENTINEL + " ", " ").replace(_SENTINEL, "").replace(_ALIAS_MARK, "")
            snip = strip_snippet_comments(snip)   # the provenance tag is metadata, not text (AG97)
            if p not in seen or sc < seen[p]["score"]:  # FTS5 rank is negative; lower = better
                seen[p] = {"path": p, "snippet": snip, "score": sc}
                if _fallback_stage == 4:
                    seen[p]["_rowid"] = r[3]     # for _loose_results' coverage count; removed there

        results = list(seen.values())
        if not results:
            if _fallback_stage == 0 and _raw is not None:
                loose = _bigram_fallback(_raw)
                if loose:
                    loose_results = search(loose, scope=scope, limit=limit, _fallback_stage=3, _raw=_raw)
                    for r in loose_results:
                        # OR-ed bigrams match on a shared particle alone; the band is 'low' at most (MN25)
                        r["score"] = min(float(r.get("score", 0) or 0), 0.35)
                        r["loose_match"] = True
                    if loose_results:
                        return loose_results
            if loose and _fallback_stage in (0, 1, 2) and _raw is not None:
                # every word was required and no note holds them all: rank by the words shared
                return _loose_results(_raw, scope, limit, 2)
            return results
        # NOTE: a single result MUST still go through the blend below.
        # The old `<= 1` early-return left a lone hit carrying its raw
        # NEGATIVE FTS5 rank, which the server's confidence bands read as
        # top score < 0.2 -> "starved" -- one perfect unique match told
        # the model to ask a clarifying question instead of answering
        # (launch blocker, competitive-sweep raw/18a; fixed 2026-10-07).

        # Re-rank: blend FTS5 BM25, mtime recency, and last_confirmed staleness.
        # FTS5 rank is negative; lower (more negative) = better BM25 hit.
        # All signals normalised to 0-1 before blending.
        #
        # Weights: BM25 65%, mtime recency 20%, confirmation recency 15%.
        # last_confirmed scores: 1.0 if confirmed <30d, 0.5 if unconfirmed (neutral),
        # 0.0 if confirmed >1y ago (stale validation).
        fts_scores = [abs(r["score"]) for r in results]
        max_fts = max(fts_scores) or 1.0

        # Fetch mtime + last_confirmed for all result paths in one query
        path_list = list(seen.keys())
        placeholders = ",".join("?" * len(path_list))
        meta_rows = conn.execute(
            # bandit B608 justification: `placeholders` is a literal "?,?,..." string;
            # every value is bound (bandit triage, quality loop run 7).
            f"SELECT path, mtime, last_confirmed FROM memory_files WHERE path IN ({placeholders})",  # nosec B608
            path_list,
        ).fetchall()
        mtime_map: dict[str, float] = {}
        confirmed_map: dict[str, float | None] = {}
        for row in meta_rows:
            mtime_map[row[0]] = row[1]
            confirmed_map[row[0]] = row[2]
        now_ts = time.time()
        max_age = 365 * 24 * 3600  # 1 year

        for r in results:
            path = r["path"]
            fts_norm = abs(r["score"]) / max_fts  # 0-1, higher = better
            mtime = mtime_map.get(path, now_ts)
            age_secs = now_ts - mtime
            recency = max(0.0, 1.0 - age_secs / max_age)  # 0-1, 1=very recent
            conf_score = _confirmed_score(confirmed_map.get(path), now_ts)
            # Scope authority: feedback/ and identity/ files carry high signal
            authority = 1.2 if path.startswith("feedback/") or path.startswith("identity/") else 1.0
            combined = (fts_norm * 0.65 + recency * 0.20 + conf_score * 0.15) * authority
            r["score"] = combined

        results.sort(key=lambda x: x["score"], reverse=True)
        if loose and _fallback_stage in (0, 1, 2) and _raw is not None and len(results) < limit:
            # a sentence-like query that few notes satisfy in full: the full matches stay on top,
            # the any-word matches follow, scored below the weakest of them
            floor = min(float(r["score"]) for r in results)
            have = {r["path"] for r in results}
            extra = [r for r in _loose_results(_raw, scope, limit, _LOOSE_FILL_WORDS, min(_LOOSE_CAP, floor * 0.99))
                     if r["path"] not in have]
            results.extend(extra[: limit - len(results)])
        return results
    except sqlite3.OperationalError as exc:
        msg = str(exc)
        # FTS5 treats operators (AND/OR/NEAR), parens, and stray quotes as
        # SYNTAX -- a human pasting a sentence is not writing FTS5. Retry the
        # exact text as an escaped literal phrase instead of failing (and
        # instead of the old message that blamed a "corrupt" index for a bad
        # query -- impatient-persona finding, quality loop run 4).
        syntax = ("fts5: syntax error" in msg or "unterminated string" in msg or "no such column" in msg)
        if syntax and _fallback_stage == 0:
            # Stage 1: quote each whitespace token and let FTS5 AND them. A dotted or
            # punctuated token ("config.toml AppData") used to drop straight to a literal
            # phrase, which only matches the words adjacent in that order and so returned
            # nothing although every word was on disk (deep install lanes 2026-11-09).
            folded = _nordic_finish(_query_words(_fold_marks(_recompose(_nordic_mark(unicodedata.normalize("NFKC", _raw).replace('"', " "))))))
            tokens = [t for t in folded.split() if any(c.isalnum() for c in t)]  # AG91: a bare `(` became the empty phrase `"("`, and AND-ed to an alternatives group it matched nothing; folded like the main query (AG83: `Straße.` and `الخادم,` kept the raw form here)
            if len(tokens) > 1 or _ALT_OPEN in folded:
                terms = [_quoted_term(t, cjk=True) for t in tokens]
                tokenized = (" AND " if _ALT_OPEN in folded else " ").join(terms)
                log.info("FTS query was not valid FTS5 syntax; retrying with each token quoted")
                conn.close()
                return search(tokenized, scope=scope, limit=limit, _fallback_stage=1, _raw=_raw, loose=loose)
            _fallback_stage = 1  # single token: go straight to the phrase form
        if syntax and _fallback_stage == 1:
            phrase = '"' + _cjk_split(_affix_split(_emoji_split(_fold_marks(_recompose(unicodedata.normalize("NFKC", _raw)))))).replace('"', '""') + '"'
            log.info("FTS query still not valid FTS5 syntax; retrying as one literal phrase")
            conn.close()
            return search(phrase, scope=scope, limit=limit, _fallback_stage=2, _raw=_raw, loose=loose)
        # Genuinely unexpected: table missing or corrupted DB.
        log.warning("FTS search failed (index missing or corrupt): %s -- returning empty results", exc)
        return []
    except sqlite3.DatabaseError as exc:
        # "database disk image is malformed" -- found LIVE 2026-10-07
        # (quality loop run 39): FTS5 shadow-table corruption passes
        # PRAGMA integrity_check yet throws on every MATCH, and only
        # OperationalError was caught, so the error escaped to every
        # caller. The index is derived and rebuildable BY DESIGN:
        # self-heal (drop + recreate the FTS tables in place, preserving
        # co-tenant tables like memory_vectors; quarantine the file only
        # if even that fails) and retry the query exactly once.
        conn.close()
        if not _is_corruption(exc):
            log.warning("FTS search failed (not corruption): %s -- returning empty results", exc)
            return []
        if not _healed and _heal_malformed():
            log.error("FTS index was corrupt (%s) -- self-healed by rebuild; retrying query once", exc)
            return search(query, scope=scope, limit=limit, _healed=True, _raw=_raw, loose=loose)
        log.error("FTS search failed on a corrupt index and self-heal did not recover: %s", exc)
        return []
    finally:
        conn.close()


if __name__ == "__main__":
    main()  # one success line for `python -m` and the console script (SEX-052 item 4)
