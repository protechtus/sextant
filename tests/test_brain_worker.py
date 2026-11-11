"""Tests for the Brain local worker loop."""

from __future__ import annotations

import pytest

from atlas_plus.brain import tasks as ts
from atlas_plus.brain import worker


@pytest.fixture()
def db(tmp_path):
    return tmp_path / "state.sqlite"


def test_worker_once_processes_noop_task(tmp_path, db):
    task = ts.create_task("noop work", kind="noop", db_path=db)

    processed = worker.run_once(db_path=db, artifact_root=tmp_path, worker_id="w1")
    fetched = ts.get_task(task.task_id, db_path=db)

    assert processed == 1
    assert fetched.status == "done"
    assert fetched.result == "noop work"
    assert fetched.artifact_path is not None


def test_worker_once_marks_executor_failure_failed(tmp_path, db):
    task = ts.create_task("fail work", kind="fail", db_path=db)

    processed = worker.run_once(db_path=db, artifact_root=tmp_path, worker_id="w1")
    fetched = ts.get_task(task.task_id, db_path=db)

    assert processed == 1
    assert fetched.status == "failed"
    assert "intentional test failure" in fetched.error


def test_worker_once_runs_pytest_executor_and_writes_report(monkeypatch, tmp_path, db):
    calls: list[list[str]] = []

    class Completed:
        returncode = 0
        stdout = "2 passed"
        stderr = ""

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return Completed()

    monkeypatch.setattr(worker.executors.subprocess, "run", fake_run)
    task = ts.create_task("run focused tests", kind="pytest", db_path=db)

    processed = worker.run_once(db_path=db, artifact_root=tmp_path, worker_id="w1")
    fetched = ts.get_task(task.task_id, db_path=db)

    assert processed == 1
    assert fetched.status == "done"
    assert "pytest passed" in fetched.result
    assert fetched.artifact_path is not None
    report = tmp_path / task.task_id / "pytest.md"
    assert report.exists()
    assert "2 passed" in report.read_text(encoding="utf-8")
    assert calls
    assert calls[0][1:3] == ["-m", "pytest"]
    assert "tests/test_brain_improvement.py" in calls[0]
    assert "tests/test_brain_tools.py" in calls[0]
    assert "tests/test_brain_scheduler.py" in calls[0]


def test_worker_once_runs_temporal_build_executor_and_writes_report(monkeypatch, tmp_path, db):
    def fake_build_temporal_graph(source="atlas"):
        return {
            "source": source,
            "files_indexed": 2,
            "files_skipped": 3,
            "facts_added": 4,
            "warnings": ["sample warning"],
            "index": {
                "episodes": 5,
                "entities": 6,
                "facts_total": 7,
                "facts_active": 8,
            },
        }

    class Agent:
        name = "brain"

    monkeypatch.setattr(worker.executors, "build_temporal_graph", fake_build_temporal_graph, raising=False)
    monkeypatch.setattr(worker.executors.agent_registry, "route", lambda instruction, agent_name=None: Agent())
    monkeypatch.setattr(
        worker.executors.agent_registry,
        "dispatch",
        lambda agent, instruction, session_id: "agent fallback used",
    )

    task = ts.create_task("build temporal graph", kind="temporal_build", db_path=db)

    processed = worker.run_once(db_path=db, artifact_root=tmp_path, worker_id="w1")
    fetched = ts.get_task(task.task_id, db_path=db)

    assert processed == 1
    assert fetched.status == "done"
    assert "Temporal build complete" in fetched.result
    assert "agent fallback used" not in fetched.result
    report = tmp_path / task.task_id / "temporal-build.md"
    assert report.exists()
    text = report.read_text(encoding="utf-8")
    assert "files_indexed: `2`" in text
    assert "facts_active: `8`" in text


def test_worker_once_runs_gaps_report_executor_and_writes_report(monkeypatch, tmp_path, db):
    calls: list[list[str]] = []

    class Completed:
        returncode = 0
        stdout = "Written: C:\\Tools\\.atlas\\reports\\gaps-2026-W21.md"
        stderr = ""

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return Completed()

    monkeypatch.setattr(worker.executors.subprocess, "run", fake_run)
    task = ts.create_task("write gaps report", kind="gaps_report", db_path=db)

    processed = worker.run_once(db_path=db, artifact_root=tmp_path, worker_id="w1")
    fetched = ts.get_task(task.task_id, db_path=db)

    assert processed == 1
    assert fetched.status == "done"
    assert "Gaps report complete" in fetched.result
    assert calls
    assert calls[0][1:4] == ["-m", "atlas_plus.curation.cli", "gaps"]
    assert "--since" in calls[0]
    assert "7d" in calls[0]
    assert "--write" in calls[0]
    report = tmp_path / task.task_id / "gaps-report.md"
    assert report.exists()
    text = report.read_text(encoding="utf-8")
    assert "Written: C:\\Tools\\.atlas\\reports\\gaps-2026-W21.md" in text


def test_worker_once_runs_curation_report_executor_and_writes_report(monkeypatch, tmp_path, db):
    calls: list[list[str]] = []

    class Completed:
        returncode = 0
        stdout = "Written: C:\\Tools\\.memory\\_atlas\\cleanup-2026-05-19.md"
        stderr = ""

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return Completed()

    monkeypatch.setattr(worker.executors.subprocess, "run", fake_run)
    task = ts.create_task("write curation report", kind="curation_report", db_path=db)

    processed = worker.run_once(db_path=db, artifact_root=tmp_path, worker_id="w1")
    fetched = ts.get_task(task.task_id, db_path=db)

    assert processed == 1
    assert fetched.status == "done"
    assert "Curation report complete" in fetched.result
    assert calls
    assert calls[0][1:4] == ["-m", "atlas_plus.curation.cli", "report"]
    report = tmp_path / task.task_id / "curation-report.md"
    assert report.exists()
    text = report.read_text(encoding="utf-8")
    assert "cleanup-2026-05-19.md" in text


def test_worker_once_runs_suggest_facts_review_executor_and_writes_report(monkeypatch, tmp_path, db):
    calls: list[list[str]] = []

    class Completed:
        returncode = 0
        stdout = "Found 2 candidate fact(s) from transcript.\n(dry-run)"
        stderr = ""

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return Completed()

    monkeypatch.setattr(worker.executors.subprocess, "run", fake_run)
    task = ts.create_task("review suggest facts", kind="suggest_facts_review", db_path=db)

    processed = worker.run_once(db_path=db, artifact_root=tmp_path, worker_id="w1")
    fetched = ts.get_task(task.task_id, db_path=db)

    assert processed == 1
    assert fetched.status == "done"
    assert "Suggest-facts review complete" in fetched.result
    assert calls
    assert calls[0][1:4] == ["-m", "atlas_plus.curation.suggest_facts", "--since"]
    assert "today" in calls[0]
    assert "--source" in calls[0]
    assert "claude-code" in calls[0]
    assert "--dry-run" in calls[0]
    report = tmp_path / task.task_id / "suggest-facts-review.md"
    assert report.exists()
    text = report.read_text(encoding="utf-8")
    assert "Found 2 candidate fact" in text


# ---------------------------------------------------------------------------
# Quality gate wiring in _finish_task
# ---------------------------------------------------------------------------

class TestQualityGateWiring:
    """_finish_task() must call reopen_ticket_if_low_quality() for done tasks."""

    def _make_db_task(self, db, session_id="ticket-ATL-001", kind="noop"):
        """Create a real task record in the SQLite DB and return the Task object."""
        return ts.create_task("test instruction", kind=kind, session_id=session_id, db_path=db)

    def _make_task_proxy(self, real_task, session_id=None):
        """Return a lightweight proxy that overrides session_id on an existing Task."""
        from unittest.mock import MagicMock
        proxy = MagicMock()
        proxy.task_id = real_task.task_id
        proxy.session_id = session_id if session_id is not None else real_task.session_id
        proxy.agent = real_task.agent or "brain"
        proxy.instruction = real_task.instruction
        proxy.kind = real_task.kind
        proxy.attempts = real_task.attempts
        proxy.started_at = real_task.started_at
        return proxy

    def test_low_quality_done_task_reopens_ticket(self, tmp_path):
        """A 'done' result with a refusal pattern triggers reopen_ticket_if_low_quality."""
        from unittest.mock import patch

        db = tmp_path / "state.sqlite"
        real_task = self._make_db_task(db, session_id="ticket-ATL-011")
        proxy = self._make_task_proxy(real_task, session_id="ticket-ATL-011")
        reopened = []

        def fake_reopen(session_id, result_text):
            reopened.append((session_id, result_text))
            return True

        with patch("atlas_plus.brain.dispatcher.reopen_ticket_if_low_quality", fake_reopen):
            result = worker.executors.ExecutionResult(
                status="done",
                text="I cannot complete this task as requested.",
            )
            worker._finish_task(real_task.task_id, result, db_path=db, task=proxy)

        assert len(reopened) == 1
        assert reopened[0][0] == "ticket-ATL-011"
        assert "I cannot" in reopened[0][1]

    def test_high_quality_done_task_does_not_reopen_ticket(self, tmp_path):
        """A 'done' result with a substantive response does not reopen the ticket."""
        from unittest.mock import patch

        db = tmp_path / "state.sqlite"
        real_task = self._make_db_task(db, session_id="ticket-ATL-012")
        proxy = self._make_task_proxy(real_task, session_id="ticket-ATL-012")
        reopened = []

        def fake_reopen(session_id, result_text):
            reopened.append((session_id, result_text))
            return False

        with patch("atlas_plus.brain.dispatcher.reopen_ticket_if_low_quality", fake_reopen):
            result = worker.executors.ExecutionResult(
                status="done",
                text="Implemented the feature successfully. Added 3 new functions to the module "
                     "with full docstrings, unit tests, and integration hooks. All 42 tests pass.",
            )
            worker._finish_task(real_task.task_id, result, db_path=db, task=proxy)

        assert len(reopened) == 1
        assert reopened[0][0] == "ticket-ATL-012"

    def test_failed_task_does_not_call_quality_gate(self, tmp_path):
        """Quality gate is only for 'done' tasks; failed tasks skip it."""
        from unittest.mock import patch

        db = tmp_path / "state.sqlite"
        real_task = self._make_db_task(db, session_id="ticket-ATL-013")
        proxy = self._make_task_proxy(real_task, session_id="ticket-ATL-013")
        reopened = []

        def fake_reopen(session_id, result_text):
            reopened.append(session_id)
            return False

        with patch("atlas_plus.brain.dispatcher.reopen_ticket_if_low_quality", fake_reopen):
            result = worker.executors.ExecutionResult(
                status="failed",
                text="Fatal error: could not connect.",
            )
            worker._finish_task(real_task.task_id, result, db_path=db, task=proxy)

        assert reopened == [], "Quality gate must not run for failed tasks"

    def test_done_task_without_task_object_skips_quality_gate(self, tmp_path):
        """If no task object is passed, quality gate is silently skipped."""
        from unittest.mock import patch

        db = tmp_path / "state.sqlite"
        real_task = self._make_db_task(db, session_id="ticket-ATL-014")
        reopened = []

        def fake_reopen(session_id, result_text):
            reopened.append(session_id)
            return False

        with patch("atlas_plus.brain.dispatcher.reopen_ticket_if_low_quality", fake_reopen):
            result = worker.executors.ExecutionResult(
                status="done",
                text="I cannot do this.",
            )
            # task=None -- caller has no Task object (e.g. exception path)
            worker._finish_task(real_task.task_id, result, db_path=db, task=None)

        assert reopened == [], "Quality gate must not run when task object is unavailable"

    def test_non_ticket_session_id_passes_through_quality_gate_harmlessly(self, tmp_path):
        """A done task with session_id='scheduler-curation' is checked but does nothing."""
        from unittest.mock import patch

        db = tmp_path / "state.sqlite"
        real_task = self._make_db_task(db, session_id="scheduler-curation")
        proxy = self._make_task_proxy(real_task, session_id="scheduler-curation")

        # reopen_ticket_if_low_quality internally checks for ticket-{ID} pattern;
        # it returns False for non-ticket session IDs. Verify the call still happens.
        with patch("atlas_plus.brain.dispatcher.reopen_ticket_if_low_quality",
                   wraps=lambda sid, txt: False) as mock_fn:
            result = worker.executors.ExecutionResult(
                status="done",
                text="I cannot do this.",
            )
            worker._finish_task(real_task.task_id, result, db_path=db, task=proxy)

        mock_fn.assert_called_once_with("scheduler-curation", "I cannot do this.")


class TestCompletionNtfyWiring:
    """_finish_task() calls _push_completion_ntfy when ATLAS_NTFY_COMPLETIONS=on."""

    def test_ntfy_called_on_done_when_enabled(self, tmp_path, monkeypatch):
        from unittest.mock import patch
        monkeypatch.setenv("ATLAS_NTFY_COMPLETIONS", "on")
        db = tmp_path / "state.sqlite"
        task = ts.create_task("do something", db_path=db)
        ts.mark_running(task.task_id, db_path=db)
        result = worker.executors.ExecutionResult(status="done", text="finished")
        calls = []
        with patch.object(worker, "_push_completion_ntfy",
                          side_effect=lambda *a: calls.append(a)):
            worker._finish_task(task.task_id, result, db_path=db, task=None)
        assert len(calls) == 1
        assert calls[0][0] == task.task_id
        assert calls[0][1] == "done"

    def test_ntfy_not_called_when_disabled(self, tmp_path, monkeypatch):
        from unittest.mock import patch
        monkeypatch.setenv("ATLAS_NTFY_COMPLETIONS", "off")
        db = tmp_path / "state.sqlite"
        task = ts.create_task("do something", db_path=db)
        ts.mark_running(task.task_id, db_path=db)
        result = worker.executors.ExecutionResult(status="done", text="finished")
        calls = []
        with patch.object(worker, "_push_completion_ntfy",
                          side_effect=lambda *a: calls.append(a)):
            worker._finish_task(task.task_id, result, db_path=db, task=None)
        assert calls == []

    def test_ntfy_called_on_failed(self, tmp_path, monkeypatch):
        from unittest.mock import patch
        monkeypatch.setenv("ATLAS_NTFY_COMPLETIONS", "on")
        db = tmp_path / "state.sqlite"
        task = ts.create_task("do something", db_path=db)
        ts.mark_running(task.task_id, db_path=db)
        result = worker.executors.ExecutionResult(status="failed", text="error")
        calls = []
        with patch.object(worker, "_push_completion_ntfy",
                          side_effect=lambda *a: calls.append(a)):
            worker._finish_task(task.task_id, result, db_path=db, task=None)
        assert len(calls) == 1
        assert calls[0][1] == "failed"

    def test_ntfy_not_called_for_blocked(self, tmp_path, monkeypatch):
        from unittest.mock import patch
        monkeypatch.setenv("ATLAS_NTFY_COMPLETIONS", "on")
        db = tmp_path / "state.sqlite"
        task = ts.create_task("do something", db_path=db)
        ts.mark_running(task.task_id, db_path=db)
        result = worker.executors.ExecutionResult(status="blocked", text="need more info")
        calls = []
        with patch.object(worker, "_push_completion_ntfy",
                          side_effect=lambda *a: calls.append(a)):
            worker._finish_task(task.task_id, result, db_path=db, task=None)
        assert calls == []


# ---------------------------------------------------------------------------
# Verify task auto-creation and verify failure wiring
# ---------------------------------------------------------------------------

class TestVerifyTaskWiring:
    """_finish_task() queues verify tasks and handles verify failures."""

    def _make_db_task(self, db, session_id="ticket-ATL-001", kind="agent"):
        return ts.create_task("test instruction", kind=kind, session_id=session_id, db_path=db)

    def _make_task_proxy(self, real_task, session_id=None, kind=None):
        from unittest.mock import MagicMock
        proxy = MagicMock()
        proxy.task_id = real_task.task_id
        proxy.session_id = session_id if session_id is not None else real_task.session_id
        proxy.agent = real_task.agent or "brain"
        proxy.instruction = real_task.instruction
        proxy.kind = kind if kind is not None else real_task.kind
        proxy.attempts = real_task.attempts
        proxy.started_at = real_task.started_at
        return proxy

    def test_done_agent_task_queues_verify_when_enabled(self, tmp_path, monkeypatch):
        """ATLAS_VERIFY_TICKETS=on causes a verify task to be queued after agent done."""
        from unittest.mock import patch
        monkeypatch.setenv("ATLAS_VERIFY_TICKETS", "on")

        db = tmp_path / "state.sqlite"
        real_task = self._make_db_task(db, session_id="ticket-ATL-040", kind="agent")
        proxy = self._make_task_proxy(real_task, session_id="ticket-ATL-040", kind="agent")
        created = []

        def fake_create_verify(session_id, instruction, **kwargs):
            created.append(session_id)
            return None

        with patch("atlas_plus.brain.dispatcher.create_verify_task", fake_create_verify), \
             patch("atlas_plus.brain.dispatcher.reopen_ticket_if_low_quality", return_value=False):
            result = worker.executors.ExecutionResult(
                status="done",
                text="Implemented the feature. Tests pass. All 20 assertions green.",
            )
            worker._finish_task(real_task.task_id, result, db_path=db, task=proxy)

        assert created == ["ticket-ATL-040"], "verify task must be queued for ticket agent task"

    def test_done_agent_task_no_verify_when_disabled(self, tmp_path, monkeypatch):
        """ATLAS_VERIFY_TICKETS=off (default) means no verify task is queued."""
        from unittest.mock import patch
        monkeypatch.setenv("ATLAS_VERIFY_TICKETS", "off")

        db = tmp_path / "state.sqlite"
        real_task = self._make_db_task(db, session_id="ticket-ATL-041", kind="agent")
        proxy = self._make_task_proxy(real_task, session_id="ticket-ATL-041", kind="agent")
        created = []

        def fake_create_verify(session_id, instruction, **kwargs):
            created.append(session_id)
            return None

        with patch("atlas_plus.brain.dispatcher.create_verify_task", fake_create_verify), \
             patch("atlas_plus.brain.dispatcher.reopen_ticket_if_low_quality", return_value=False):
            result = worker.executors.ExecutionResult(status="done", text="x" * 200)
            worker._finish_task(real_task.task_id, result, db_path=db, task=proxy)

        assert created == [], "No verify task when ATLAS_VERIFY_TICKETS=off"

    def test_failed_verify_task_reopens_original_ticket(self, tmp_path):
        """A kind=verify task with status=failed triggers ticket reopen via _reopen_ticket_from_verify_failure."""
        from unittest.mock import patch

        db = tmp_path / "state.sqlite"
        real_task = self._make_db_task(db, session_id="verify-ticket-ATL-042", kind="verify")
        proxy = self._make_task_proxy(
            real_task, session_id="verify-ticket-ATL-042", kind="verify"
        )
        reopened = []

        def fake_reopen(session_id, result_text):
            reopened.append(session_id)
            return True

        with patch("atlas_plus.brain.dispatcher._reopen_ticket_from_verify_failure", fake_reopen):
            result = worker.executors.ExecutionResult(
                status="failed",
                text="verify failed: 3 failed, 1480 passed",
            )
            worker._finish_task(real_task.task_id, result, db_path=db, task=proxy)

        assert reopened == ["verify-ticket-ATL-042"]

    def test_passed_verify_task_does_not_reopen_ticket(self, tmp_path):
        """A kind=verify task with status=done does NOT call _reopen_ticket_from_verify_failure."""
        from unittest.mock import patch

        db = tmp_path / "state.sqlite"
        real_task = self._make_db_task(db, session_id="verify-ticket-ATL-043", kind="verify")
        proxy = self._make_task_proxy(
            real_task, session_id="verify-ticket-ATL-043", kind="verify"
        )
        reopened = []

        with patch("atlas_plus.brain.dispatcher._reopen_ticket_from_verify_failure",
                   side_effect=lambda s, t: reopened.append(s)), \
             patch("atlas_plus.brain.dispatcher.reopen_ticket_if_low_quality", return_value=False):
            result = worker.executors.ExecutionResult(
                status="done",
                text="verify passed: 1489 passed",
            )
            worker._finish_task(real_task.task_id, result, db_path=db, task=proxy)

        assert reopened == [], "_reopen_ticket_from_verify_failure must not run for passed verify"


# ---------------------------------------------------------------------------
# update_heartbeat (tasks.py)
# ---------------------------------------------------------------------------

class TestUpdateHeartbeat:
    """Tests for tasks.update_heartbeat() -- renews heartbeat_at and extends lease."""

    def test_updates_heartbeat_at_for_running_task(self, tmp_path):
        db = tmp_path / "state.sqlite"
        task = ts.create_task("long work", kind="noop", db_path=db)
        ts.mark_running(task.task_id, db_path=db, lease_owner="w1")

        import time
        time.sleep(0.05)  # ensure timestamp differs
        ts.update_heartbeat(task.task_id, db_path=db)

        fetched = ts.get_task(task.task_id, db_path=db)
        assert fetched.heartbeat_at is not None

    def test_extends_lease_expires_at(self, tmp_path):
        db = tmp_path / "state.sqlite"
        task = ts.create_task("long work", kind="noop", db_path=db)
        ts.mark_running(task.task_id, db_path=db, lease_owner="w1", lease_seconds=10)

        # Wait a moment then renew with a long lease
        ts.update_heartbeat(task.task_id, db_path=db, extend_lease_seconds=3600)

        fetched = ts.get_task(task.task_id, db_path=db)
        from datetime import datetime, timezone, timedelta
        if fetched.lease_expires_at:
            expires = datetime.fromisoformat(fetched.lease_expires_at)
            # Should be well in the future (> 3500s from now)
            diff = (expires - datetime.now(timezone.utc)).total_seconds()
            assert diff > 3500, f"Lease should be extended to ~3600s, got {diff:.0f}s remaining"

    def test_no_op_for_non_running_task(self, tmp_path):
        db = tmp_path / "state.sqlite"
        task = ts.create_task("done work", kind="noop", db_path=db)
        # Don't mark running -- update_heartbeat on a pending task is a no-op
        ts.update_heartbeat(task.task_id, db_path=db)
        fetched = ts.get_task(task.task_id, db_path=db)
        assert fetched.status == "pending"  # unchanged


# ---------------------------------------------------------------------------
# _HeartbeatKeeper (worker.py)
# ---------------------------------------------------------------------------

class TestHeartbeatKeeper:
    """Tests for the _HeartbeatKeeper context manager."""

    def test_heartbeat_keeper_calls_update_heartbeat(self, tmp_path):
        """_HeartbeatKeeper._loop() calls task_store.update_heartbeat on the correct task_id."""
        import threading

        db = tmp_path / "state.sqlite"
        task = ts.create_task("long task", kind="noop", db_path=db)
        ts.mark_running(task.task_id, db_path=db, lease_owner="test-worker")

        # Directly test _loop by calling it with a short-circuit stop
        received = []
        fired = threading.Event()

        def mock_hb(tid, db_path=None, extend_lease_seconds=None):
            received.append(tid)
            fired.set()

        keeper = worker._HeartbeatKeeper(task.task_id, db_path=db, interval=1)
        # Replace the real update_heartbeat with our mock on the task_store reference in worker
        original = ts.update_heartbeat
        ts.update_heartbeat = mock_hb
        try:
            keeper._stop.clear()
            keeper._thread = threading.Thread(target=keeper._loop, daemon=True)
            keeper._thread.start()
            fired.wait(timeout=3.0)
            keeper._stop.set()
            keeper._thread.join(timeout=2.0)
        finally:
            ts.update_heartbeat = original

        assert len(received) >= 1, "Expected at least 1 heartbeat call"
        assert received[0] == task.task_id

    def test_heartbeat_stops_after_context_exit(self, tmp_path):
        db = tmp_path / "state.sqlite"
        task = ts.create_task("quick task", kind="noop", db_path=db)
        ts.mark_running(task.task_id, db_path=db, lease_owner="test-worker")

        call_count = [0]
        from unittest.mock import patch

        def mock_hb(tid, **kwargs):
            call_count[0] += 1

        with patch("atlas_plus.brain.tasks.update_heartbeat", side_effect=mock_hb):
            with worker._HeartbeatKeeper(task.task_id, db_path=db, interval=60):
                pass  # Exit immediately

            import time
            count_at_exit = call_count[0]
            time.sleep(0.2)
            # Count should not increase after context exit
            assert call_count[0] == count_at_exit, "Heartbeat must stop after context exit"

    def test_run_once_uses_heartbeat_keeper(self, tmp_path):
        """run_once() wraps executor in _HeartbeatKeeper."""
        db = tmp_path / "state.sqlite"
        ts.create_task("noop work", kind="noop", db_path=db)

        keepings = []
        from unittest.mock import patch

        real_hk_enter = worker._HeartbeatKeeper.__enter__

        class TrackingKeeper(worker._HeartbeatKeeper):
            def __enter__(self):
                keepings.append(self._task_id)
                return super().__enter__()

        with patch.object(worker, "_HeartbeatKeeper", TrackingKeeper):
            worker.run_once(db_path=db, artifact_root=tmp_path, worker_id="w1")

        assert len(keepings) == 1, "run_once should create exactly one HeartbeatKeeper"


# ---------------------------------------------------------------------------
# _push_completion_ntfy
# ---------------------------------------------------------------------------

class TestPushCompletionNtfy:
    def test_no_op_when_url_not_set(self, monkeypatch):
        monkeypatch.delenv("ATLAS_NTFY_URL", raising=False)
        from unittest.mock import patch
        with patch("httpx.post") as mock_post:
            worker._push_completion_ntfy("task-abc", "done", "result text")
        mock_post.assert_not_called()

    def test_posts_when_url_set(self, monkeypatch):
        monkeypatch.setenv("ATLAS_NTFY_URL", "https://ntfy.sh/test-topic")
        from unittest.mock import patch, MagicMock
        with patch("httpx.post", return_value=MagicMock()) as mock_post:
            worker._push_completion_ntfy("task-abc123def", "done", "task completed successfully")
        mock_post.assert_called_once()
        call_kwargs = mock_post.call_args
        # short_id = task_id[:12] = "task-abc123d"
        assert "task-abc123d" in str(call_kwargs)

    def test_message_includes_short_id_and_status(self, monkeypatch):
        monkeypatch.setenv("ATLAS_NTFY_URL", "https://ntfy.sh/test")
        from unittest.mock import patch, MagicMock
        calls = []
        with patch("httpx.post", side_effect=lambda *a, **kw: calls.append(kw) or MagicMock()):
            worker._push_completion_ntfy("task-longtaskid123", "failed", "some error occurred")
        assert calls
        body = calls[0].get("content", b"").decode()
        assert "failed" in body
        assert "task-longtas" in body  # first 12 chars of "task-longtaskid123"

    def test_long_result_truncated_to_100_chars(self, monkeypatch):
        monkeypatch.setenv("ATLAS_NTFY_URL", "https://ntfy.sh/test")
        long_result = "A" * 200
        from unittest.mock import patch, MagicMock
        calls = []
        with patch("httpx.post", side_effect=lambda *a, **kw: calls.append(kw) or MagicMock()):
            worker._push_completion_ntfy("task-abc", "done", long_result)
        body = calls[0]["content"].decode()
        # Result is truncated to 100 chars; total message is short_id + separator + 100
        assert len(body) < 150

    def test_graceful_when_post_fails(self, monkeypatch):
        monkeypatch.setenv("ATLAS_NTFY_URL", "https://ntfy.sh/test")
        from unittest.mock import patch
        import httpx
        with patch("httpx.post", side_effect=httpx.TimeoutException("timeout")):
            # Should not raise
            worker._push_completion_ntfy("task-abc", "done", "text")
