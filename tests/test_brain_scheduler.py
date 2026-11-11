"""Tests for Brain scheduler -- job functions, registration, zombie cleanup."""

from __future__ import annotations

import logging
import logging.handlers
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from atlas_plus.brain import scheduler
from atlas_plus.brain import tasks as task_store


@pytest.mark.asyncio
async def test_temporal_build_queues_worker_task_without_agent_chat(monkeypatch):
    captured: dict[str, object] = {}

    class Task:
        task_id = "task-temporal"

    def fake_create_task(**kwargs):
        captured.update(kwargs)
        return Task()

    monkeypatch.setattr(task_store, "create_task", fake_create_task)
    monkeypatch.setattr(task_store, "mark_running", lambda task_id: pytest.fail("agent dispatch should not run"))

    await scheduler._temporal_build()

    assert captured["kind"] == "temporal_build"
    assert captured["priority"] == 2
    assert captured["session_id"] == "scheduler-temporal-build"


@pytest.mark.asyncio
async def test_suggest_facts_queues_worker_task_without_agent_chat(monkeypatch):
    captured: dict[str, object] = {}

    async def fake_queue_worker_task(**kwargs):
        captured.update(kwargs)

    async def fail_agent_dispatch(*args, **kwargs):
        pytest.fail("suggest-facts should be queued for the worker, not run through agent chat")

    monkeypatch.setattr(scheduler, "_queue_worker_task", fake_queue_worker_task)
    monkeypatch.setattr(scheduler, "_dispatch", fail_agent_dispatch)

    await scheduler._suggest_facts()

    # kind changed review -> auto in e9488e5 (nightly dream activation, 2026-07-02)
    assert captured["kind"] == "suggest_facts_auto"
    assert captured["priority"] == 3
    assert captured["session_id"] == "scheduler-suggest-facts"


@pytest.mark.asyncio
async def test_curation_queues_worker_task_without_agent_chat(monkeypatch):
    captured: dict[str, object] = {}

    async def fake_queue_worker_task(**kwargs):
        captured.update(kwargs)

    async def fail_agent_dispatch(*args, **kwargs):
        pytest.fail("curation should be queued for the worker, not run through agent chat")

    monkeypatch.setattr(scheduler, "_queue_worker_task", fake_queue_worker_task)
    monkeypatch.setattr(scheduler, "_dispatch", fail_agent_dispatch)

    await scheduler._curation()

    assert captured["kind"] == "curation_report"
    assert captured["priority"] == 4
    assert captured["session_id"] == "scheduler-curation"


@pytest.mark.asyncio
async def test_gaps_report_queues_worker_task_without_inline_subprocess(monkeypatch):
    captured: dict[str, object] = {}

    async def fake_queue_worker_task(**kwargs):
        captured.update(kwargs)

    def fail_inline_subprocess(*args, **kwargs):
        pytest.fail("gaps report should be queued for the worker, not run inline")

    monkeypatch.setattr(scheduler, "_queue_worker_task", fake_queue_worker_task)
    monkeypatch.setattr(subprocess, "run", fail_inline_subprocess)

    await scheduler._gaps_report()

    assert captured["kind"] == "gaps_report"
    assert captured["priority"] == 4
    assert captured["session_id"] == "scheduler-gaps-report"


@pytest.mark.asyncio
async def test_zombie_cleanup_calls_task_store(monkeypatch):
    called_with = {}

    def fake_cleanup(**kwargs):
        called_with.update(kwargs)
        return 0

    monkeypatch.setattr(task_store, "cleanup_zombie_tasks", fake_cleanup)
    await scheduler._zombie_cleanup()
    assert "max_running_minutes" in called_with
    assert called_with["max_running_minutes"] == 30


@pytest.mark.asyncio
async def test_zombie_cleanup_logs_warning_on_stale_tasks(monkeypatch, caplog):
    import logging
    monkeypatch.setattr(task_store, "cleanup_zombie_tasks", lambda **kwargs: 3)
    with caplog.at_level(logging.WARNING, logger="atlas.brain.scheduler"):
        await scheduler._zombie_cleanup()
    assert any("3" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_patterns_rebuild_queues_correct_kind(monkeypatch):
    captured = {}

    async def fake_queue(**kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(scheduler, "_queue_worker_task", fake_queue)
    await scheduler._patterns_rebuild()
    assert captured["kind"] == "patterns_rebuild"


@pytest.mark.asyncio
async def test_memory_agent_weekly_queues_correct_kind(monkeypatch):
    captured = {}

    async def fake_queue(**kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(scheduler, "_queue_worker_task", fake_queue)
    await scheduler._memory_agent_weekly()
    assert captured["kind"] == "memory_agent"
    assert captured["session_id"] == "scheduler-memory-agent"


@pytest.mark.asyncio
async def test_narrative_weekly_queues_correct_kind(monkeypatch):
    captured = {}

    async def fake_queue(**kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(scheduler, "_queue_worker_task", fake_queue)
    await scheduler._narrative_weekly()
    assert captured["kind"] == "narrative_weekly"


@pytest.mark.asyncio
async def test_convergence_report_queues_worker_task(monkeypatch):
    captured: dict[str, object] = {}

    async def fake_queue_worker_task(**kwargs):
        captured.update(kwargs)

    def _fail_inline(*args, **kwargs):
        pytest.fail("convergence report should be queued for the worker, not run inline")

    monkeypatch.setattr(scheduler, "_queue_worker_task", fake_queue_worker_task)
    monkeypatch.setattr(subprocess, "run", _fail_inline)

    await scheduler._convergence_report()

    assert captured["kind"] == "convergence_report"
    assert captured["priority"] == 4
    assert captured["session_id"] == "scheduler-convergence-report"


@pytest.mark.asyncio
async def test_decay_report_queues_worker_task(monkeypatch):
    """_decay_report() queues a decay_report worker task (does not run inline)."""
    captured: dict[str, object] = {}

    async def fake_queue_worker_task(**kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(scheduler, "_queue_worker_task", fake_queue_worker_task)

    await scheduler._decay_report()

    assert captured["kind"] == "decay_report"
    assert captured["priority"] == 4
    assert captured["session_id"] == "scheduler-decay-report"


@pytest.mark.asyncio
async def test_start_scheduler_registers_expected_jobs():
    """start_scheduler() must register all known job IDs."""
    sched = scheduler.start_scheduler()
    try:
        job_ids = {j.id for j in sched.get_jobs()}
        expected = {
            "temporal-build", "suggest-facts", "curation",
            "research-batch", "patterns-rebuild", "gaps-report",
            "convergence-report", "unconfirmed-decay", "decay-report",
            "memory-agent", "narrative-weekly",
            "zombie-cleanup", "sla-check", "db-backup",
            "memory-trends", "research-quality", "suggest-tags", "weekly-digest",
            "ticket-dispatch",
        }
        assert expected.issubset(job_ids), f"Missing jobs: {expected - job_ids}"
    finally:
        scheduler.stop_scheduler()


@pytest.mark.asyncio
async def test_sla_check_alerts_long_running_task(monkeypatch):
    """_sla_check fires a log warning for tasks over threshold, adds to alerted set."""
    import logging
    from datetime import datetime, timedelta, timezone
    from unittest.mock import MagicMock

    # Clear alerted set from previous runs
    scheduler._sla_alerted.clear()

    # Create a mock task that started 20 minutes ago
    mock_task = MagicMock()
    mock_task.task_id = "sla-test-001"
    mock_task.agent = "brain"
    mock_task.started_at = (datetime.now(timezone.utc) - timedelta(minutes=20)).strftime(
        "%Y-%m-%dT%H:%M:%S"
    )
    mock_task.instruction = "Long running task for SLA test"

    monkeypatch.setattr(task_store, "list_tasks", lambda status=None, limit=50: [mock_task])
    monkeypatch.setenv("ATLAS_SLA_MINUTES", "15")

    await scheduler._sla_check()

    assert "sla-test-001" in scheduler._sla_alerted
    scheduler._sla_alerted.clear()


@pytest.mark.asyncio
async def test_sla_check_no_double_alert(monkeypatch):
    """A task already in _sla_alerted is not re-alerted."""
    from datetime import datetime, timedelta, timezone
    from unittest.mock import MagicMock

    scheduler._sla_alerted.clear()
    scheduler._sla_alerted.add("sla-dup-001")

    mock_task = MagicMock()
    mock_task.task_id = "sla-dup-001"
    mock_task.agent = "brain"
    mock_task.started_at = (datetime.now(timezone.utc) - timedelta(minutes=20)).strftime(
        "%Y-%m-%dT%H:%M:%S"
    )
    mock_task.instruction = "Duplicate SLA task"

    log_calls: list[str] = []

    def fake_list_tasks(status=None, limit=50):
        return [mock_task]

    monkeypatch.setattr(task_store, "list_tasks", fake_list_tasks)
    monkeypatch.setenv("ATLAS_SLA_MINUTES", "15")

    original_warning = scheduler.log.warning
    scheduler.log.warning = lambda msg, *args: log_calls.append(msg % args if args else msg)
    try:
        await scheduler._sla_check()
    finally:
        scheduler.log.warning = original_warning
        scheduler._sla_alerted.clear()

    assert not any("sla-dup-001" in s for s in log_calls), "Already-alerted task must not re-alert"


@pytest.mark.asyncio
async def test_sla_check_disabled_when_zero(monkeypatch):
    """_sla_check is a no-op when ATLAS_SLA_MINUTES=0."""
    from unittest.mock import MagicMock

    scheduler._sla_alerted.clear()
    called = []
    monkeypatch.setattr(task_store, "list_tasks", lambda **kw: called.append(1) or [])
    monkeypatch.setenv("ATLAS_SLA_MINUTES", "0")
    await scheduler._sla_check()
    assert not called, "list_tasks should not be called when SLA is disabled"


@pytest.mark.asyncio
async def test_sla_check_skips_recent_task(monkeypatch):
    """Tasks started within the threshold are not alerted."""
    from datetime import datetime, timedelta, timezone
    from unittest.mock import MagicMock

    scheduler._sla_alerted.clear()

    mock_task = MagicMock()
    mock_task.task_id = "sla-fresh-001"
    mock_task.agent = "brain"
    # Started only 5 minutes ago, threshold is 15 -- should NOT alert
    mock_task.started_at = (datetime.now(timezone.utc) - timedelta(minutes=5)).strftime(
        "%Y-%m-%dT%H:%M:%S"
    )
    mock_task.instruction = "Fresh task"

    monkeypatch.setattr(task_store, "list_tasks", lambda status=None, limit=50: [mock_task])
    monkeypatch.setenv("ATLAS_SLA_MINUTES", "15")

    await scheduler._sla_check()

    assert "sla-fresh-001" not in scheduler._sla_alerted
    scheduler._sla_alerted.clear()


@pytest.mark.asyncio
async def test_stop_scheduler_is_idempotent():
    """stop_scheduler() should not raise even if called before start_scheduler."""
    scheduler.stop_scheduler()  # no-op, should not raise
    scheduler.start_scheduler()
    scheduler.stop_scheduler()  # normal stop
    scheduler.stop_scheduler()  # second call should also be safe


@pytest.mark.asyncio
async def test_unconfirmed_decay_weekly_calls_downgrade(monkeypatch, tmp_path):
    """_unconfirmed_decay_weekly runs downgrade_stale_confirmations in a thread."""
    downgrade_calls = []

    def fake_downgrade(threshold_days, memory_root, dry_run):
        downgrade_calls.append((threshold_days, dry_run))
        return []

    monkeypatch.setenv("ATLAS_CONFIRM_DECAY_DAYS", "90")
    monkeypatch.setattr(
        "atlas_plus.mcp.memory_server.routing.downgrade_stale_confirmations",
        fake_downgrade,
    )
    await scheduler._unconfirmed_decay_weekly()
    assert len(downgrade_calls) == 1
    assert downgrade_calls[0] == (90, False)  # threshold=90, dry_run=False


@pytest.mark.asyncio
async def test_unconfirmed_decay_weekly_logs_downgrade_count(monkeypatch, caplog):
    """_unconfirmed_decay_weekly logs at INFO when files are downgraded."""
    import logging

    monkeypatch.setenv("ATLAS_CONFIRM_DECAY_DAYS", "90")
    monkeypatch.setattr(
        "atlas_plus.mcp.memory_server.routing.downgrade_stale_confirmations",
        lambda threshold_days, memory_root, dry_run: [
            {"path": "feedback/stale.md", "old_confidence": "high",
             "last_confirmed": "2020-01-01", "age_days": 2000}
        ],
    )
    with caplog.at_level(logging.INFO, logger="atlas.brain.scheduler"):
        await scheduler._unconfirmed_decay_weekly()
    assert any("downgraded" in r.message.lower() or "decay" in r.message.lower()
               for r in caplog.records)


@pytest.mark.asyncio
async def test_ticket_dispatch_sends_ntfy_when_dispatched(monkeypatch):
    """_ticket_dispatch sends ntfy notification when tickets are dispatched."""
    import asyncio as _asyncio
    from unittest.mock import patch

    dispatched_result = [
        {"ticket_id": "ATL-030", "task_id": "task-abc", "agent": "brain"},
        {"ticket_id": "ATL-031", "task_id": "task-def", "agent": "claude-code"},
    ]
    ntfy_calls: list[tuple] = []

    async def fake_ntfy(title, message, priority="default"):
        ntfy_calls.append((title, message, priority))

    # create_task schedules the ntfy coroutine but it won't run before the assertion
    # unless we flush the event loop. Replace create_task with ensure_future so we
    # can await the pending tasks with asyncio.sleep(0).
    def immediate_task(coro):
        return _asyncio.ensure_future(coro)

    monkeypatch.setenv("ATLAS_TICKET_DISPATCH", "on")
    monkeypatch.setenv("ATLAS_AUTONOMOUS", "on")

    with patch("atlas_plus.brain.dispatcher.dispatch_all_eligible", return_value=dispatched_result), \
         patch("atlas_plus.brain.dispatcher.is_dispatch_enabled", return_value=True), \
         patch("atlas_plus.brain.server._ntfy", new=fake_ntfy), \
         patch("atlas_plus.brain.scheduler.asyncio.create_task", side_effect=immediate_task):
        await scheduler._ticket_dispatch()
        await _asyncio.sleep(0)  # flush scheduled tasks

    assert len(ntfy_calls) == 1
    title, message, priority = ntfy_calls[0]
    assert "2" in title or "ticket" in title.lower()
    assert "ATL-030" in message
    assert priority == "low"


@pytest.mark.asyncio
async def test_ticket_dispatch_no_ntfy_when_nothing_dispatched(monkeypatch):
    """_ticket_dispatch does NOT send ntfy when no tickets were dispatched."""
    import asyncio as _asyncio
    from unittest.mock import patch

    ntfy_calls: list = []

    async def fake_ntfy(title, message, priority="default"):
        ntfy_calls.append(title)

    def immediate_task(coro):
        return _asyncio.ensure_future(coro)

    monkeypatch.setenv("ATLAS_TICKET_DISPATCH", "on")
    monkeypatch.setenv("ATLAS_AUTONOMOUS", "on")

    with patch("atlas_plus.brain.dispatcher.dispatch_all_eligible", return_value=[]), \
         patch("atlas_plus.brain.dispatcher.is_dispatch_enabled", return_value=True), \
         patch("atlas_plus.brain.server._ntfy", new=fake_ntfy), \
         patch("atlas_plus.brain.scheduler.asyncio.create_task", side_effect=immediate_task):
        await scheduler._ticket_dispatch()
        await _asyncio.sleep(0)

    assert ntfy_calls == [], "ntfy must not fire when no tickets were dispatched"


# ---------------------------------------------------------------------------
# _build_weekly_digest -- loop stats section
# ---------------------------------------------------------------------------

class TestBuildWeeklyDigest:
    """Unit tests for the weekly digest builder, focused on the loop stats section."""

    def _make_task(self, kind: str, status: str, session_id: str, finished_at: str | None):
        from unittest.mock import MagicMock
        t = MagicMock()
        t.kind = kind
        t.status = status
        t.session_id = session_id
        t.finished_at = finished_at
        return t

    def _all_patches(self, task_stats=None, list_tasks=None, tickets=None, dispatch_stats=None):
        """Return a context manager stack for patching all digest dependencies."""
        from contextlib import ExitStack
        from unittest.mock import patch as _patch
        stack = ExitStack()
        stack.enter_context(_patch("atlas_plus.brain.tasks.task_stats",
                                    return_value=task_stats or {}))
        stack.enter_context(_patch("atlas_plus.brain.tasks.list_tasks",
                                    return_value=list_tasks or []))
        stack.enter_context(_patch("atlas_plus.tickets.store.list_tickets",
                                    return_value=tickets or []))
        stack.enter_context(_patch("atlas_plus.brain.dispatcher.get_dispatch_stats",
                                    return_value=dispatch_stats or {"total_dispatched": 0, "enabled": False}))
        # Silence doctor checks that hit the filesystem
        stack.enter_context(_patch("atlas_plus.doctor.cli.check_memory_quality",
                                    return_value={}))
        stack.enter_context(_patch("atlas_plus.doctor.cli.check_research_quality",
                                    return_value={}))
        stack.enter_context(_patch("atlas_plus.brain.task_facts.pending_count",
                                    return_value=0))
        return stack

    def test_digest_includes_atlas_weekly_header(self):
        with self._all_patches():
            result = scheduler._build_weekly_digest()
        assert "Atlas Weekly Digest" in result

    def test_digest_includes_task_stats_when_available(self):
        with self._all_patches(task_stats={"done": 10, "failed": 2, "total": 12}):
            result = scheduler._build_weekly_digest()
        assert "12 total" in result
        assert "10 done" in result
        assert "2 failed" in result

    def test_digest_includes_ticket_breakdown(self):
        tickets = []
        for s in ("todo", "todo", "in-progress", "blocked", "done", "done", "done"):
            t = MagicMock()
            t.status = s
            tickets.append(t)
        with self._all_patches(tickets=tickets):
            result = scheduler._build_weekly_digest()
        assert "2 todo" in result
        assert "1 in-progress" in result
        assert "1 blocked" in result
        assert "3 done" in result

    def test_digest_loop_stats_shows_dispatched_count(self):
        from datetime import datetime, timezone, timedelta
        recent = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()[:10]
        verify_task = self._make_task("verify", "done", "verify-ticket-ATL-001", recent)
        with self._all_patches(
            list_tasks=[verify_task],
            dispatch_stats={"total_dispatched": 7, "enabled": True},
        ):
            result = scheduler._build_weekly_digest()
        assert "dispatched 7" in result

    def test_digest_loop_stats_shows_verify_pass_fail(self):
        from datetime import datetime, timezone, timedelta
        recent = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()[:10]
        tasks = [
            self._make_task("verify", "done",   "verify-ticket-ATL-001", recent),
            self._make_task("verify", "done",   "verify-ticket-ATL-002", recent),
            self._make_task("verify", "failed", "verify-ticket-ATL-003", recent),
        ]
        with self._all_patches(
            list_tasks=tasks,
            dispatch_stats={"total_dispatched": 3, "enabled": True},
        ):
            result = scheduler._build_weekly_digest()
        assert "2 pass" in result
        assert "1 fail" in result

    def test_digest_no_loop_stats_when_nothing_dispatched(self):
        with self._all_patches():
            result = scheduler._build_weekly_digest()
        assert "dispatched" not in result

    def test_digest_graceful_on_exception(self):
        """_build_weekly_digest must not raise even if a subsystem throws."""
        with patch("atlas_plus.brain.tasks.task_stats", side_effect=RuntimeError("db gone")):
            result = scheduler._build_weekly_digest()
        assert "Atlas Weekly Digest" in result
        assert "Generated:" in result


# ---------------------------------------------------------------------------
# start_scheduler: job registration
# ---------------------------------------------------------------------------

class TestStartScheduler:
    def test_registers_expected_job_ids(self):
        """start_scheduler must register all known job IDs."""
        from apscheduler.schedulers.asyncio import AsyncIOScheduler
        from unittest.mock import MagicMock, patch

        mock_scheduler = MagicMock(spec=AsyncIOScheduler)
        mock_scheduler.running = False
        registered_ids = []

        def capture_add_job(fn, trigger, id, **kwargs):
            registered_ids.append(id)

        mock_scheduler.add_job = capture_add_job
        mock_scheduler.add_listener = MagicMock()
        mock_scheduler.start = MagicMock()

        with patch("atlas_plus.brain.scheduler.AsyncIOScheduler",
                   return_value=mock_scheduler), \
             patch("atlas_plus.brain.scheduler._scheduler", None):
            scheduler.start_scheduler()

        expected = {
            "db-backup", "temporal-build", "suggest-facts", "curation",
            "research-batch", "patterns-rebuild", "gaps-report",
            "convergence-report", "unconfirmed-decay", "decay-report",
            "memory-agent", "claude-supervisor", "narrative-weekly",
            "memory-trends", "research-quality", "suggest-tags",
            "weekly-digest", "auto-session-log",
            "ticket-dispatch", "sla-check", "zombie-cleanup",
        }
        missing = expected - set(registered_ids)
        assert not missing, f"Missing scheduler jobs: {missing}"

    def test_returns_running_scheduler_if_already_started(self):
        """start_scheduler is idempotent -- returns existing scheduler if running."""
        from unittest.mock import MagicMock

        mock_sched = MagicMock()
        mock_sched.running = True

        with patch("atlas_plus.brain.scheduler._scheduler", mock_sched):
            result = scheduler.start_scheduler()

        assert result is mock_sched  # same object returned, no new scheduler created

    def test_stop_scheduler_calls_shutdown(self):
        from unittest.mock import MagicMock

        mock_sched = MagicMock()
        mock_sched.running = True

        with patch("atlas_plus.brain.scheduler._scheduler", mock_sched):
            scheduler.stop_scheduler()

        mock_sched.shutdown.assert_called_once_with(wait=False)


# ---------------------------------------------------------------------------
# _auto_session_log
# ---------------------------------------------------------------------------

class TestAutoSessionLog:
    @pytest.mark.asyncio
    async def test_calls_write_session_log_with_today(self):
        """_auto_session_log must call write_session_log(since='today')."""
        captured = {}

        def fake_write_session_log(since="today"):
            captured["since"] = since
            return {"ok": True, "paths_written": ["/a", "/b"], "entry": "some text"}

        with patch("atlas_plus.sprint_summary.write_session_log", fake_write_session_log):
            await scheduler._auto_session_log()

        assert captured.get("since") == "today"

    @pytest.mark.asyncio
    async def test_no_op_when_disabled(self, monkeypatch):
        """ATLAS_AUTO_SESSION_LOG=off prevents session log write."""
        monkeypatch.setenv("ATLAS_AUTO_SESSION_LOG", "off")
        called = []

        def fake_write(*args, **kwargs):
            called.append(True)
            return {"ok": True, "paths_written": []}

        with patch("atlas_plus.sprint_summary.write_session_log", fake_write):
            await scheduler._auto_session_log()

        assert not called, "write_session_log should not be called when disabled"

    @pytest.mark.asyncio
    async def test_graceful_on_exception(self):
        """_auto_session_log must not raise even when write_session_log throws."""
        with patch("atlas_plus.sprint_summary.write_session_log",
                   side_effect=RuntimeError("git gone")):
            await scheduler._auto_session_log()  # must not raise


# ---------------------------------------------------------------------------
# _build_weekly_digest: exhausted tasks section
# ---------------------------------------------------------------------------

class TestDigestExhaustedSection:
    def _make_task(self, kind, status, session_id="brain-test", finished_at=None):
        from atlas_plus.brain import tasks as ts
        t = ts.Task(
            task_id=f"task-{kind[:6]}-{status[:3]}",
            session_id=session_id,
            instruction=f"{kind} task",
            kind=kind,
            status=status,
            created_at="2026-05-20T00:00:00+00:00",
            attempts=3,
            max_attempts=3,
            finished_at=finished_at,
        )
        return t

    def _all_patches(self, exhausted=None, **kwargs):
        from unittest.mock import MagicMock, patch
        exhausted = exhausted or []

        class CM:
            def __init__(self_):
                self_.patches = [
                    patch("atlas_plus.brain.tasks.task_stats",
                          return_value={"done": 5, "failed": 1, "total": 6}),
                    patch("atlas_plus.doctor.cli.check_memory_quality", return_value={}),
                    patch("atlas_plus.doctor.cli.check_research_quality", return_value={}),
                    patch("atlas_plus.brain.task_facts.pending_count", return_value=0),
                    patch("atlas_plus.tickets.store.list_tickets", return_value=[]),
                    patch("atlas_plus.brain.tasks.list_tasks",
                          return_value=kwargs.get("list_tasks", [])),
                    patch("atlas_plus.brain.dispatcher.get_dispatch_stats",
                          return_value=kwargs.get("dispatch_stats", {"total_dispatched": 0})),
                    patch("atlas_plus.brain.tasks.list_exhausted_tasks", return_value=exhausted),
                ]
            def __enter__(self_):
                for p in self_.patches: p.start()
                return self_
            def __exit__(self_, *a):
                for p in self_.patches: p.stop()

        return CM()

    def test_digest_includes_exhausted_section_when_present(self):
        exhausted = [self._make_task("agent", "failed")]
        with self._all_patches(exhausted=exhausted):
            result = scheduler._build_weekly_digest()
        assert "exhausted" in result.lower() or "Exhausted" in result

    def test_digest_omits_exhausted_section_when_none(self):
        with self._all_patches(exhausted=[]):
            result = scheduler._build_weekly_digest()
        assert "Exhausted tasks" not in result


# ---------------------------------------------------------------------------
# memory tag coverage in weekly digest
# ---------------------------------------------------------------------------

class TestDigestTagCoverage:
    def test_digest_shows_untagged_hint_when_many_untagged(self):
        """Weekly digest includes untagged files hint when count > 10."""
        from unittest.mock import patch, MagicMock
        with patch("atlas_plus.brain.tasks.task_stats", return_value={}), \
             patch("atlas_plus.brain.tasks.list_tasks", return_value=[]), \
             patch("atlas_plus.tickets.store.list_tickets", return_value=[]), \
             patch("atlas_plus.brain.dispatcher.get_dispatch_stats",
                   return_value={"total_dispatched": 0}), \
             patch("atlas_plus.doctor.cli.check_memory_quality",
                   return_value={"total_files": 100, "confirmed_pct": 50, "tagged_pct": 30,
                                  "untagged_files": 25}), \
             patch("atlas_plus.doctor.cli.check_research_quality", return_value={}), \
             patch("atlas_plus.brain.task_facts.pending_count", return_value=0), \
             patch("atlas_plus.brain.tasks.list_exhausted_tasks", return_value=[]):
            result = scheduler._build_weekly_digest()
        assert "suggest-tags" in result.lower() or "Untagged" in result or "untagged" in result

    def test_digest_omits_untagged_hint_when_few_untagged(self):
        """Weekly digest omits untagged hint when count is <= 10 (within acceptable range)."""
        from unittest.mock import patch
        with patch("atlas_plus.brain.tasks.task_stats", return_value={}), \
             patch("atlas_plus.brain.tasks.list_tasks", return_value=[]), \
             patch("atlas_plus.tickets.store.list_tickets", return_value=[]), \
             patch("atlas_plus.brain.dispatcher.get_dispatch_stats",
                   return_value={"total_dispatched": 0}), \
             patch("atlas_plus.doctor.cli.check_memory_quality",
                   return_value={"total_files": 100, "confirmed_pct": 80, "tagged_pct": 90,
                                  "untagged_files": 5}), \
             patch("atlas_plus.doctor.cli.check_research_quality", return_value={}), \
             patch("atlas_plus.brain.task_facts.pending_count", return_value=0), \
             patch("atlas_plus.brain.tasks.list_exhausted_tasks", return_value=[]):
            result = scheduler._build_weekly_digest()
        assert "suggest-tags" not in result


# ---------------------------------------------------------------------------
# _count_untagged_by_scope
# ---------------------------------------------------------------------------

class TestCountUntaggedByScope:
    def _write_md(self, path, tags=None):
        """Write a minimal markdown file with optional tags in frontmatter."""
        path.parent.mkdir(parents=True, exist_ok=True)
        if tags is None:
            path.write_text("# No frontmatter\nbody\n", encoding="utf-8")
        elif tags == []:
            path.write_text("---\ntitle: test\ntags: []\n---\nbody\n", encoding="utf-8")
        else:
            path.write_text(f"---\ntitle: test\ntags: {tags}\n---\nbody\n", encoding="utf-8")

    def test_counts_untagged_files_by_scope(self, tmp_path):
        from unittest.mock import patch
        mem = tmp_path / ".memory"
        self._write_md(mem / "feedback" / "a.md")          # no frontmatter = untagged
        self._write_md(mem / "feedback" / "b.md", tags=[])  # empty tags = untagged
        self._write_md(mem / "feedback" / "c.md", tags=["style"])  # tagged = skip
        self._write_md(mem / "projects" / "x.md")           # no frontmatter = untagged
        with patch("os.environ.get",
                   side_effect=lambda k, d="": str(mem) if k == "ATLAS_MEMORY_ROOT" else d):
            counts = scheduler._count_untagged_by_scope()
        assert counts.get("feedback") == 2
        assert counts.get("projects") == 1

    def test_returns_empty_when_no_memory_dir(self, tmp_path):
        from unittest.mock import patch
        with patch("os.environ.get",
                   side_effect=lambda k, d="": str(tmp_path / "no-memory") if k == "ATLAS_MEMORY_ROOT" else d):
            counts = scheduler._count_untagged_by_scope()
        assert counts == {}

    def test_tagged_files_not_counted(self, tmp_path):
        from unittest.mock import patch
        mem = tmp_path / ".memory"
        self._write_md(mem / "reference" / "a.md", tags=["examplecorp"])
        self._write_md(mem / "reference" / "b.md", tags=["sip"])
        with patch("os.environ.get",
                   side_effect=lambda k, d="": str(mem) if k == "ATLAS_MEMORY_ROOT" else d):
            counts = scheduler._count_untagged_by_scope()
        assert counts.get("reference", 0) == 0

    def test_hidden_dirs_skipped(self, tmp_path):
        from unittest.mock import patch
        mem = tmp_path / ".memory"
        self._write_md(mem / ".git" / "config.md")
        with patch("os.environ.get",
                   side_effect=lambda k, d="": str(mem) if k == "ATLAS_MEMORY_ROOT" else d):
            counts = scheduler._count_untagged_by_scope()
        assert ".git" not in counts


class TestDigestScopeBreakdown:
    def test_digest_includes_scope_breakdown_when_untagged_high(self):
        """Top untagged scopes appear in digest when untagged > 10."""
        from unittest.mock import patch
        with patch("atlas_plus.brain.tasks.task_stats", return_value={}), \
             patch("atlas_plus.brain.tasks.list_tasks", return_value=[]), \
             patch("atlas_plus.tickets.store.list_tickets", return_value=[]), \
             patch("atlas_plus.brain.dispatcher.get_dispatch_stats",
                   return_value={"total_dispatched": 0}), \
             patch("atlas_plus.doctor.cli.check_memory_quality",
                   return_value={"total_files": 100, "confirmed_pct": 50, "tagged_pct": 30,
                                  "untagged_files": 30}), \
             patch("atlas_plus.doctor.cli.check_research_quality", return_value={}), \
             patch("atlas_plus.brain.task_facts.pending_count", return_value=0), \
             patch("atlas_plus.brain.tasks.list_exhausted_tasks", return_value=[]), \
             patch("atlas_plus.brain.scheduler._count_untagged_by_scope",
                   return_value={"feedback": 15, "projects": 8, "reference": 7}):
            result = scheduler._build_weekly_digest()
        assert "feedback" in result
        assert "Top untagged" in result or "untagged scopes" in result.lower()

    def test_digest_omits_scope_breakdown_when_untagged_low(self):
        from unittest.mock import patch
        with patch("atlas_plus.brain.tasks.task_stats", return_value={}), \
             patch("atlas_plus.brain.tasks.list_tasks", return_value=[]), \
             patch("atlas_plus.tickets.store.list_tickets", return_value=[]), \
             patch("atlas_plus.brain.dispatcher.get_dispatch_stats",
                   return_value={"total_dispatched": 0}), \
             patch("atlas_plus.doctor.cli.check_memory_quality",
                   return_value={"total_files": 100, "confirmed_pct": 80, "tagged_pct": 90,
                                  "untagged_files": 3}), \
             patch("atlas_plus.doctor.cli.check_research_quality", return_value={}), \
             patch("atlas_plus.brain.task_facts.pending_count", return_value=0), \
             patch("atlas_plus.brain.tasks.list_exhausted_tasks", return_value=[]):
            result = scheduler._build_weekly_digest()
        assert "Top untagged" not in result


# ---------------------------------------------------------------------------
# Outage catch-up (P-1)
# ---------------------------------------------------------------------------

class TestCatchupPlan:
    def _now(self):
        from datetime import datetime, timezone
        return datetime(2026, 8, 6, 12, 0, 0, tzinfo=timezone.utc)

    def test_fresh_runs_need_nothing(self):
        from atlas_plus.brain.scheduler import catchup_plan
        last = {"db-backup": "2026-08-06T02:00:00+00:00", "dream": "2026-08-06T04:30:00+00:00"}
        assert catchup_plan(last, self._now()) == []

    def test_overdue_daily_job_is_caught_up(self):
        from atlas_plus.brain.scheduler import catchup_plan
        last = {"db-backup": "2026-08-04T02:00:00+00:00"}  # 58h ago
        assert catchup_plan(last, self._now()) == ["db-backup"]

    def test_within_grace_is_not_overdue(self):
        from atlas_plus.brain.scheduler import catchup_plan
        last = {"db-backup": "2026-08-05T11:00:00+00:00"}  # 25h ago < 24+2 grace
        assert catchup_plan(last, self._now()) == []

    def test_weekly_cadence_respected(self):
        from atlas_plus.brain.scheduler import catchup_plan
        last = {
            "gaps-report": "2026-08-02T03:30:00+00:00",   # 4 days: fine for weekly
            "reverify": "2026-07-25T04:20:00+00:00",       # 12 days: overdue
        }
        assert catchup_plan(last, self._now()) == ["reverify"]

    def test_never_ran_jobs_are_left_alone(self):
        from atlas_plus.brain.scheduler import catchup_plan
        assert catchup_plan({}, self._now()) == []
        assert catchup_plan({"db-backup": None}, self._now()) == []

    def test_unparseable_heartbeat_skipped(self):
        from atlas_plus.brain.scheduler import catchup_plan
        assert catchup_plan({"db-backup": "not-a-date"}, self._now()) == []

    def test_unknown_jobs_ignored(self):
        from atlas_plus.brain.scheduler import catchup_plan
        assert catchup_plan({"sla-check": "2026-01-01T00:00:00+00:00"}, self._now()) == []


class TestScheduleCatchup:
    class _FakeScheduler:
        def __init__(self):
            self.jobs = []

        def add_job(self, func, trigger, **kwargs):
            self.jobs.append((func, trigger, kwargs))

    def test_overdue_jobs_queued_staggered(self, monkeypatch):
        from atlas_plus.brain import scheduler as sched

        monkeypatch.setattr(
            "atlas_plus.brain.tasks.list_job_runs",
            lambda *a, **k: [
                {"job_id": "db-backup", "last_run_at": "2026-01-01T02:00:00+00:00"},
                {"job_id": "dream", "last_run_at": "2026-01-01T04:30:00+00:00"},
            ],
        )
        fake = self._FakeScheduler()
        marker = object()
        overdue = sched._schedule_catchup(fake, {"db-backup": marker, "dream": marker})
        assert overdue == ["db-backup", "dream"]
        assert len(fake.jobs) == 2
        ids = [j[2]["id"] for j in fake.jobs]
        assert ids == ["db-backup-catchup", "dream-catchup"]
        run_dates = [j[2]["run_date"] for j in fake.jobs]
        assert (run_dates[1] - run_dates[0]).total_seconds() == 90

    def test_env_kill_switch(self, monkeypatch):
        from atlas_plus.brain import scheduler as sched
        monkeypatch.setenv("ATLAS_SCHEDULER_CATCHUP", "off")
        fake = self._FakeScheduler()
        assert sched._schedule_catchup(fake, {}) == []
        assert fake.jobs == []

    def test_ledger_read_failure_is_safe(self, monkeypatch):
        from atlas_plus.brain import scheduler as sched

        def boom(*a, **k):
            raise RuntimeError("db locked")

        monkeypatch.setattr("atlas_plus.brain.tasks.list_job_runs", boom)
        fake = self._FakeScheduler()
        assert sched._schedule_catchup(fake, {}) == []
