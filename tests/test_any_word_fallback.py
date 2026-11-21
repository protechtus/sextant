"""A question typed the way a person asks it must find the note that answers it.

Found by the LongMemEval run on 2026-11-20: search required every word of the query, so a full
sentence came back empty for 406 of 470 answerable questions. The fix ranks by the words shared
when no note holds them all, flags those matches as loose and keeps them in the low band.
"""
from __future__ import annotations

import pytest


def _note(title: str, body: str) -> str:
    return "\n".join(["---", f"title: {title}", "type: session", "---", "", body, ""])


@pytest.fixture
def corpus(tmp_path, monkeypatch):
    root = tmp_path / "memory"
    (root / "sessions").mkdir(parents=True)
    notes = {
        "car": "I got my new car serviced on March 15th. Afterwards the GPS system stopped working correctly.",
        "garden": "The tomato seedlings went into the raised bed this weekend, next to the basil.",
        "trip": "We booked the flights to Lisbon for the first week of June and a car for the coast.",
        "recipe": "The sourdough needs a longer first rise when the kitchen is cold.",
    }
    for name, body in notes.items():
        (root / "sessions" / f"{name}.md").write_text(_note(name, body), encoding="utf-8")
    monkeypatch.setenv("ATLAS_MEMORY_ROOT", str(root))
    monkeypatch.setenv("ATLAS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("ATLAS_SEMANTIC_SEARCH", "off")
    from atlas_plus.mcp.memory_server import fts
    monkeypatch.setattr(fts, "MEMORY_ROOT", root)
    monkeypatch.setattr(fts, "FTS_DB", tmp_path / "state" / "fts.sqlite")
    (tmp_path / "state").mkdir(exist_ok=True)
    fts.rebuild(force=True)
    return root


def test_a_question_as_asked_finds_the_answering_note(corpus):
    from atlas_plus.mcp.memory_server import fts
    hits = fts.search("What was the first issue I had with my new car after its first service?")
    assert hits, "a natural question returned nothing"
    assert hits[0]["path"] == "sessions/car.md"


def test_loose_matches_are_flagged_and_stay_in_the_low_band(corpus):
    from atlas_plus.mcp.memory_server import fts
    hits = fts.search("which vegetables did I plant beside the herbs in spring?")
    assert hits and all(h.get("loose_match") for h in hits)
    assert max(h["score"] for h in hits) <= 0.35


def test_a_full_match_stays_on_top_and_is_not_flagged(corpus):
    from atlas_plus.mcp.memory_server import fts
    hits = fts.search("flights Lisbon June car")
    assert hits[0]["path"] == "sessions/trip.md" and not hits[0].get("loose_match")
    rest = hits[1:]
    assert rest, "a four-word query with one full match should be topped up"
    assert all(h.get("loose_match") and h["score"] < hits[0]["score"] for h in rest)


def test_short_query_with_a_full_match_is_not_topped_up(corpus):
    from atlas_plus.mcp.memory_server import fts
    hits = fts.search("sourdough rise")
    assert [h["path"] for h in hits] == ["sessions/recipe.md"]


def test_words_found_nowhere_still_return_nothing(corpus):
    from atlas_plus.mcp.memory_server import fts
    assert fts.search("zeppelin quasar xylophone") == []


def test_fts_syntax_keeps_its_meaning(corpus):
    from atlas_plus.mcp.memory_server import fts
    assert fts.search('"car GPS seedlings"') == []          # a quoted phrase is exact
    assert fts.search("car AND seedlings") == []            # an explicit AND is an AND
    assert fts._any_word_fallback("car AND seedlings") == ""
    assert fts._any_word_fallback("gps") == ""              # one word has nothing to loosen


def test_search_tool_reports_low_confidence_for_a_loose_answer(corpus):
    from atlas_plus.mcp.memory_server.tools.search import confidence_band
    from atlas_plus.mcp.memory_server import fts
    hits = fts.search("which vegetables did I plant beside the herbs in spring?")
    assert confidence_band(hits) == "low"
