"""memory_search_tool(full=N) returns the whole note for the top N results.

Born from the LongMemEval run of 2026-11-20: an agent shown snippets answered from them and
left the notes holding the evidence unopened. One call that finds and reads fixed most of it.
"""
from __future__ import annotations

import importlib

import pytest


def _note(title: str, body: str) -> str:
    return "\n".join(["---", f"title: {title}", "type: session", "---", "", body, ""])


@pytest.fixture
def server(tmp_path, monkeypatch):
    root = tmp_path / "memory"
    (root / "sessions").mkdir(parents=True)
    filler = " ".join(f"filler{i}" for i in range(400))
    notes = {
        "car": f"I got my new car serviced on March 15th. {filler} The invoice came to 412 dollars.",
        "trip": "We booked the flights to Lisbon and hired a car for the coast road.",
        "garden": "The tomato seedlings went into the raised bed next to the basil.",
    }
    for name, body in notes.items():
        (root / "sessions" / f"{name}.md").write_text(_note(name, body), encoding="utf-8")
    (tmp_path / "state").mkdir()
    monkeypatch.setenv("ATLAS_MEMORY_ROOT", str(root))
    monkeypatch.setenv("ATLAS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("ATLAS_SEMANTIC_SEARCH", "off")
    config = importlib.import_module("atlas_plus.mcp.memory_server.config")
    read = importlib.import_module("atlas_plus.mcp.memory_server.tools.read")
    fts = importlib.import_module("atlas_plus.mcp.memory_server.fts")
    monkeypatch.setattr(config, "MEMORY_ROOT", root)
    monkeypatch.setattr(read, "MEMORY_ROOT", root)
    monkeypatch.setattr(fts, "MEMORY_ROOT", root)
    monkeypatch.setattr(fts, "FTS_DB", tmp_path / "state" / "fts.sqlite")
    fts.rebuild(force=True)
    return importlib.import_module("atlas_plus.mcp.memory_server.server")


def test_default_search_returns_snippets_only(server):
    out = server.memory_search_tool("car serviced")
    assert out["results"]
    assert all("body" not in r for r in out["results"])


def test_full_returns_the_detail_a_snippet_leaves_out(server):
    out = server.memory_search_tool("car serviced", full=1)
    top = out["results"][0]
    assert top["path"] == "sessions/car.md"
    assert "412 dollars" not in top["snippet"]          # the snippet alone cannot answer it
    assert "412 dollars" in top["body"]
    assert top["body_truncated"] is False
    assert top["frontmatter"]["title"] == "car"           # a date or title question needs the note's header too
    assert all("body" not in r for r in out["results"][1:])


def test_full_covers_only_the_top_n(server):
    out = server.memory_search_tool("car", full=2)
    assert len(out["results"]) >= 2
    assert ["body" in r for r in out["results"]][:3] == [True, True, False][: len(out["results"])]


def test_full_is_capped_in_count_and_length(server, monkeypatch):
    monkeypatch.setattr(server, "_FULL_MAX_CHARS", 200)
    out = server.memory_search_tool("car serviced", full=99)
    top = out["results"][0]
    assert len(top["body"]) == 200 and top["body_truncated"] is True
    assert sum("body" in r for r in out["results"]) <= server._FULL_MAX_RESULTS


@pytest.mark.parametrize("bad", [-3, "many", None])
def test_a_bad_full_value_is_treated_as_zero(server, bad):
    out = server.memory_search_tool("car serviced", full=bad)
    assert out["results"] and all("body" not in r for r in out["results"])
