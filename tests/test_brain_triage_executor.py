"""Tests for the `triage` executor (LOO-001): classify ROUTINE/HARD via the local model,
then enqueue a follow-up task (routine -> agent, hard -> claude_task). The model call and
create_task are mocked so nothing runs Ollama or writes the real task DB."""
from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from atlas_plus.brain.executors import ExecutorContext, run_executor
from atlas_plus.brain.tasks import Task


class _FakeProvider:
    """Stand-in for an llm.ModelProvider; .generate() returns a value or raises."""

    def __init__(self, value=None, *, error=None):
        self._value, self._error = value, error

    def generate(self, prompt, **kw):
        if self._error is not None:
            raise self._error
        return self._value


def _ctx(tmp: Path, instruction: str) -> tuple[Task, ExecutorContext]:
    task = MagicMock(spec=Task)
    task.task_id = "test-task-triage"
    task.kind = "triage"
    task.instruction = instruction
    task.session_id = "sess-triage"
    ctx = ExecutorContext(
        task=task,
        artifact_root=tmp / "artifacts",
        db_path=tmp / "state.sqlite",
        worker_id="test-worker",
    )
    return task, ctx


def _follow(task_id: str = "task-followup") -> MagicMock:
    fu = MagicMock(spec=Task)
    fu.task_id = task_id
    return fu


class TestTriageExecutor:
    def test_routine_enqueues_agent(self):
        with tempfile.TemporaryDirectory() as tmp:
            task, ctx = _ctx(Path(tmp), "Index research reports missing a summary")
            with patch("atlas_plus.llm.get_provider", return_value=_FakeProvider("ROUTINE")), \
                 patch("atlas_plus.brain.tasks.create_task", return_value=_follow()) as mk:
                result = run_executor(task, ctx)
        assert result.status == "done"
        assert "ROUTINE" in result.text
        # follow-up enqueued as a free local agent task, in the SAME db the worker drains
        assert mk.call_args.kwargs["kind"] == "agent"
        assert mk.call_args.kwargs["db_path"] == ctx.db_path
        assert mk.call_args.kwargs["instruction"] == "Index research reports missing a summary"

    def test_hard_enqueues_claude_task(self):
        with tempfile.TemporaryDirectory() as tmp:
            task, ctx = _ctx(Path(tmp), "Redesign the lease locking for concurrency")
            with patch("atlas_plus.llm.get_provider", return_value=_FakeProvider("HARD")), \
                 patch("atlas_plus.brain.tasks.create_task", return_value=_follow()) as mk:
                result = run_executor(task, ctx)
        assert result.status == "done"
        assert "HARD" in result.text
        assert mk.call_args.kwargs["kind"] == "claude_task"

    def test_think_tags_stripped_before_match(self):
        # The <think> block mentions "routine" but the real verdict is HARD; stripping must win.
        raw = "<think>this might be routine work, hmm</think>\nHARD"
        with tempfile.TemporaryDirectory() as tmp:
            task, ctx = _ctx(Path(tmp), "subtle concurrency bug")
            with patch("atlas_plus.llm.get_provider", return_value=_FakeProvider(raw)), \
                 patch("atlas_plus.brain.tasks.create_task", return_value=_follow()) as mk:
                result = run_executor(task, ctx)
        assert "HARD" in result.text
        assert mk.call_args.kwargs["kind"] == "claude_task"

    def test_ambiguous_defaults_to_hard(self):
        with tempfile.TemporaryDirectory() as tmp:
            task, ctx = _ctx(Path(tmp), "do the thing")
            with patch("atlas_plus.llm.get_provider", return_value=_FakeProvider("I am not sure")), \
                 patch("atlas_plus.brain.tasks.create_task", return_value=_follow()) as mk:
                result = run_executor(task, ctx)
        assert result.status == "done"
        assert mk.call_args.kwargs["kind"] == "claude_task"  # safe tie-break = escalate

    def test_classifier_error_fails_cleanly(self):
        with tempfile.TemporaryDirectory() as tmp:
            task, ctx = _ctx(Path(tmp), "anything")
            with patch("atlas_plus.llm.get_provider", return_value=_FakeProvider(error=RuntimeError("ollama down"))), \
                 patch("atlas_plus.brain.tasks.create_task") as mk:
                result = run_executor(task, ctx)
        assert result.status == "failed"
        assert "ollama down" in result.text
        mk.assert_not_called()  # no follow-up when classification failed

    def test_writes_triage_artifact(self):
        with tempfile.TemporaryDirectory() as tmp:
            task, ctx = _ctx(Path(tmp), "Rebuild the temporal graph")
            with patch("atlas_plus.llm.get_provider", return_value=_FakeProvider("ROUTINE")), \
                 patch("atlas_plus.brain.tasks.create_task", return_value=_follow("task-xyz")):
                result = run_executor(task, ctx)
                # Assert inside the with-block: the tmp dir (and the artifact) exist only here.
                assert result.artifact_path is not None
                assert result.artifact_path.exists()
                body = result.artifact_path.read_text(encoding="utf-8")
                assert "Decision: ROUTINE" in body
                assert "task-xyz" in body
