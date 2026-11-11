"""Tests for the token-budget preflight (atlas_plus.budget). Events are injected with a fixed
clock; no transcripts are read."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from atlas_plus.budget import (SIZE_PRESETS, UsageEvent, max_rolling_window, minutes_until_freed,
                               preflight, preflight_size, snapshot, window_sum)

NOW = datetime(2026, 6, 5, 12, 0, 0, tzinfo=timezone.utc)


def ev(mins_ago: float, total: int) -> UsageEvent:
    half = total // 2
    return UsageEvent(ts=NOW - timedelta(minutes=mins_ago), total=total, output=half,
                      input=total - half, cache_creation=0, cache_read=0)


# A history with a dense 900k 5h-window (the ceiling) and 200k spent in the current window.
def _events_headroom_700k() -> list[UsageEvent]:
    return [
        ev(600, 300_000), ev(560, 300_000), ev(520, 300_000),  # ~9-10h ago: ceiling 900k
        ev(180, 100_000), ev(30, 100_000),                     # within last 5h: used 200k
    ]


# --- meter primitives -----------------------------------------------------

def test_window_sum_counts_only_trailing_window():
    e = _events_headroom_700k()
    assert window_sum(e, 5, now=NOW) == 200_000          # only the two recent events
    assert window_sum(e, 24, now=NOW) == 1_100_000       # all five


def test_max_rolling_window_is_the_dense_history():
    assert max_rolling_window(_events_headroom_700k(), 5) == 900_000


def test_minutes_until_freed():
    # need 150k freed; oldest in-window event is 400k at 290 min ago -> exits at now+10min
    events = [ev(290, 400_000), ev(60, 450_000)]
    assert minutes_until_freed(events, 150_000, 5, now=NOW) == pytest.approx(10.0, abs=0.1)


def test_minutes_until_freed_zero_when_nothing_needed():
    assert minutes_until_freed(_events_headroom_700k(), 0, now=NOW) == 0.0


def test_minutes_until_freed_none_when_window_too_small():
    # deficit exceeds everything in the window -> waiting cannot help
    assert minutes_until_freed([ev(60, 100_000)], 500_000, 5, now=NOW) is None


# --- snapshot -------------------------------------------------------------

def test_snapshot_headroom():
    s = snapshot(events=_events_headroom_700k(), now=NOW)
    assert s.used_5h == 200_000
    assert s.ceiling == 900_000
    assert s.headroom == 700_000


def test_snapshot_window_budget_override():
    s = snapshot(events=_events_headroom_700k(), window_budget=1_000_000, now=NOW)
    assert s.ceiling == 1_000_000
    assert s.headroom == 800_000


# --- preflight verdicts ---------------------------------------------------

def test_preflight_proceed():
    v = preflight(400_000, events=_events_headroom_700k(), now=NOW)  # need 500k <= 700k
    assert v.status == "proceed"
    assert v.ok is True
    assert v.deficit == 0


def test_preflight_tight():
    # estimate 650k: required 812.5k > headroom 700k >= estimate 650k -> tight
    v = preflight(650_000, events=_events_headroom_700k(), now=NOW)
    assert v.status == "tight"
    assert v.ok is False
    assert "caution" in v.recommendation.lower()


def test_preflight_insufficient_not_waitable():
    # estimate 900k > headroom 700k; deficit can't be freed (only 200k in window)
    v = preflight(900_000, events=_events_headroom_700k(), now=NOW)
    assert v.status == "insufficient"
    assert v.deficit > 0
    assert v.minutes_until_enough is None
    assert "split" in v.recommendation.lower() or "scope" in v.recommendation.lower()


def test_preflight_insufficient_but_waitable():
    # headroom small now, but an old in-window event rolls off soon to free the deficit.
    # The old cluster is >5h from the recent events so it stays isolated (ceiling 900k).
    events = [ev(900, 300_000), ev(880, 300_000), ev(860, 300_000),  # ~14-15h ago: ceiling 900k
              ev(295, 400_000), ev(60, 450_000)]                     # used 850k -> headroom 50k
    v = preflight(100_000, events=events, now=NOW)   # required 125k, deficit 75k
    assert v.status == "insufficient"
    # the 400k event at 295 min ago exits the 5h window in ~5 min, freeing the 75k deficit
    assert v.minutes_until_enough == pytest.approx(5.0, abs=0.6)
    assert "wait" in v.recommendation.lower()


def test_preflight_margin_respected():
    # with zero margin, 700k estimate exactly fits 700k headroom -> proceed
    v = preflight(700_000, margin=0.0, events=_events_headroom_700k(), now=NOW)
    assert v.status == "proceed"


def test_preflight_as_dict_serialisable():
    import json
    v = preflight(400_000, events=_events_headroom_700k(), now=NOW)
    d = json.loads(json.dumps(v.as_dict()))
    assert d["status"] == "proceed" and d["ok"] is True
    assert "snapshot" in d and d["snapshot"]["headroom"] == 700_000


# --- size presets ---------------------------------------------------------

def test_preflight_size_uses_preset():
    v = preflight_size("small", events=_events_headroom_700k(), now=NOW)
    assert v.estimated_tokens == SIZE_PRESETS["small"]
    assert v.status == "proceed"


def test_preflight_size_rejects_unknown():
    with pytest.raises(ValueError):
        preflight_size("gigantic", events=_events_headroom_700k(), now=NOW)
