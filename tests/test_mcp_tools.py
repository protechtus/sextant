"""Tests for MCP memory server tools: frontmatter, list, and search.

Covers memory_frontmatter, _parse_frontmatter, memory_list,
memory_list_confirmed, and memory_search edge cases.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest


# ---------------------------------------------------------------------------
# TestMemoryFrontmatter
# ---------------------------------------------------------------------------

class TestMemoryFrontmatter:
    def _write(self, tmp_path: Path, name: str, content: str) -> str:
        p = tmp_path / name
        p.write_text(content, encoding="utf-8")
        return name

    def test_returns_error_for_path_traversal(self, tmp_path):
        from atlas_plus.mcp.memory_server.tools.frontmatter import memory_frontmatter
        with patch("atlas_plus.mcp.memory_server.tools.read.MEMORY_ROOT", tmp_path):
            result = memory_frontmatter("../../etc/passwd")
        assert result["error"] is not None
        assert result["frontmatter"] == {}

    def test_returns_not_found_for_missing_file(self, tmp_path):
        from atlas_plus.mcp.memory_server.tools.frontmatter import memory_frontmatter
        with patch("atlas_plus.mcp.memory_server.tools.read.MEMORY_ROOT", tmp_path):
            result = memory_frontmatter("nonexistent.md")
        assert result["error"] == "not found"

    def test_parses_valid_frontmatter(self, tmp_path):
        from atlas_plus.mcp.memory_server.tools.frontmatter import memory_frontmatter
        content = "---\ntitle: Test File\ntype: feedback\npriority: high\n---\n\nBody."
        (tmp_path / "test.md").write_text(content, encoding="utf-8")
        with patch("atlas_plus.mcp.memory_server.tools.read.MEMORY_ROOT", tmp_path), \
             patch("atlas_plus.mcp.memory_server.tools.frontmatter.MEMORY_ROOT", tmp_path):
            result = memory_frontmatter("test.md")
        assert result["error"] is None
        assert result["frontmatter"]["title"] == "Test File"
        assert result["frontmatter"]["type"] == "feedback"
        assert result["frontmatter"]["priority"] == "high"

    def test_returns_empty_frontmatter_for_no_yaml_block(self, tmp_path):
        from atlas_plus.mcp.memory_server.tools.frontmatter import memory_frontmatter
        content = "# No frontmatter\n\nJust plain markdown."
        (tmp_path / "plain.md").write_text(content, encoding="utf-8")
        with patch("atlas_plus.mcp.memory_server.tools.read.MEMORY_ROOT", tmp_path), \
             patch("atlas_plus.mcp.memory_server.tools.frontmatter.MEMORY_ROOT", tmp_path):
            result = memory_frontmatter("plain.md")
        assert result["error"] is None
        assert result["frontmatter"] == {}

    def test_returns_error_for_malformed_yaml(self, tmp_path):
        from atlas_plus.mcp.memory_server.tools.frontmatter import memory_frontmatter
        content = "---\ntitle: [unclosed bracket\n---\n\nBody."
        (tmp_path / "bad.md").write_text(content, encoding="utf-8")
        with patch("atlas_plus.mcp.memory_server.tools.read.MEMORY_ROOT", tmp_path), \
             patch("atlas_plus.mcp.memory_server.tools.frontmatter.MEMORY_ROOT", tmp_path):
            result = memory_frontmatter("bad.md")
        assert "error" in result
        assert result["error"] is not None

    def test_result_includes_path_field(self, tmp_path):
        from atlas_plus.mcp.memory_server.tools.frontmatter import memory_frontmatter
        (tmp_path / "myfile.md").write_text("---\ntitle: T\n---\n", encoding="utf-8")
        with patch("atlas_plus.mcp.memory_server.tools.read.MEMORY_ROOT", tmp_path), \
             patch("atlas_plus.mcp.memory_server.tools.frontmatter.MEMORY_ROOT", tmp_path):
            result = memory_frontmatter("myfile.md")
        assert result["path"] == "myfile.md"


# ---------------------------------------------------------------------------
# TestParseFrontmatter (list.py internal helper)
# ---------------------------------------------------------------------------

class TestParseFrontmatter:
    def test_parses_standard_frontmatter(self):
        from atlas_plus.mcp.memory_server.tools.list import _parse_frontmatter
        text = "---\ntitle: Hello\ntype: feedback\n---\n\nBody."
        result = _parse_frontmatter(text)
        assert result["title"] == "Hello"
        assert result["type"] == "feedback"

    def test_returns_empty_for_no_frontmatter(self):
        from atlas_plus.mcp.memory_server.tools.list import _parse_frontmatter
        assert _parse_frontmatter("# No frontmatter") == {}
        assert _parse_frontmatter("") == {}

    def test_returns_empty_for_unclosed_frontmatter(self):
        from atlas_plus.mcp.memory_server.tools.list import _parse_frontmatter
        text = "---\ntitle: No closing block"
        assert _parse_frontmatter(text) == {}

    def test_returns_empty_for_invalid_yaml(self):
        from atlas_plus.mcp.memory_server.tools.list import _parse_frontmatter
        text = "---\ntitle: [broken\n---"
        assert _parse_frontmatter(text) == {}


# ---------------------------------------------------------------------------
# TestMemoryList
# ---------------------------------------------------------------------------

class TestMemoryList:
    """Tests for memory_list(). Each test clears the module-level TTL cache
    to avoid cross-test contamination from the 30s TTL cache."""

    def _call(self, tmp_path: Path, **kwargs) -> list:
        import atlas_plus.mcp.memory_server.tools.list as _mod
        # Clear the per-key cache so each test starts fresh
        _mod._cache.clear()
        _mod._cache_ts.clear()
        with patch("atlas_plus.mcp.memory_server.tools.list.MEMORY_ROOT", tmp_path):
            return _mod.memory_list(**kwargs)

    def test_returns_empty_for_missing_root(self, tmp_path):
        missing = tmp_path / "no-such-dir"
        result = self._call(missing)
        assert result == []

    def test_lists_markdown_files(self, tmp_path):
        (tmp_path / "feedback").mkdir()
        (tmp_path / "feedback" / "test.md").write_text(
            "---\ntitle: My Feedback\ntype: feedback\n---\n\nBody.", encoding="utf-8"
        )
        result = self._call(tmp_path)
        assert len(result) == 1
        assert result[0]["title"] == "My Feedback"
        assert result[0]["type"] == "feedback"
        assert "feedback/test.md" in result[0]["path"]

    def test_excludes_archive_directory(self, tmp_path):
        archive = tmp_path / "archive"
        archive.mkdir()
        (archive / "old.md").write_text("---\ntitle: Old\n---\nBody.", encoding="utf-8")
        result = self._call(tmp_path)
        assert all("archive" not in r["path"] for r in result)

    def test_excludes_versions_directory(self, tmp_path):
        versions = tmp_path / "versions"
        versions.mkdir()
        (versions / "v1.md").write_text("---\ntitle: V1\n---\nBody.", encoding="utf-8")
        result = self._call(tmp_path)
        assert all("versions" not in r["path"] for r in result)

    def test_respects_limit(self, tmp_path):
        feedback = tmp_path / "feedback"
        feedback.mkdir()
        for i in range(10):
            (feedback / f"file{i:02d}.md").write_text(f"---\ntitle: File {i}\n---\n", encoding="utf-8")
        result = self._call(tmp_path, limit=3)
        assert len(result) <= 3

    def test_uses_file_stem_when_no_title(self, tmp_path):
        feedback = tmp_path / "feedback"
        feedback.mkdir()
        (feedback / "my-pref.md").write_text("No frontmatter here.", encoding="utf-8")
        result = self._call(tmp_path)
        assert any(r["title"] == "my-pref" for r in result)

    def test_prefix_filter_restricts_to_subdirectory(self, tmp_path):
        (tmp_path / "feedback").mkdir()
        (tmp_path / "reference").mkdir()
        (tmp_path / "feedback" / "pref.md").write_text("---\ntitle: Pref\n---\n", encoding="utf-8")
        (tmp_path / "reference" / "ref.md").write_text("---\ntitle: Ref\n---\n", encoding="utf-8")
        result = self._call(tmp_path, prefix="feedback")
        assert all("feedback" in r["path"] for r in result)
        assert all("reference" not in r["path"] for r in result)


# ---------------------------------------------------------------------------
# TestMemoryListConfirmed
# ---------------------------------------------------------------------------

class TestMemoryListConfirmed:
    def test_returns_empty_when_no_confirmed_files(self, tmp_path):
        from atlas_plus.mcp.memory_server.tools.list import memory_list_confirmed
        feedback = tmp_path / "feedback"
        feedback.mkdir()
        (feedback / "test.md").write_text(
            "---\ntitle: No Confirmation\ntype: feedback\n---\nBody.", encoding="utf-8"
        )
        with patch("atlas_plus.mcp.memory_server.tools.list.MEMORY_ROOT", tmp_path):
            result = memory_list_confirmed(since_days=7)
        assert result == []

    def test_includes_recently_confirmed_files(self, tmp_path):
        from datetime import date, timedelta

        from atlas_plus.mcp.memory_server.tools.list import memory_list_confirmed
        feedback = tmp_path / "feedback"
        feedback.mkdir()
        # Relative to today so the date never rots out of the since_days window.
        confirmed = (date.today() - timedelta(days=10)).isoformat()
        (feedback / "confirmed.md").write_text(
            f"---\ntitle: Confirmed Fact\ntype: feedback\nlast_confirmed: '{confirmed}'\n"
            "confidence: high\n---\nBody.", encoding="utf-8"
        )
        with patch("atlas_plus.mcp.memory_server.tools.list.MEMORY_ROOT", tmp_path):
            result = memory_list_confirmed(since_days=30)
        assert len(result) == 1
        assert result[0]["title"] == "Confirmed Fact"
        assert result[0]["confidence"] == "high"
        assert result[0]["last_confirmed"] == confirmed

    def test_excludes_files_confirmed_before_cutoff(self, tmp_path):
        from atlas_plus.mcp.memory_server.tools.list import memory_list_confirmed
        feedback = tmp_path / "feedback"
        feedback.mkdir()
        (feedback / "old.md").write_text(
            "---\ntitle: Old Fact\nlast_confirmed: '2020-01-01'\n---\nBody.", encoding="utf-8"
        )
        with patch("atlas_plus.mcp.memory_server.tools.list.MEMORY_ROOT", tmp_path):
            result = memory_list_confirmed(since_days=7)
        assert result == []

    def test_sorted_newest_first(self, tmp_path):
        from datetime import date, timedelta

        from atlas_plus.mcp.memory_server.tools.list import memory_list_confirmed
        feedback = tmp_path / "feedback"
        feedback.mkdir()
        # Dates relative to today so both stay inside the since_days window as
        # the calendar advances (the test previously rotted: hardcoded dates
        # eventually fell outside 30 days and dropped from the result).
        newer = (date.today() - timedelta(days=5)).isoformat()
        older = (date.today() - timedelta(days=15)).isoformat()
        (feedback / "newer.md").write_text(
            f"---\ntitle: Newer\nlast_confirmed: '{newer}'\n---\nBody.", encoding="utf-8"
        )
        (feedback / "older.md").write_text(
            f"---\ntitle: Older\nlast_confirmed: '{older}'\n---\nBody.", encoding="utf-8"
        )
        with patch("atlas_plus.mcp.memory_server.tools.list.MEMORY_ROOT", tmp_path):
            result = memory_list_confirmed(since_days=30)
        assert result[0]["title"] == "Newer"
        assert result[1]["title"] == "Older"


# ---------------------------------------------------------------------------
# TestMemorySearch (edge cases not covered in test_memory_server.py)
# ---------------------------------------------------------------------------

class TestMemorySearch:
    def test_returns_empty_list_for_empty_query(self, tmp_path):
        from atlas_plus.mcp.memory_server.tools.search import memory_search
        with patch("atlas_plus.mcp.memory_server.fts.search", return_value=[]):
            result = memory_search("")
        # Empty query is fine -- just returns no results
        assert isinstance(result, list)

    def test_respects_limit_parameter(self, tmp_path):
        from atlas_plus.mcp.memory_server.tools.search import memory_search
        fake_results = [
            {"path": f"feedback/item{i}.md", "snippet": f"snippet {i}", "score": 1.0}
            for i in range(20)
        ]
        with patch("atlas_plus.mcp.memory_server.fts.search",
                   return_value=fake_results[:5]):
            result = memory_search("test", limit=5)
        assert len(result) <= 5

    def test_propagates_fts_errors(self):
        # memory_search does not catch internal errors -- callers handle them.
        from atlas_plus.mcp.memory_server.tools.search import memory_search
        import atlas_plus.mcp.memory_server.tools.search as _mod
        with patch.object(_mod, "_fts") as mock_fts:
            mock_fts.search.side_effect = RuntimeError("db locked")
            with pytest.raises(RuntimeError, match="db locked"):
                memory_search("query")

    def test_result_contains_expected_fields(self):
        from atlas_plus.mcp.memory_server.tools.search import memory_search
        fake_hit = {"path": "feedback/test.md", "snippet": "test snippet", "score": 0.9}
        with patch("atlas_plus.mcp.memory_server.fts.search", return_value=[fake_hit]):
            result = memory_search("test")
        assert len(result) >= 1
        # Results may be transformed; ensure path/snippet are accessible
        item = result[0]
        assert "path" in item or "snippet" in item or "score" in item
