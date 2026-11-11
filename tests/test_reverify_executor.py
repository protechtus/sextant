"""Tests for the `reverify` Brain executor (decay-as-behavior trigger).

run_reverification + the decay scan are mocked, so no model runs and no real memory
is scanned. Verifies registry dispatch, the default-OFF reset gate, and the env override."""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from atlas_plus.brain.executors import ExecutorContext, run_executor
from atlas_plus.brain.tasks import Task


def _ctx(tmp: Path):
    task = MagicMock(spec=Task)
    task.task_id = "t-reverify"
    task.kind = "reverify"
    task.instruction = "re-verify stale memories"
    task.session_id = "sess-reverify"
    ctx = ExecutorContext(task=task, artifact_root=tmp / "artifacts",
                          db_path=tmp / "state.sqlite", worker_id="w1")
    return task, ctx


def test_reverify_executor_nothing_queued():
    with tempfile.TemporaryDirectory() as tmp:
        task, ctx = _ctx(Path(tmp))
        with patch("atlas_plus.curation.decay.find_decayed_facts", return_value=[]), \
             patch("atlas_plus.curation.decay.reverify_queue", return_value=[]):
            result = run_executor(task, ctx)
        assert result.status == "done"
        assert "nothing queued" in result.text.lower()
        assert result.artifact_path is not None and result.artifact_path.exists()


def test_reverify_executor_runs_and_summarises():
    from atlas_plus.curation.reverify import ReverifyRun
    fake_run = ReverifyRun(checked=2, reset=["a.md"], quarantine=["b.md"])
    with tempfile.TemporaryDirectory() as tmp:
        task, ctx = _ctx(Path(tmp))
        with patch("atlas_plus.curation.decay.find_decayed_facts", return_value=["x"]), \
             patch("atlas_plus.curation.decay.reverify_queue", return_value=["c1"]), \
             patch("atlas_plus.curation.reverify.run_reverification", return_value=fake_run) as mrun:
            result = run_executor(task, ctx)
        assert result.status == "done"
        assert "checked 2" in result.text
        assert "reset 1" in result.text
        # mutation gate is OFF unless explicitly enabled
        assert mrun.call_args.kwargs.get("autonomous") is False
        assert result.artifact_path.exists()


def test_reverify_executor_honors_reset_env(monkeypatch):
    from atlas_plus.curation.reverify import ReverifyRun
    monkeypatch.setenv("ATLAS_REVERIFY_RESET", "on")
    with tempfile.TemporaryDirectory() as tmp:
        task, ctx = _ctx(Path(tmp))
        with patch("atlas_plus.curation.decay.find_decayed_facts", return_value=["x"]), \
             patch("atlas_plus.curation.decay.reverify_queue", return_value=["c1"]), \
             patch("atlas_plus.curation.reverify.run_reverification",
                   return_value=ReverifyRun(checked=1)) as mrun:
            run_executor(task, ctx)
        assert mrun.call_args.kwargs.get("autonomous") is True


def test_reverify_executor_scan_failure_is_clean():
    with tempfile.TemporaryDirectory() as tmp:
        task, ctx = _ctx(Path(tmp))
        with patch("atlas_plus.curation.decay.find_decayed_facts",
                   side_effect=RuntimeError("scan boom")):
            result = run_executor(task, ctx)
        assert result.status == "failed"
        assert "scan boom" in result.text
