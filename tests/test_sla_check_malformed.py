"""scheduler._sla_check must not crash on a malformed started_at (B2).

A truncated/garbage started_at (e.g. left by a crashed worker) can slip past the lexical
pre-filter; previously the task was marked alerted and THEN parsed, so the parse raised
ValueError mid-loop -- aborting the SLA check and leaving the bad task stuck in _sla_alerted
forever (so it never alerts even once fixed, and later good tasks in the batch are skipped).
Now the parse is defensive: bad rows are skipped, not marked, and the loop continues.
"""
from __future__ import annotations

from types import SimpleNamespace


async def test_sla_check_skips_malformed_started_at(monkeypatch):
    from atlas_plus.brain import scheduler
    from atlas_plus.brain import tasks as task_store
    import atlas_plus.brain.server as server

    # "2020-99-99T00:00:00" is lexically <= the cutoff (2020 < the current year, so it passes
    # the pre-filter) but has an invalid month/day, so datetime.fromisoformat raises on every
    # Python version (3.14's lenient parser still rejects month=99) -> the malformed case.
    bad = SimpleNamespace(task_id="bad1", agent="a", instruction="x", started_at="2020-99-99T00:00:00")
    good = SimpleNamespace(task_id="good1", agent="a", instruction="x", started_at="2020-01-01T00:00:00")

    monkeypatch.setattr(task_store, "list_tasks", lambda **k: [bad, good])

    async def _noop(*a, **k):
        return None
    monkeypatch.setattr(server, "_ntfy", _noop)

    scheduler._sla_alerted.clear()
    await scheduler._sla_check()  # must NOT raise

    assert "bad1" not in scheduler._sla_alerted   # malformed skipped, not stuck
    assert "good1" in scheduler._sla_alerted        # valid task still alerted (loop continued)
    scheduler._sla_alerted.clear()
