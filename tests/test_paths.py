"""Tests for atlas_plus.paths -- central workspace path resolution."""
from __future__ import annotations

from pathlib import Path

from atlas_plus import paths


class TestWorkspaceRoot:
    def test_env_var_wins(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ATLAS_WORKSPACE_ROOT", str(tmp_path))
        assert paths.workspace_root() == tmp_path

    def test_home_is_the_only_fallback(self, monkeypatch):
        # No autodetect of any folder: the maintainer's install path must never ship as a
        # default (persona pass 2026-11-10).
        monkeypatch.delenv("ATLAS_WORKSPACE_ROOT", raising=False)
        assert paths.workspace_root() == Path.home()
        import inspect
        src = inspect.getsource(paths)
        assert "_LEGACY_WORKSPACE" not in src and "Tools" not in src


class TestDerivedPaths:
    def test_specific_env_vars_win(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ATLAS_MEMORY_ROOT", str(tmp_path / "m"))
        monkeypatch.setenv("ATLAS_STATE_DIR", str(tmp_path / "s"))
        monkeypatch.setenv("ATLAS_PROJECTS_ROOT", str(tmp_path / "p"))
        monkeypatch.setenv("ATLAS_UPDATES_FILE", str(tmp_path / "u.txt"))
        assert paths.memory_root() == tmp_path / "m"
        assert paths.state_dir() == tmp_path / "s"
        assert paths.projects_root() == tmp_path / "p"
        assert paths.updates_file() == tmp_path / "u.txt"

    def test_derive_from_workspace_root(self, tmp_path, monkeypatch):
        for var in ("ATLAS_MEMORY_ROOT", "ATLAS_STATE_DIR",
                    "ATLAS_PROJECTS_ROOT", "ATLAS_UPDATES_FILE"):
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setenv("ATLAS_WORKSPACE_ROOT", str(tmp_path))
        assert paths.memory_root() == tmp_path / ".memory"
        assert paths.state_dir() == tmp_path / ".atlas"
        assert paths.projects_root() == tmp_path / "projects"
        # the session log now defaults into the state dir, never into a file the user may own
        assert paths.updates_file() == tmp_path / ".atlas" / "updates.txt"

    def test_blank_env_var_treated_as_unset(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ATLAS_MEMORY_ROOT", "  ")
        monkeypatch.setenv("ATLAS_WORKSPACE_ROOT", str(tmp_path))
        assert paths.memory_root() == tmp_path / ".memory"


class TestSextantTwinWarning:
    """SEXTANT_MEMORY_ROOT (the natural wrong namespace guess -- every command
    is sextant-*) was silently ignored; it must warn on stderr, once, and
    still resolve the default (impatient persona, quality loop run 20)."""

    def _clear(self, monkeypatch):
        for var in ("ATLAS_MEMORY_ROOT", "SEXTANT_MEMORY_ROOT",
                    "ATLAS_WORKSPACE_ROOT", "SEXTANT_WORKSPACE_ROOT"):
            monkeypatch.delenv(var, raising=False)
        paths._warned_twins.clear()

    def test_twin_set_warns_and_falls_back(self, tmp_path, monkeypatch, capsys):
        self._clear(monkeypatch)
        monkeypatch.setenv("ATLAS_WORKSPACE_ROOT", str(tmp_path))
        monkeypatch.setenv("SEXTANT_MEMORY_ROOT", str(tmp_path / "guessed"))
        assert paths.memory_root() == tmp_path / ".memory"
        err = capsys.readouterr().err
        assert "SEXTANT_MEMORY_ROOT is set but never read" in err
        assert "ATLAS_MEMORY_ROOT" in err

    def test_warns_only_once_per_process(self, tmp_path, monkeypatch, capsys):
        self._clear(monkeypatch)
        monkeypatch.setenv("ATLAS_WORKSPACE_ROOT", str(tmp_path))
        monkeypatch.setenv("SEXTANT_MEMORY_ROOT", str(tmp_path / "guessed"))
        paths.memory_root()
        paths.memory_root()
        assert capsys.readouterr().err.count("SEXTANT_MEMORY_ROOT") == 1

    def test_no_warning_when_real_var_also_set(self, tmp_path, monkeypatch, capsys):
        self._clear(monkeypatch)
        monkeypatch.setenv("SEXTANT_MEMORY_ROOT", str(tmp_path / "guessed"))
        monkeypatch.setenv("ATLAS_MEMORY_ROOT", str(tmp_path / "real"))
        assert paths.memory_root() == tmp_path / "real"
        assert "SEXTANT_MEMORY_ROOT" not in capsys.readouterr().err

    def test_no_warning_without_twin(self, tmp_path, monkeypatch, capsys):
        self._clear(monkeypatch)
        monkeypatch.setenv("ATLAS_WORKSPACE_ROOT", str(tmp_path))
        paths.memory_root()
        assert "SEXTANT" not in capsys.readouterr().err
