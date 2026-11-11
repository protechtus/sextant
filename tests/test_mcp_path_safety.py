"""Path-traversal regression tests for MCP filesystem tools (audit 2026-06-04 HIGH-2).

Covers the shared confiner and each previously-exploitable tool: memory_read(.git/config),
memory_list(prefix=".."), project_state(".."), tickets_list(project="../sessions").
"""
from __future__ import annotations

import pytest

from atlas_plus.mcp.memory_server.config import safe_resolve_within


class TestSafeResolveWithin:
    def test_accepts_simple_and_nested(self, tmp_path):
        (tmp_path / "a.md").write_text("x")
        (tmp_path / "sub").mkdir()
        assert safe_resolve_within(tmp_path, "a.md") == (tmp_path / "a.md").resolve()
        assert safe_resolve_within(tmp_path, "sub/b.md") == (tmp_path / "sub" / "b.md").resolve()

    def test_empty_resolves_to_root(self, tmp_path):
        assert safe_resolve_within(tmp_path, "") == tmp_path.resolve()

    def test_rejects_parent_traversal(self, tmp_path):
        assert safe_resolve_within(tmp_path, "..") is None
        assert safe_resolve_within(tmp_path, "../secret.md") is None
        assert safe_resolve_within(tmp_path, "a/../../b") is None

    def test_rejects_absolute_and_drive(self, tmp_path):
        assert safe_resolve_within(tmp_path, "/etc/passwd") is None
        assert safe_resolve_within(tmp_path, "\\Windows\\x") is None
        assert safe_resolve_within(tmp_path, "C:\\Windows\\x") is None

    def test_rejects_dot_dirs_and_dotfiles(self, tmp_path):
        assert safe_resolve_within(tmp_path, ".git/config") is None
        assert safe_resolve_within(tmp_path, ".env") is None
        assert safe_resolve_within(tmp_path, "sub/.hidden") is None

    def test_rejects_url_encoded_traversal(self, tmp_path):
        assert safe_resolve_within(tmp_path, "%2e%2e/secret") is None

    def test_rejects_null_byte(self, tmp_path):
        assert safe_resolve_within(tmp_path, "a\x00.md") is None

    def test_excluded_dirs(self, tmp_path):
        assert safe_resolve_within(tmp_path, "archive/x.md") is None
        assert safe_resolve_within(tmp_path, "versions/x.md") is None
        # exclude=set() lets a non-dot dir through (used by project_state)
        assert safe_resolve_within(tmp_path, "archive/x.md", exclude=set()) is not None


class TestMemoryReadTraversal:
    def test_git_config_blocked(self, tmp_path, monkeypatch):
        import atlas_plus.mcp.memory_server.tools.read as read_mod
        (tmp_path / ".git").mkdir()
        (tmp_path / ".git" / "config").write_text("[core]\n\tsecret = yes\n")
        (tmp_path / "real.md").write_text("# real\nbody")
        monkeypatch.setattr(read_mod, "MEMORY_ROOT", tmp_path)
        assert read_mod.memory_read(".git/config")["error"] == "invalid path"
        assert read_mod.memory_read("..")["error"] == "invalid path"
        ok = read_mod.memory_read("real.md")
        assert ok["error"] is None and "body" in ok["body"]


class TestMemoryListTraversal:
    def test_prefix_parent_returns_empty(self, tmp_path, monkeypatch):
        import atlas_plus.mcp.memory_server.tools.list as list_mod
        (tmp_path / "ok.md").write_text("---\ntitle: ok\n---\n")
        monkeypatch.setattr(list_mod, "MEMORY_ROOT", tmp_path)
        list_mod._cache.clear(); list_mod._cache_ts.clear()
        assert list_mod.memory_list(prefix="..") == []
        assert any(r["path"] == "ok.md" for r in list_mod.memory_list())


class TestProjectStateTraversal:
    def test_parent_rejected(self, tmp_path, monkeypatch):
        import atlas_plus.mcp.memory_server.tools.project_state as ps
        (tmp_path / "myproj").mkdir()
        monkeypatch.setattr(ps, "_PROJECTS_ROOT", tmp_path)
        ps._CACHE.clear()
        assert "error" in ps.project_state("..")
        assert "error" in ps.project_state("../../Windows")


class TestListTicketsValidation:
    def test_traversal_project_returns_empty(self, tmp_path, monkeypatch):
        from atlas_plus.tickets import store
        assert store.list_tickets(project="../sessions") == []
        assert store.list_tickets(project="..") == []
