"""A question typed the way a person asks it must find the note that answers it.

Found by the LongMemEval run on 2026-11-20: search required every word of the query, so a full
sentence came back empty for 406 of 470 answerable questions. The fix ranks by the words shared
when no note holds them all, flags those matches as loose and keeps them in the low band.

Tightened after a code review on 2026-11-23: the fallback is opt-in (`loose=True`, used by the
search tool and the CLI), and how confident a partial match looks depends on how much of the
question it holds, not on being the best of a bad lot.
"""
from __future__ import annotations

import importlib

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
        "contacts": "My name is on the lease and what is due each month is paid from the joint account.",
    }
    for name, body in notes.items():
        (root / "sessions" / f"{name}.md").write_text(_note(name, body), encoding="utf-8")
    monkeypatch.setenv("ATLAS_MEMORY_ROOT", str(root))
    monkeypatch.setenv("ATLAS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("ATLAS_SEMANTIC_SEARCH", "off")
    from atlas_plus.mcp.memory_server import fts
    config = importlib.import_module("atlas_plus.mcp.memory_server.config")
    monkeypatch.setattr(config, "MEMORY_ROOT", root)
    monkeypatch.setattr(fts, "MEMORY_ROOT", root)
    monkeypatch.setattr(fts, "FTS_DB", tmp_path / "state" / "fts.sqlite")
    (tmp_path / "state").mkdir(exist_ok=True)
    fts.rebuild(force=True)
    return root


def test_a_question_as_asked_finds_the_answering_note(corpus):
    from atlas_plus.mcp.memory_server import fts
    hits = fts.search("What was the first issue I had with my new car after its first service?", loose=True)
    assert hits, "a natural question returned nothing"
    assert hits[0]["path"] == "sessions/car.md"


def test_the_fallback_is_opt_in(corpus):
    """Duplicate checks, session priming and bots call search without `loose`: they get strict
    matching and never a partial match dressed as a finding."""
    from atlas_plus.mcp.memory_server import fts
    question = "What was the first issue I had with my new car after its first service?"
    assert fts.search(question) == []
    hits = fts.search("flights Lisbon June car")
    assert [h["path"] for h in hits] == ["sessions/trip.md"]          # no top-up either


def test_a_partial_match_holding_enough_of_the_question_is_low_and_flagged(corpus):
    from atlas_plus.mcp.memory_server import fts
    from atlas_plus.mcp.memory_server.tools.search import confidence_band
    hits = fts.search("where did the tomato seedlings go beside the basil?", loose=True)
    assert hits and hits[0]["path"] == "sessions/garden.md"
    assert all(h.get("loose_match") for h in hits)
    assert hits[0]["coverage"] >= 0.4
    assert max(h["score"] for h in hits) <= 0.35
    assert confidence_band(hits) == "low"


def test_one_shared_word_is_not_an_answer(corpus):
    """ "what is my dentist's name": the corpus has "name", "what", "is" and "my", and nothing
    about a dentist. The tool must say it does not know (starved), not hedge from the lease note."""
    from atlas_plus.mcp.memory_server import fts
    from atlas_plus.mcp.memory_server.tools.search import confidence_band
    hits = fts.search("what is my dentist's name", loose=True)
    assert confidence_band(hits) == "starved"
    assert all(h.get("loose_match") for h in hits)


def test_a_note_sharing_only_stopwords_is_not_returned(corpus):
    from atlas_plus.mcp.memory_server import fts
    hits = fts.search("which vegetables did I plant beside the herbs in spring?", loose=True)
    assert hits == [], "notes that share only 'the', 'I', 'in' are not matches"
    assert fts.search("what is it that you are", loose=True) == []      # nothing but stopwords


def test_a_full_match_stays_on_top_and_is_not_flagged(corpus):
    from atlas_plus.mcp.memory_server import fts
    hits = fts.search("flights Lisbon June car", loose=True)
    assert hits[0]["path"] == "sessions/trip.md" and not hits[0].get("loose_match")
    rest = hits[1:]
    assert rest, "a four-word query with one full match should be topped up"
    assert all(h.get("loose_match") and h["score"] < hits[0]["score"] for h in rest)


def test_short_query_with_a_full_match_is_not_topped_up(corpus):
    from atlas_plus.mcp.memory_server import fts
    hits = fts.search("sourdough rise", loose=True)
    assert [h["path"] for h in hits] == ["sessions/recipe.md"]


def test_words_found_nowhere_still_return_nothing(corpus):
    from atlas_plus.mcp.memory_server import fts
    assert fts.search("zeppelin quasar xylophone", loose=True) == []


def test_fts_syntax_keeps_its_meaning(corpus):
    from atlas_plus.mcp.memory_server import fts
    assert fts.search('"car GPS seedlings"', loose=True) == []          # a quoted phrase is exact
    assert fts.search("car AND seedlings", loose=True) == []            # an explicit AND is an AND
    assert fts._any_word_fallback("car AND seedlings") == ""
    assert fts._any_word_fallback("gps") == ""              # one word has nothing to loosen


def test_a_search_answered_only_by_partial_matches_is_logged_as_a_miss(corpus, tmp_path, monkeypatch):
    """The gap reports are built from the search log. A multi-word miss must stay a miss there."""
    import json
    server = importlib.import_module("atlas_plus.mcp.memory_server.server")
    search_log = importlib.import_module("atlas_plus.mcp.memory_server.tools.search_log")
    log_file = tmp_path / "logs" / "search.ndjson"
    monkeypatch.setattr(search_log, "LOG_FILE", log_file)
    out = server.memory_search_tool("where did the tomato seedlings go beside the basil?")
    assert out["results"] and out["confidence"] == "low"                 # the agent still gets the partial matches
    record = json.loads(log_file.read_text(encoding="utf-8").splitlines()[-1])
    assert record["miss"] is True and record["result_count"] == 0
    server.memory_search_tool("sourdough rise")
    record = json.loads(log_file.read_text(encoding="utf-8").splitlines()[-1])
    assert record["miss"] is False and record["result_count"] == 1
