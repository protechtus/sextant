"""P1-T12: Smoke tests for the Atlas MCP memory server tools."""

import os
import textwrap
from pathlib import Path

import pytest

# Point tools at a temp .memory/ for all tests
@pytest.fixture(autouse=True)
def temp_memory(tmp_path, monkeypatch):
    mem = tmp_path / ".memory"
    mem.mkdir()

    (mem / "identity").mkdir()
    (mem / "feedback").mkdir()
    (mem / "archive").mkdir()
    (mem / "_sync").mkdir()

    (mem / "identity" / "alex.md").write_text(
        textwrap.dedent("""\
        ---
        id: alex
        title: Alex Example
        type: identity
        priority: critical
        updated: 2026-04-24
        ---
        Alex is a Tier 3 support engineer at ExampleCorp.
        """),
        encoding="utf-8",
    )

    (mem / "feedback" / "archive-not-delete.md").write_text(
        textwrap.dedent("""\
        ---
        id: archive-not-delete
        title: Archive not delete
        type: feedback
        priority: high
        updated: 2026-04-24
        ---
        Never delete files. Move to archive/ with a provenance note.
        """),
        encoding="utf-8",
    )

    # Should be excluded from all results
    (mem / "archive" / "old.md").write_text("old stuff", encoding="utf-8")
    (mem / "_sync" / "sync.py").write_text("# sync", encoding="utf-8")

    state = tmp_path / ".atlas"
    state.mkdir()

    monkeypatch.setenv("ATLAS_MEMORY_ROOT", str(mem))
    monkeypatch.setenv("ATLAS_STATE_DIR", str(state))

    # Force reload of config and fts modules with new env
    import importlib
    import atlas_plus.mcp.memory_server.config as cfg
    cfg.MEMORY_ROOT = mem
    cfg.FTS_DB = state / "state.sqlite"

    import atlas_plus.mcp.memory_server.fts as fts_mod
    fts_mod.MEMORY_ROOT = mem
    fts_mod.FTS_DB = state / "state.sqlite"

    # Each tool module holds its own binding from `from ..config import MEMORY_ROOT`
    # so we must patch each one directly.
    import atlas_plus.mcp.memory_server.tools.list as list_mod
    import atlas_plus.mcp.memory_server.tools.read as read_mod
    import atlas_plus.mcp.memory_server.tools.frontmatter as fm_mod
    import atlas_plus.mcp.memory_server.tools.search as search_mod
    list_mod.MEMORY_ROOT = mem
    read_mod.MEMORY_ROOT = mem
    fm_mod.MEMORY_ROOT = mem
    search_mod.MEMORY_ROOT = mem

    yield mem


# -- list tests --

def test_list_root_returns_only_non_excluded_files():
    from atlas_plus.mcp.memory_server.tools.list import memory_list
    results = memory_list()
    paths = [r["path"] for r in results]
    assert any("identity/alex.md" in p for p in paths)
    assert any("feedback/archive-not-delete.md" in p for p in paths)
    assert not any("archive/" in p for p in paths)
    assert not any("_sync/" in p for p in paths)


def test_list_with_prefix_filters_correctly():
    from atlas_plus.mcp.memory_server.tools.list import memory_list
    results = memory_list(prefix="identity")
    paths = [r["path"] for r in results]
    assert all("identity" in p for p in paths)
    assert not any("feedback" in p for p in paths)


def test_list_returns_frontmatter_metadata():
    from atlas_plus.mcp.memory_server.tools.list import memory_list
    results = memory_list(prefix="identity")
    alex = next((r for r in results if "alex.md" in r["path"]), None)
    assert alex is not None
    assert alex["title"] == "Alex Example"
    assert alex["type"] == "identity"
    assert alex["priority"] == "critical"


# -- read tests --

def test_read_existing_file():
    from atlas_plus.mcp.memory_server.tools.read import memory_read
    result = memory_read("identity/alex.md")
    assert result["error"] is None
    assert "Alex is a Tier 3" in result["body"]
    assert result["frontmatter"]["id"] == "alex"
    assert not result["truncated"]


def test_read_missing_file():
    from atlas_plus.mcp.memory_server.tools.read import memory_read
    result = memory_read("identity/nobody.md")
    assert result["error"] == "not found"
    assert result["body"] == ""


def test_read_traversal_rejected():
    from atlas_plus.mcp.memory_server.tools.read import memory_read
    result = memory_read("../../../etc/passwd")
    assert result["error"] == "invalid path"


# -- search tests --

def test_search_with_hits():
    from atlas_plus.mcp.memory_server.fts import rebuild
    from atlas_plus.mcp.memory_server.tools.search import memory_search
    rebuild(force=True)
    results = memory_search("ExampleCorp")
    assert len(results) > 0
    assert any("identity/alex.md" in r["path"] for r in results)


def test_search_no_hits():
    from atlas_plus.mcp.memory_server.fts import rebuild
    from atlas_plus.mcp.memory_server.tools.search import memory_search
    rebuild(force=True)
    results = memory_search("xyzzy_nonexistent_term_zqq")
    assert results == []


# -- frontmatter test --

def test_frontmatter_returns_dict():
    from atlas_plus.mcp.memory_server.tools.frontmatter import memory_frontmatter
    result = memory_frontmatter("feedback/archive-not-delete.md")
    assert result["error"] is None
    assert result["frontmatter"]["type"] == "feedback"
    assert result["frontmatter"]["priority"] == "high"


# -- excluded dir enforcement in read and frontmatter --

def test_read_rejects_archive_dir(tmp_path, temp_memory):
    from atlas_plus.mcp.memory_server.tools.read import memory_read
    result = memory_read("archive/old.md")
    assert result["error"] == "invalid path"


def test_read_rejects_sync_dir(tmp_path, temp_memory):
    from atlas_plus.mcp.memory_server.tools.read import memory_read
    result = memory_read("_sync/sync.py")
    assert result["error"] == "invalid path"


def test_read_rejects_versions_dir(tmp_path, temp_memory):
    from atlas_plus.mcp.memory_server.tools.read import memory_read
    versions = temp_memory / "versions"
    versions.mkdir()
    (versions / "old.md").write_text("old", encoding="utf-8")
    result = memory_read("versions/old.md")
    assert result["error"] == "invalid path"


def test_frontmatter_rejects_archive_dir(tmp_path, temp_memory):
    from atlas_plus.mcp.memory_server.tools.frontmatter import memory_frontmatter
    result = memory_frontmatter("archive/old.md")
    assert result["error"] == "invalid path"


def test_frontmatter_rejects_sync_dir(tmp_path, temp_memory):
    from atlas_plus.mcp.memory_server.tools.frontmatter import memory_frontmatter
    result = memory_frontmatter("_sync/sync.py")
    assert result["error"] == "invalid path"


# ---------------------------------------------------------------------------
# Temporal context injection (v0.5-E)
# ---------------------------------------------------------------------------

class TestTemporalContextInjection:
    def test_temporal_context_absent_when_graph_empty(self, tmp_path, monkeypatch):
        """When temporal graph root does not exist, temporal_context is not in response."""
        monkeypatch.setenv("ATLAS_TEMPORAL_ROOT", str(tmp_path / "nonexistent"))
        from unittest.mock import patch
        from atlas_plus.mcp.memory_server import server

        with patch.object(server, "_temporal_context_for_query", return_value=[]) as mock_tc, \
             patch("atlas_plus.mcp.memory_server.fts.rebuild"), \
             patch("atlas_plus.mcp.memory_server.fts.search", return_value=[]), \
             patch("atlas_plus.mcp.memory_server.tools.search_log.log_search"):
            result = server.memory_search_tool("anything")

        assert "temporal_context" not in result

    def test_temporal_context_present_when_results_returned(self, tmp_path, monkeypatch):
        """temporal_context key is included when _temporal_context_for_query returns facts."""
        from unittest.mock import patch
        from atlas_plus.mcp.memory_server import server

        fake_temporal = [
            {"fact": "Atlas MCP is live at port 8765.", "valid_from": "2026-05-01",
             "confidence": 0.95, "score": 0.8, "entity": "Atlas MCP"},
        ]
        with patch.object(server, "_temporal_context_for_query", return_value=fake_temporal), \
             patch("atlas_plus.mcp.memory_server.fts.rebuild"), \
             patch("atlas_plus.mcp.memory_server.fts.search", return_value=[]), \
             patch("atlas_plus.mcp.memory_server.tools.search_log.log_search"):
            result = server.memory_search_tool("atlas mcp")

        assert "temporal_context" in result
        assert result["temporal_context"][0]["entity"] == "Atlas MCP"

    def test_temporal_context_for_query_handles_missing_graph(self, tmp_path, monkeypatch):
        """_temporal_context_for_query returns [] when graph directory does not exist."""
        monkeypatch.setenv("ATLAS_TEMPORAL_ROOT", str(tmp_path / "no_graph"))
        from atlas_plus.mcp.memory_server.server import _temporal_context_for_query
        result = _temporal_context_for_query("test query")
        assert result == []

    def test_temporal_context_for_query_handles_exception(self, monkeypatch):
        """_temporal_context_for_query returns [] when provider raises."""
        import os
        monkeypatch.setenv("ATLAS_TEMPORAL_ROOT", r"C:\Windows")  # exists but not a valid graph
        from atlas_plus.mcp.memory_server.server import _temporal_context_for_query
        result = _temporal_context_for_query("test query")
        assert isinstance(result, list)


# ---------------------------------------------------------------------------
# downgrade_stale_confirmations
# ---------------------------------------------------------------------------

class TestDowngradeStaleConfirmations:
    """Tests for routing.downgrade_stale_confirmations()."""

    def _write_md(self, path, frontmatter: str, body: str = "Content here.") -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"---\n{frontmatter}\n---\n\n{body}\n", encoding="utf-8")

    def test_returns_empty_when_no_memory_root(self, tmp_path):
        from atlas_plus.mcp.memory_server.routing import downgrade_stale_confirmations
        result = downgrade_stale_confirmations(
            threshold_days=90,
            memory_root=tmp_path / "nonexistent",
        )
        assert result == []

    def test_dry_run_does_not_modify_files(self, tmp_path):
        from atlas_plus.mcp.memory_server.routing import downgrade_stale_confirmations
        mem = tmp_path / "memory"
        p = mem / "feedback" / "old.md"
        self._write_md(p, "type: feedback\nconfidence: high\nlast_confirmed: 2020-01-01")
        original = p.read_text(encoding="utf-8")
        result = downgrade_stale_confirmations(threshold_days=90, memory_root=mem, dry_run=True)
        assert len(result) == 1
        assert p.read_text(encoding="utf-8") == original  # file unchanged

    def test_write_mode_updates_confidence(self, tmp_path):
        from atlas_plus.mcp.memory_server.routing import downgrade_stale_confirmations
        mem = tmp_path / "memory"
        p = mem / "feedback" / "old.md"
        self._write_md(p, "type: feedback\nconfidence: high\nlast_confirmed: 2020-01-01")
        result = downgrade_stale_confirmations(threshold_days=90, memory_root=mem, dry_run=False)
        assert len(result) == 1
        updated = p.read_text(encoding="utf-8")
        assert "confidence: low" in updated

    def test_skips_files_without_last_confirmed(self, tmp_path):
        from atlas_plus.mcp.memory_server.routing import downgrade_stale_confirmations
        mem = tmp_path / "memory"
        p = mem / "reference" / "unconfirmed.md"
        self._write_md(p, "type: reference\nconfidence: high")
        result = downgrade_stale_confirmations(threshold_days=90, memory_root=mem)
        assert result == []
        assert "confidence: high" in p.read_text(encoding="utf-8")

    def test_skips_already_low_confidence(self, tmp_path):
        from atlas_plus.mcp.memory_server.routing import downgrade_stale_confirmations
        mem = tmp_path / "memory"
        p = mem / "feedback" / "low.md"
        self._write_md(p, "type: feedback\nconfidence: low\nlast_confirmed: 2020-01-01")
        result = downgrade_stale_confirmations(threshold_days=90, memory_root=mem)
        assert result == []

    def test_skips_recently_confirmed_files(self, tmp_path):
        from datetime import date
        from atlas_plus.mcp.memory_server.routing import downgrade_stale_confirmations
        mem = tmp_path / "memory"
        recent = date.today().isoformat()
        p = mem / "feedback" / "recent.md"
        self._write_md(p, f"type: feedback\nconfidence: high\nlast_confirmed: {recent}")
        result = downgrade_stale_confirmations(threshold_days=90, memory_root=mem)
        assert result == []

    def test_returns_metadata_for_each_downgraded_file(self, tmp_path):
        from atlas_plus.mcp.memory_server.routing import downgrade_stale_confirmations
        mem = tmp_path / "memory"
        p = mem / "feedback" / "stale.md"
        self._write_md(p, "type: feedback\nconfidence: medium\nlast_confirmed: 2021-03-15")
        result = downgrade_stale_confirmations(threshold_days=90, memory_root=mem, dry_run=True)
        assert len(result) == 1
        entry = result[0]
        assert "path" in entry
        assert entry["old_confidence"] == "medium"
        assert entry["last_confirmed"] == "2021-03-15"
        assert entry["age_days"] > 0

    def test_skips_archive_dir(self, tmp_path):
        from atlas_plus.mcp.memory_server.routing import downgrade_stale_confirmations
        mem = tmp_path / "memory"
        p = mem / "archive" / "old.md"
        self._write_md(p, "type: reference\nconfidence: high\nlast_confirmed: 2020-01-01")
        result = downgrade_stale_confirmations(threshold_days=90, memory_root=mem)
        assert result == []
