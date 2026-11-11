"""Tests for the scheduler heartbeat ledger + maintenance-lane health surface (WS2).

Covers three layers:
  - tasks.record_job_run / list_job_runs           -- the SQLite heartbeat ledger
  - scheduler._job_event_listener                  -- records a heartbeat per job
  - brain.maintenance.summarize_maintenance        -- ok/stale/error/unknown policy
  - GET /api/maintenance                           -- Helm fragment smoke test
plus a drift-guard that fails if a scheduler job is left unclassified.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

# Mirror test_web_app.py's env EXACTLY before importing the app. setdefault is
# only order-safe across test modules when the values agree, so the app's
# module-level auth constant must resolve to the same key whichever file imports
# the app first.
_TEST_KEY = "test-key-abc"
os.environ.setdefault("ATLAS_WEB_API_KEY", _TEST_KEY)
os.environ.setdefault("ATLAS_BRAIN_URL", "http://127.0.0.1:9999")  # non-routable
os.environ.setdefault("ATLAS_BRAIN_API_KEY", "brain-key")
os.environ.setdefault("ATLAS_MCP_TOKEN", "mcp-token")
AUTH = {"Authorization": f"Bearer {_TEST_KEY}"}

from fastapi.testclient import TestClient  # noqa: E402

from atlas_plus.brain import maintenance  # noqa: E402
from atlas_plus.brain import scheduler  # noqa: E402
from atlas_plus.brain import tasks as task_store  # noqa: E402
from atlas_plus.web.app import app  # noqa: E402

_client = TestClient(app, raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# Heartbeat ledger: record_job_run / list_job_runs
# ---------------------------------------------------------------------------

def _db(tmp_path):
    return tmp_path / "state.sqlite"


def test_record_and_list_job_run_roundtrip(tmp_path):
    db = _db(tmp_path)
    task_store.record_job_run("decay-report", "ok", db_path=db)
    task_store.record_job_run("curation", "error", error="boom", db_path=db)

    runs = {r["job_id"]: r for r in task_store.list_job_runs(db_path=db)}
    assert set(runs) == {"decay-report", "curation"}
    assert runs["decay-report"]["outcome"] == "ok"
    assert runs["decay-report"]["error"] is None
    assert runs["decay-report"]["run_count"] == 1
    assert runs["curation"]["outcome"] == "error"
    assert runs["curation"]["error"] == "boom"


def test_record_job_run_upserts_and_increments(tmp_path):
    db = _db(tmp_path)
    task_store.record_job_run("curation", "ok", when="2026-06-10T03:15:00+00:00", db_path=db)
    task_store.record_job_run("curation", "error", error="late failure",
                              when="2026-06-11T03:15:00+00:00", db_path=db)

    runs = task_store.list_job_runs(db_path=db)
    assert len(runs) == 1, "same job_id must upsert, not insert a second row"
    row = runs[0]
    assert row["run_count"] == 2
    assert row["outcome"] == "error"  # latest write wins
    assert row["last_run_at"] == "2026-06-11T03:15:00+00:00"
    assert row["error"] == "late failure"


def test_record_job_run_truncates_long_error(tmp_path):
    db = _db(tmp_path)
    task_store.record_job_run("reverify", "error", error="x" * 9000, db_path=db)
    row = task_store.list_job_runs(db_path=db)[0]
    assert len(row["error"]) <= task_store._RESULT_MAX_CHARS


def test_list_job_runs_empty_db_returns_empty(tmp_path):
    assert task_store.list_job_runs(db_path=_db(tmp_path)) == []


def test_list_job_runs_orders_newest_first(tmp_path):
    db = _db(tmp_path)
    task_store.record_job_run("a", "ok", when="2026-06-01T00:00:00+00:00", db_path=db)
    task_store.record_job_run("b", "ok", when="2026-06-09T00:00:00+00:00", db_path=db)
    task_store.record_job_run("c", "ok", when="2026-06-05T00:00:00+00:00", db_path=db)
    assert [r["job_id"] for r in task_store.list_job_runs(db_path=db)] == ["b", "c", "a"]


# ---------------------------------------------------------------------------
# Scheduler listener writes a heartbeat for every job event
# ---------------------------------------------------------------------------

class _Event:
    """Stand-in for an APScheduler JobExecutionEvent."""

    def __init__(self, job_id, exception=None, traceback=None):
        self.job_id = job_id
        self.exception = exception
        self.traceback = traceback


def test_listener_records_ok_on_clean_execution(monkeypatch):
    calls = []
    monkeypatch.setattr(task_store, "record_job_run",
                        lambda *a, **k: calls.append((a, k)))
    scheduler._job_event_listener(_Event("decay-report"))
    assert calls == [(("decay-report", "ok"), {})]


def test_listener_records_error_with_message(monkeypatch):
    calls = []
    monkeypatch.setattr(task_store, "record_job_run",
                        lambda *a, **k: calls.append((a, k)))
    scheduler._job_event_listener(_Event("curation", exception=RuntimeError("nope")))
    assert len(calls) == 1
    args, kwargs = calls[0]
    assert args == ("curation", "error")
    assert kwargs.get("error") == "nope"


def test_listener_swallows_record_failure(monkeypatch):
    """A bookkeeping failure must never stop the scheduler from running jobs."""
    def boom(*a, **k):
        raise RuntimeError("db gone")

    monkeypatch.setattr(task_store, "record_job_run", boom)
    # Must not raise for either a clean or a failed job event.
    scheduler._job_event_listener(_Event("decay-report"))
    scheduler._job_event_listener(_Event("curation", exception=ValueError("x")))


# ---------------------------------------------------------------------------
# Maintenance policy: summarize_maintenance
# ---------------------------------------------------------------------------

_NOW = datetime(2026, 6, 12, 12, 0, 0, tzinfo=timezone.utc)


def _run(job_id, hours_ago, outcome="ok", error=None):
    return {
        "job_id": job_id,
        "last_run_at": (_NOW - timedelta(hours=hours_ago)).isoformat(),
        "outcome": outcome,
        "error": error,
        "run_count": 1,
    }


def _status_of(summary, job_id):
    return next(j["status"] for j in summary["jobs"] if j["job_id"] == job_id)


def test_summarize_classifies_each_status():
    runs = [
        _run("db-backup", 2),             # daily, fresh -> ok
        _run("temporal-build", 40),       # daily, > 36h -> stale
        _run("decay-report", 100),        # weekly, < 192h -> ok
        _run("gaps-report", 200),         # weekly, > 192h -> stale
        _run("curation", 1, outcome="error", error="kaboom"),  # error regardless of age
        # suggest-facts intentionally omitted -> unknown
    ]
    summary = maintenance.summarize_maintenance(runs, now=_NOW)

    assert _status_of(summary, "db-backup") == "ok"
    assert _status_of(summary, "temporal-build") == "stale"
    assert _status_of(summary, "decay-report") == "ok"
    assert _status_of(summary, "gaps-report") == "stale"
    assert _status_of(summary, "curation") == "error"
    assert _status_of(summary, "suggest-facts") == "unknown"
    # error present -> overall error
    assert summary["overall"] == "error"
    # every tracked job appears exactly once
    assert len(summary["jobs"]) == len(maintenance.MAINTENANCE_JOBS)
    assert sum(summary["counts"].values()) == len(maintenance.MAINTENANCE_JOBS)


def test_summarize_overall_precedence_stale_over_ok():
    runs = [_run(j["job_id"], 2) for j in maintenance.MAINTENANCE_JOBS]
    # make one daily job stale
    runs[0] = _run(maintenance.MAINTENANCE_JOBS[0]["job_id"], 99)
    summary = maintenance.summarize_maintenance(runs, now=_NOW)
    assert summary["overall"] == "stale"


def test_summarize_overall_ok_when_all_fresh():
    runs = [_run(j["job_id"], 1) for j in maintenance.MAINTENANCE_JOBS]
    summary = maintenance.summarize_maintenance(runs, now=_NOW)
    assert summary["overall"] == "ok"
    assert summary["counts"]["ok"] == len(maintenance.MAINTENANCE_JOBS)


def test_summarize_empty_is_all_unknown():
    summary = maintenance.summarize_maintenance([], now=_NOW)
    assert summary["overall"] == "unknown"
    assert summary["counts"]["unknown"] == len(maintenance.MAINTENANCE_JOBS)
    assert all(j["age_label"] == "never" for j in summary["jobs"])


def test_summarize_handles_naive_now():
    """A naive now must be treated as UTC, not crash on tz-aware subtraction."""
    summary = maintenance.summarize_maintenance([_run("db-backup", 2)],
                                                now=_NOW.replace(tzinfo=None))
    assert _status_of(summary, "db-backup") == "ok"


def test_summarize_bad_timestamp_is_unknown():
    runs = [{"job_id": "db-backup", "last_run_at": "not-a-date",
             "outcome": "ok", "error": None, "run_count": 1}]
    summary = maintenance.summarize_maintenance(runs, now=_NOW)
    assert _status_of(summary, "db-backup") == "unknown"


@pytest.mark.parametrize("hours,expected", [
    (None, "never"),
    (0.25, "15m ago"),
    (5, "5h ago"),
    (72, "3d ago"),
])
def test_humanize_age(hours, expected):
    assert maintenance._humanize_age(hours) == expected


# ---------------------------------------------------------------------------
# Drift guard: every scheduler job must be classified
# ---------------------------------------------------------------------------

def test_every_scheduler_job_is_classified():
    """maintenance + operational sets must exactly cover the registered jobs.

    Fails when someone adds a scheduler job without deciding whether it is a
    maintenance lane (surfaced in Helm) or an operational job (excluded).
    """
    from apscheduler.schedulers.asyncio import AsyncIOScheduler

    mock_scheduler = MagicMock(spec=AsyncIOScheduler)
    mock_scheduler.running = False
    registered = []
    mock_scheduler.add_job = lambda fn, trigger, id, **kw: registered.append(id)
    mock_scheduler.add_listener = MagicMock()
    mock_scheduler.start = MagicMock()
    mock_scheduler.get_jobs = MagicMock(return_value=[])

    with patch("atlas_plus.brain.scheduler.AsyncIOScheduler", return_value=mock_scheduler), \
         patch("atlas_plus.brain.scheduler._scheduler", None):
        scheduler.start_scheduler()

    # '*-catchup' jobs are transient one-shots derived from classified bases
    # (P-1 outage recovery); classify by their base id.
    registered_set = {r[:-8] if r.endswith("-catchup") else r for r in registered}
    tracked = maintenance.maintenance_job_ids() | set(maintenance.OPERATIONAL_JOBS)
    assert registered_set == tracked, (
        f"unclassified scheduler jobs: {registered_set - tracked}; "
        f"phantom (tracked but not registered): {tracked - registered_set}"
    )
    assert maintenance.maintenance_job_ids().isdisjoint(maintenance.OPERATIONAL_JOBS)


# ---------------------------------------------------------------------------
# Helm endpoint smoke test
# ---------------------------------------------------------------------------

def test_maintenance_endpoint_requires_auth():
    r = _client.get("/api/maintenance", headers={"Authorization": "Bearer wrong"})
    assert r.status_code in (401, 403)


def test_maintenance_endpoint_renders_health(monkeypatch):
    now = datetime.now(timezone.utc)

    def fake_runs(*a, **k):
        return [
            {"job_id": "db-backup", "last_run_at": now.isoformat(),
             "outcome": "ok", "error": None, "run_count": 5},
            {"job_id": "curation",
             "last_run_at": (now - timedelta(hours=1)).isoformat(),
             "outcome": "error", "error": "import blew up", "run_count": 2},
        ]

    monkeypatch.setattr("atlas_plus.brain.tasks.list_job_runs", fake_runs)
    r = _client.get("/api/maintenance", headers=AUTH)
    assert r.status_code == 200
    body = r.text
    # A known label always renders, and the errored lane drives an "error" rollup.
    assert "DB backup" in body
    assert "Curation report" in body
    assert "error" in body


class TestCatchupHeartbeatCanonical:
    """Operator-reported 2026-09-27: catch-up runs recorded heartbeats under
    '<job>-catchup', so the canonical job stayed stale forever and every
    Brain restart replayed the whole weekly fleet (repeated ntfy pushes)."""

    def test_listener_records_catchup_under_canonical_id(self, monkeypatch):
        from types import SimpleNamespace
        from atlas_plus.brain import scheduler as sched

        recorded = {}
        monkeypatch.setattr(
            "atlas_plus.brain.tasks.record_job_run",
            lambda job_id, status, error=None: recorded.update(job=job_id, status=status),
        )
        sched._job_event_listener(
            SimpleNamespace(job_id="weekly-digest-catchup", exception=None, traceback=None)
        )
        assert recorded == {"job": "weekly-digest", "status": "ok"}

    def test_catchup_plan_uses_freshest_of_both_ids(self):
        from datetime import datetime, timezone
        from atlas_plus.brain.scheduler import catchup_plan

        now = datetime(2026, 9, 27, 12, 0, tzinfo=timezone.utc)
        # Canonical stamp ancient, but a catch-up ran yesterday: NOT overdue.
        plan = catchup_plan(
            {
                "weekly-digest": "2026-09-06T09:00:00+00:00",
                "weekly-digest-catchup": "2026-09-26T09:00:00+00:00",
            },
            now,
        )
        assert "weekly-digest" not in plan
        # Both stale -> still caught up as before.
        plan = catchup_plan({"weekly-digest": "2026-09-06T09:00:00+00:00"}, now)
        assert "weekly-digest" in plan
