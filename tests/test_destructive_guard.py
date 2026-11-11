"""The central destructive-target guard (misuse audit 2026-11-09).

A misconfigured ATLAS_STATE_DIR used to let `--state --yes` delete the memory corpus (and
anything else under a broad state path) because state removal only checked existence and
consent. Every case here uses tmp fixtures only.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from atlas_plus import reset_cmd, uninstall_cmd
from atlas_plus.uninstall_cmd import PROTECT_MARKER, validate_destructive_target


def _corpus(root: Path, protected: bool = False) -> Path:
    c = root / "memory"
    (c / "projects").mkdir(parents=True)
    (c / "projects" / "note.md").write_text("precious\n", encoding="utf-8")
    if protected:
        (c / PROTECT_MARKER).write_text("", encoding="utf-8")
    return c


def _mark_state(state: Path) -> Path:
    state.mkdir(parents=True, exist_ok=True)
    (state / ".sextant-state").write_text("", encoding="utf-8")
    return state


def _env(monkeypatch, corpus: Path, state: Path, workspace: Path | None = None):
    monkeypatch.setenv("ATLAS_MEMORY_ROOT", str(corpus))
    monkeypatch.setenv("ATLAS_STATE_DIR", str(state))
    monkeypatch.setenv("ATLAS_WORKSPACE_ROOT", str(workspace or corpus.parent / "ws"))
    monkeypatch.delenv("ATLAS_PROJECTS_ROOT", raising=False)


class TestGuard:
    def test_separate_state_is_allowed(self, tmp_path, monkeypatch):
        corpus = _corpus(tmp_path)
        state = _mark_state(tmp_path / "state")
        _env(monkeypatch, corpus, state)
        assert validate_destructive_target("state", state) is None

    def test_state_equal_to_corpus_refused(self, tmp_path, monkeypatch):
        corpus = _corpus(tmp_path)
        _env(monkeypatch, corpus, corpus)
        assert "REFUSED" in (validate_destructive_target("state", corpus) or "")

    def test_state_ancestor_of_corpus_refused(self, tmp_path, monkeypatch):
        corpus = _corpus(tmp_path)
        _env(monkeypatch, corpus, tmp_path)  # state is the parent of the corpus
        msg = validate_destructive_target("state", tmp_path)
        assert msg and "contains the" in msg   # it contains the corpus (and the workspace root)

    def test_state_inside_corpus_refused(self, tmp_path, monkeypatch):
        corpus = _corpus(tmp_path)
        inner = corpus / "state"; inner.mkdir()
        _env(monkeypatch, corpus, inner)
        msg = validate_destructive_target("state", inner)
        assert msg and "inside the memory corpus" in msg

    def test_home_refused(self, tmp_path, monkeypatch):
        corpus = _corpus(tmp_path)
        home = tmp_path / "home"; home.mkdir()
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
        _env(monkeypatch, corpus, home)
        msg = validate_destructive_target("state", home)
        assert msg and "home directory" in msg

    def test_workspace_root_refused(self, tmp_path, monkeypatch):
        corpus = _corpus(tmp_path)
        ws = tmp_path / "ws"; ws.mkdir()
        _env(monkeypatch, corpus, ws, workspace=ws)
        msg = validate_destructive_target("state", ws)
        assert msg and "workspace root" in msg

    def test_filesystem_root_refused(self, tmp_path, monkeypatch):
        corpus = _corpus(tmp_path)
        _env(monkeypatch, corpus, tmp_path / "state")
        root = Path(tmp_path.anchor)
        msg = validate_destructive_target("state", root)
        assert msg and "filesystem root" in msg

    def test_marker_inside_state_refused(self, tmp_path, monkeypatch):
        corpus = _corpus(tmp_path)
        state = tmp_path / "state"; state.mkdir()
        (state / PROTECT_MARKER).write_text("", encoding="utf-8")
        _env(monkeypatch, corpus, state)
        msg = validate_destructive_target("state", state)
        assert msg and PROTECT_MARKER in msg

    def test_relative_and_dotted_spellings_resolve(self, tmp_path, monkeypatch):
        corpus = _corpus(tmp_path)
        _env(monkeypatch, corpus, corpus)
        spelled = corpus.parent / "memory" / "." / "projects" / ".."
        assert "REFUSED" in (validate_destructive_target("state", spelled) or "")


class TestRemoveState:
    def test_overlap_refuses_even_with_yes_and_keeps_notes(self, tmp_path, monkeypatch):
        corpus = _corpus(tmp_path, protected=True)
        _env(monkeypatch, corpus, corpus)
        out = uninstall_cmd.remove_state(yes=True)
        assert out.startswith("REFUSED")
        assert (corpus / "projects" / "note.md").read_text(encoding="utf-8") == "precious\n"
        assert (corpus / PROTECT_MARKER).exists()

    def test_reset_state_inherits_the_guard(self, tmp_path, monkeypatch):
        corpus = _corpus(tmp_path)
        _env(monkeypatch, corpus, tmp_path)  # state = parent of corpus
        out = reset_cmd.reset_state(yes=True)
        assert out.startswith("REFUSED")
        assert (corpus / "projects" / "note.md").exists()

    def test_separate_state_still_removed(self, tmp_path, monkeypatch):
        corpus = _corpus(tmp_path)
        state = _mark_state(tmp_path / "state"); (state / "x.db").write_text("", encoding="utf-8")
        _env(monkeypatch, corpus, state)
        assert uninstall_cmd.remove_state(yes=True, confirm_path=str(state)) == "removed"
        assert not state.exists() and (corpus / "projects" / "note.md").exists()


class TestCliTwoPhase:
    def test_all_with_unsafe_state_changes_nothing(self, tmp_path, monkeypatch, capsys):
        corpus = _corpus(tmp_path)
        _env(monkeypatch, corpus, tmp_path)  # unsafe state (ancestor of corpus)
        monkeypatch.setattr("sys.argv", ["atlas-uninstall", "--all", "--yes", "--confirm-path", str(corpus)])
        with pytest.raises(SystemExit) as e:
            uninstall_cmd.main()
        assert e.value.code == 1
        assert (corpus / "projects" / "note.md").exists()
        assert "REFUSED" in capsys.readouterr().out

    def test_reset_all_with_unsafe_state_changes_nothing(self, tmp_path, monkeypatch, capsys):
        corpus = _corpus(tmp_path)
        _env(monkeypatch, corpus, corpus)
        monkeypatch.setattr("sys.argv", ["atlas-reset", "--all", "--yes", "--confirm-path", str(corpus)])
        with pytest.raises(SystemExit) as e:
            reset_cmd.main()
        assert e.value.code == 1
        assert (corpus / "projects" / "note.md").exists()


class TestPositiveGuard:
    """Audit rows 2 and 22: the guard must be positive and must refuse ancestors."""

    def test_unmarked_directory_refused(self, tmp_path, monkeypatch):
        corpus = _corpus(tmp_path)
        docs = tmp_path / "Documents"; docs.mkdir(); (docs / "thesis.md").write_text("x", encoding="utf-8")
        _env(monkeypatch, corpus, docs)
        msg = validate_destructive_target("state", docs)
        assert msg and ".sextant-state" in msg

    def test_parent_of_home_refused(self, tmp_path, monkeypatch):
        corpus = _corpus(tmp_path)
        home = tmp_path / "users" / "me"; home.mkdir(parents=True)
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
        target = _mark_state(tmp_path / "users")   # even with a marker: it contains home
        _env(monkeypatch, corpus, target)
        msg = validate_destructive_target("state", target)
        assert msg and "contains the home directory" in msg

    def test_unc_spelling_refused(self, tmp_path, monkeypatch):
        corpus = _corpus(tmp_path)
        state = _mark_state(tmp_path / "state")
        _env(monkeypatch, corpus, state)
        msg = validate_destructive_target("state", Path("//localhost/C$/anything"))
        assert msg and "UNC" in msg

    def test_corpus_ancestor_of_home_refused(self, tmp_path, monkeypatch):
        home = tmp_path / "home"; home.mkdir()
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
        state = _mark_state(tmp_path / "state")
        _env(monkeypatch, tmp_path, state, workspace=tmp_path / "ws")
        msg = validate_destructive_target("corpus", tmp_path)
        assert msg and "contains the" in msg
