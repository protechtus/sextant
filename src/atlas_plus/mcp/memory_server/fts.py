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
_INDEX_TAG = " +cjk2"
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
})


def _recompose(text: str) -> str:
    """NFKC decomposes Thai sara am (U+0E33) into nikhahit + sara aa and Lao am (U+0EB3) likewise; the index and
    the snippets then showed `ทํา` for `ทำ` and `น้ำ` met `นา` (MN52). Put them back."""
    if "\u0e4d\u0e32" in text or "\u0ecd\u0eb2" in text:
        text = text.replace("\u0e4d\u0e32", "\u0e33").replace("\u0ecd\u0eb2", "\u0eb3")
    if "\u200c" in text or "\u200d" in text:
        text = _INDIC_JOINER_RE.sub("", text)  # ZWJ/ZWNJ between Indic letters are typed or not by keyboard: `राम` must meet `रा\u200dम` (MN55); between Persian letters the ZWNJ stays a separator
    if any("\u0386" <= c <= "\u03ce" or c in "\u1e9e\u0259\u018f\u0111\u0110\u0142\u0141\u00f8\u00d8\u00f0\u00d0\u00fe\u00de" for c in text):
        text = text.translate(_GREEK_ETC)  # Greek tonos/dialytika, capital ẞ, Azeri ə: unicode61 folds none of them (MN62)
    if "\u0130" in text or "\u0131" in text:
        text = text.replace("\u0130", "i").replace("\u0131", "i")  # Turkish dotted I and dotless i: `Diyarbak\u0131r` met neither `diyarbakir` nor `D\u0130YARBAKIR` (AG72)
    if "\u0451" in text or "\u0401" in text or "\u00df" in text:
        text = text.replace("\u0451", "\u0435").replace("\u0401", "\u0415").replace("\u00df", "ss")  # ё is typed as е and ß as ss (MN58: `елка` missed `ёлка`, `Strasse` missed `Straße`)
    return text


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


@functools.lru_cache(maxsize=65536)
def _affix_word(word: str, keep_prefix: bool) -> str:
    pre, stem, suf = (_he_split if "\u05d0" <= word[0] <= "\u05ea" else _ar_split)(word)
    out = (pre + _CJK_SEP if pre and keep_prefix else "") + stem
    return out + (_CJK_SEP + suf if suf else "")


def _affix_split(text: str, keep_prefix: bool = True) -> str:
    """Index form (keep_prefix): `prefix SEP stem SEP suffix`; unquoted query form: `stem SEP suffix`."""
    if text.isascii() or not _RTL_RE.search(text):
        return text
    text = _AR_WORD_RE.sub(lambda m: _affix_word(m.group(0), keep_prefix), text)
    return _HE_WORD_RE.sub(lambda m: _affix_word(m.group(0), keep_prefix), text)


def _query_words(q: str) -> str:
    """The query side of the AG83 transforms: emoji clusters as separate terms, prefixes dropped outside quotes and
    kept inside them (a quoted phrase is matched in the indexed form, prefix token included)."""
    q = _emoji_split(q, " ")
    if '"' not in q:
        return _affix_split(q, keep_prefix=False)
    parts = q.split('"')
    return '"'.join(_affix_split(part, keep_prefix=bool(i % 2)) for i, part in enumerate(parts))


def _norm_index_text(text: str) -> str:
    broken = _BREAK_RE.sub(lambda m: " " + _SENTINEL + " ", text)
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


def _index_body(frontmatter_json: str, body: str) -> str:
    """The indexed body: the normalised note body followed by the frontmatter's values, so a
    CJK title or tag is searchable (MN16: `大阪城の歴史` in a title found nothing) while the
    stored frontmatter_json stays byte-for-byte parseable for the topics tool."""
    text = _norm_index_text(body)
    try:
        values = _fm_values(json.loads(frontmatter_json))
    except (ValueError, TypeError):
        values = []
    if values:
        text += "\n" + _norm_index_text("\n".join(values)[:_MAX_FM_INDEX_CHARS])
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
        raw = path.read_text(encoding="utf-8-sig", errors="replace")
    except (OSError, ValueError):
        return ""
    frontmatter_json, body, _ = _parse_md(raw)
    try:
        values = _fm_values(json.loads(frontmatter_json))
    except (ValueError, TypeError):
        values = []
    return body + ("\n" + "\n".join(values)[:_MAX_FM_INDEX_CHARS] if values else "")


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
    try:
        body = conn.execute("SELECT body FROM memory_fts WHERE rowid = ?", (rowid,)).fetchone()[0] or ""
    except Exception:  # noqa: BLE001
        return ""
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
    q = _query_words(_fold_marks(_recompose(unicodedata.normalize("NFKC", raw))).replace(_SENTINEL, " "))  # the index holds recomposed sara am: decomposed bigrams never matched (MN62)
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
        parts += ['"' + w.replace('"', '""') + '"' for w in q[pos:m.start()].split()]
        run = m.group(0)
        parts.append(_group(run) if len(run) >= 3 else '"' + _CJK_SEP.join(run) + '"')
        pos = m.end()
    parts += ['"' + w.replace('"', '""') + '"' for w in q[pos:].split()]
    return " AND ".join(parts)


def _prepare_query(query: str) -> str:
    """NFKC-fold the query and turn every CJK run into a phrase of its characters; a run glued
    to Latin text becomes its own term (`2025年` -> `2025 "年"`)."""
    q = _query_words(_fold_marks(_recompose(unicodedata.normalize("NFKC", query))).replace(_SENTINEL, " "))
    q = re.sub(r"(?<=\S):(?=\S)", " ", q)  # `alpha:beta` is two words, not an FTS5 column filter (AG46: a silent miss)
    if not _CJK_RE.search(q):
        return q
    q = _CJK_RUN_OR_CHAR_RE.sub(lambda m: ' "' + _CJK_SEP.join(m.group(0)) + '" ', q)
    return " ".join(q.split())
_CREATE_FTS = """
CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts
USING fts5(path, frontmatter_json, body, tokenize='{tokenizer}');
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


def _connect() -> sqlite3.Connection:
    try:
        FTS_DB.parent.mkdir(parents=True, exist_ok=True)
    except FileExistsError:
        # The state dir is a FILE (AG10, sweep 10): a named OperationalError reaches search's
        # "not corruption" branch as one log line instead of a traceback.
        raise sqlite3.OperationalError(
            f"state directory {FTS_DB.parent} is a file, not a directory; move it aside")
    conn = sqlite3.connect(str(FTS_DB), timeout=30)
    # If setup raises (a trashed file fails at the WAL pragma, which reads
    # the header), CLOSE before re-raising: sqlite3.connect() has already
    # opened the file, the caller never receives the object, and on Windows
    # the leaked handle blocks the quarantine rename with WinError 32
    # (found 2026-10-09 hardening the file-level-corruption path).
    try:
        # busy_timeout FIRST so the journal_mode switch waits on a lock
        # instead of failing instantly with "database is locked".
        conn.execute("PRAGMA busy_timeout=30000")  # embed-index bulk writes vs interactive search
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
    stack: list[tuple[str, str]] = [(str(MEMORY_ROOT), "")]
    while stack:
        d, prefix = stack.pop()
        try:
            entries = os.scandir(d)
        except OSError:
            continue
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
                if not _utf8_name(name):
                    continue   # parity with _walk_memory (MN17)
                rel = prefix + name
                parts = rel.split("/")
                if parts[0] in EXCLUDED_DIRS or is_excluded_mirror(parts):
                    continue
                if not is_dir and name == "_README.md":
                    continue
                if is_dir:
                    stack.append((entry.path, rel + "/"))
                else:
                    try:
                        st = entry.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    yield rel, st.st_mtime, st.st_size


INDEX_FORMAT = "7"  # bump when _norm_index_text changes: every note is re-indexed (2 = Thai-like runs split, Arabic/Hebrew marks folded; 3 = Hebrew punctuation kept, and an index stamped 2 by the search path without a rebuild; 4 = Turkish dotted/dotless i folded, AG72; 5 = Greek tonos, capital sharp s, Azeri schwa, MN62; 6 = emoji clusters, Arabic/Hebrew prefixes, Persian plurals, AG83; 7 = stroke letters folded, toned ZWJ sequences, MN74)


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
        last_indexed = float(row[0])
        for _, mtime, _size in _walk_memory_stats():
            if mtime > last_indexed:
                return True
        return False
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


def _parse_md(raw: str) -> tuple[str, str, float | None]:
    """Split a memory file into (frontmatter_json, body, confirmed_ts)."""
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
                fm = yaml.load(fm_str, Loader=yaml.SafeLoader if "\t" in fm_str else _YAML_LOADER) or {}
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
    # Empty/missing index falls back to the full rebuild -- checked OUTSIDE
    # the lock (rebuild() takes the same non-reentrant lock).
    conn = _connect()
    try:
        empty = conn.execute("SELECT COUNT(*) FROM memory_files").fetchone()[0] == 0
    finally:
        conn.close()
    if empty:
        return _noticed_rebuild("the index is new")
    _fmt = _index_format_tag()
    if _fmt is not None and _fmt != INDEX_FORMAT:
        with _rebuild_lock:  # re-entrant: a concurrent search may have rebuilt while we waited (AG60: four searches made four full rebuilds)
            if _index_format_tag() != INDEX_FORMAT:
                return _noticed_rebuild("this version indexes text differently")  # a normalisation change re-indexes every note once; the tag was read by the rebuild command only (MN46)

    with _rebuild_lock:
        conn = _connect()
        try:
            db_state = {
                row[0]: (row[1], row[2])
                for row in conn.execute("SELECT path, mtime, size FROM memory_files")
            }
            rowids = {row[0]: row[1] for row in conn.execute("SELECT path, fts_rowid FROM memory_files")}
            now = time.time()
            changed = 0
            seen: set[str] = set()
            # (mtime, size) pair: mtime alone misses a same-mtime rewrite
            # on coarse-timestamp mounts (SMB, some Docker volumes) --
            # expert review 2026-09-27. Size catches the common case;
            # a same-size same-mtime rewrite remains theoretically
            # invisible until the next real change.
            todo = []
            for rel, mtime, size in _walk_memory_stats():
                seen.add(rel)
                if db_state.get(rel) != (mtime, size):
                    todo.append((rel, mtime, size))
            _t0 = time.monotonic()
            if len(todo) >= _NOTICE_MIN_NOTES:  # a pull, sync or restore re-indexes thousands of notes: say so before the wait (AG84)
                _notice(f"Sextant: re-indexing {len(todo):,} changed notes for search; this runs once.")
            for rel, mtime, size in todo:
                try:
                    raw = (MEMORY_ROOT / rel).read_text(encoding="utf-8-sig", errors="replace")
                except OSError:
                    continue
                frontmatter_json, body, confirmed_ts = _parse_md(raw)
                _fts_delete(conn, rel, rowids.get(rel))
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
            for rel in set(db_state) - seen:
                _fts_delete(conn, rel, rowids.get(rel))
                conn.execute("DELETE FROM memory_files WHERE path=?", (rel,))
                changed += 1
            if changed:
                conn.execute(
                    "INSERT OR REPLACE INTO atlas_meta(key,value) VALUES ('index_format','" + INDEX_FORMAT + "'), ('last_indexed',?)",
                    (str(now),),
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
            conn.execute("DELETE FROM memory_fts")
            conn.execute("DELETE FROM memory_files")
            count = 0
            now = time.time()
            for rel, abs_path in _walk_memory():
                try:
                    raw = abs_path.read_text(encoding="utf-8-sig", errors="replace")
                except OSError:
                    continue

                frontmatter_json, body, confirmed_ts = _parse_md(raw)

                cur = conn.execute(
                    "INSERT INTO memory_fts(path, frontmatter_json, body) VALUES (?,?,?)",
                    (rel, frontmatter_json, _index_body(frontmatter_json, body)),
                )
                st = abs_path.stat()
                conn.execute(
                    "INSERT OR REPLACE INTO memory_files(path, mtime, indexed, last_confirmed, size, fts_rowid)"
                    " VALUES (?,?,?,?,?,?)",
                    (rel, st.st_mtime, now, confirmed_ts, st.st_size, cur.lastrowid),
                )
                count += 1

            conn.execute(
                "INSERT OR REPLACE INTO atlas_meta(key,value) VALUES ('index_format','" + INDEX_FORMAT + "'), ('last_indexed',?)",
                (str(now),),
            )
            conn.commit()
            log.info("FTS index rebuilt: %d files indexed into %s", count, FTS_DB)
            return count
        finally:
            conn.close()


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

    parser = argparse.ArgumentParser(
        prog=__import__("atlas_plus.branding", fromlist=["prog"]).prog("atlas-fts-rebuild"),
        description="Rebuild the .memory FTS index (full rebuild by default).",
    )
    parser.add_argument(
        "--if-stale", action="store_true",
        help="only rebuild when the index is out of date (default: force a full rebuild)",
    )
    args = parser.parse_args()

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
           _healed: bool = False, _fallback_stage: int = 0, _raw: str | None = None) -> list[dict]:
    """FTS search. Returns list of {path, snippet, score}."""
    if _raw is None:
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
            log.warning("freshness update skipped (db busy): %s -- serving current index", exc)
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
            log.warning("FTS connect failed (not corruption, e.g. locked): %s -- returning empty results", exc)
            _LAST_CONNECT_ERROR["text"] = str(exc)
            return []
        if not _healed and _heal_malformed():
            log.error("FTS DB was corrupt at connect (%s) -- self-healed; retrying query once", exc)
            return search(query, scope=scope, limit=limit, _healed=True, _raw=_raw)
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
            snip = (snip or "").replace(_CJK_SEP, "").replace(" " + _SENTINEL + " ", " ").replace(_SENTINEL, "")
            if p not in seen or sc < seen[p]["score"]:  # FTS5 rank is negative; lower = better
                seen[p] = {"path": p, "snippet": snip, "score": sc}

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
                    return loose_results
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
            folded = _query_words(_fold_marks(_recompose(unicodedata.normalize("NFKC", _raw))).replace('"', " "))
            tokens = [t for t in folded.split() if t]  # folded like the main query (AG83: `Straße.` and `الخادم,` kept the raw form here)
            if len(tokens) > 1:
                tokenized = " ".join('"' + _cjk_split(t).replace('"', '""') + '"' for t in tokens)
                log.info("FTS query was not valid FTS5 syntax; retrying with each token quoted")
                conn.close()
                return search(tokenized, scope=scope, limit=limit, _fallback_stage=1, _raw=_raw)
            _fallback_stage = 1  # single token: go straight to the phrase form
        if syntax and _fallback_stage == 1:
            phrase = '"' + _cjk_split(_affix_split(_emoji_split(_fold_marks(_recompose(unicodedata.normalize("NFKC", _raw)))))).replace('"', '""') + '"'
            log.info("FTS query still not valid FTS5 syntax; retrying as one literal phrase")
            conn.close()
            return search(phrase, scope=scope, limit=limit, _fallback_stage=2, _raw=_raw)
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
            return search(query, scope=scope, limit=limit, _healed=True, _raw=_raw)
        log.error("FTS search failed on a corrupt index and self-heal did not recover: %s", exc)
        return []
    finally:
        conn.close()


if __name__ == "__main__":
    n = rebuild(force=True)
    print(f"Indexed {n} files into {FTS_DB}")
