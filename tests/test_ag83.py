"""AG83 (sweep 22 agentic wave): search across languages.

1. Arabic and Hebrew glue the article, conjunctions and one-letter prepositions to the word: `الخادم` never met
   `خادم`, `השרת` never met `שרת`, `ובבית` was invisible to `בית`. Short words (`الله`, `بيت`, `הולך`) stay whole;
   a bare Hebrew מ (the participle prefix far more often than `from`) is not split: the rules were measured on the
   hermitdave FrequencyWords 2018 50k lists (top 20k words per language, hand-judged samples in the lane report).
2. A Persian plural written without its ZWNJ (`کتابها`) met neither `کتاب` nor `کتاب‌ها`.
3. Emoji were separators to the tokenizer: no emoji query found anything.
4. Snippets showed the folded index text (`Strasse`, `iSTANBUL`, punctuation dropped), on both snippet paths.
5. The FTS5-syntax fallback (`config.toml` in the query) used the raw query form: `Straße` missed.
Every test runs a real index over a temporary corpus.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from atlas_plus.mcp.memory_server import fts


@pytest.fixture
def corpus(tmp_path):
    mem = tmp_path / "mem"
    db = tmp_path / "state" / "search.sqlite"

    def write(name: str, body: str) -> None:
        p = mem / "reference" / f"{name}.md"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f"---\ntitle: {name}\n---\n{body}\n", encoding="utf-8")

    with patch.object(fts, "MEMORY_ROOT", mem), patch.object(fts, "FTS_DB", db):
        def search(q: str) -> dict:
            return {Path(r["path"]).stem: r["snippet"] for r in fts.search(q)}
        yield write, search


def test_arabic_article_and_prepositions_meet_the_bare_word(corpus):
    write, search = corpus
    write("ar1", "تم تحديث الخادم اليوم")
    write("ar2", "خادم جديد في المكتب")
    write("ar3", "ذهبنا للمدرسة وبالسيارة")
    write("ar4", "الله كريم والبيت قريب")
    assert set(search("خادم")) == {"ar1", "ar2"}
    assert set(search("الخادم")) == {"ar1", "ar2"}
    assert set(search("مدرسة")) == {"ar3"} and set(search("سيارة")) == {"ar3"}
    assert "[الخادم]" in search("خادم")["ar1"]          # the snippet brackets the whole word as written
    # short words stay whole: `الله` is not `ال` + `له`, `بيت` is not `ب` + `يت`
    assert search("له") == {} and search("يت") == {}
    assert set(search("بيت")) == {"ar4"}


def test_a_quoted_arabic_phrase_matches_the_written_form(corpus):
    write, search = corpus
    write("ar5", "الخادم الجديد يعمل")
    assert set(search('"الخادم الجديد"')) == {"ar5"}
    assert set(search("خادم جديد")) == {"ar5"}


def test_hebrew_prefixes_meet_the_bare_word(corpus):
    write, search = corpus
    write("he1", "השרת נפל בלילה")
    write("he2", "שרת חדש הגיע")
    write("he3", "ובבית יש מחשב")
    write("he4", "הוא הולך הביתה מהעבודה")
    write("he5", "אני מרגיש טוב")
    assert set(search("שרת")) == {"he1", "he2"} and set(search("השרת")) == {"he1", "he2"}
    assert set(search("בית")) == {"he3"}
    assert set(search("עבודה")) == {"he4"}               # מה + stem
    # words that merely start with a prefix letter stay whole
    assert search("ולך") == {}                           # הולך is not ה + ולך
    assert search("רגיש") == {}                          # מרגיש (feels) is not מ + רגיש (sensitive)


def test_persian_plural_without_zwnj_meets_both_spellings(corpus):
    write, search = corpus
    write("fa1", "کتابها روی میز است")
    write("fa2", "کتاب‌ها روی میز است")
    write("fa3", "من تنها هستم")
    assert {"fa1", "fa2"} <= set(search("کتاب"))
    assert set(search("کتابها")) == {"fa1", "fa2"}
    assert set(search("کتاب‌ها")) == {"fa1", "fa2"}
    assert search("تن") == {}                            # تنها (alone) is not تن + ها


def test_emoji_are_searchable_and_never_fuse_with_words(corpus):
    write, search = corpus
    write("em1", "launch day 🚀 went well")
    write("em2", "rocket🚀fuel")
    write("em3", "family 👨‍👩‍👧 photo")
    write("em4", "trip to 🇯🇵🇫🇷 soon")
    write("em5", "I ❤️ this, a→b")
    assert set(search("🚀")) == {"em1", "em2"}
    assert set(search("rocket")) == {"em2"} and set(search("fuel")) == {"em2"}
    assert set(search("👨‍👩‍👧")) == {"em3"}
    assert set(search("🇯🇵")) == {"em4"} and set(search("🇫🇷")) == {"em4"}
    assert set(search("❤")) == {"em5"} and set(search("❤️")) == {"em5"}
    assert set(search("b")) == {"em5"}                   # an arrow is a token of its own, not glue
    assert "[👨‍👩‍👧]" in search("👨‍👩‍👧")["em3"]   # the ZWJ sequence shown intact


def test_snippets_show_the_notes_own_letters(corpus):
    write, search = corpus
    write("de1", "Die Straße ist lang, wirklich.")
    write("tr1", "İSTANBUL ve Diyarbakır")
    assert search("strasse")["de1"].startswith("Die [Straße] ist lang, wirklich.")
    assert search("istanbul")["tr1"].startswith("[İSTANBUL] ve Diyarbakır")
    assert "[Diyarbakır]" in search("diyarbakir")["tr1"]


def test_the_large_note_snippet_path_shows_the_notes_own_letters(corpus):
    write, search = corpus
    write("big", "filler " * 9000 + "Die Straße endet hier.")
    snip = search("strasse")["big"]
    assert "[Straße]" in snip and "Strasse" not in snip


def test_the_syntax_fallback_folds_like_the_main_query(corpus):
    write, search = corpus
    write("cfg", "Die Straße und config.toml liegen hier")
    assert set(search("Straße config.toml")) == {"cfg"}


def test_tag_flags_skin_tones_and_keycaps_are_one_cluster_each(corpus):
    write, search = corpus
    scotland = "\U0001F3F4\U000E0067\U000E0062\U000E0073\U000E0063\U000E0074\U000E007F"
    write("em6", f"pride \U0001F3F3\ufe0f\u200d\U0001F308 flag, keycap 1\ufe0f\u20e3 and {scotland} scotland; \U0001F44D\U0001F3FD ok")
    snip = search(scotland)["em6"]
    assert f"[{scotland}]" in snip and "]]" not in snip and "[[" not in snip
    assert f"[\U0001F3F3\ufe0f\u200d\U0001F308]" in search("\U0001F3F3\ufe0f\u200d\U0001F308")["em6"]
    assert "[1\ufe0f\u20e3]" in search("1\ufe0f\u20e3")["em6"]
    assert "[\U0001F44D\U0001F3FD]" in search("\U0001F44D\U0001F3FD")["em6"]
    assert "[\U0001F44D\U0001F3FD]" in search("\U0001F44D")["em6"]     # the plain thumb finds the toned one
