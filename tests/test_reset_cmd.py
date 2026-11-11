"""Tests for atlas_plus.reset_cmd -- uninstall's safety + init's scaffold, composed."""
from __future__ import annotations

import pytest

from atlas_plus import reset_cmd, uninstall_cmd


@pytest.fixture
def world(tmp_path, monkeypatch):
    corpus = tmp_path / "mem"
    state = tmp_path / "state"
    (corpus / "projects").mkdir(parents=True)
    (corpus / "projects" / "old-work.md").write_text("history", encoding="utf-8")
    (state / "logs").mkdir(parents=True)
    (state / "logs" / "x.log").write_text("log", encoding="utf-8")
    (state / ".sextant-state").write_text("", encoding="utf-8")
    # a real state dir carries Sextant's own entries, not just the marker (round B 2026-11-09)
    (state / "embeddings").mkdir(); (state / "embeddings" / "index.npy").write_bytes(b"0")
    monkeypatch.setenv("ATLAS_MEMORY_ROOT", str(corpus))
    monkeypatch.setenv("ATLAS_STATE_DIR", str(state))
    monkeypatch.setenv("ATLAS_WORKSPACE_ROOT", str(tmp_path))
    return corpus, state


class TestResetState:
    def test_requires_yes(self, world):
        _, state = world
        assert reset_cmd.reset_state(yes=False).startswith("refused")
        assert (state / "logs" / "x.log").exists()

    def test_clears_derived_entries_only(self, world):
        _, state = world
        (state / "transcripts").mkdir(); (state / "transcripts" / "keep.ndjson").write_text("{}", encoding="utf-8")
        (state / "state.sqlite").write_text("tasks", encoding="utf-8")
        (state / "temporal_graph").mkdir(); (state / "temporal_graph" / "g.jsonl").write_text("", encoding="utf-8")
        outcome = reset_cmd.reset_state(yes=True)
        assert outcome.startswith("reset")
        assert state.exists()
        assert not (state / "embeddings").exists() and not (state / "temporal_graph").exists()
        assert (state / "logs" / "x.log").exists()                        # activity log is an only copy too
        assert (state / "transcripts" / "keep.ndjson").exists()          # only copy of transcripts
        assert (state / "state.sqlite").read_text(encoding="utf-8") == "tasks"
        assert (state / ".sextant-state").exists()


class TestResetCorpus:
    def test_requires_exact_path(self, world):
        corpus, _ = world
        outcome = reset_cmd.reset_corpus(yes=True, confirm_path="wrong")
        assert outcome.startswith("refused")
        assert (corpus / "projects" / "old-work.md").exists()

    def test_protect_marker_refuses(self, world):
        corpus, _ = world
        (corpus / uninstall_cmd.PROTECT_MARKER).write_text("", encoding="utf-8")
        outcome = reset_cmd.reset_corpus(yes=True, confirm_path=str(corpus))
        assert outcome.startswith("REFUSED")
        assert (corpus / "projects" / "old-work.md").exists()

    def test_reset_replaces_with_fresh_skeleton(self, world):
        corpus, _ = world
        outcome = reset_cmd.reset_corpus(yes=True, confirm_path=str(corpus))
        assert outcome.startswith("reset")
        assert not (corpus / "projects" / "old-work.md").exists()
        assert (corpus / "identity" / "user.md").exists()  # init skeleton is back
        assert (corpus / "sessions").is_dir()


class TestMainCli:
    def test_no_args_explains_and_changes_nothing(self, world, monkeypatch, capsys):
        corpus, state = world
        monkeypatch.setattr("sys.argv", ["atlas-reset"])
        reset_cmd.main()
        out = capsys.readouterr().out
        assert "nothing selected" in out
        assert (corpus / "projects" / "old-work.md").exists()
        assert (state / "logs" / "x.log").exists()

    def test_all_resets_both(self, world, monkeypatch):
        corpus, state = world
        monkeypatch.setattr(
            "sys.argv", ["atlas-reset", "--all", "--yes", "--confirm-path", str(corpus)]
        )
        with pytest.raises(SystemExit) as exc:
            reset_cmd.main()
        assert exc.value.code == 0
        assert (corpus / "identity" / "user.md").exists()
        assert sorted(q.name for q in state.iterdir()) == [".sextant-state", "logs"]   # derived cleared; marker + logs kept

    def test_refusal_exits_nonzero(self, world, monkeypatch):
        monkeypatch.setattr("sys.argv", ["atlas-reset", "--corpus"])
        with pytest.raises(SystemExit) as exc:
            reset_cmd.main()
        assert exc.value.code == 1


class TestTwoPhaseReset:
    def test_all_with_wrong_confirm_path_resets_nothing(self, world, monkeypatch):
        corpus, state = world
        monkeypatch.setattr(
            "sys.argv", ["sextant-reset", "--all", "--yes", "--confirm-path", "wrong"]
        )
        with pytest.raises(SystemExit) as exc:
            reset_cmd.main()
        assert exc.value.code == 1
        assert (corpus / "projects" / "old-work.md").exists()   # corpus intact
        assert (state / "logs" / "x.log").exists()               # state intact
