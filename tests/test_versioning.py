"""Unit tests for mcp/memory_server/versioning.py -- version-before-write.

Covers: returns None for non-existent file, creates versioned copy in the
correct versions/ sibling directory, includes ISO date in name, increments
the version counter on collision, and is idempotent for identical content
(skips creating a duplicate version for unchanged files).
"""
from __future__ import annotations

from datetime import date
from pathlib import Path
from unittest.mock import patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _patch_versioning(memory_root: Path):
    """Patch MEMORY_ROOT in the versioning module."""
    import atlas_plus.mcp.memory_server.versioning as mod
    return patch.object(mod, "MEMORY_ROOT", memory_root)


def _write(path: Path, content: str = "original") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestVersionBeforeWrite:
    def test_returns_none_for_nonexistent_file(self, tmp_path):
        from atlas_plus.mcp.memory_server.versioning import version_before_write
        mem = tmp_path / "memory"
        ghost = mem / "feedback" / "ghost.md"
        with _patch_versioning(mem):
            result = version_before_write(ghost)
        assert result is None

    def test_creates_version_in_versions_dir(self, tmp_path):
        from atlas_plus.mcp.memory_server.versioning import version_before_write
        mem = tmp_path / "memory"
        target = mem / "feedback" / "style.md"
        _write(target)
        with _patch_versioning(mem):
            result = version_before_write(target)
        assert result is not None
        assert result.exists()
        assert result.read_text(encoding="utf-8") == "original"
        # Must be inside memory/versions/
        assert "versions" in result.parts

    def test_version_filename_includes_iso_date(self, tmp_path):
        from atlas_plus.mcp.memory_server.versioning import version_before_write
        mem = tmp_path / "memory"
        target = mem / "reference" / "tools.md"
        _write(target, "ripgrep is the tool")
        with _patch_versioning(mem):
            result = version_before_write(target)
        today = date.today().isoformat()
        assert today in result.name

    def test_counter_increments_on_collision(self, tmp_path):
        from atlas_plus.mcp.memory_server.versioning import version_before_write
        mem = tmp_path / "memory"
        target = mem / "feedback" / "rules.md"
        _write(target, "first version content")
        today = date.today().isoformat()
        # Pre-create v1 in versions dir
        versions_dir = mem / "versions" / "feedback"
        versions_dir.mkdir(parents=True, exist_ok=True)
        (versions_dir / f"rules_{today}_v1.md").write_text("different v1 content")

        with _patch_versioning(mem):
            result = version_before_write(target)

        assert "_v2" in result.name

    def test_idempotent_skips_duplicate_content(self, tmp_path):
        from atlas_plus.mcp.memory_server.versioning import version_before_write
        mem = tmp_path / "memory"
        target = mem / "feedback" / "rules.md"
        _write(target, "same content")
        today = date.today().isoformat()
        # Pre-create v1 with identical content
        versions_dir = mem / "versions" / "feedback"
        versions_dir.mkdir(parents=True, exist_ok=True)
        v1 = versions_dir / f"rules_{today}_v1.md"
        v1.write_text("same content")

        with _patch_versioning(mem):
            result = version_before_write(target)

        # Should return the existing v1 (no new v2 created)
        assert result == v1
        assert not (versions_dir / f"rules_{today}_v2.md").exists()

    def test_preserves_subdir_structure_in_versions(self, tmp_path):
        from atlas_plus.mcp.memory_server.versioning import version_before_write
        mem = tmp_path / "memory"
        target = mem / "projects" / "atlas" / "state.md"
        _write(target, "project state")
        with _patch_versioning(mem):
            result = version_before_write(target)
        # Version should be in versions/projects/atlas/
        assert result.parent.parent.name == "projects" or \
               "projects" in str(result)

    def test_handles_missing_target_gracefully(self, tmp_path):
        from atlas_plus.mcp.memory_server.versioning import version_before_write
        mem = tmp_path / "memory"
        missing = mem / "nonexistent.md"
        with _patch_versioning(mem):
            result = version_before_write(missing)
        assert result is None

    def test_copies_exact_bytes(self, tmp_path):
        from atlas_plus.mcp.memory_server.versioning import version_before_write
        mem = tmp_path / "memory"
        content = "---\nid: test\ntitle: Test\n---\n\nFact with unicode: café"
        target = mem / "reference" / "unicode.md"
        _write(target, content)
        with _patch_versioning(mem):
            result = version_before_write(target)
        assert result.read_text(encoding="utf-8") == content


class TestEdgeProbes2026_10:
    """Quality loop run 37 edge probes: the module guarding version-before-
    edit had its own edges. All reproduced before fixing."""

    def test_out_of_root_target_returns_none(self, tmp_path):
        """ValueError from relative_to must not crash the WRITE path."""
        from atlas_plus.mcp.memory_server.versioning import version_before_write
        mem = tmp_path / "mem"
        outside = tmp_path / "elsewhere" / "x.md"
        _write(outside, "outside")
        (mem / "reference").mkdir(parents=True)
        assert version_before_write(outside, root=mem) is None

    def test_extensionless_file_is_idempotent(self, tmp_path):
        """The old glob pattern had a trailing dot for extensionless files
        and never matched -- a NEW copy on every identical write."""
        from atlas_plus.mcp.memory_server.versioning import version_before_write
        mem = tmp_path / "mem"
        target = mem / "reference" / "MANIFEST"
        _write(target, "same content")
        for _ in range(3):
            version_before_write(target, root=mem)
        copies = list((mem / "versions" / "reference").glob("MANIFEST_*"))
        assert len(copies) == 1

    def test_glob_metacharacters_in_name_idempotent(self, tmp_path):
        """'notes [draft].md' broke the glob-based idempotence check."""
        from atlas_plus.mcp.memory_server.versioning import version_before_write
        mem = tmp_path / "mem"
        target = mem / "reference" / "notes [draft].md"
        _write(target, "same")
        version_before_write(target, root=mem)
        version_before_write(target, root=mem)
        copies = list((mem / "versions" / "reference").glob("notes*"))
        assert len(copies) == 1

    def test_concurrent_writers_lose_no_state_and_leave_no_empties(self, tmp_path):
        """Atomic slot claim + per-thread tmp: unserialized concurrent
        callers may duplicate identical content (the real write path is
        lock-serialized) but must never overwrite each other's copy nor
        orphan empty claimed slots."""
        import threading
        from atlas_plus.mcp.memory_server.versioning import version_before_write
        mem = tmp_path / "mem"
        target = mem / "reference" / "hot.md"
        _write(target, "original")
        for r in range(5):
            target.write_text(f"state {r}", encoding="utf-8")
            ts = [threading.Thread(
                target=lambda: version_before_write(target, root=mem))
                for _ in range(4)]
            [t.start() for t in ts]
            [t.join() for t in ts]
        copies = list((mem / "versions" / "reference").glob("hot_*"))
        contents = [p.read_text(encoding="utf-8") for p in copies]
        assert "" not in contents, "orphaned empty claimed slot"
        for r in range(5):
            assert f"state {r}" in contents, f"state {r} lost"
