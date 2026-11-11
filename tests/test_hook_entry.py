"""Unit tests for summarizer/hook_entry.py -- Claude Code Stop hook.

Covers: _find_script path resolution, _capture_fsdiffs subprocess logic
(with subprocess mocked), and main() gating (autonomous check + kill switches).
No real git operations are performed.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest


# ---------------------------------------------------------------------------
# _find_script
# ---------------------------------------------------------------------------

class TestFindScript:
    def test_finds_exe_in_scripts_dir(self, tmp_path):
        from atlas_plus.summarizer.hook_entry import _find_script

        # _find_script uses Path(sys.executable).parent as scripts_dir
        # So if executable = tmp_path/python.exe, scripts_dir = tmp_path
        exe = tmp_path / "atlas-temporal.exe"
        exe.write_bytes(b"")

        with patch.object(sys, "executable", str(tmp_path / "python.exe")):
            result = _find_script("atlas-temporal")

        assert result == exe

    def test_finds_non_exe_when_exe_missing(self, tmp_path):
        from atlas_plus.summarizer.hook_entry import _find_script

        script = tmp_path / "atlas-temporal"
        script.write_bytes(b"")

        with patch.object(sys, "executable", str(tmp_path / "python.exe")):
            result = _find_script("atlas-temporal")

        assert result == script

    def test_returns_none_when_not_found(self, tmp_path):
        from atlas_plus.summarizer.hook_entry import _find_script

        with patch.object(sys, "executable", str(tmp_path / "python.exe")):
            result = _find_script("atlas-temporal")

        assert result is None


# ---------------------------------------------------------------------------
# _capture_fsdiffs
# ---------------------------------------------------------------------------

class TestCaptureFsdiffs:
    def _make_git_project(self, projects_dir: Path, name: str) -> Path:
        proj = projects_dir / name
        proj.mkdir(parents=True, exist_ok=True)
        (proj / ".git").mkdir()
        return proj

    def test_does_nothing_when_projects_root_missing(self, tmp_path):
        from atlas_plus.summarizer.hook_entry import _capture_fsdiffs

        missing = tmp_path / "no-such-dir"
        state_dir = tmp_path / "state"
        with patch("atlas_plus.summarizer.hook_entry._PROJECTS_ROOT", missing), \
             patch("atlas_plus.summarizer.hook_entry._STATE_DIR", state_dir):
            _capture_fsdiffs()  # should not raise

        assert not (state_dir / "fsdiffs").exists()

    def test_skips_dirs_without_git(self, tmp_path):
        from atlas_plus.summarizer.hook_entry import _capture_fsdiffs

        projects = tmp_path / "projects"
        non_git = projects / "not-a-repo"
        non_git.mkdir(parents=True)
        state_dir = tmp_path / "state"

        with patch("atlas_plus.summarizer.hook_entry._PROJECTS_ROOT", projects), \
             patch("atlas_plus.summarizer.hook_entry._STATE_DIR", state_dir):
            _capture_fsdiffs()

        # Dir may be created but no .fsdiff.json should be written
        fsdiff_files = list((state_dir / "fsdiffs").rglob("*.fsdiff.json")) if (state_dir / "fsdiffs").exists() else []
        assert fsdiff_files == []

    def test_skips_clean_repos_with_no_recent_commits(self, tmp_path):
        from atlas_plus.summarizer.hook_entry import _capture_fsdiffs

        projects = tmp_path / "projects"
        self._make_git_project(projects, "clean-repo")
        state_dir = tmp_path / "state"

        def fake_check_output(cmd, **kwargs):
            # Both git status and git log return empty (clean, no recent commits)
            return ""

        with patch("atlas_plus.summarizer.hook_entry._PROJECTS_ROOT", projects), \
             patch("atlas_plus.summarizer.hook_entry._STATE_DIR", state_dir), \
             patch("subprocess.check_output", side_effect=fake_check_output):
            _capture_fsdiffs()

        # No JSON file should be written for a clean repo
        fsdiff_files = list((state_dir / "fsdiffs").rglob("*.fsdiff.json")) if (state_dir / "fsdiffs").exists() else []
        assert fsdiff_files == []

    def test_writes_fsdiff_json_for_dirty_repo(self, tmp_path):
        from atlas_plus.summarizer.hook_entry import _capture_fsdiffs

        projects = tmp_path / "projects"
        self._make_git_project(projects, "dirty-repo")
        state_dir = tmp_path / "state"

        call_count = [0]
        def fake_check_output(cmd, **kwargs):
            call_count[0] += 1
            if "status" in cmd:
                return " M modified-file.py\n"
            if "log" in cmd and "--since" in cmd:
                return "abc1234 feat: add new thing"
            if "rev-parse" in cmd and "--abbrev-ref" in cmd:
                return "main"
            if "rev-parse" in cmd and "--short" in cmd:
                return "abc1234"
            if "diff" in cmd:
                return "diff content here"
            return ""

        with patch("atlas_plus.summarizer.hook_entry._PROJECTS_ROOT", projects), \
             patch("atlas_plus.summarizer.hook_entry._STATE_DIR", state_dir), \
             patch("subprocess.check_output", side_effect=fake_check_output):
            _capture_fsdiffs()

        fsdiff_files = list((state_dir / "fsdiffs").rglob("*.fsdiff.json"))
        assert len(fsdiff_files) == 1

        data = json.loads(fsdiff_files[0].read_text())
        assert data["projects"][0]["project"] == "dirty-repo"
        assert data["projects"][0]["branch"] == "main"
        assert len(data["projects"][0]["dirty_files"]) >= 1

    def test_handles_subprocess_error_gracefully(self, tmp_path):
        from atlas_plus.summarizer.hook_entry import _capture_fsdiffs

        projects = tmp_path / "projects"
        self._make_git_project(projects, "broken-repo")
        state_dir = tmp_path / "state"

        with patch("atlas_plus.summarizer.hook_entry._PROJECTS_ROOT", projects), \
             patch("atlas_plus.summarizer.hook_entry._STATE_DIR", state_dir), \
             patch("subprocess.check_output", side_effect=Exception("git not found")):
            _capture_fsdiffs()  # should not raise

        # No JSON output written since the subprocess failed on each project
        fsdiff_files = list((state_dir / "fsdiffs").rglob("*.fsdiff.json")) if (state_dir / "fsdiffs").exists() else []
        assert fsdiff_files == []

    def test_diff_content_truncated_to_4000_chars(self, tmp_path):
        from atlas_plus.summarizer.hook_entry import _capture_fsdiffs

        projects = tmp_path / "projects"
        self._make_git_project(projects, "big-diff")
        state_dir = tmp_path / "state"
        big_diff = "x" * 10000

        def fake_check_output(cmd, **kwargs):
            if "status" in cmd:
                return " M big-file.py\n"
            if "log" in cmd and "--since" in cmd:
                return "abc1234 feat: big change"
            if "rev-parse" in cmd and "--abbrev-ref" in cmd:
                return "main"
            if "rev-parse" in cmd and "--short" in cmd:
                return "abc1234"
            if "diff" in cmd:
                return big_diff
            return ""

        with patch("atlas_plus.summarizer.hook_entry._PROJECTS_ROOT", projects), \
             patch("atlas_plus.summarizer.hook_entry._STATE_DIR", state_dir), \
             patch("subprocess.check_output", side_effect=fake_check_output):
            _capture_fsdiffs()

        fsdiff_files = list((state_dir / "fsdiffs").rglob("*.fsdiff.json"))
        assert len(fsdiff_files) == 1
        data = json.loads(fsdiff_files[0].read_text())
        assert len(data["projects"][0]["diff_summary"]) <= 4000


# ---------------------------------------------------------------------------
# main()
# ---------------------------------------------------------------------------

class TestHookEntryMain:
    def test_returns_zero_when_not_autonomous(self):
        from atlas_plus.summarizer.hook_entry import main

        with patch("atlas_plus.summarizer.hook_entry.is_session_autonomous", return_value=False):
            result = main()

        assert result == 0

    def test_autonomous_launches_summarizer_popen(self, monkeypatch):
        from atlas_plus.summarizer.hook_entry import main
        monkeypatch.setenv("ATLAS_DISABLE_AUTOSUMMARIZE", "")
        monkeypatch.setenv("ATLAS_DISABLE_SUGGEST_FACTS", "1")
        monkeypatch.setenv("ATLAS_DISABLE_TRANSCRIPT_MINING", "1")

        popen_calls = []
        def fake_popen(cmd, **kwargs):
            popen_calls.append(cmd)
            return MagicMock()

        with patch("atlas_plus.summarizer.hook_entry.is_session_autonomous", return_value=True), \
             patch("atlas_plus.summarizer.hook_entry._summarizer_unavailable_reason", return_value=""), \
             patch("subprocess.Popen", side_effect=fake_popen), \
             patch("atlas_plus.summarizer.hook_entry._capture_fsdiffs"):
            result = main()

        assert result == 0
        # At least one Popen call for the summarizer
        assert any("atlas_plus.summarizer.cli" in " ".join(str(c) for c in cmd)
                   for cmd in popen_calls)

    def test_kill_switch_disables_summarizer(self, monkeypatch):
        from atlas_plus.summarizer.hook_entry import main
        monkeypatch.setenv("ATLAS_DISABLE_AUTOSUMMARIZE", "1")
        monkeypatch.setenv("ATLAS_DISABLE_SUGGEST_FACTS", "1")
        monkeypatch.setenv("ATLAS_DISABLE_TRANSCRIPT_MINING", "1")

        popen_calls = []
        def fake_popen(cmd, **kwargs):
            popen_calls.append(cmd)
            return MagicMock()

        with patch("atlas_plus.summarizer.hook_entry.is_session_autonomous", return_value=True), \
             patch("atlas_plus.summarizer.hook_entry._summarizer_unavailable_reason", return_value=""), \
             patch("subprocess.Popen", side_effect=fake_popen), \
             patch("atlas_plus.summarizer.hook_entry._capture_fsdiffs"):
            result = main()

        assert result == 0
        # No summarizer Popen (kill switch active)
        for cmd in popen_calls:
            assert "atlas_plus.summarizer.cli" not in " ".join(str(c) for c in cmd)

    def test_capture_fsdiffs_called_when_autonomous(self, monkeypatch):
        from atlas_plus.summarizer.hook_entry import main
        monkeypatch.setenv("ATLAS_DISABLE_AUTOSUMMARIZE", "1")
        monkeypatch.setenv("ATLAS_DISABLE_SUGGEST_FACTS", "1")
        monkeypatch.setenv("ATLAS_DISABLE_TRANSCRIPT_MINING", "1")

        fsdiff_calls = []
        with patch("atlas_plus.summarizer.hook_entry.is_session_autonomous", return_value=True), \
             patch("subprocess.Popen", return_value=MagicMock()), \
             patch("atlas_plus.summarizer.hook_entry._capture_fsdiffs",
                   side_effect=lambda: fsdiff_calls.append(1)):
            main()

        assert fsdiff_calls == [1]

    def test_fsdiff_exception_does_not_crash_main(self, monkeypatch):
        from atlas_plus.summarizer.hook_entry import main
        monkeypatch.setenv("ATLAS_DISABLE_AUTOSUMMARIZE", "1")
        monkeypatch.setenv("ATLAS_DISABLE_SUGGEST_FACTS", "1")
        monkeypatch.setenv("ATLAS_DISABLE_TRANSCRIPT_MINING", "1")

        with patch("atlas_plus.summarizer.hook_entry.is_session_autonomous", return_value=True), \
             patch("subprocess.Popen", return_value=MagicMock()), \
             patch("atlas_plus.summarizer.hook_entry._capture_fsdiffs",
                   side_effect=RuntimeError("disk full")):
            result = main()  # should not raise

        assert result == 0
