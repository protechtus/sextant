"""Multi-word queries with punctuated tokens must AND the words, not degrade to a phrase."""
from __future__ import annotations

import pytest


@pytest.fixture
def corpus(tmp_path, monkeypatch):
    root = tmp_path / "memory"
    (root / "projects").mkdir(parents=True)
    note = "\n".join([
        "---", "id: demo", "title: Demo", "type: project", "---", "",
        "The app keeps its config.toml under AppData on Windows.", "",
    ])
    (root / "projects" / "demo.md").write_text(note, encoding="utf-8")
    monkeypatch.setenv("ATLAS_MEMORY_ROOT", str(root))
    monkeypatch.setenv("ATLAS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("ATLAS_SEMANTIC_SEARCH", "off")
    from atlas_plus.mcp.memory_server import fts
    # the module resolves its roots at import time, like the existing FTS tests do
    monkeypatch.setattr(fts, "MEMORY_ROOT", root)
    monkeypatch.setattr(fts, "FTS_DB", tmp_path / "state" / "fts.sqlite")
    (tmp_path / "state").mkdir(exist_ok=True)
    fts.rebuild(force=True)
    return root


def test_dotted_token_query_finds_the_note(corpus):
    from atlas_plus.mcp.memory_server import fts
    hits = fts.search("config.toml AppData")
    assert hits and hits[0]["path"] == "projects/demo.md"


def test_words_in_reverse_order_still_match(corpus):
    from atlas_plus.mcp.memory_server import fts
    assert fts.search("AppData config.toml")


def test_plain_queries_unchanged(corpus):
    from atlas_plus.mcp.memory_server import fts
    assert fts.search("config under AppData")
