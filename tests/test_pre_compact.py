"""Tests for the PreCompact hook (saves session state before context compaction)."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch


class TestDetectProject:
    def test_returns_none_when_no_git_repo_in_projects_root(self, tmp_path, monkeypatch):
        """_detect_project returns None when CWD is not under a git-tracked projects root."""
        from atlas_plus.hooks.pre_compact import _detect_project
        monkeypatch.chdir(tmp_path)
        with patch("atlas_plus.hooks.pre_compact.PROJECTS_ROOT", tmp_path / "projects"):
            result = _detect_project()
        assert result is None

    def test_returns_name_and_path_for_git_project(self, tmp_path, monkeypatch):
        """_detect_project finds the project root when inside a git repo under PROJECTS_ROOT."""
        from atlas_plus.hooks.pre_compact import _detect_project
        # Create a fake git project dir
        proj_root = tmp_path / "projects" / "my-project"
        proj_root.mkdir(parents=True)
        (proj_root / ".git").mkdir()
        # Change into a subdirectory of the project
        sub = proj_root / "src"
        sub.mkdir()
        monkeypatch.chdir(sub)
        with patch("atlas_plus.hooks.pre_compact.PROJECTS_ROOT", tmp_path / "projects"):
            result = _detect_project()
        assert result is not None
        name, path = result
        assert name == "my-project"
        assert path == proj_root


class TestMain:
    def test_writes_session_state_json(self, tmp_path):
        """main() writes a session-state.json to STATE_DIR."""
        from atlas_plus.hooks.pre_compact import main
        with patch("atlas_plus.hooks.pre_compact.STATE_DIR", tmp_path), \
             patch("atlas_plus.hooks.pre_compact.SESSION_STATE_FILE", tmp_path / "session-state.json"), \
             patch("atlas_plus.hooks.pre_compact._detect_project", return_value=None):
            main()
        state_file = tmp_path / "session-state.json"
        assert state_file.exists()

    def test_state_file_has_expected_keys(self, tmp_path):
        """main() state file always contains timestamp, project, git, summary."""
        from atlas_plus.hooks.pre_compact import main
        with patch("atlas_plus.hooks.pre_compact.STATE_DIR", tmp_path), \
             patch("atlas_plus.hooks.pre_compact.SESSION_STATE_FILE", tmp_path / "session-state.json"), \
             patch("atlas_plus.hooks.pre_compact._detect_project", return_value=None):
            main()
        data = json.loads((tmp_path / "session-state.json").read_text(encoding="utf-8"))
        assert "timestamp" in data
        assert "project" in data
        assert "git" in data
        assert "summary" in data

    def test_state_file_includes_project_info_when_detected(self, tmp_path):
        """main() populates project and summary when _detect_project returns info."""
        from atlas_plus.hooks.pre_compact import main
        fake_proj_dir = tmp_path / "atlas-memory+"
        fake_proj_dir.mkdir()
        with patch("atlas_plus.hooks.pre_compact.STATE_DIR", tmp_path), \
             patch("atlas_plus.hooks.pre_compact.SESSION_STATE_FILE", tmp_path / "session-state.json"), \
             patch("atlas_plus.hooks.pre_compact._detect_project",
                   return_value=("atlas-memory+", fake_proj_dir)), \
             patch("atlas_plus.hooks.pre_compact._git_oneliner", return_value="main | clean | last: abc"):
            main()
        data = json.loads((tmp_path / "session-state.json").read_text(encoding="utf-8"))
        assert data["project"] == "atlas-memory+"
        assert "main | clean" in data["git"]
        assert "atlas-memory+" in data["summary"]

    def test_main_exits_cleanly_on_error(self, tmp_path, monkeypatch):
        """main() entry point must not raise even when STATE_DIR write fails."""
        from atlas_plus.hooks import pre_compact as pc
        # Point SESSION_STATE_FILE to a path that can't be created
        with patch.object(pc, "SESSION_STATE_FILE", tmp_path / "nonexistent" / "state.json"), \
             patch.object(pc, "STATE_DIR", tmp_path / "nonexistent"), \
             patch("atlas_plus.hooks.pre_compact._detect_project", return_value=None):
            # Should not raise -- exits with sys.exit(0) on exception
            try:
                pc.main()
            except SystemExit as e:
                assert e.code == 0 or e.code is None
