"""Search accepts the write-side scope grammar (deep install lane, 2026-11-09).

`memory_remember_tool` stores under `reference:<topic>` / `project:<name>`; a search scoped
the same way used to match nothing and return an empty result with no hint.
"""
from __future__ import annotations

import pytest


@pytest.fixture
def corpus(tmp_path, monkeypatch):
    root = tmp_path / "memory"
    (root / "reference").mkdir(parents=True)
    (root / "projects").mkdir()
    (root / "feedback").mkdir()
    monkeypatch.setenv("ATLAS_MEMORY_ROOT", str(root))
    monkeypatch.setenv("ATLAS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("ATLAS_SEMANTIC_SEARCH", "off")
    # module-level roots are resolved at import time; redirect them like the other server tests
    import importlib
    config = importlib.import_module("atlas_plus.mcp.memory_server.config")
    routing = importlib.import_module("atlas_plus.mcp.memory_server.routing")
    fts = importlib.import_module("atlas_plus.mcp.memory_server.fts")
    monkeypatch.setattr(config, "MEMORY_ROOT", root)
    monkeypatch.setattr(routing, "MEMORY_ROOT", root)
    monkeypatch.setattr(routing, "ROUTING_CONFIG", root / "_atlas" / "routing.yaml")  # not a routing.yaml another test left behind
    monkeypatch.setattr(fts, "MEMORY_ROOT", root)
    monkeypatch.setattr(fts, "FTS_DB", tmp_path / "state" / "fts.sqlite")
    (tmp_path / "state").mkdir(exist_ok=True)
    return root


def test_plain_prefix_passes_through(corpus):
    from atlas_plus.mcp.memory_server import server
    assert server._normalize_search_scope("feedback") == "feedback"
    assert server._normalize_search_scope("") == ""


def test_grammar_scope_maps_to_the_routed_file(corpus):
    from atlas_plus.mcp.memory_server import server
    assert server._normalize_search_scope("reference:infrastructure") == "reference/infrastructure.md"
    assert server._normalize_search_scope("project:demo") == "projects/demo.md"


def test_unknown_grammar_scope_falls_back_to_unscoped(corpus):
    from atlas_plus.mcp.memory_server import server
    assert server._normalize_search_scope("nonsense:thing") == ""


def test_scoped_search_finds_the_fact_written_under_that_scope(corpus):
    from atlas_plus.mcp.memory_server import server
    w = server.memory_remember_tool("reference:infrastructure", "The staging server is at 10.0.0.5.")
    assert w.get("ok") is True
    from atlas_plus.mcp.memory_server import fts
    fts.rebuild(force=True)   # other tests may leave the module-level index pointing elsewhere
    hit = server.memory_search_tool("staging server", scope="reference:infrastructure")
    assert hit["results"], hit
    assert hit["results"][0]["path"] == "reference/infrastructure.md"
    # a different grammar scope does not see it
    miss = server.memory_search_tool("staging server", scope="project:other")
    assert not miss["results"]
