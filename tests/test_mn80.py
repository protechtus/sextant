"""MN80 (sweep 23 manual wave): a query word whose stem starts with a prefix letter lost it.

`לילה` is `ל`+`ילה` to the prefix split, so the query searched `ילה`, but a note's `בלילה` indexes `ב`+`לילה`:
`לילה` never met `בלילה` or `הלילה`, `בדיקה` never met `הבדיקה`. Arabic had the same hole through the bare و/ب rule:
`برنامج` searched `رنامج` and missed `البرنامج` (indexed `ال`+`برنامج`), `وزارة` missed `الوزارة`. And the other
direction: `בלילה` searched `לילה`, which a bare `לילה` never indexes. An unquoted query word now matches its stem,
the whole word, or (for a bare stem that itself splits) that stem in its index form, on the main query, both syntax fallbacks and the CJK bigram path.
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


def test_a_hebrew_word_that_starts_with_a_prefix_letter_meets_its_prefixed_forms(corpus):
    write, search = corpus
    write("night1", "עבדנו בלילה על השרת")
    write("night2", "הלילה היה שקט")
    write("night3", "לילה טוב לכולם")
    write("check1", "הבדיקה עברה בהצלחה")
    assert set(search("לילה")) == {"night1", "night2", "night3"}
    assert set(search("בלילה")) == {"night1", "night2", "night3"}   # the bare `לילה` too
    assert set(search("בדיקה")) == {"check1"}
    assert "[בלילה]" in search("לילה")["night1"]


def test_an_arabic_word_that_starts_with_a_bare_prefix_letter_meets_the_article_form(corpus):
    write, search = corpus
    write("prog1", "تم تحديث البرنامج اليوم")
    write("prog2", "برنامج جديد")
    write("min1", "قرار الوزارة نهائي")
    assert set(search("برنامج")) == {"prog1", "prog2"}
    assert set(search("البرنامج")) == {"prog1", "prog2"}
    assert set(search("وزارة")) == {"min1"}


def test_ag83_results_are_kept(corpus):
    write, search = corpus
    write("he", "עבדנו על השרת")
    write("ar1", "تم تحديث الخادم اليوم")
    write("ar2", "خادم جديد في المكتب")
    write("ar4", "الله كريم والبيت قريب")
    write("check2", "בדיקה פשוטה")
    write("check1", "הבדיקה עברה")
    assert set(search("שרת")) == {"he"} and set(search("השרת")) == {"he"}
    assert set(search("خادم")) == {"ar1", "ar2"} and set(search("الخادم")) == {"ar1", "ar2"}
    assert search("له") == {} and search("يت") == {}
    assert set(search('"הבדיקה"')) == {"check1"}   # a quoted word keeps its exact form


def test_both_syntax_fallbacks_and_operators(corpus):
    write, search = corpus
    write("night1", "עבדנו בלילה על השרת")
    write("check1", "הבדיקה עברה")
    assert set(search("לילה, שרת")) == {"night1"}        # several tokens: the first fallback
    assert set(search("בדיקה)")) == {"check1"}           # one token with broken syntax
    assert set(search("alpha OR לילה")) == {"night1"}
    assert set(search("לילה NOT שרת")) == set()
    assert set(search("בדיקה NOT שרת")) == {"check1"}
    assert set(search("לילה*")) == {"night1"}
    assert search("") == {}           # the marker letters in a query are inert


def test_the_cjk_bigram_path_keeps_the_alternatives(corpus):
    write, search = corpus
    write("mix", "東京の塔 בלילה")
    hits = fts.search("東京タワー לילה")
    assert [Path(r["path"]).stem for r in hits] == ["mix"] and hits[0].get("loose_match")
