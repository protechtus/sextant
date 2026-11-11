"""Tests for atlas_plus.activity -- the cross-component activity event log."""
from __future__ import annotations

import json

import pytest

from atlas_plus import activity


@pytest.fixture()
def state(tmp_path, monkeypatch):
    monkeypatch.setenv("ATLAS_STATE_DIR", str(tmp_path / ".atlas"))
    return tmp_path / ".atlas"


class TestRecord:
    def test_appends_one_json_line(self, state):
        activity.record("brain", "task_started", task_id="t-1", kind="digest")
        lines = activity.activity_path().read_text(encoding="utf-8").splitlines()
        assert len(lines) == 1
        entry = json.loads(lines[0])
        assert entry["source"] == "brain"
        assert entry["event"] == "task_started"
        assert entry["task_id"] == "t-1"
        assert "ts" in entry

    def test_never_raises_on_unwritable_path(self, tmp_path, monkeypatch):
        # Point the state dir at a path whose parent is a FILE -- mkdir fails.
        blocker = tmp_path / "blocker"
        blocker.write_text("x", encoding="utf-8")
        monkeypatch.setenv("ATLAS_STATE_DIR", str(blocker / "nope"))
        activity.record("brain", "task_started")  # must not raise

    def test_redacts_string_fields(self, state, monkeypatch):
        monkeypatch.setattr(activity, "redact", lambda s: "[REDACTED]")
        activity.record("mcp", "tool_called", tool="memory_search")
        entry = json.loads(activity.activity_path().read_text(encoding="utf-8"))
        assert entry["tool"] == "[REDACTED]"

    def test_non_json_safe_field_is_stringified(self, state):
        activity.record("sync", "push", path=object())
        entry = json.loads(activity.activity_path().read_text(encoding="utf-8"))
        assert isinstance(entry["path"], str)

    def test_trims_when_over_cap(self, state, monkeypatch):
        monkeypatch.setattr(activity, "ACTIVITY_MAX_BYTES", 500)
        monkeypatch.setattr(activity, "ACTIVITY_KEEP_LINES", 3)
        for i in range(30):
            activity.record("brain", "task_started", n=i)
        lines = activity.activity_path().read_text(encoding="utf-8").splitlines()
        assert len(lines) <= 4  # kept tail + at most the line that triggered


class TestSuiteIsolation:
    def test_record_without_env_never_touches_the_default_state_dir(self, monkeypatch):
        """Contract with conftest's _isolated_activity_feed: a test that
        does NOT set ATLAS_STATE_DIR must write to an isolated per-test
        file, not the operator's live feed."""
        monkeypatch.delenv("ATLAS_STATE_DIR", raising=False)
        from atlas_plus import paths as atlas_paths
        live_default = atlas_paths.state_dir() / "logs" / "activity.ndjson"
        resolved = activity.activity_path()
        assert resolved != live_default
        assert "activity-isolated" in str(resolved)


class TestTail:
    def test_missing_file_returns_empty(self, state):
        assert activity.tail() == []

    def test_returns_oldest_first_capped_at_limit(self, state):
        for i in range(10):
            activity.record("brain", "task_started", n=i)
        events = activity.tail(limit=4)
        assert [e["n"] for e in events] == [6, 7, 8, 9]

    def test_since_filters_strictly_after(self, state):
        activity.record("brain", "task_started", n=1)
        # Windows CI (3.11): the system clock can tick coarsely enough that
        # two back-to-back records share one timestamp, and the strict-after
        # cursor would then correctly drop BOTH. Wait out the tick so the
        # second event is genuinely later (what the cursor contract is about).
        import time
        first_ts = activity.tail()[0]["ts"]
        for _ in range(200):  # worst case ~200ms; one ~16ms tick in practice
            if activity._now() > first_ts:
                break
            time.sleep(0.001)
        activity.record("brain", "task_started", n=2)
        later = activity.tail(since=first_ts)
        assert [e["n"] for e in later] == [2]

    def test_skips_corrupt_lines(self, state):
        activity.record("brain", "task_started", n=1)
        with activity.activity_path().open("a", encoding="utf-8") as f:
            f.write("not json{\n")
            f.write('"a bare string"\n')
        activity.record("brain", "task_started", n=2)
        assert [e["n"] for e in activity.tail()] == [1, 2]

    def test_reads_only_the_file_tail(self, state, monkeypatch):
        for i in range(50):
            activity.record("brain", "task_started", n=i)
        monkeypatch.setattr(activity, "_TAIL_READ_BYTES", 300)
        events = activity.tail()
        assert events, "a bounded tail read must still yield events"
        assert events[-1]["n"] == 49
        assert all(isinstance(e["n"], int) for e in events)


class TestMonotonicTimestamps:
    """tail(since=cursor) is strictly-greater, so record() must never emit
    two identical timestamps in one process (Windows clock ticks ~15.6ms;
    caught by CI on windows-latest 2026-10-16)."""

    def test_now_is_strictly_increasing_under_frozen_clock(self, monkeypatch):
        from datetime import datetime, timezone

        frozen = datetime(2026, 10, 16, 12, 0, 0, 500000, tzinfo=timezone.utc)

        class _FrozenDateTime:
            @staticmethod
            def now(tz=None):
                return frozen

        monkeypatch.setattr(activity, "datetime", _FrozenDateTime)
        monkeypatch.setattr(activity, "_last_now", None)
        stamps = [activity._now() for _ in range(50)]
        assert stamps == sorted(stamps)
        assert len(set(stamps)) == 50, "same-tick events must get distinct ts"

    def test_cursor_poll_sees_same_tick_followup_event(self, state):
        # The exact CI failure shape: two records in one tick, poll with the
        # first event's ts as cursor, expect the second event to be visible.
        activity.record("mcp", "tool_called", tool="x", ok=True)
        activity.record("tickets", "ticket_created", ticket_id="T-1")
        events = activity.tail()
        assert [e["event"] for e in events] == ["tool_called", "ticket_created"]
        later = activity.tail(since=events[0]["ts"])
        assert [e["event"] for e in later] == ["ticket_created"]
