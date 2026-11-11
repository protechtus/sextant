"""Tests for atlas_plus.uninstall_cmd -- the safety model IS the feature."""
from __future__ import annotations

import pytest

from atlas_plus import uninstall_cmd


@pytest.fixture
def world(tmp_path, monkeypatch):
    corpus = tmp_path / "mem"
    state = tmp_path / "state"
    (corpus / "identity").mkdir(parents=True)
    (corpus / "identity" / "user.md").write_text("# me", encoding="utf-8")
    (state / "logs").mkdir(parents=True)
    (state / "logs" / "x.log").write_text("log", encoding="utf-8")
    (state / ".sextant-state").write_text("", encoding="utf-8")
    monkeypatch.setenv("ATLAS_MEMORY_ROOT", str(corpus))
    monkeypatch.setenv("ATLAS_STATE_DIR", str(state))
    return corpus, state


class TestReport:
    def test_inventories_both_dirs(self, world):
        corpus, state = world
        info = uninstall_cmd.report()
        assert info["corpus"]["exists"] and info["corpus"]["files"] == 1
        assert info["state"]["exists"] and info["state"]["files"] == 2   # x.log + .sextant-state marker
        assert info["corpus"]["protected"] is False

    def test_absent_dirs_reported(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ATLAS_MEMORY_ROOT", str(tmp_path / "no-mem"))
        monkeypatch.setenv("ATLAS_STATE_DIR", str(tmp_path / "no-state"))
        info = uninstall_cmd.report()
        assert info["corpus"]["exists"] is False
        assert info["state"]["exists"] is False


class TestRemoveState:
    def test_requires_yes(self, world):
        _, state = world
        assert uninstall_cmd.remove_state(yes=False) == "refused: --yes required"
        assert state.exists()

    def test_removes_with_yes(self, world):
        _, state = world
        assert uninstall_cmd.remove_state(yes=True, confirm_path=str(world[1])) == "removed"
        assert not state.exists()

    def test_absent_is_absent(self, world, monkeypatch, tmp_path):
        monkeypatch.setenv("ATLAS_STATE_DIR", str(tmp_path / "gone"))
        assert uninstall_cmd.remove_state(yes=True) == "absent"


class TestRemoveCorpus:
    def test_requires_yes_even_with_confirm_path(self, world):
        corpus, _ = world
        outcome = uninstall_cmd.remove_corpus(yes=False, confirm_path=str(corpus))
        assert outcome == "refused: --yes required"
        assert corpus.exists()

    def test_requires_exact_path_confirmation(self, world):
        corpus, _ = world
        outcome = uninstall_cmd.remove_corpus(yes=True, confirm_path="wrong/path")
        assert outcome == "refused: confirmation path did not match"
        assert corpus.exists()

    def test_interactive_confirmation_must_match(self, world):
        corpus, _ = world
        outcome = uninstall_cmd.remove_corpus(
            yes=True, confirm_path=None, ask=lambda _prompt: "nope"
        )
        assert outcome.startswith("refused")
        assert corpus.exists()

    def test_removes_with_exact_confirmation(self, world):
        corpus, _ = world
        outcome = uninstall_cmd.remove_corpus(yes=True, confirm_path=str(corpus))
        assert outcome == "removed"
        assert not corpus.exists()

    def test_interactive_exact_match_removes(self, world):
        corpus, _ = world
        outcome = uninstall_cmd.remove_corpus(
            yes=True, confirm_path=None, ask=lambda _prompt: str(corpus)
        )
        assert outcome == "removed"
        assert not corpus.exists()

    def test_protect_marker_refuses_unconditionally(self, world):
        corpus, _ = world
        (corpus / uninstall_cmd.PROTECT_MARKER).write_text("", encoding="utf-8")
        outcome = uninstall_cmd.remove_corpus(yes=True, confirm_path=str(corpus))
        assert outcome.startswith("REFUSED")
        assert corpus.exists()
        assert (corpus / "identity" / "user.md").exists()


class TestMainCli:
    def test_default_is_report_only(self, world, monkeypatch, capsys):
        corpus, state = world
        monkeypatch.setattr("sys.argv", ["atlas-uninstall"])
        uninstall_cmd.main()
        out = capsys.readouterr().out
        assert "Report only. Nothing was changed." in out
        assert corpus.exists() and state.exists()

    def test_all_with_confirmations(self, world, monkeypatch, capsys):
        corpus, state = world
        monkeypatch.setattr(
            "sys.argv",
            ["atlas-uninstall", "--all", "--yes", "--confirm-path", str(corpus), "--confirm-state-path", str(state)],
        )
        with pytest.raises(SystemExit) as exc:
            uninstall_cmd.main()
        assert exc.value.code == 0
        assert not corpus.exists() and not state.exists()

    def test_refusals_exit_nonzero(self, world, monkeypatch):
        corpus, _ = world
        monkeypatch.setattr("sys.argv", ["atlas-uninstall", "--corpus"])
        with pytest.raises(SystemExit) as exc:
            uninstall_cmd.main()
        assert exc.value.code == 1
        assert corpus.exists()


class TestTwoPhaseTeardown:
    """Codex artifact audit P1-B: no partial mutation on refused --all."""

    def test_all_with_wrong_confirm_path_deletes_nothing(self, world, monkeypatch):
        corpus, state = world
        monkeypatch.setattr(
            "sys.argv",
            ["sextant-uninstall", "--all", "--yes", "--confirm-path", "wrong"],
        )
        import pytest as _pytest
        with _pytest.raises(SystemExit) as exc:
            uninstall_cmd.main()
        assert exc.value.code == 1
        assert corpus.exists() and state.exists()  # NOTHING deleted

    def test_all_noninteractive_eof_deletes_nothing(self, world, monkeypatch):
        corpus, state = world
        monkeypatch.setattr("sys.argv", ["sextant-uninstall", "--all", "--yes"])

        def eof(_prompt):
            raise EOFError

        monkeypatch.setattr("builtins.input", eof)
        import pytest as _pytest
        with _pytest.raises(SystemExit) as exc:
            uninstall_cmd.main()
        assert exc.value.code == 1
        assert corpus.exists() and state.exists()

    def test_eof_message_is_clean_refusal(self, world, monkeypatch, capsys):
        corpus, _ = world

        def eof(_prompt):
            raise EOFError

        outcome = uninstall_cmd.confirm_corpus_removal(True, None, ask=eof)
        assert outcome and "confirm-path" in outcome and "Traceback" not in outcome


class TestConfirmPathNormalization:
    """Forward-slash and case variants of the exact corpus path confirm it
    (the user demonstrably knows which corpus); other paths still refuse."""

    def test_forward_slash_form_accepted(self, tmp_path, monkeypatch):
        corpus = tmp_path / "mem"
        corpus.mkdir()
        monkeypatch.setenv("ATLAS_MEMORY_ROOT", str(corpus))
        from atlas_plus import uninstall_cmd
        fwd = str(corpus).replace("\\", "/")
        assert uninstall_cmd.confirm_corpus_removal(True, fwd) is None

    def test_different_path_still_refused(self, tmp_path, monkeypatch):
        corpus = tmp_path / "mem"
        corpus.mkdir()
        monkeypatch.setenv("ATLAS_MEMORY_ROOT", str(corpus))
        from atlas_plus import uninstall_cmd
        refusal = uninstall_cmd.confirm_corpus_removal(True, str(tmp_path / "other"))
        assert refusal and "did not match" in refusal


class TestReportCoversResidue:
    """2026-10-16 SRE-persona fixes: the report must surface the session log
    and client-config references, and must never delete either."""

    def test_report_has_session_log_and_client_config_keys(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ATLAS_MEMORY_ROOT", str(tmp_path / "mem"))
        monkeypatch.setenv("ATLAS_STATE_DIR", str(tmp_path / "state"))
        from atlas_plus import uninstall_cmd as uc

        info = uc.report()
        assert "session_log" in info
        assert set(info["session_log"]) == {"path", "exists", "versions_dir", "version_copies"}
        assert isinstance(info["client_configs"], list)

    def test_session_log_version_copies_counted(self, tmp_path, monkeypatch):
        log = tmp_path / "updates.txt"
        log.write_text("x", encoding="utf-8")
        (tmp_path / "versions").mkdir()
        (tmp_path / "versions" / "updates_2026-10-16_v1.txt").write_text("x", encoding="utf-8")
        monkeypatch.setenv("ATLAS_UPDATES_FILE", str(log))
        from atlas_plus import uninstall_cmd as uc

        info = uc.report()
        assert info["session_log"]["exists"] is True
        assert info["session_log"]["version_copies"] == 1

    def test_client_config_scan_never_raises(self, monkeypatch):
        from atlas_plus import uninstall_cmd as uc

        hits = uc._client_config_references()
        assert isinstance(hits, list)


class TestStateIsNotACache:
    """Data-safety audit 2026-11-09: the state dir holds the only copy of transcripts, the task
    DB, the intake ledger, sync devices and research reports. Removing it takes the typed path."""

    def test_state_removal_needs_the_typed_path(self, world):
        _, state = world
        out = uninstall_cmd.remove_state(yes=True, confirm_path="wrong")
        assert out.startswith("refused") and state.exists()

    def test_state_without_marker_is_refused(self, tmp_path, monkeypatch):
        corpus = tmp_path / "mem"; corpus.mkdir()
        state = tmp_path / "Documents"; state.mkdir(); (state / "thesis.docx").write_text("x", encoding="utf-8")
        monkeypatch.setenv("ATLAS_MEMORY_ROOT", str(corpus)); monkeypatch.setenv("ATLAS_STATE_DIR", str(state))
        out = uninstall_cmd.remove_state(yes=True, confirm_path=str(state))
        assert out.startswith("REFUSED") and ".sextant-state" in out
        assert (state / "thesis.docx").exists()

    def test_report_lists_non_derived_contents(self, world, capsys, monkeypatch):
        _, state = world
        (state / "transcripts").mkdir(); (state / "transcripts" / "a.ndjson").write_text("{}", encoding="utf-8")
        (state / "state.sqlite").write_text("", encoding="utf-8")
        monkeypatch.setattr("sys.argv", ["atlas-uninstall"])
        uninstall_cmd.main()
        out = capsys.readouterr().out
        assert "not rebuildable" in out and "transcripts" in out and "state.sqlite" in out
