"""ATL-068 (2026-11-15): ATLAS_SESSION_AUTONOMOUS is scoped to the Stop hook's writers.

On 2026-11-14 the Brain's weekly unconfirmed-decay job lowered the confidence of 47 memory notes
and the nightly tag job tagged 290 files although ATLAS_AUTONOMOUS was off: an early return added
to require_autonomous_writes() in sweep 15 (AG40) passed every write when the session variable was
on, and the shared service environment sets that variable for the hook. These tests run the fixed
paths and check the outcome on disk, not the source text.
"""
from __future__ import annotations

import pytest

from atlas_plus import autonomous


SESSION_ON_GENERAL_OFF = {"ATLAS_AUTONOMOUS": "off", "ATLAS_SESSION_AUTONOMOUS": "on"}


def _env(monkeypatch, values):
    for k, v in values.items():
        if v is None:
            monkeypatch.delenv(k, raising=False)
        else:
            monkeypatch.setenv(k, v)


class TestGeneralGateIgnoresSessionVariable:
    @pytest.mark.parametrize("source", [
        "memory_confidence_downgrade", "curation_tag_write", "inbox_create", "ticket_create",
        "curation_gap_stub", "auto_session_log",
    ])
    def test_unattended_sources_blocked_under_session_on(self, monkeypatch, source):
        _env(monkeypatch, SESSION_ON_GENERAL_OFF)
        with pytest.raises(RuntimeError, match="Sextant autonomous"):
            autonomous.require_autonomous_writes(source)

    def test_require_write_non_interactive_blocked_under_session_on(self, monkeypatch):
        _env(monkeypatch, SESSION_ON_GENERAL_OFF)
        with pytest.raises(RuntimeError, match="Sextant autonomous"):
            autonomous.require_write("memory_confidence_downgrade", interactive=False)

    def test_general_on_still_passes(self, monkeypatch):
        _env(monkeypatch, {"ATLAS_AUTONOMOUS": "on", "ATLAS_SESSION_AUTONOMOUS": "off"})
        autonomous.require_autonomous_writes("memory_confidence_downgrade")  # no raise

    def test_interactive_write_unaffected(self, monkeypatch):
        _env(monkeypatch, {"ATLAS_AUTONOMOUS": "off", "ATLAS_SESSION_AUTONOMOUS": None,
                           "ATLAS_INTERACTIVE_WRITES": None})
        autonomous.require_write("memory_remember", interactive=True)  # no raise


class TestSessionGateIsTheOnlyPlaceTheOverrideApplies:
    def test_session_on_passes_the_hook_writer(self, monkeypatch):
        _env(monkeypatch, SESSION_ON_GENERAL_OFF)
        autonomous.require_session_write("suggest_facts")  # no raise

    def test_session_off_blocks_even_with_general_on(self, monkeypatch):
        _env(monkeypatch, {"ATLAS_AUTONOMOUS": "on", "ATLAS_SESSION_AUTONOMOUS": "off"})
        with pytest.raises(RuntimeError, match="Sextant session"):
            autonomous.require_session_write("suggest_facts")

    def test_session_unset_falls_back_to_general(self, monkeypatch):
        _env(monkeypatch, {"ATLAS_AUTONOMOUS": "on", "ATLAS_SESSION_AUTONOMOUS": None})
        autonomous.require_session_write("suggest_facts")
        _env(monkeypatch, {"ATLAS_AUTONOMOUS": "off", "ATLAS_SESSION_AUTONOMOUS": None})
        with pytest.raises(RuntimeError, match="Sextant session"):
            autonomous.require_session_write("suggest_facts")

    @pytest.mark.parametrize("garbage", ["yes", "onn", " ", "On "])
    def test_garbage_session_value_is_fail_closed(self, monkeypatch, garbage):
        # "On " strips to "on" in is_session_autonomous? No: _ENABLED_VALUES is exact after strip.
        _env(monkeypatch, {"ATLAS_AUTONOMOUS": "off", "ATLAS_SESSION_AUTONOMOUS": garbage})
        if garbage.strip().lower() in autonomous._ENABLED_VALUES:
            autonomous.require_session_write("suggest_facts")
        else:
            with pytest.raises(RuntimeError, match="Sextant session"):
                autonomous.require_session_write("suggest_facts")


def _stale_note(root, rel, confidence="high"):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        "---\n"
        f"id: {p.stem}\n"
        f"confidence: {confidence}\n"
        "last_confirmed: 2025-01-01\n"
        "updated: 2025-01-01\n"
        "---\n\nA standing rule.\n",
        encoding="utf-8",
    )
    return p


class TestTheJobsThatWroteOnTheFourteenth:
    def test_decay_job_leaves_files_alone_under_session_on(self, tmp_path, monkeypatch):
        from atlas_plus.mcp.memory_server.routing import downgrade_stale_confirmations

        _env(monkeypatch, SESSION_ON_GENERAL_OFF)
        note = _stale_note(tmp_path, "feedback/rule.md")
        before = note.read_text(encoding="utf-8")
        with pytest.raises(RuntimeError, match="memory_confidence_downgrade"):
            downgrade_stale_confirmations(90, tmp_path, dry_run=False, interactive=False)
        assert note.read_text(encoding="utf-8") == before
        assert "confidence: high" in before

    def test_decay_job_dry_run_still_reports(self, tmp_path, monkeypatch):
        from atlas_plus.mcp.memory_server.routing import downgrade_stale_confirmations

        _env(monkeypatch, SESSION_ON_GENERAL_OFF)
        _stale_note(tmp_path, "feedback/rule.md")
        found = downgrade_stale_confirmations(90, tmp_path, dry_run=True, interactive=False)
        assert [c["path"] for c in found] == ["feedback/rule.md"]

    def test_decay_job_writes_when_the_general_gate_is_on(self, tmp_path, monkeypatch):
        from atlas_plus.mcp.memory_server.routing import downgrade_stale_confirmations

        _env(monkeypatch, {"ATLAS_AUTONOMOUS": "on", "ATLAS_SESSION_AUTONOMOUS": "off"})
        note = _stale_note(tmp_path, "feedback/rule.md")
        downgrade_stale_confirmations(90, tmp_path, dry_run=False, interactive=False)
        assert "confidence: low" in note.read_text(encoding="utf-8")

    def test_a_person_at_the_terminal_still_can(self, tmp_path, monkeypatch):
        from atlas_plus.mcp.memory_server.routing import downgrade_stale_confirmations

        _env(monkeypatch, {"ATLAS_AUTONOMOUS": "off", "ATLAS_SESSION_AUTONOMOUS": None,
                           "ATLAS_INTERACTIVE_WRITES": None})
        note = _stale_note(tmp_path, "feedback/rule.md")
        downgrade_stale_confirmations(90, tmp_path, dry_run=False, interactive=True)
        assert "confidence: low" in note.read_text(encoding="utf-8")


class TestTheHookWriterStillWorks:
    """The AG40 regression must stay fixed: the Stop hook's fact writer writes under the session
    override, through the primitive's explicit session path, and nothing else does."""

    def test_primitive_session_path_writes_under_session_on(self, tmp_path, monkeypatch):
        from atlas_plus.mcp.memory_server.routing import append_fact

        _env(monkeypatch, SESSION_ON_GENERAL_OFF)
        target = tmp_path / "reference" / "hook.md"
        append_fact(target, "The hook wrote this.", {"type": "reference"},
                    source="suggest-facts", session=True)
        assert "The hook wrote this." in target.read_text(encoding="utf-8")

    def test_primitive_default_path_blocked_under_session_on(self, tmp_path, monkeypatch):
        from atlas_plus.mcp.memory_server.routing import append_fact

        _env(monkeypatch, SESSION_ON_GENERAL_OFF)
        target = tmp_path / "reference" / "job.md"
        with pytest.raises(RuntimeError, match="Sextant autonomous"):
            append_fact(target, "A Brain job wrote this.", {"type": "reference"}, source="agent")
        assert not target.exists()

    def test_primitive_session_path_blocked_when_both_off(self, tmp_path, monkeypatch):
        from atlas_plus.mcp.memory_server.routing import append_fact

        _env(monkeypatch, {"ATLAS_AUTONOMOUS": "off", "ATLAS_SESSION_AUTONOMOUS": "off"})
        target = tmp_path / "reference" / "hook.md"
        with pytest.raises(RuntimeError, match="Sextant session"):
            append_fact(target, "x", {"type": "reference"}, source="suggest-facts", session=True)
        assert not target.exists()

    def test_cli_auto_mode_runs_under_session_on_and_not_when_both_off(self, monkeypatch):
        from io import StringIO
        from rich.console import Console
        import atlas_plus.brain.cli as brain_cli
        import atlas_plus.curation.suggest_facts as sf

        calls = []
        monkeypatch.setattr(sf, "suggest_facts",
                            lambda **kw: calls.append(kw) or {"candidates": 0, "approved": 0, "written": 0})

        def run(env):
            _env(monkeypatch, env)
            buf = StringIO()
            monkeypatch.setattr(brain_cli, "console", Console(file=buf, width=200, highlight=False, markup=False))
            brain_cli._handle_command("/suggest-facts --auto", "atl068")
            return buf.getvalue()

        run(SESSION_ON_GENERAL_OFF)
        assert len(calls) == 1 and calls[0]["auto"] is True
        out = run({"ATLAS_AUTONOMOUS": "off", "ATLAS_SESSION_AUTONOMOUS": "off"})
        assert len(calls) == 1
        assert "blocked" in out
