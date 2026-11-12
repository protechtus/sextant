"""P6-T5: Smoke tests for project state readers and MCP tool."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from atlas_plus.project_state.compose import compose_state
from atlas_plus.project_state.git import git_state
from atlas_plus.project_state.manifest import manifest_state
from atlas_plus.mcp.memory_server.tools.project_state import project_state


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

pytestmark = pytest.mark.skipif(__import__("shutil").which("git") is None,
                                reason="git is not installed (a git-less machine is a supported install)")


def _git_init(path: Path) -> None:
    subprocess.run(["git", "init", str(path)], capture_output=True, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"],
        cwd=str(path), capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=str(path), capture_output=True,
    )


def _git_commit(path: Path, message: str = "init") -> None:
    subprocess.run(["git", "add", "-A"], cwd=str(path), capture_output=True)
    subprocess.run(
        ["git", "commit", "--allow-empty", "-m", message],
        cwd=str(path), capture_output=True,
    )


# ---------------------------------------------------------------------------
# P6-T1: git_state
# ---------------------------------------------------------------------------

class TestGitState:
    def test_not_a_git_repo(self, tmp_path):
        result = git_state(tmp_path)
        assert result == {"is_git_repo": False}

    def test_clean_repo(self, tmp_path):
        _git_init(tmp_path)
        _git_commit(tmp_path)
        result = git_state(tmp_path)
        assert result["is_git_repo"] is True
        assert result["dirty"] is False
        assert result["changed_files"] == []
        assert isinstance(result["branch"], str)
        assert len(result["recent_commits"]) >= 1
        assert "oid" in result["recent_commits"][0]
        assert "message" in result["recent_commits"][0]

    def test_dirty_repo(self, tmp_path):
        _git_init(tmp_path)
        _git_commit(tmp_path)
        (tmp_path / "dirty.txt").write_text("change")
        result = git_state(tmp_path)
        assert result["dirty"] is True
        assert any("dirty.txt" in f for f in result["changed_files"])

    def test_no_upstream_returns_none(self, tmp_path):
        _git_init(tmp_path)
        _git_commit(tmp_path)
        result = git_state(tmp_path)
        # No remote configured — ahead/behind should be None
        assert result["ahead"] is None
        assert result["behind"] is None

    def test_recent_commits_capped_at_5(self, tmp_path):
        _git_init(tmp_path)
        for i in range(10):
            _git_commit(tmp_path, f"commit {i}")
        result = git_state(tmp_path)
        assert len(result["recent_commits"]) == 5


# ---------------------------------------------------------------------------
# P6-T2: manifest_state
# ---------------------------------------------------------------------------

class TestManifestState:
    def test_empty_directory(self, tmp_path):
        result = manifest_state(tmp_path)
        assert result["detected"] == []
        assert result["lang"] is None

    def test_pyproject_toml(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "myapp"\nversion = "0.1.0"\n'
            'dependencies = ["requests>=2.0"]\n'
            '[project.scripts]\nmyapp = "myapp:main"\n',
            encoding="utf-8",
        )
        result = manifest_state(tmp_path)
        assert "pyproject.toml" in result["detected"]
        assert result["lang"] == "python"
        assert "requests>=2.0" in result["pyproject"]["deps"]
        assert "myapp" in result["pyproject"]["scripts"]

    def test_requirements_txt(self, tmp_path):
        (tmp_path / "requirements.txt").write_text(
            "requests==2.31.0\n# comment\nflask>=3.0\n",
            encoding="utf-8",
        )
        result = manifest_state(tmp_path)
        assert "requirements.txt" in result["detected"]
        assert result["lang"] == "python"
        assert "requests==2.31.0" in result["requirements"]
        assert "flask>=3.0" in result["requirements"]

    def test_package_json(self, tmp_path):
        pkg = {
            "name": "myapp",
            "dependencies": {"express": "^4.18.0"},
            "devDependencies": {"jest": "^29.0.0"},
            "scripts": {"test": "jest"},
        }
        (tmp_path / "package.json").write_text(json.dumps(pkg), encoding="utf-8")
        result = manifest_state(tmp_path)
        assert "package.json" in result["detected"]
        assert result["lang"] == "node"
        assert "express" in result["package_json"]["deps"]
        assert "jest" in result["package_json"]["dev_deps"]

    def test_uv_lock_detected(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "x"\ndependencies = []\n', encoding="utf-8"
        )
        (tmp_path / "uv.lock").write_text("# lock file\n", encoding="utf-8")
        result = manifest_state(tmp_path)
        assert "uv.lock" in result["detected"]


# ---------------------------------------------------------------------------
# P6-T3: compose_state
# ---------------------------------------------------------------------------

class TestComposeState:
    def test_no_compose_file(self, tmp_path):
        result = compose_state(tmp_path)
        assert result == {"has_compose": False}

    def test_compose_file_present_docker_unavailable(self, tmp_path):
        (tmp_path / "docker-compose.yml").write_text("services:\n  web:\n    image: nginx\n")
        with patch("atlas_plus.project_state.compose.subprocess.run") as mock_run:
            mock_run.side_effect = FileNotFoundError("docker not found")
            result = compose_state(tmp_path)
        assert result["has_compose"] is True
        assert result["compose_file"] == "docker-compose.yml"
        assert result["docker_unavailable"] is True
        assert result["running"] is False

    def test_compose_file_present_services_running(self, tmp_path):
        (tmp_path / "compose.yml").write_text("services:\n  app:\n    image: myapp\n")
        service_json = json.dumps({"Name": "app", "State": "running", "Status": "Up"})
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = service_json + "\n"
        with patch("atlas_plus.project_state.compose.subprocess.run", return_value=mock_proc):
            result = compose_state(tmp_path)
        assert result["has_compose"] is True
        assert result["compose_file"] == "compose.yml"
        assert result["running"] is True
        assert len(result["services"]) == 1

    def test_compose_file_present_services_stopped(self, tmp_path):
        (tmp_path / "docker-compose.yaml").write_text("services:\n  db:\n    image: postgres\n")
        service_json = json.dumps({"Name": "db", "State": "exited", "Status": "Exited (0)"})
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = service_json + "\n"
        with patch("atlas_plus.project_state.compose.subprocess.run", return_value=mock_proc):
            result = compose_state(tmp_path)
        assert result["running"] is False


# ---------------------------------------------------------------------------
# P6-T4: project_state MCP tool
# ---------------------------------------------------------------------------

class TestProjectStateTool:
    def test_by_absolute_path(self, tmp_path, monkeypatch):
        # Monkeypatch PROJECTS_ROOT so the security whitelist accepts tmp_path
        import atlas_plus.mcp.memory_server.tools.project_state as ps_mod
        monkeypatch.setattr(ps_mod, "_PROJECTS_ROOT", tmp_path.parent)
        ps_mod._CACHE.clear()

        _git_init(tmp_path)
        _git_commit(tmp_path)
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "proj"\ndependencies = []\n', encoding="utf-8"
        )
        result = project_state(str(tmp_path))
        assert result["project"] == tmp_path.name
        assert result["git"]["is_git_repo"] is True
        assert "pyproject.toml" in result["manifest"]["detected"]
        assert result["compose"]["has_compose"] is False

    def test_error_on_unknown_name(self):
        result = project_state("__nonexistent_project_xyz__")
        assert "error" in result

    def test_cache_returns_cached_flag(self, tmp_path, monkeypatch):
        import atlas_plus.mcp.memory_server.tools.project_state as ps_mod
        monkeypatch.setattr(ps_mod, "_PROJECTS_ROOT", tmp_path.parent)
        ps_mod._CACHE.clear()

        _git_init(tmp_path)
        _git_commit(tmp_path)
        # First call — not cached
        r1 = project_state(str(tmp_path))
        assert r1["cached"] is False
        # Second call within TTL — cached
        r2 = project_state(str(tmp_path))
        assert r2["cached"] is True

    def test_no_name_uses_cwd(self, tmp_path, monkeypatch):
        _git_init(tmp_path)
        _git_commit(tmp_path)
        monkeypatch.chdir(tmp_path)
        # Clear cache so we get a fresh call
        import atlas_plus.mcp.memory_server.tools.project_state as ps_mod
        ps_mod._CACHE.clear()
        result = project_state(None)
        assert result["git"]["is_git_repo"] is True
