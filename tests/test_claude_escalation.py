"""Tests for the Claude Code escalation layer (phases 1-4).

Covers: escalation.py logic, server-path escalation hooks, worker-path
escalation, supervisor executor, and scheduler registration.
"""
from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Phase 1+3: escalation.py -- helper functions
# ---------------------------------------------------------------------------

class TestShouldEscalateFailure:
    def test_enabled_agent_task_escalates(self):
        from atlas_plus.brain.escalation import should_escalate_failure
        with patch.dict("os.environ", {"ATLAS_CLAUDE_ESCALATION": "on"}):
            assert should_escalate_failure("brain", "sess-123", "agent") is True

    def test_disabled_by_default(self):
        from atlas_plus.brain.escalation import should_escalate_failure
        with patch.dict("os.environ", {"ATLAS_CLAUDE_ESCALATION": "off"}):
            assert should_escalate_failure("brain", "sess-123", "agent") is False

    def test_claude_code_agent_never_escalates(self):
        from atlas_plus.brain.escalation import should_escalate_failure
        with patch.dict("os.environ", {"ATLAS_CLAUDE_ESCALATION": "on"}):
            assert should_escalate_failure("claude-code", "sess-123", "agent") is False

    def test_escalated_session_never_escalates(self):
        from atlas_plus.brain.escalation import should_escalate_failure, ESCALATION_SESSION_PREFIX
        with patch.dict("os.environ", {"ATLAS_CLAUDE_ESCALATION": "on"}):
            assert should_escalate_failure("brain", f"{ESCALATION_SESSION_PREFIX}sess", "agent") is False

    def test_non_agent_kind_never_escalates(self):
        from atlas_plus.brain.escalation import should_escalate_failure
        with patch.dict("os.environ", {"ATLAS_CLAUDE_ESCALATION": "on"}):
            assert should_escalate_failure("brain", "sess-123", "temporal_build") is False
            assert should_escalate_failure("brain", "sess-123", "curation_report") is False

    def test_verify_kind_never_escalates(self):
        """verify failures are handled by reopen_ticket -- escalating would create extra CC tasks."""
        from atlas_plus.brain.escalation import should_escalate_failure
        with patch.dict("os.environ", {"ATLAS_CLAUDE_ESCALATION": "on"}):
            assert should_escalate_failure("brain", "verify-ticket-ATL-007", "verify") is False

    def test_claude_supervisor_kind_never_escalates(self):
        """claude_supervisor IS the escalation target; escalating it would loop."""
        from atlas_plus.brain.escalation import should_escalate_failure
        with patch.dict("os.environ", {"ATLAS_CLAUDE_ESCALATION": "on"}):
            assert should_escalate_failure("brain", "sess-123", "claude_supervisor") is False


class TestShouldEscalateQuality:
    def test_low_quality_triggers_when_enabled(self):
        from atlas_plus.brain.escalation import should_escalate_quality
        with patch.dict("os.environ", {
            "ATLAS_CLAUDE_QUALITY_ESCALATION": "on",
            "ATLAS_CLAUDE_QUALITY_THRESHOLD": "4.0",
        }):
            assert should_escalate_quality("brain", "sess", "agent", 2.5) is True

    def test_high_quality_does_not_trigger(self):
        from atlas_plus.brain.escalation import should_escalate_quality
        with patch.dict("os.environ", {
            "ATLAS_CLAUDE_QUALITY_ESCALATION": "on",
            "ATLAS_CLAUDE_QUALITY_THRESHOLD": "4.0",
        }):
            assert should_escalate_quality("brain", "sess", "agent", 7.5) is False

    def test_disabled_by_default(self):
        from atlas_plus.brain.escalation import should_escalate_quality
        with patch.dict("os.environ", {"ATLAS_CLAUDE_QUALITY_ESCALATION": "off"}):
            assert should_escalate_quality("brain", "sess", "agent", 1.0) is False


class TestCreateEscalationTask:
    def test_creates_task_with_correct_fields(self):
        from atlas_plus.brain.escalation import create_escalation_task, ESCALATION_SESSION_PREFIX

        mock_task = MagicMock()
        mock_task.task_id = "esc-new-001"

        with patch("atlas_plus.brain.tasks.create_task", return_value=mock_task) as mock_create:
            result = create_escalation_task(
                "original-001", "sess-abc", "Write a plan", "failed"
            )

        assert result == "esc-new-001"
        call_kwargs = mock_create.call_args[1]
        assert call_kwargs["agent"] == "claude-code"
        assert call_kwargs["priority"] == 1
        assert call_kwargs["session_id"].startswith(ESCALATION_SESSION_PREFIX)
        assert "original-001" in call_kwargs["instruction"]

    def test_returns_none_on_exception(self):
        from atlas_plus.brain.escalation import create_escalation_task
        with patch("atlas_plus.brain.tasks.create_task", side_effect=RuntimeError("db error")):
            result = create_escalation_task("t1", "sess", "instruction", "failed")
        assert result is None

    def test_original_result_included_in_instruction(self):
        from atlas_plus.brain.escalation import build_escalation_instruction
        instr = build_escalation_instruction("t1", "do a thing", "failed", "original output here")
        assert "original output here" in instr
        assert "t1" in instr
        assert "do a thing" in instr


# ---------------------------------------------------------------------------
# Phase 1: worker.py escalation hook
# ---------------------------------------------------------------------------

class TestWorkerEscalation:
    def test_failed_task_triggers_escalation_when_enabled(self):
        from atlas_plus.brain.worker import _finish_task
        from atlas_plus.brain.executors import ExecutionResult

        mock_task = MagicMock()
        mock_task.agent = "brain"
        mock_task.session_id = "sess-worker"
        mock_task.kind = "agent"
        mock_task.instruction = "do a thing"

        result = ExecutionResult(status="failed", text="brain error")

        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "state.sqlite"
            with patch("atlas_plus.brain.tasks.mark_failed"), \
                 patch("atlas_plus.brain.tasks.record_task_attempt"), \
                 patch.dict("os.environ", {"ATLAS_CLAUDE_ESCALATION": "on"}), \
                 patch("atlas_plus.brain.escalation.create_escalation_task",
                       return_value="esc-new") as mock_esc:
                _finish_task("t1", result, db_path=db, task=mock_task)

        mock_esc.assert_called_once()
        call_args = mock_esc.call_args
        assert call_args[0][0] == "t1"  # original_task_id
        assert "failed" in call_args[0][3].lower()

    def test_done_task_does_not_escalate(self):
        from atlas_plus.brain.worker import _finish_task
        from atlas_plus.brain.executors import ExecutionResult

        mock_task = MagicMock()
        mock_task.agent = "brain"
        mock_task.session_id = "sess-worker"
        mock_task.kind = "agent"

        result = ExecutionResult(status="done", text="success")

        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "state.sqlite"
            with patch("atlas_plus.brain.tasks.mark_done"), \
                 patch("atlas_plus.brain.tasks.record_task_attempt"), \
                 patch("atlas_plus.brain.dispatcher.reopen_ticket_if_low_quality"), \
                 patch.dict("os.environ", {"ATLAS_CLAUDE_ESCALATION": "on"}), \
                 patch("atlas_plus.brain.escalation.create_escalation_task") as mock_esc:
                _finish_task("t1", result, db_path=db, task=mock_task)

        mock_esc.assert_not_called()

    def test_no_escalation_without_task_arg(self):
        from atlas_plus.brain.worker import _finish_task
        from atlas_plus.brain.executors import ExecutionResult

        result = ExecutionResult(status="failed", text="error")
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "state.sqlite"
            with patch("atlas_plus.brain.tasks.mark_failed"), \
                 patch.dict("os.environ", {"ATLAS_CLAUDE_ESCALATION": "on"}), \
                 patch("atlas_plus.brain.escalation.create_escalation_task") as mock_esc:
                _finish_task("t1", result, db_path=db)  # no task= argument
        mock_esc.assert_not_called()


# ---------------------------------------------------------------------------
# Phase 4: claude_supervisor executor
# ---------------------------------------------------------------------------

class TestClaudeSupervisorExecutor:
    def test_builds_report_and_dispatches_to_claude(self):
        from atlas_plus.brain.executors import ExecutorContext, run_executor
        from atlas_plus.brain.tasks import Task

        with tempfile.TemporaryDirectory() as tmp:
            mock_task = MagicMock(spec=Task)
            mock_task.task_id = "sup-001"
            mock_task.kind = "claude_supervisor"
            mock_task.instruction = "supervisor pass"
            mock_task.session_id = "scheduler-claude-supervisor"

            ctx = ExecutorContext(
                task=mock_task,
                artifact_root=Path(tmp) / "artifacts",
                db_path=Path(tmp) / "state.sqlite",
                worker_id="test-worker",
            )

            # Mock task list (no failed tasks) and agent dispatch
            with patch("atlas_plus.brain.tasks.list_tasks", return_value=[]), \
                 patch("atlas_plus.brain.executors._write_text_artifact",
                       return_value=Path(tmp) / "supervisor.md") as mock_write, \
                 patch("atlas_plus.brain.agents.route") as mock_route, \
                 patch("atlas_plus.brain.agents.dispatch",
                       return_value="1. PATTERNS: No critical failures.\n5. HEALTH RATING: Good"):
                mock_route.return_value = MagicMock(name="claude-code")
                (Path(tmp) / "supervisor.md").write_text("report content")
                result = run_executor(mock_task, ctx)

            assert result.status == "done"
            assert "Supervisor report complete" in result.text

    def test_dispatch_failure_returns_failed(self):
        from atlas_plus.brain.executors import ExecutorContext, run_executor
        from atlas_plus.brain.tasks import Task

        with tempfile.TemporaryDirectory() as tmp:
            mock_task = MagicMock(spec=Task)
            mock_task.task_id = "sup-002"
            mock_task.kind = "claude_supervisor"
            mock_task.instruction = "supervisor pass"
            mock_task.session_id = "test"

            ctx = ExecutorContext(
                task=mock_task,
                artifact_root=Path(tmp) / "artifacts",
                db_path=Path(tmp) / "state.sqlite",
                worker_id="test-worker",
            )

            with patch("atlas_plus.brain.tasks.list_tasks", return_value=[]), \
                 patch("atlas_plus.brain.agents.route", side_effect=RuntimeError("claude offline")):
                result = run_executor(mock_task, ctx)

            assert result.status == "failed"
            assert "offline" in result.text


# ---------------------------------------------------------------------------
# Phase 4: scheduler registration
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_supervisor_weekly_queues_correct_kind(monkeypatch):
    from atlas_plus.brain import scheduler
    captured: dict = {}

    async def fake_queue(**kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(scheduler, "_queue_worker_task", fake_queue)
    await scheduler._claude_supervisor_weekly()

    assert captured["kind"] == "claude_supervisor"
    assert captured["priority"] == 2
    assert "supervisor" in captured["session_id"]


@pytest.mark.asyncio
async def test_start_scheduler_includes_supervisor_job():
    from atlas_plus.brain import scheduler
    sched = scheduler.start_scheduler()
    try:
        job_ids = {j.id for j in sched.get_jobs()}
        assert "claude-supervisor" in job_ids
    finally:
        scheduler.stop_scheduler()
