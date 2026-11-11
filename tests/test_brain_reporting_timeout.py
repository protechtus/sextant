"""The shared reporting-subprocess helper must fail GRACEFULLY on a subprocess
timeout, not crash the worker with an unhandled subprocess.TimeoutExpired.

Regression guard: reflexion_loop / suggest-facts-auto route through
_run_reporting_subprocess, which previously did not catch TimeoutExpired, so a
slow run on the local LLM killed the task outright (never recorded as "failed").
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from atlas_plus.brain import executors
from atlas_plus.brain.executors import ExecutorContext, _run_reporting_subprocess
from atlas_plus.brain.tasks import Task


def _ctx(tmp_path: Path) -> ExecutorContext:
    task = Task(
        task_id="task-timeout-001",
        session_id="session-test",
        instruction="run a reporting subprocess",
        status="queued",
        created_at="2026-07-18T00:00:00",
        kind="reflexion_loop",
        priority=6,
    )
    return ExecutorContext(
        task=task,
        artifact_root=tmp_path / "artifacts",
        db_path=tmp_path / "fake.db",
        worker_id="test-worker-01",
    )


def test_timeout_produces_failed_result_not_exception(tmp_path, monkeypatch):
    def _raise_timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=["x"], timeout=1, output="partial stdout", stderr="partial stderr")

    monkeypatch.setattr(executors.subprocess, "run", _raise_timeout)

    result = _run_reporting_subprocess(
        _ctx(tmp_path), ["py", "-m", "atlas_plus.curation.reflexion_loop"],
        "reflexion-loop.md", "Reflexion Loop", timeout=1,
    )

    assert result.status == "failed"  # graceful, no exception escaped
    assert result.artifact_path is not None
    text = Path(result.artifact_path).read_text(encoding="utf-8")
    assert "TimeoutExpired after 1s" in text
    assert "Return code: `124`" in text
    assert "partial stdout" in text  # partial output preserved for debugging


def test_success_path_still_reports_done(tmp_path, monkeypatch):
    def _ok(*args, **kwargs):
        return subprocess.CompletedProcess(["x"], returncode=0, stdout="all good", stderr="")

    monkeypatch.setattr(executors.subprocess, "run", _ok)

    result = _run_reporting_subprocess(_ctx(tmp_path), ["x"], "dream.md", "Dream")
    assert result.status == "done"


def test_nonzero_exit_reports_failed(tmp_path, monkeypatch):
    def _fail(*args, **kwargs):
        return subprocess.CompletedProcess(["x"], returncode=2, stdout="", stderr="boom")

    monkeypatch.setattr(executors.subprocess, "run", _fail)

    result = _run_reporting_subprocess(_ctx(tmp_path), ["x"], "dream.md", "Dream")
    assert result.status == "failed"
