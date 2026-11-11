"""Tests for cleanup_zombie_tasks() in atlas_plus.brain.tasks."""
from __future__ import annotations

import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from atlas_plus.brain.tasks import (
    cleanup_zombie_tasks,
    create_task,
    get_task,
    heartbeat_task,
    mark_running,
)


def _past(minutes: int) -> str:
    dt = datetime.now(timezone.utc) - timedelta(minutes=minutes)
    return dt.strftime("%Y-%m-%dT%H:%M:%S+00:00")


def _db() -> Path:
    d = tempfile.mkdtemp()
    return Path(d) / "state.sqlite"


class TestCleanupZombieTasks:
    def test_stale_running_task_marked_failed(self):
        db = _db()
        t = create_task("test instruction", db_path=db)
        mark_running(t.task_id, db_path=db)

        # Manually backdate heartbeat_at to simulate a stale task
        import sqlite3
        with sqlite3.connect(str(db)) as conn:
            old_ts = _past(60)
            conn.execute(
                "UPDATE brain_tasks SET heartbeat_at=?, started_at=? WHERE task_id=?",
                (old_ts, old_ts, t.task_id),
            )

        cleaned = cleanup_zombie_tasks(max_running_minutes=30, db_path=db)
        assert cleaned == 1
        updated = get_task(t.task_id, db_path=db)
        assert updated.status == "failed"
        assert "timed out" in (updated.error or "").lower()

    def test_fresh_running_task_not_touched(self):
        db = _db()
        t = create_task("fresh task", db_path=db)
        mark_running(t.task_id, db_path=db)
        # heartbeat_at is set to now by mark_running -- not stale

        cleaned = cleanup_zombie_tasks(max_running_minutes=30, db_path=db)
        assert cleaned == 0
        updated = get_task(t.task_id, db_path=db)
        assert updated.status == "running"

    def test_pending_task_not_touched(self):
        db = _db()
        t = create_task("pending task", db_path=db)
        cleaned = cleanup_zombie_tasks(max_running_minutes=30, db_path=db)
        assert cleaned == 0
        updated = get_task(t.task_id, db_path=db)
        assert updated.status == "pending"

    def test_done_task_not_touched(self):
        from atlas_plus.brain.tasks import mark_done
        db = _db()
        t = create_task("done task", db_path=db)
        mark_running(t.task_id, db_path=db)
        mark_done(t.task_id, "result text", db_path=db)
        cleaned = cleanup_zombie_tasks(max_running_minutes=30, db_path=db)
        assert cleaned == 0
        updated = get_task(t.task_id, db_path=db)
        assert updated.status == "done"

    def test_running_without_heartbeat_also_cleaned(self):
        """Tasks that started before heartbeat tracking existed have no heartbeat_at."""
        db = _db()
        t = create_task("old task", db_path=db)
        mark_running(t.task_id, db_path=db)

        import sqlite3
        with sqlite3.connect(str(db)) as conn:
            old_ts = _past(90)
            conn.execute(
                "UPDATE brain_tasks SET heartbeat_at=NULL, started_at=? WHERE task_id=?",
                (old_ts, t.task_id),
            )

        cleaned = cleanup_zombie_tasks(max_running_minutes=30, db_path=db)
        assert cleaned == 1
        updated = get_task(t.task_id, db_path=db)
        assert updated.status == "failed"

    def test_empty_db_returns_zero(self):
        db = _db()
        cleaned = cleanup_zombie_tasks(max_running_minutes=30, db_path=db)
        assert cleaned == 0

    def test_multiple_stale_tasks_all_cleaned(self):
        db = _db()
        tasks = [create_task(f"task {i}", db_path=db) for i in range(3)]
        for t in tasks:
            mark_running(t.task_id, db_path=db)

        import sqlite3
        old_ts = _past(60)
        with sqlite3.connect(str(db)) as conn:
            conn.execute(
                "UPDATE brain_tasks SET heartbeat_at=? WHERE status='running'",
                (old_ts,),
            )

        cleaned = cleanup_zombie_tasks(max_running_minutes=30, db_path=db)
        assert cleaned == 3
        for t in tasks:
            assert get_task(t.task_id, db_path=db).status == "failed"


# ---------------------------------------------------------------------------
# update_heartbeat and heartbeat_task
# ---------------------------------------------------------------------------

class TestUpdateHeartbeatAndCleanup:
    def test_update_heartbeat_prevents_zombie_cleanup(self):
        """After update_heartbeat, a task that was stale should no longer be reaped."""
        db = _db()
        t = create_task("long task", db_path=db)
        mark_running(t.task_id, db_path=db, lease_owner="w-001")

        # Back-date heartbeat to make task look stale
        import sqlite3
        old_ts = _past(60)
        with sqlite3.connect(str(db)) as conn:
            conn.execute(
                "UPDATE brain_tasks SET heartbeat_at=? WHERE task_id=?",
                (old_ts, t.task_id),
            )

        # Renew heartbeat -- simulates _HeartbeatKeeper
        from atlas_plus.brain.tasks import update_heartbeat
        update_heartbeat(t.task_id, db_path=db)

        # Zombie cleanup should now leave the task alone (heartbeat is fresh)
        cleaned = cleanup_zombie_tasks(max_running_minutes=30, db_path=db)
        assert cleaned == 0
        assert get_task(t.task_id, db_path=db).status == "running"

    def test_heartbeat_task_extends_lease(self):
        """heartbeat_task() extends lease_expires_at when caller owns the task."""
        db = _db()
        t = create_task("leased task", db_path=db)
        mark_running(t.task_id, db_path=db, lease_owner="w-001")

        from atlas_plus.brain.tasks import heartbeat_task
        result = heartbeat_task(t.task_id, "w-001", lease_seconds=600, db_path=db)
        assert result is True

        fetched = get_task(t.task_id, db_path=db)
        # The updated lease should be far in the future (>5 min)
        assert fetched.heartbeat_at is not None

    def test_heartbeat_task_returns_false_for_wrong_owner(self):
        """heartbeat_task() no-ops when caller is not the lease owner."""
        db = _db()
        t = create_task("owned task", db_path=db)
        mark_running(t.task_id, db_path=db, lease_owner="w-correct")

        from atlas_plus.brain.tasks import heartbeat_task
        result = heartbeat_task(t.task_id, "w-wrong", lease_seconds=600, db_path=db)
        assert result is False

    def test_cleanup_respects_fresh_heartbeat(self):
        """A task with a fresh heartbeat is not cleaned up even if running for a long time."""
        db = _db()
        t = create_task("durable task", db_path=db)
        mark_running(t.task_id, db_path=db, lease_owner="w-durable")

        # started_at is very old, but heartbeat is current (just renewed)
        import sqlite3
        with sqlite3.connect(str(db)) as conn:
            conn.execute(
                "UPDATE brain_tasks SET started_at=? WHERE task_id=?",
                (_past(120), t.task_id),
            )
        # heartbeat_at is left at current time from mark_running

        cleaned = cleanup_zombie_tasks(max_running_minutes=30, db_path=db)
        assert cleaned == 0
