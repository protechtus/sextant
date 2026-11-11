"""Unit tests for mcp/memory_server/tools/anti.py -- anti-memory store.

Covers: anti_remember validation (empty scope/pattern, invalid applies_to,
missing project), autonomous gate, new-file creation, append to existing,
duplicate detection, and list_anti filtering.
"""
from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _patch_anti(anti_root: Path):
    """Patch module-level ANTI_ROOT and bypass the autonomous gate."""
    import atlas_plus.mcp.memory_server.tools.anti as mod
    return [
        patch.object(mod, "ANTI_ROOT", anti_root),
        patch("atlas_plus.mcp.memory_server.tools.anti.anti_remember.__wrapped__"
              if hasattr(getattr(mod, "anti_remember", None), "__wrapped__") else
              "atlas_plus.autonomous.require_autonomous_writes",
              lambda *a, **kw: None),
    ]


# ---------------------------------------------------------------------------
# anti_remember -- validation
# ---------------------------------------------------------------------------

class TestAntiRememberValidation:
    def _call(self, anti_root: Path, **kwargs) -> dict:
        import atlas_plus.mcp.memory_server.tools.anti as mod
        with patch.object(mod, "ANTI_ROOT", anti_root), \
             patch("atlas_plus.autonomous.require_autonomous_writes", lambda *a, **kw: None):
            return mod.anti_remember(**kwargs)

    def test_empty_scope_returns_error(self, tmp_path):
        result = self._call(tmp_path / "anti",
                            scope="", reject_pattern="never use X")
        assert result["ok"] is False
        assert "scope" in result["error"]

    def test_empty_pattern_returns_error(self, tmp_path):
        result = self._call(tmp_path / "anti",
                            scope="libraries", reject_pattern="")
        assert result["ok"] is False
        assert "reject_pattern" in result["error"]

    def test_invalid_applies_to_returns_error(self, tmp_path):
        result = self._call(tmp_path / "anti",
                            scope="libraries", reject_pattern="no mocks",
                            applies_to="team")
        assert result["ok"] is False
        assert "applies_to" in result["error"]

    def test_project_required_when_applies_to_project(self, tmp_path):
        result = self._call(tmp_path / "anti",
                            scope="libraries", reject_pattern="no mocks",
                            applies_to="project", project="")
        assert result["ok"] is False
        assert "project" in result["error"]

    def test_whitespace_scope_rejected(self, tmp_path):
        result = self._call(tmp_path / "anti",
                            scope="   ", reject_pattern="pattern")
        assert result["ok"] is False

    def test_whitespace_pattern_rejected(self, tmp_path):
        result = self._call(tmp_path / "anti",
                            scope="libraries", reject_pattern="   ")
        assert result["ok"] is False


# ---------------------------------------------------------------------------
# anti_remember -- file creation and append
# ---------------------------------------------------------------------------

class TestAntiRememberWrite:
    def _call(self, anti_root: Path, **kwargs) -> dict:
        import atlas_plus.mcp.memory_server.tools.anti as mod
        with patch.object(mod, "ANTI_ROOT", anti_root), \
             patch("atlas_plus.autonomous.require_autonomous_writes", lambda *a, **kw: None):
            return mod.anti_remember(**kwargs)

    def test_creates_new_file(self, tmp_path):
        anti_root = tmp_path / "anti"
        result = self._call(anti_root,
                            scope="libraries", reject_pattern="Never use pandas for this.")
        assert result["ok"] is True
        assert (anti_root / "libraries.md").exists()

    def test_new_file_has_frontmatter(self, tmp_path):
        anti_root = tmp_path / "anti"
        self._call(anti_root, scope="libraries", reject_pattern="Never use pandas.")
        content = (anti_root / "libraries.md").read_text(encoding="utf-8")
        assert content.startswith("---")
        assert "anti-libraries" in content
        assert "type: anti" in content

    def test_slug_normalizes_space_in_scope(self, tmp_path):
        anti_root = tmp_path / "anti"
        result = self._call(anti_root,
                            scope="test approaches", reject_pattern="No mocks in DB tests.")
        assert result["ok"] is True
        assert (anti_root / "test-approaches.md").exists()

    def test_appends_to_existing_file(self, tmp_path):
        anti_root = tmp_path / "anti"
        self._call(anti_root, scope="libraries", reject_pattern="No pandas.")
        self._call(anti_root, scope="libraries", reject_pattern="No polars either.")
        content = (anti_root / "libraries.md").read_text(encoding="utf-8")
        assert "No pandas" in content
        assert "No polars" in content

    def test_duplicate_pattern_returns_already_recorded(self, tmp_path):
        anti_root = tmp_path / "anti"
        self._call(anti_root, scope="libraries", reject_pattern="No pandas in this project.")
        result = self._call(anti_root, scope="libraries",
                            reject_pattern="No pandas in this project.")
        assert result["ok"] is True
        assert result.get("note") == "already recorded"

    def test_workspace_applies_to_written(self, tmp_path):
        anti_root = tmp_path / "anti"
        self._call(anti_root, scope="approaches", reject_pattern="No manual deploys.",
                   applies_to="workspace")
        content = (anti_root / "approaches.md").read_text(encoding="utf-8")
        assert "workspace" in content

    def test_project_applies_to_written(self, tmp_path):
        anti_root = tmp_path / "anti"
        self._call(anti_root, scope="approaches", reject_pattern="No kubectl apply.",
                   applies_to="project", project="atlas-memory+")
        content = (anti_root / "approaches.md").read_text(encoding="utf-8")
        assert "project" in content
        assert "atlas-memory+" in content

    def test_returns_file_path_in_result(self, tmp_path):
        anti_root = tmp_path / "anti"
        result = self._call(anti_root, scope="libs", reject_pattern="No X.")
        assert result["file"] is not None
        assert "libs.md" in result["file"]


# ---------------------------------------------------------------------------
# anti_remember -- autonomous gate
# ---------------------------------------------------------------------------

class TestAntiRememberAutonomousGate:
    def test_blocks_when_not_autonomous(self, tmp_path):
        from atlas_plus.autonomous import require_autonomous_writes
        import atlas_plus.mcp.memory_server.tools.anti as mod

        with patch.object(mod, "ANTI_ROOT", tmp_path / "anti"), \
             patch("atlas_plus.autonomous.require_autonomous_writes",
                   side_effect=RuntimeError("autonomous writes are off")):
            result = mod.anti_remember(scope="libs", reject_pattern="No X.")

        assert result["ok"] is False
        assert "autonomous" in result["error"].lower()


# ---------------------------------------------------------------------------
# list_anti
# ---------------------------------------------------------------------------

class TestListAnti:
    def _write_anti(self, anti_root: Path, scope: str, entries: list[dict]) -> None:
        anti_root.mkdir(parents=True, exist_ok=True)
        header = yaml.dump(
            {"id": f"anti-{scope}", "title": f"Anti-memory: {scope}",
             "type": "anti", "status": "active"},
            default_flow_style=False,
        ).strip()
        blocks = [f"---\n{header}\n---\n\n# Do not suggest"]
        for e in entries:
            blocks.append("\n---\n" + yaml.dump(e, default_flow_style=False).strip())
        (anti_root / f"{scope}.md").write_text("".join(blocks) + "\n", encoding="utf-8")

    def test_empty_when_no_anti_dir(self, tmp_path):
        import atlas_plus.mcp.memory_server.tools.anti as mod
        with patch.object(mod, "ANTI_ROOT", tmp_path / "anti"):
            result = mod.list_anti()
        assert result == []

    def test_returns_all_entries(self, tmp_path):
        anti_root = tmp_path / "anti"
        self._write_anti(anti_root, "libraries", [
            {"reject_pattern": "No pandas.", "applies_to": "workspace", "added": "2026-01-01"},
            {"reject_pattern": "No polars.", "applies_to": "workspace", "added": "2026-01-02"},
        ])
        import atlas_plus.mcp.memory_server.tools.anti as mod
        with patch.object(mod, "ANTI_ROOT", anti_root):
            result = mod.list_anti()
        assert len(result) == 2

    def test_filters_by_applies_to(self, tmp_path):
        anti_root = tmp_path / "anti"
        self._write_anti(anti_root, "approaches", [
            {"reject_pattern": "No manual deploy.", "applies_to": "workspace", "added": "2026-01-01"},
            {"reject_pattern": "No kubectl.", "applies_to": "project", "project": "atlas", "added": "2026-01-02"},
        ])
        import atlas_plus.mcp.memory_server.tools.anti as mod
        with patch.object(mod, "ANTI_ROOT", anti_root):
            result = mod.list_anti(applies_to="workspace")
        assert len(result) == 1
        assert result[0]["applies_to"] == "workspace"

    def test_filters_by_project(self, tmp_path):
        anti_root = tmp_path / "anti"
        self._write_anti(anti_root, "approaches", [
            {"reject_pattern": "No X.", "applies_to": "project", "project": "atlas", "added": "2026-01-01"},
            {"reject_pattern": "No Y.", "applies_to": "project", "project": "gatekeeper", "added": "2026-01-02"},
        ])
        import atlas_plus.mcp.memory_server.tools.anti as mod
        with patch.object(mod, "ANTI_ROOT", anti_root):
            result = mod.list_anti(project="atlas")
        assert len(result) == 1
        assert "atlas" in result[0]["project"]

    def test_scope_included_in_result(self, tmp_path):
        anti_root = tmp_path / "anti"
        self._write_anti(anti_root, "libraries", [
            {"reject_pattern": "No X.", "applies_to": "workspace", "added": "2026-01-01"},
        ])
        import atlas_plus.mcp.memory_server.tools.anti as mod
        with patch.object(mod, "ANTI_ROOT", anti_root):
            result = mod.list_anti()
        assert result[0]["scope"] == "libraries"


# ---------------------------------------------------------------------------
# propagate_anti_rejection
# ---------------------------------------------------------------------------

class TestPropagateAntiRejection:
    """Tests for the propagation logic that flags existing .memory/ files."""

    def _make_memory_file(self, root: Path, rel: str, content: str) -> Path:
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return p

    def test_flags_matching_file(self, tmp_path):
        root = tmp_path / "memory"
        self._make_memory_file(
            root, "feedback/testing.md",
            "---\ntype: feedback\n---\n\nAlways mock the database in unit tests "
            "to avoid hitting production data and slow queries.",
        )
        import atlas_plus.mcp.memory_server.tools.anti as mod
        result = mod.propagate_anti_rejection(
            reject_pattern="Never mock the database in unit tests. Real DB required.",
            anti_scope="testing",
            memory_root=root,
        )
        assert result["flagged"] == 1
        flagged_content = (root / "feedback/testing.md").read_text(encoding="utf-8")
        assert "anti_flagged: true" in flagged_content

    def test_skips_anti_directory(self, tmp_path):
        root = tmp_path / "memory"
        self._make_memory_file(
            root, "anti/testing.md",
            "---\ntype: anti\n---\n\n---\nreject_pattern: Never mock database.\nadded: 2026-01-01\n",
        )
        import atlas_plus.mcp.memory_server.tools.anti as mod
        result = mod.propagate_anti_rejection(
            reject_pattern="Never mock the database in unit tests. Real DB required.",
            anti_scope="testing",
            memory_root=root,
        )
        assert result["flagged"] == 0

    def test_skips_already_flagged_file(self, tmp_path):
        root = tmp_path / "memory"
        self._make_memory_file(
            root, "feedback/testing.md",
            "---\ntype: feedback\nanti_flagged: true\n---\n\nAlways mock the database "
            "in unit tests to avoid slow queries and production hits.",
        )
        import atlas_plus.mcp.memory_server.tools.anti as mod
        result = mod.propagate_anti_rejection(
            reject_pattern="Never mock the database in unit tests. Real DB required.",
            anti_scope="testing",
            memory_root=root,
        )
        assert result["flagged"] == 0
        assert result["skipped"] == 1

    def test_no_flag_when_few_keywords_match(self, tmp_path):
        root = tmp_path / "memory"
        self._make_memory_file(
            root, "feedback/style.md",
            "---\ntype: feedback\n---\n\nAlways use consistent indentation in code.",
        )
        import atlas_plus.mcp.memory_server.tools.anti as mod
        result = mod.propagate_anti_rejection(
            reject_pattern="Never mock the database in production unit tests.",
            anti_scope="testing",
            memory_root=root,
        )
        assert result["flagged"] == 0

    def test_short_pattern_skips_propagation(self, tmp_path):
        root = tmp_path / "memory"
        self._make_memory_file(
            root, "feedback/style.md",
            "---\ntype: feedback\n---\n\nUse tabs.",
        )
        import atlas_plus.mcp.memory_server.tools.anti as mod
        result = mod.propagate_anti_rejection(
            reject_pattern="No.",
            anti_scope="style",
            memory_root=root,
        )
        assert result["flagged"] == 0

    def test_project_scoped_only_flags_project_files(self, tmp_path):
        root = tmp_path / "memory"
        # File in target project -- should be flagged
        self._make_memory_file(
            root, "projects/atlas-memory+/notes.md",
            "---\ntype: reference\n---\n\nAlways mock the database for atlas unit tests "
            "so the pipeline runs faster and avoids hitting production.",
        )
        # File outside the project -- should NOT be flagged
        self._make_memory_file(
            root, "feedback/general.md",
            "---\ntype: feedback\n---\n\nAlways mock the database in any unit test "
            "to keep tests fast and isolated from production.",
        )
        import atlas_plus.mcp.memory_server.tools.anti as mod
        result = mod.propagate_anti_rejection(
            reject_pattern="Never mock the database in unit tests. Real DB required.",
            anti_scope="testing",
            memory_root=root,
            applies_to="project",
            project="atlas-memory+",
        )
        assert result["flagged"] == 1
        # Only the project file should be flagged
        assert any("atlas-memory+" in p for p in result["paths"])

    def test_anti_remember_includes_propagated_count(self, tmp_path):
        """anti_remember() result includes propagated key."""
        root = tmp_path / "memory"
        anti_root = root / "anti"
        self._make_memory_file(
            root, "feedback/testing.md",
            "---\ntype: feedback\n---\n\nAlways mock the database in unit tests "
            "to avoid slow queries and keep tests isolated from production.",
        )
        import atlas_plus.mcp.memory_server.tools.anti as mod
        with patch.object(mod, "ANTI_ROOT", anti_root), \
             patch.object(mod, "_MEMORY_ROOT", root), \
             patch("atlas_plus.autonomous.require_autonomous_writes", lambda *a, **kw: None):
            result = mod.anti_remember(
                scope="testing",
                reject_pattern="Never mock the database in unit tests. Real DB required.",
            )
        assert result["ok"] is True
        assert "propagated" in result
