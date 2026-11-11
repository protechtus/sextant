"""Tests for the Helm build-loop status surface (LOO-002).

Covers:
  - loop_status.read_loop_state   -- tolerant JSON reader (never raises)
  - loop_status.summarize_loops   -- running/throttled/stale/idle/unknown policy
  - rollup_span / render_panel    -- pure HTML renderers
  - GET /api/loop/status          -- Helm fragment smoke test
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone

# Mirror test_web_app.py env EXACTLY so the app's auth constant matches no matter
# which test module imports the app first.
_TEST_KEY = "test-key-abc"
os.environ.setdefault("ATLAS_WEB_API_KEY", _TEST_KEY)
os.environ.setdefault("ATLAS_BRAIN_URL", "http://127.0.0.1:9999")
os.environ.setdefault("ATLAS_BRAIN_API_KEY", "brain-key")
os.environ.setdefault("ATLAS_MCP_TOKEN", "mcp-token")
AUTH = {"Authorization": f"Bearer {_TEST_KEY}"}

from fastapi.testclient import TestClient  # noqa: E402

from atlas_plus.web import loop_status as loops  # noqa: E402
from atlas_plus.web.app import app  # noqa: E402

_client = TestClient(app, raise_server_exceptions=False)

_NOW = datetime(2026, 6, 16, 12, 0, 0, tzinfo=timezone.utc)


def _ts(minutes_ago: float) -> str:
    return (_NOW - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _lease(kind="loop", model="sonnet", skill="tui-build-loop", state="running"):
    return {"kind": kind, "model": model, "skill": skill, "state": state,
            "pid": 1234, "uptime": "3m", "heartbeat": "1s"}


def _state(generated_min_ago=1.0, running=1, backoff=0, opus=0, throttles=0,
           leases=None, stop=False, window=6) -> dict:
    if leases is None:
        leases = [_lease() for _ in range(running + backoff)]
    return {
        "generated": _ts(generated_min_ago),
        "host": "TESTHOST",
        "leases": leases,
        "running_count": running,
        "backoff_count": backoff,
        "opus_count": opus,
        "throttle_window_hours": window,
        "throttle_count": throttles,
        "throttle_events": [{"log": "iter-x.try1.json", "when": "2026-06-16 09:00:00"}] * throttles,
        "stop_requested": stop,
        "active_locks": [],
    }


# ---------------------------------------------------------------------------
# read_loop_state -- tolerant reader
# ---------------------------------------------------------------------------

def test_read_state_returns_dict(tmp_path):
    p = tmp_path / "loop-state.json"
    p.write_text(json.dumps(_state()), encoding="utf-8")
    out = loops.read_loop_state(p)
    assert out["running_count"] == 1


def test_read_state_missing_file_is_empty(tmp_path):
    assert loops.read_loop_state(tmp_path / "nope.json") == {}


def test_read_state_bad_json_is_empty(tmp_path):
    p = tmp_path / "loop-state.json"
    p.write_text("{not json", encoding="utf-8")
    assert loops.read_loop_state(p) == {}


def test_read_state_non_dict_is_empty(tmp_path):
    p = tmp_path / "loop-state.json"
    p.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
    assert loops.read_loop_state(p) == {}


# ---------------------------------------------------------------------------
# summarize_loops -- the policy
# ---------------------------------------------------------------------------

def test_summarize_empty_is_unknown():
    s = loops.summarize_loops({}, now=_NOW)
    assert s["status"] == "unknown"
    assert s["age_label"] == "never"


def test_summarize_running_fresh():
    s = loops.summarize_loops(_state(running=1, opus=0), now=_NOW)
    assert s["status"] == "running"
    assert s["stale"] is False
    assert s["running_count"] == 1


def test_summarize_idle_when_no_workers():
    s = loops.summarize_loops(_state(running=0, leases=[]), now=_NOW)
    assert s["status"] == "idle"


def test_summarize_backoff_is_throttled():
    s = loops.summarize_loops(_state(running=0, backoff=1), now=_NOW)
    assert s["status"] == "throttled"
    assert s["backoff_count"] == 1


def test_summarize_old_snapshot_is_stale():
    # Older than the 15-minute freshness window -> cannot trust it reflects now.
    s = loops.summarize_loops(_state(generated_min_ago=60, running=1), now=_NOW)
    assert s["status"] == "stale"
    assert s["stale"] is True


def test_summarize_throttled_beats_stale():
    s = loops.summarize_loops(_state(generated_min_ago=60, running=0, backoff=1), now=_NOW)
    assert s["status"] == "throttled"  # active backoff outranks staleness


def test_summarize_unparseable_ts_is_stale():
    st = _state(running=1)
    st["generated"] = "garbage"
    s = loops.summarize_loops(st, now=_NOW)
    assert s["status"] == "stale"


def test_summarize_surfaces_throttle_count():
    s = loops.summarize_loops(_state(running=1, throttles=3), now=_NOW)
    assert s["throttle_count"] == 3
    assert s["throttle_window_hours"] == 6


def test_summarize_naive_now_treated_as_utc():
    s = loops.summarize_loops(_state(running=1), now=_NOW.replace(tzinfo=None))
    assert s["status"] == "running"


def test_summarize_tolerates_garbage_counts():
    st = _state(running=1)
    st["running_count"] = "oops"
    st["backoff_count"] = None
    s = loops.summarize_loops(st, now=_NOW)
    assert s["running_count"] == 0 and s["backoff_count"] == 0


# ---------------------------------------------------------------------------
# renderers
# ---------------------------------------------------------------------------

def test_rollup_span_running_is_green():
    span = loops.rollup_span(loops.summarize_loops(_state(running=2), now=_NOW))
    assert "loops: running (2)" in span and "text-green-400" in span


def test_rollup_span_throttled_shows_backoff():
    span = loops.rollup_span(loops.summarize_loops(_state(running=0, backoff=1), now=_NOW))
    assert "loops: throttled (1 backoff)" in span and "text-red-400" in span


def test_rollup_span_unknown_is_gray():
    span = loops.rollup_span(loops.summarize_loops({}, now=_NOW))
    assert "loops: unknown" in span and "text-gray-500" in span


def test_render_panel_unknown_mentions_no_snapshot():
    assert "no loop-state snapshot" in loops.render_panel(loops.summarize_loops({}, now=_NOW))


def test_render_panel_shows_counts_and_workers():
    html = loops.render_panel(loops.summarize_loops(
        _state(running=1, opus=1, throttles=2,
               leases=[_lease(kind="interactive", model="opus", skill="interactive")]),
        now=_NOW))
    assert "running: 1" in html and "opus: 1" in html
    assert "interactive/opus" in html
    assert "throttle event(s)" in html


def test_render_panel_shows_stop_requested():
    html = loops.render_panel(loops.summarize_loops(_state(running=1, stop=True), now=_NOW))
    assert "STOP requested" in html


# ---------------------------------------------------------------------------
# endpoint smoke test
# ---------------------------------------------------------------------------

def test_loop_status_endpoint_requires_auth():
    r = _client.get("/api/loop/status", headers={"Authorization": "Bearer wrong"})
    assert r.status_code in (401, 403)


def test_loop_status_endpoint_renders(monkeypatch):
    monkeypatch.setattr(
        "atlas_plus.web.loop_status.read_loop_state",
        lambda *a, **k: _state(running=1, opus=1, backoff=1,
                               leases=[_lease(state="backoff:120s", model="opus")]),
    )
    r = _client.get("/api/loop/status", headers=AUTH)
    assert r.status_code == 200
    assert "backoff" in r.text
