"""The Brain's pytest/verify subprocesses must not inherit the worker's ATLAS_* env.

Found 2026-10-12: the always-on worker runs with the live deployment
environment loaded (ATLAS_STATE_DIR, ATLAS_WEB_API_KEY, ...). The verify
executor ran `pytest` with that environment inherited, so ~260 tests failed
on every verify run (a live API key alone breaks the web-app tests) and test
fixtures resolved live operator state -- 61 failed verify tasks in 14 days.
The suite inside a verify run must look exactly like a clean CI shell.
"""
from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from atlas_plus.brain.executors import (
    ExecutorContext,
    _clean_pytest_env,
    run_executor,
)
from atlas_plus.brain.tasks import Task


def _ctx(tmp: Path, kind: str) -> tuple[Task, ExecutorContext]:
    task = MagicMock(spec=Task)
    task.task_id = "test-task-env"
    task.kind = kind
    task.instruction = f"run {kind}"
    task.session_id = "verify-ticket-ATL-000"
    ctx = ExecutorContext(
        task=task,
        artifact_root=tmp / "artifacts",
        db_path=tmp / "state.sqlite",
        worker_id="test-worker",
    )
    return task, ctx


class TestCleanPytestEnv:
    def test_drops_every_atlas_var(self, monkeypatch):
        monkeypatch.setenv("ATLAS_WEB_API_KEY", "leaked-key")
        monkeypatch.setenv("ATLAS_STATE_DIR", r"C:\somewhere\live")
        monkeypatch.setenv("ATLAS_AUTONOMOUS", "on")
        env = _clean_pytest_env()
        assert not [k for k in env if k.startswith("ATLAS_")]

    def test_keeps_the_rest_of_the_environment(self, monkeypatch):
        monkeypatch.setenv("ATLAS_WEB_API_KEY", "leaked-key")
        monkeypatch.setenv("KEEP_ME", "yes")
        env = _clean_pytest_env()
        assert env.get("KEEP_ME") == "yes"
        # PATH (any casing) survives so the venv python can actually run
        assert any(k.upper() == "PATH" for k in env)

    def test_drops_lowercase_atlas_vars_posix_style(self, monkeypatch):
        # os.environ upcases keys on Windows but not on POSIX; the filter
        # must be case-insensitive so CI on Linux/macOS is scrubbed too.
        monkeypatch.setenv("atlas_state_dir", "/somewhere/live")
        env = _clean_pytest_env()
        assert not [k for k in env if k.upper().startswith("ATLAS_")]

    def test_drops_other_live_deployment_keys(self, monkeypatch):
        # Same failure class as ATLAS_WEB_API_KEY: these change src/ behavior
        # when present, so a verify run must not inherit them.
        monkeypatch.setenv("RESEARCH_API_KEY", "live-research-key")
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "live-bot-token")
        env = _clean_pytest_env()
        assert "RESEARCH_API_KEY" not in env
        assert "TELEGRAM_BOT_TOKEN" not in env


class TestVerifyExecutorEnv:
    def test_verify_subprocess_runs_with_scrubbed_env(self, monkeypatch):
        monkeypatch.setenv("ATLAS_WEB_API_KEY", "leaked-key")
        monkeypatch.setenv("ATLAS_STATE_DIR", r"C:\somewhere\live")
        captured: dict = {}

        def fake_run(cmd, **kwargs):
            captured.update(kwargs)
            m = MagicMock()
            m.returncode = 0
            m.stdout = "4000 passed in 1.00s"
            m.stderr = ""
            return m

        with tempfile.TemporaryDirectory() as tmp:
            task, ctx = _ctx(Path(tmp), "verify")
            with patch("subprocess.run", side_effect=fake_run):
                result = run_executor(task, ctx)

        assert result.status == "done"
        assert "env" in captured, "verify must pass an explicit env to pytest"
        assert not [k for k in captured["env"] if k.startswith("ATLAS_")]

    def test_pytest_subprocess_runs_with_scrubbed_env(self, monkeypatch):
        monkeypatch.setenv("ATLAS_WEB_API_KEY", "leaked-key")
        captured: dict = {}

        def fake_run(cmd, **kwargs):
            captured.update(kwargs)
            m = MagicMock()
            m.returncode = 0
            m.stdout = "10 passed in 0.10s"
            m.stderr = ""
            return m

        with tempfile.TemporaryDirectory() as tmp:
            task, ctx = _ctx(Path(tmp), "pytest")
            with patch("subprocess.run", side_effect=fake_run):
                result = run_executor(task, ctx)

        assert result.status == "done"
        assert "env" in captured
        assert not [k for k in captured["env"] if k.startswith("ATLAS_")]
