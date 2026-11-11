"""Tests for atlas_plus.brain.tasks -- task store lifecycle."""

from __future__ import annotations

import pytest

from atlas_plus.brain import tasks as ts


@pytest.fixture()
def db(tmp_path):
    return tmp_path / "state.sqlite"


def test_create_task(db):
    task = ts.create_task("do something", db_path=db)
    assert task.status == "pending"
    assert task.task_id.startswith("task-")
    assert task.session_id.startswith("brain-")
    assert task.result is None
    assert task.error is None


def test_create_task_custom_ids(db):
    task = ts.create_task("do something", task_id="t-001", session_id="sess-abc", db_path=db)
    assert task.task_id == "t-001"
    assert task.session_id == "sess-abc"


def test_create_task_with_worker_metadata(db):
    task = ts.create_task(
        "digest research",
        kind="research_digest",
        priority=2,
        agent="fast",
        max_attempts=3,
        db_path=db,
    )

    assert task.kind == "research_digest"
    assert task.priority == 2
    assert task.agent == "fast"
    assert task.attempts == 0
    assert task.max_attempts == 3
    assert task.lease_owner is None
    assert task.artifact_path is None


def test_lifecycle(db):
    task = ts.create_task("run a job", db_path=db)
    ts.mark_running(task.task_id, db_path=db)

    fetched = ts.get_task(task.task_id, db_path=db)
    assert fetched.status == "running"
    assert fetched.started_at is not None
    assert fetched.finished_at is None

    ts.mark_done(task.task_id, "all done", db_path=db)
    fetched = ts.get_task(task.task_id, db_path=db)
    assert fetched.status == "done"
    assert fetched.result == "all done"
    assert fetched.finished_at is not None


def test_lifecycle_failed(db):
    task = ts.create_task("run a failing job", db_path=db)
    ts.mark_running(task.task_id, db_path=db)
    ts.mark_failed(task.task_id, "something broke", db_path=db)

    fetched = ts.get_task(task.task_id, db_path=db)
    assert fetched.status == "failed"
    assert fetched.error == "something broke"
    assert fetched.result is None


def test_get_missing_task(db):
    assert ts.get_task("does-not-exist", db_path=db) is None


def test_list_tasks(db):
    ts.create_task("job 1", db_path=db)
    t2 = ts.create_task("job 2", db_path=db)
    ts.mark_running(t2.task_id, db_path=db)
    ts.mark_done(t2.task_id, "result", db_path=db)

    all_tasks = ts.list_tasks(db_path=db)
    assert len(all_tasks) == 2

    pending = ts.list_tasks(status="pending", db_path=db)
    assert len(pending) == 1
    assert pending[0].status == "pending"

    done = ts.list_tasks(status="done", db_path=db)
    assert len(done) == 1
    assert done[0].result == "result"


def test_list_tasks_agent_filter(db):
    ts.create_task("task a", agent="brain", db_path=db)
    ts.create_task("task b", agent="claude-code", db_path=db)
    ts.create_task("task c", agent="brain", db_path=db)

    brain_tasks = ts.list_tasks(agent="brain", db_path=db)
    assert len(brain_tasks) == 2
    assert all(t.agent == "brain" for t in brain_tasks)

    claude_tasks = ts.list_tasks(agent="claude-code", db_path=db)
    assert len(claude_tasks) == 1
    assert claude_tasks[0].agent == "claude-code"


def test_find_duplicate_pending_returns_existing(db):
    t1 = ts.create_task("Do the thing", agent="brain", db_path=db)
    dup = ts.find_duplicate_pending("Do the thing", "brain", dedup_window_minutes=5, db_path=db)
    assert dup is not None
    assert dup.task_id == t1.task_id


def test_find_duplicate_pending_no_match(db):
    ts.create_task("Do one thing", agent="brain", db_path=db)
    result = ts.find_duplicate_pending("Do another thing", "brain", db_path=db)
    assert result is None


def test_find_duplicate_pending_different_agent_no_match(db):
    ts.create_task("Do the thing", agent="brain", db_path=db)
    result = ts.find_duplicate_pending("Do the thing", "coder", db_path=db)
    assert result is None


def test_create_task_dedup_returns_existing(db):
    t1 = ts.create_task("Exact instruction", agent="brain", db_path=db)
    t2 = ts.create_task("Exact instruction", agent="brain", db_path=db, dedup=True)
    assert t2.task_id == t1.task_id


def test_create_task_dedup_false_creates_new(db):
    t1 = ts.create_task("Same instruction", agent="brain", db_path=db)
    t2 = ts.create_task("Same instruction", agent="brain", db_path=db, dedup=False)
    assert t2.task_id != t1.task_id


def test_claim_task_notification_first_wins(db):
    # The first push attempt for a (task_id, status) wins; repeats are suppressed.
    # This is what stops the inline server path and the worker path from both
    # firing a phone notification for the same completion.
    assert ts.claim_task_notification("task-abc", "done", db_path=db) is True
    assert ts.claim_task_notification("task-abc", "done", db_path=db) is False
    assert ts.claim_task_notification("task-abc", "done", db_path=db) is False


def test_claim_task_notification_distinct_status_independent(db):
    # done and failed are separate events for the same task -- each may notify once.
    assert ts.claim_task_notification("task-xyz", "done", db_path=db) is True
    assert ts.claim_task_notification("task-xyz", "failed", db_path=db) is True
    assert ts.claim_task_notification("task-xyz", "done", db_path=db) is False


def test_claim_task_notification_distinct_tasks_independent(db):
    assert ts.claim_task_notification("task-1", "done", db_path=db) is True
    assert ts.claim_task_notification("task-2", "done", db_path=db) is True


def test_list_tasks_combined_status_agent_filter(db):
    t = ts.create_task("task x", agent="brain", db_path=db)
    ts.mark_running(t.task_id, db_path=db)
    ts.mark_done(t.task_id, "done!", db_path=db)
    ts.create_task("task y", agent="brain", db_path=db)  # still pending

    done_brain = ts.list_tasks(status="done", agent="brain", db_path=db)
    assert len(done_brain) == 1
    assert done_brain[0].status == "done"

    pending_brain = ts.list_tasks(status="pending", agent="brain", db_path=db)
    assert len(pending_brain) == 1


def test_claim_next_task_orders_by_priority_and_sets_lease(db):
    low = ts.create_task("low", priority=9, db_path=db)
    high = ts.create_task("high", priority=1, db_path=db)

    claimed = ts.claim_next_task("worker-1", db_path=db)

    assert claimed is not None
    assert claimed.task_id == high.task_id
    assert claimed.status == "running"
    assert claimed.attempts == 1
    assert claimed.lease_owner == "worker-1"
    assert claimed.lease_expires_at is not None
    assert claimed.heartbeat_at is not None
    assert ts.get_task(low.task_id, db_path=db).status == "pending"


def test_claim_next_task_reclaims_expired_lease(db):
    task = ts.create_task("stale", db_path=db)
    claimed = ts.claim_next_task("worker-1", lease_seconds=-1, db_path=db)

    assert claimed.task_id == task.task_id
    reclaimed = ts.claim_next_task("worker-2", db_path=db)

    assert reclaimed is not None
    assert reclaimed.task_id == task.task_id
    assert reclaimed.attempts == 2
    assert reclaimed.lease_owner == "worker-2"


def test_heartbeat_extends_only_owned_lease(db):
    task = ts.create_task("lease", db_path=db)
    claimed = ts.claim_next_task("worker-1", db_path=db)

    assert claimed.task_id == task.task_id
    assert ts.heartbeat_task(claimed.task_id, "other", db_path=db) is False
    assert ts.heartbeat_task(claimed.task_id, "worker-1", db_path=db) is True


def test_result_truncation(db):
    long_result = "x" * 10000
    task = ts.create_task("heavy job", db_path=db)
    ts.mark_done(task.task_id, long_result, db_path=db)

    fetched = ts.get_task(task.task_id, db_path=db)
    assert len(fetched.result) == ts._RESULT_MAX_CHARS


def test_as_dict(db):
    task = ts.create_task("dict test", db_path=db)
    d = task.as_dict()
    assert d["task_id"] == task.task_id
    assert d["status"] == "pending"
    assert "instruction" in d


def test_update_task_agent(db):
    task = ts.create_task("route me", db_path=db)
    assert task.agent is None

    ts.update_task_agent(task.task_id, "coder", db_path=db)
    fetched = ts.get_task(task.task_id, db_path=db)
    assert fetched.agent == "coder"

    # update again (credit fallback scenario)
    ts.update_task_agent(task.task_id, "brain", db_path=db)
    fetched2 = ts.get_task(task.task_id, db_path=db)
    assert fetched2.agent == "brain"


def test_update_task_agent_on_nonexistent_is_noop(db):
    ts.update_task_agent("task-doesnotexist", "brain", db_path=db)


def test_set_task_priority_pending(db):
    task = ts.create_task("important job", priority=5, db_path=db)
    updated = ts.set_task_priority(task.task_id, 1, db_path=db)
    assert updated is True
    fetched = ts.get_task(task.task_id, db_path=db)
    assert fetched.priority == 1


def test_set_task_priority_not_pending(db):
    task = ts.create_task("done job", db_path=db)
    ts.mark_done(task.task_id, "result", db_path=db)
    updated = ts.set_task_priority(task.task_id, 1, db_path=db)
    assert updated is False  # can't bump a finished task


def test_task_stats_empty(db):
    stats = ts.task_stats(since_days=7, db_path=db)
    assert "today" in stats
    assert "pending_total" in stats
    assert "by_agent_last_Nd" in stats
    assert stats["pending_total"] == 0


def test_task_stats_counts(db):
    ts.create_task("job 1", db_path=db, agent="brain")
    t2 = ts.create_task("job 2", db_path=db, agent="fast")
    ts.mark_running(t2.task_id, db_path=db)
    ts.mark_done(t2.task_id, "ok", db_path=db)

    stats = ts.task_stats(since_days=7, db_path=db)
    assert stats["pending_total"] == 1
    assert stats["today"].get("done", 0) == 1


def test_list_tasks_sort_by_priority(db):
    low  = ts.create_task("low",  priority=9, db_path=db)
    high = ts.create_task("high", priority=1, db_path=db)
    mid  = ts.create_task("mid",  priority=5, db_path=db)

    tasks = ts.list_tasks(db_path=db, sort_by="priority", sort_desc=False)
    priorities = [t.priority for t in tasks]
    assert priorities == sorted(priorities), "should be ascending by priority"
    assert tasks[0].task_id == high.task_id


def test_list_tasks_sort_by_invalid_column_falls_back_to_created_at(db):
    ts.create_task("a", db_path=db)
    ts.create_task("b", db_path=db)
    tasks = ts.list_tasks(db_path=db, sort_by="__invalid__")
    assert len(tasks) == 2  # should not raise; falls back gracefully


def test_list_tasks_sort_desc_default(db):
    t1 = ts.create_task("first",  db_path=db)
    t2 = ts.create_task("second", db_path=db)
    tasks = ts.list_tasks(db_path=db)
    # Default created_at DESC means most recent first
    assert tasks[0].task_id == t2.task_id


# ---------------------------------------------------------------------------
# worker_detail
# ---------------------------------------------------------------------------

class TestWorkerDetail:
    def test_empty_db_returns_zero_counts(self, tmp_path):
        result = ts.worker_detail(db_path=tmp_path / "state.sqlite")
        assert result["active_workers"] == []
        assert result["queue_depth"] == 0
        assert result["zombie_count"] == 0

    def test_pending_counted_in_queue_depth(self, tmp_path):
        db = tmp_path / "state.sqlite"
        ts.create_task("job 1", db_path=db)
        ts.create_task("job 2", db_path=db)
        result = ts.worker_detail(db_path=db)
        assert result["queue_depth"] == 2

    def test_pending_by_agent_populated(self, tmp_path):
        db = tmp_path / "state.sqlite"
        ts.create_task("brain job", agent="brain", db_path=db)
        ts.create_task("fast job", agent="fast", db_path=db)
        result = ts.worker_detail(db_path=db)
        pba = result["pending_by_agent"]
        assert pba.get("brain", 0) == 1
        assert pba.get("fast", 0) == 1

    def test_active_worker_detected(self, tmp_path):
        db = tmp_path / "state.sqlite"
        task = ts.create_task("running job", db_path=db)
        ts.mark_running(task.task_id, db_path=db, lease_owner="worker-abc-001")
        result = ts.worker_detail(db_path=db)
        assert len(result["active_workers"]) == 1
        assert result["active_workers"][0]["worker_id"] == "worker-abc-001"
        assert result["active_workers"][0]["running_tasks"] == 1

    def test_zombie_counted_when_stale_heartbeat(self, tmp_path):
        db = tmp_path / "state.sqlite"
        task = ts.create_task("stale job", db_path=db)
        # Force a very old heartbeat by marking running then manually setting stale heartbeat
        ts.mark_running(task.task_id, db_path=db, lease_owner="old-worker")
        import sqlite3
        conn = sqlite3.connect(str(db))
        conn.execute(
            "UPDATE brain_tasks SET heartbeat_at='2020-01-01T00:00:00' WHERE task_id=?",
            (task.task_id,),
        )
        conn.commit()
        conn.close()
        result = ts.worker_detail(db_path=db)
        assert result["zombie_count"] == 1

    def test_nonexistent_db_returns_empty(self, tmp_path):
        result = ts.worker_detail(db_path=tmp_path / "nonexistent.sqlite")
        assert result["active_workers"] == []
        assert result["queue_depth"] == 0


# ---------------------------------------------------------------------------
# result_digest
# ---------------------------------------------------------------------------

class TestResultDigest:
    def test_set_result_digest_on_done_task(self, tmp_path):
        db = tmp_path / "state.sqlite"
        task = ts.create_task("research Python async", db_path=db)
        ts.mark_done(task.task_id, "Long result text here.", db_path=db)
        ts.set_result_digest(task.task_id, "2-sentence summary.", db_path=db)
        fetched = ts.get_task(task.task_id, db_path=db)
        assert fetched.result_digest == "2-sentence summary."

    def test_digest_truncated_to_500_chars(self, tmp_path):
        db = tmp_path / "state.sqlite"
        task = ts.create_task("job", db_path=db)
        ts.mark_done(task.task_id, "result", db_path=db)
        long_digest = "x" * 600
        ts.set_result_digest(task.task_id, long_digest, db_path=db)
        fetched = ts.get_task(task.task_id, db_path=db)
        assert fetched.result_digest is not None
        assert len(fetched.result_digest) <= 500

    def test_digest_not_written_for_pending_task(self, tmp_path):
        db = tmp_path / "state.sqlite"
        task = ts.create_task("still pending", db_path=db)
        ts.set_result_digest(task.task_id, "This should not be stored.", db_path=db)
        fetched = ts.get_task(task.task_id, db_path=db)
        # Task is pending, not done -- digest should NOT be set
        assert fetched.result_digest is None

    def test_task_as_dict_includes_result_digest(self, tmp_path):
        db = tmp_path / "state.sqlite"
        task = ts.create_task("job", db_path=db)
        ts.mark_done(task.task_id, "result", db_path=db)
        ts.set_result_digest(task.task_id, "summary", db_path=db)
        fetched = ts.get_task(task.task_id, db_path=db)
        d = fetched.as_dict()
        assert "result_digest" in d
        assert d["result_digest"] == "summary"


# ---------------------------------------------------------------------------
# task_attempts telemetry
# ---------------------------------------------------------------------------

class TestRecordTaskAttempt:
    def test_record_and_list_single_attempt(self, tmp_path):
        db = tmp_path / "state.sqlite"
        task = ts.create_task("research topic", db_path=db)
        ts.record_task_attempt(
            task.task_id,
            attempt_num=1,
            outcome="done",
            agent="brain",
            result_summary="completed successfully",
            db_path=db,
        )
        attempts = ts.list_task_attempts(task.task_id, db_path=db)
        assert len(attempts) == 1
        assert attempts[0]["task_id"] == task.task_id
        assert attempts[0]["attempt_num"] == 1
        assert attempts[0]["outcome"] == "done"
        assert attempts[0]["agent"] == "brain"
        assert attempts[0]["result_summary"] == "completed successfully"

    def test_multiple_attempts_ordered_by_num(self, tmp_path):
        db = tmp_path / "state.sqlite"
        task = ts.create_task("retry task", db_path=db)
        ts.record_task_attempt(task.task_id, attempt_num=1, outcome="failed", db_path=db)
        ts.record_task_attempt(task.task_id, attempt_num=2, outcome="failed", db_path=db)
        ts.record_task_attempt(task.task_id, attempt_num=3, outcome="done", db_path=db)
        attempts = ts.list_task_attempts(task.task_id, db_path=db)
        assert len(attempts) == 3
        assert [a["attempt_num"] for a in attempts] == [1, 2, 3]
        assert attempts[-1]["outcome"] == "done"

    def test_result_summary_truncated_to_300_chars(self, tmp_path):
        db = tmp_path / "state.sqlite"
        task = ts.create_task("long result task", db_path=db)
        long_text = "x" * 500
        ts.record_task_attempt(
            task.task_id, attempt_num=1, outcome="done",
            result_summary=long_text, db_path=db,
        )
        attempts = ts.list_task_attempts(task.task_id, db_path=db)
        assert len(attempts[0]["result_summary"]) <= 300

    def test_list_returns_empty_for_unknown_task(self, tmp_path):
        db = tmp_path / "state.sqlite"
        ts.create_task("seed db", db_path=db)  # ensure schema exists
        attempts = ts.list_task_attempts("task-nonexistent", db_path=db)
        assert attempts == []

    def test_none_result_summary_stored_as_none(self, tmp_path):
        db = tmp_path / "state.sqlite"
        task = ts.create_task("quiet task", db_path=db)
        ts.record_task_attempt(
            task.task_id, attempt_num=1, outcome="blocked",
            result_summary=None, db_path=db,
        )
        attempts = ts.list_task_attempts(task.task_id, db_path=db)
        assert attempts[0]["result_summary"] is None

    def test_task_stats_includes_exhausted_total(self, tmp_path):
        db = tmp_path / "state.sqlite"
        task = ts.create_task("job", max_attempts=1, db_path=db)
        ts.mark_running(task.task_id, db_path=db)
        ts.mark_failed(task.task_id, "error", db_path=db)
        # Force attempts=1 to match max_attempts=1
        import sqlite3
        conn = sqlite3.connect(str(db))
        conn.execute("UPDATE brain_tasks SET attempts=1 WHERE task_id=?", (task.task_id,))
        conn.commit()
        conn.close()
        stats = ts.task_stats(db_path=db)
        assert "exhausted_total" in stats
        assert stats["exhausted_total"] == 1

    def test_task_stats_includes_stale_running(self, tmp_path):
        from datetime import datetime, timezone, timedelta
        import sqlite3
        db = tmp_path / "state.sqlite"
        task = ts.create_task("stale-job", db_path=db)
        ts.mark_running(task.task_id, db_path=db)
        # Backdate heartbeat_at to >30 min ago so it counts as stale
        stale_ts = (datetime.now(timezone.utc) - timedelta(minutes=45)).strftime("%Y-%m-%dT%H:%M:%SZ")
        conn = sqlite3.connect(str(db))
        conn.execute("UPDATE brain_tasks SET heartbeat_at=? WHERE task_id=?",
                     (stale_ts, task.task_id))
        conn.commit()
        conn.close()
        stats = ts.task_stats(db_path=db)
        assert "stale_running" in stats
        assert stats["stale_running"] == 1

    def test_task_stats_stale_running_zero_when_fresh(self, tmp_path):
        db = tmp_path / "state.sqlite"
        task = ts.create_task("fresh-job", db_path=db)
        ts.mark_running(task.task_id, db_path=db)
        # heartbeat_at is set to now by mark_running -- should not be stale
        stats = ts.task_stats(db_path=db)
        assert stats.get("stale_running", 0) == 0

    def test_attempt_id_is_unique_per_row(self, tmp_path):
        db = tmp_path / "state.sqlite"
        task = ts.create_task("task", db_path=db)
        ts.record_task_attempt(task.task_id, attempt_num=1, outcome="done", db_path=db)
        ts.record_task_attempt(task.task_id, attempt_num=2, outcome="done", db_path=db)
        attempts = ts.list_task_attempts(task.task_id, db_path=db)
        ids = [a["attempt_id"] for a in attempts]
        assert len(set(ids)) == len(ids)


# ---------------------------------------------------------------------------
# classify_result / result_indicates_failure
# ---------------------------------------------------------------------------

class TestClassifyResult:
    def test_empty_is_done(self):
        assert ts.classify_result("") == "done"

    def test_brain_error_prefix_is_failed(self):
        assert ts.classify_result("[Brain error: Ollama unreachable]") == "failed"

    def test_dispatch_prefix_is_failed(self):
        assert ts.classify_result("[dispatch] something went wrong") == "failed"

    def test_normal_output_is_done(self):
        assert ts.classify_result("Here is the answer to your question.") == "done"

    def test_blocked_marker_returns_blocked(self):
        # Check that a _BLOCKED_TEXT_MARKERS string triggers blocked
        from atlas_plus.brain.tasks import _BLOCKED_TEXT_MARKERS
        if _BLOCKED_TEXT_MARKERS:
            result = ts.classify_result(_BLOCKED_TEXT_MARKERS[0])
            assert result == "blocked"

    def test_result_indicates_failure_true(self):
        assert ts.result_indicates_failure("[Brain error: timeout]") is True

    def test_result_indicates_failure_false_for_done(self):
        assert ts.result_indicates_failure("Task completed successfully.") is False


# ---------------------------------------------------------------------------
# task_stats
# ---------------------------------------------------------------------------

class TestTaskStats:
    def test_returns_expected_keys(self, db):
        task = ts.create_task("a task", db_path=db)
        ts.mark_done(task.task_id, "ok", db_path=db)
        stats = ts.task_stats(since_days=7, db_path=db)
        for key in ("today", "pending_total", "exhausted_total", "stale_running",
                    "by_agent_last_Nd", "since_days"):
            assert key in stats

    def test_pending_total_counts_pending(self, db):
        ts.create_task("a", db_path=db)
        ts.create_task("b", db_path=db)
        stats = ts.task_stats(db_path=db)
        assert stats["pending_total"] >= 2

    def test_exhausted_total_counts_exhausted(self, db):
        task = ts.create_task("fail me", max_attempts=1, db_path=db)
        ts.claim_next_task("worker-1", db_path=db)
        ts.mark_failed(task.task_id, "hard fail", db_path=db)
        # Bump attempts to max
        import sqlite3
        conn = sqlite3.connect(str(db))
        conn.execute("UPDATE brain_tasks SET attempts=max_attempts WHERE task_id=?", (task.task_id,))
        conn.commit()
        conn.close()
        stats = ts.task_stats(db_path=db)
        assert stats["exhausted_total"] >= 1


# ---------------------------------------------------------------------------
# cleanup_zombie_tasks
# ---------------------------------------------------------------------------

class TestCleanupZombieTasks:
    def test_returns_zero_when_no_zombies(self, db):
        ts.create_task("ok task", db_path=db)
        cleaned = ts.cleanup_zombie_tasks(max_running_minutes=30, db_path=db)
        assert cleaned == 0

    def test_marks_stale_running_as_failed(self, db):
        import sqlite3
        from datetime import datetime, timezone, timedelta
        task = ts.create_task("long task", db_path=db)
        # Manually set it as running with old heartbeat
        old_hb = (datetime.now(timezone.utc) - timedelta(minutes=60)).strftime("%Y-%m-%dT%H:%M:%S")
        conn = sqlite3.connect(str(db))
        conn.execute(
            "UPDATE brain_tasks SET status='running', started_at=?, heartbeat_at=? WHERE task_id=?",
            (old_hb, old_hb, task.task_id)
        )
        conn.commit()
        conn.close()
        cleaned = ts.cleanup_zombie_tasks(max_running_minutes=30, db_path=db)
        assert cleaned >= 1
        reloaded = ts.get_task(task.task_id, db_path=db)
        assert reloaded.status == "failed"


# ---------------------------------------------------------------------------
# list_exhausted_tasks
# ---------------------------------------------------------------------------

class TestListExhaustedTasks:
    def test_empty_when_no_exhausted(self, db):
        ts.create_task("fresh task", db_path=db)
        result = ts.list_exhausted_tasks(db_path=db)
        assert result == []

    def test_returns_exhausted_tasks(self, db):
        import sqlite3
        task = ts.create_task("exhausted task", max_attempts=2, db_path=db)
        conn = sqlite3.connect(str(db))
        conn.execute(
            "UPDATE brain_tasks SET status='failed', attempts=2, finished_at='2026-05-21T03:00:00' WHERE task_id=?",
            (task.task_id,)
        )
        conn.commit()
        conn.close()
        result = ts.list_exhausted_tasks(db_path=db)
        assert any(t.task_id == task.task_id for t in result)

    def test_does_not_return_pending_tasks(self, db):
        ts.create_task("still pending", db_path=db)
        result = ts.list_exhausted_tasks(db_path=db)
        assert all(t.status in ("failed", "blocked") for t in result)


# ---------------------------------------------------------------------------
# DB path late binding + live-state isolation (2026-10-12)
# ---------------------------------------------------------------------------

class TestDbPathLateBinding:
    """_DB_PATH must resolve at CALL time, and tests must never see the live DB.

    The old `db_path: Path = _DB_PATH` defaults bound the live path at import,
    so patches like `patch("atlas_plus.brain.tasks._DB_PATH", tmp)` were silent
    no-ops -- which let suite runs plant real tasks ("test instruction",
    "Verify ticket TES-001") in the live queue that the live worker then ran.
    """

    def test_patching_module_db_path_redirects_default_callers(self, tmp_path, monkeypatch):
        db = tmp_path / "redirect" / "state.sqlite"
        monkeypatch.setattr(ts, "_DB_PATH", db)
        task = ts.create_task("late-binding pin")  # no db_path: must hit the patched DB
        assert db.exists()
        got = ts.get_task(task.task_id)
        assert got is not None
        assert got.instruction == "late-binding pin"

    def test_suite_default_db_is_isolated_per_test(self):
        # The conftest _isolated_state_db autouse fixture must have pointed
        # the module default into pytest's tmp tree, not the operator's
        # live .atlas/state.sqlite.
        assert "state-isolated" in str(ts._DB_PATH)
