"""Unit tests for summarizer/renderer.py -- session log + updates.txt writer.

Covers: _version_file (non-existent, creates versioned copy, collision counter),
write_outputs (dry_run, autonomous gate, new vs. existing session log,
prepend to updates.txt, version-before-edit side effect).
All file operations use tmp_path; no real .memory/ or updates.txt touched.
"""
from __future__ import annotations

import tempfile
from datetime import date
from pathlib import Path
from unittest.mock import patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _setup_module(tmp_path: Path):
    """Return patched module-level paths for renderer."""
    sessions_dir = tmp_path / "sessions"
    updates_txt  = tmp_path / "updates.txt"
    versions_dir = tmp_path / "versions"
    sessions_dir.mkdir()
    return sessions_dir, updates_txt, versions_dir


def _patch_renderer(sessions_dir, updates_txt, versions_dir):
    """Context: patch all three module-level Paths in renderer.py."""
    import atlas_plus.summarizer.renderer as mod
    return [
        patch.object(mod, "MEMORY_SESSIONS_DIR", sessions_dir),
        patch.object(mod, "UPDATES_TXT", updates_txt),
        patch.object(mod, "VERSIONS_DIR", versions_dir),
    ]


# ---------------------------------------------------------------------------
# _version_file
# ---------------------------------------------------------------------------

class TestVersionFile:
    def test_returns_none_for_nonexistent_file(self, tmp_path):
        from atlas_plus.summarizer.renderer import _version_file
        result = _version_file(tmp_path / "ghost.md")
        assert result is None

    def test_copies_existing_file(self, tmp_path):
        from atlas_plus.summarizer.renderer import _version_file
        sessions_dir, _, versions_dir = _setup_module(tmp_path)
        target = sessions_dir / "2026-05-20.md"
        target.write_text("original content", encoding="utf-8")

        import atlas_plus.summarizer.renderer as mod
        with patch.object(mod, "MEMORY_SESSIONS_DIR", sessions_dir), \
             patch.object(mod, "VERSIONS_DIR", versions_dir):
            result = _version_file(target)

        assert result is not None
        assert result.exists()
        assert result.read_text(encoding="utf-8") == "original content"

    def test_version_filename_contains_date(self, tmp_path):
        from atlas_plus.summarizer.renderer import _version_file
        sessions_dir, _, versions_dir = _setup_module(tmp_path)
        target = sessions_dir / "2026-05-20.md"
        target.write_text("data", encoding="utf-8")

        import atlas_plus.summarizer.renderer as mod
        with patch.object(mod, "MEMORY_SESSIONS_DIR", sessions_dir), \
             patch.object(mod, "VERSIONS_DIR", versions_dir):
            result = _version_file(target)

        today = date.today().isoformat()
        assert today in result.name

    def test_collision_increments_version_counter(self, tmp_path):
        from atlas_plus.summarizer.renderer import _version_file
        sessions_dir, _, versions_dir = _setup_module(tmp_path)
        target = sessions_dir / "2026-05-20.md"
        target.write_text("data", encoding="utf-8")

        today = date.today().isoformat()
        import atlas_plus.summarizer.renderer as mod
        with patch.object(mod, "MEMORY_SESSIONS_DIR", sessions_dir), \
             patch.object(mod, "VERSIONS_DIR", versions_dir):
            # Pre-create v1 so the second call increments to v2
            v1 = versions_dir / f"2026-05-20_{today}_v1.md"
            versions_dir.mkdir(parents=True, exist_ok=True)
            v1.write_text("old v1", encoding="utf-8")

            result = _version_file(target)

        assert "_v2" in result.name

    def test_non_session_file_uses_sibling_versions_dir(self, tmp_path):
        from atlas_plus.summarizer.renderer import _version_file
        sessions_dir, _, versions_dir = _setup_module(tmp_path)
        # A file that is NOT under sessions_dir
        other_dir = tmp_path / "other"
        other_dir.mkdir()
        target = other_dir / "myfile.md"
        target.write_text("side file", encoding="utf-8")

        import atlas_plus.summarizer.renderer as mod
        with patch.object(mod, "MEMORY_SESSIONS_DIR", sessions_dir), \
             patch.object(mod, "VERSIONS_DIR", versions_dir):
            result = _version_file(target)

        # Should go to other/versions/, not the global VERSIONS_DIR
        assert result.parent.parent == other_dir


# ---------------------------------------------------------------------------
# write_outputs
# ---------------------------------------------------------------------------

class TestWriteOutputs:
    def test_dry_run_returns_paths_without_writing(self, tmp_path):
        from atlas_plus.summarizer.renderer import write_outputs
        sessions_dir, updates_txt, versions_dir = _setup_module(tmp_path)

        patches = _patch_renderer(sessions_dir, updates_txt, versions_dir)
        for p in patches:
            p.start()
        try:
            result = write_outputs("log text", "updates text", "2026-05-20", dry_run=True)
        finally:
            for p in patches:
                p.stop()

        assert "2026-05-20" in result["session_log"]
        assert not (sessions_dir / "2026-05-20.md").exists()

    def test_raises_when_not_autonomous(self, tmp_path):
        from atlas_plus.summarizer.renderer import write_outputs
        sessions_dir, updates_txt, versions_dir = _setup_module(tmp_path)

        patches = _patch_renderer(sessions_dir, updates_txt, versions_dir)
        for p in patches:
            p.start()
        try:
            with patch("atlas_plus.summarizer.renderer.is_session_autonomous", return_value=False):
                with pytest.raises(RuntimeError, match="blocked"):
                    write_outputs("log", "updates", "2026-05-20", dry_run=False)
        finally:
            for p in patches:
                p.stop()

    def test_creates_new_session_log(self, tmp_path):
        from atlas_plus.summarizer.renderer import write_outputs
        sessions_dir, updates_txt, versions_dir = _setup_module(tmp_path)

        patches = _patch_renderer(sessions_dir, updates_txt, versions_dir)
        for p in patches:
            p.start()
        try:
            with patch("atlas_plus.summarizer.renderer.is_session_autonomous", return_value=True):
                result = write_outputs("## Session\n\nnew log", "update line\n", "2026-05-20")
        finally:
            for p in patches:
                p.stop()

        log_path = sessions_dir / "2026-05-20.md"
        assert log_path.exists()
        assert "new log" in log_path.read_text(encoding="utf-8")

    def test_prepends_to_existing_session_log(self, tmp_path):
        from atlas_plus.summarizer.renderer import write_outputs
        sessions_dir, updates_txt, versions_dir = _setup_module(tmp_path)
        (sessions_dir / "2026-05-20.md").write_text("## Old session\n\nOld content.\n")

        patches = _patch_renderer(sessions_dir, updates_txt, versions_dir)
        for p in patches:
            p.start()
        try:
            with patch("atlas_plus.summarizer.renderer.is_session_autonomous", return_value=True):
                write_outputs("## New session\n\nNew content.", "update\n", "2026-05-20")
        finally:
            for p in patches:
                p.stop()

        content = (sessions_dir / "2026-05-20.md").read_text(encoding="utf-8")
        # New content comes before old content
        assert content.index("New content") < content.index("Old content")

    def test_creates_updates_txt_when_missing(self, tmp_path):
        from atlas_plus.summarizer.renderer import write_outputs
        sessions_dir, updates_txt, versions_dir = _setup_module(tmp_path)

        patches = _patch_renderer(sessions_dir, updates_txt, versions_dir)
        for p in patches:
            p.start()
        try:
            with patch("atlas_plus.summarizer.renderer.is_session_autonomous", return_value=True):
                write_outputs("log", "my update text\n", "2026-05-20")
        finally:
            for p in patches:
                p.stop()

        assert updates_txt.exists()
        assert "my update text" in updates_txt.read_text(encoding="utf-8")

    def test_versions_session_log_before_overwrite(self, tmp_path):
        from atlas_plus.summarizer.renderer import write_outputs
        sessions_dir, updates_txt, versions_dir = _setup_module(tmp_path)
        original = "## Original\n\nOriginal content."
        (sessions_dir / "2026-05-20.md").write_text(original)

        patches = _patch_renderer(sessions_dir, updates_txt, versions_dir)
        for p in patches:
            p.start()
        try:
            with patch("atlas_plus.summarizer.renderer.is_session_autonomous", return_value=True):
                write_outputs("## New\n\nNew content.", "update\n", "2026-05-20")
        finally:
            for p in patches:
                p.stop()

        # A versioned copy of the original should exist
        version_files = list(versions_dir.rglob("2026-05-20_*.md")) if versions_dir.exists() else []
        assert len(version_files) >= 1
        assert version_files[0].read_text(encoding="utf-8") == original

    def test_returns_both_paths_in_result(self, tmp_path):
        from atlas_plus.summarizer.renderer import write_outputs
        sessions_dir, updates_txt, versions_dir = _setup_module(tmp_path)

        patches = _patch_renderer(sessions_dir, updates_txt, versions_dir)
        for p in patches:
            p.start()
        try:
            with patch("atlas_plus.summarizer.renderer.is_session_autonomous", return_value=True):
                result = write_outputs("log", "updates", "2026-05-20")
        finally:
            for p in patches:
                p.stop()

        assert "session_log" in result
        assert "updates_txt" in result
