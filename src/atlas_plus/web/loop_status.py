"""Build-loop status surface for the Atlas control plane (Helm).

The autonomous build loops (run-build-loop.ps1) and the interactive-session hooks
(session-lease.ps1) coordinate through a governor lease registry; ``loop-status.ps1 -Json``
snapshots that registry plus recent throttle events to ``<workspace>/.build-logs/loop-state.json``::

    {"generated": "2026-06-16T15:00:00Z", "host": "HOSTNAME",
     "leases": [{"kind": "loop|interactive", "model": "...", "skill": "...",
                 "state": "running|backoff:120s", "uptime": "3m", "heartbeat": "1s"}],
     "running_count": 1, "backoff_count": 0, "opus_count": 1,
     "throttle_window_hours": 6, "throttle_count": 0, "throttle_events": [...],
     "stop_requested": false, "active_locks": [...]}

This module reads that snapshot and turns it into an operator-facing health summary so Helm can
answer "what is running, is anything backing off on a rate limit, and have we been throttled
recently?" at a glance (LOO-002).

Pure functions (``summarize_loops`` / the render helpers) so it unit-tests without the file or a
server; only ``read_loop_state`` touches disk, and it never raises -- a missing or malformed
snapshot reads as "no data".
"""
from __future__ import annotations

import html as _html
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# The snapshot is refreshed by ``loop-status.ps1 -Json`` (per loop iteration and/or a periodic
# scheduled task). Past this age we cannot trust it to reflect what is running now, so the
# verdict is treated as STALE rather than reassuring.
_LOOP_MAX_AGE_MINUTES = 15

_STATUS_CLASS = {
    "throttled": "text-red-400",
    "stale": "text-yellow-400",
    "running": "text-green-400",
    "idle": "text-gray-500",
    "unknown": "text-gray-500",
}


def loop_state_path() -> Path:
    from atlas_plus import paths as atlas_paths
    return Path(
        os.environ.get("ATLAS_LOOP_STATE_FILE")
        or (atlas_paths.workspace_root() / ".build-logs" / "loop-state.json")
    )


def read_loop_state(path: Optional[Path] = None) -> dict:
    """Read the loop-state snapshot. Never raises.

    Returns {} when the file is missing, unreadable, or not a JSON object.
    """
    p = Path(path) if path is not None else loop_state_path()
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _parse_ts(ts) -> Optional[datetime]:
    if not ts:
        return None
    s = str(ts).strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except (ValueError, TypeError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _humanize_age(minutes: Optional[float]) -> str:
    if minutes is None:
        return "never"
    if minutes < 1:
        return f"{max(1, int(minutes * 60))}s ago"
    if minutes < 90:
        return f"{int(round(minutes))}m ago"
    if minutes < 2880:
        return f"{int(round(minutes / 60))}h ago"
    return f"{int(round(minutes / 1440))}d ago"


def _as_int(v) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def summarize_loops(state: dict, now: Optional[datetime] = None) -> dict:
    """Summarize the loop-state snapshot.

    Returns {status, running_count, backoff_count, opus_count, throttle_count,
    throttle_window_hours, leases, ts, age_minutes, age_label, stale, stop_requested}.
    status precedence: "throttled" (something backing off on a rate limit) > "stale"
    (snapshot too old to trust) > "running" (workers active) > "idle" (registry empty);
    "unknown" when there is no snapshot at all.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    if not state:
        return {"status": "unknown", "running_count": 0, "backoff_count": 0,
                "opus_count": 0, "throttle_count": 0, "throttle_window_hours": 0,
                "leases": [], "ts": None, "age_minutes": None, "age_label": "never",
                "stale": False, "stop_requested": False}

    ts = _parse_ts(state.get("generated"))
    age_minutes = max(0.0, (now - ts).total_seconds() / 60.0) if ts else None
    stale = (age_minutes is not None and age_minutes > _LOOP_MAX_AGE_MINUTES) or ts is None

    leases = state.get("leases") if isinstance(state.get("leases"), list) else []
    running = _as_int(state.get("running_count"))
    backoff = _as_int(state.get("backoff_count"))
    opus = _as_int(state.get("opus_count"))
    throttles = _as_int(state.get("throttle_count"))

    if backoff > 0:
        status = "throttled"
    elif stale:
        status = "stale"
    elif running > 0:
        status = "running"
    else:
        status = "idle"

    return {
        "status": status,
        "running_count": running,
        "backoff_count": backoff,
        "opus_count": opus,
        "throttle_count": throttles,
        "throttle_window_hours": _as_int(state.get("throttle_window_hours")),
        "leases": leases,
        "ts": state.get("generated"),
        "age_minutes": age_minutes,
        "age_label": _humanize_age(age_minutes),
        "stale": stale,
        "stop_requested": bool(state.get("stop_requested")),
    }


def _loop_title(summary: dict) -> str:
    if summary["status"] == "unknown":
        return ("no loop-state snapshot found "
                "(loop-status.ps1 -Json writes .build-logs/loop-state.json)")
    note = " -- STALE snapshot" if summary["stale"] else ""
    thr = (f", {summary['throttle_count']} throttle event(s)/{summary['throttle_window_hours']}h"
           if summary["throttle_count"] else "")
    return (f"loops: {summary['running_count']} running, {summary['backoff_count']} backing off, "
            f"{summary['opus_count']} opus; snapshot {summary['age_label']}{thr}{note}")


def rollup_span(summary: dict) -> str:
    """A compact status-bar span: 'loops: <status>' with a count/backoff hint."""
    status = summary["status"]
    cls = _STATUS_CLASS.get(status, "text-gray-500")
    extra = ""
    if status == "throttled":
        extra = f" ({summary['backoff_count']} backoff)"
    elif status == "running":
        extra = f" ({summary['running_count']})"
    elif status == "stale":
        extra = f" ({summary['age_label']})"
    return (f'<span class="{cls}" title="{_html.escape(_loop_title(summary))}">'
            f'loops: {status}{extra}</span>')


def render_panel(summary: dict) -> str:
    """Pure: the dashboard 'Build loops' card fragment."""
    status = summary["status"]
    cls = _STATUS_CLASS.get(status, "text-gray-500")
    if status == "unknown":
        return ('<div class="text-xs text-slate-500">no loop-state snapshot '
                '<span class="text-slate-600">(.build-logs/loop-state.json)</span></div>')
    stale_note = ' <span class="text-yellow-400">(stale)</span>' if summary["stale"] else ""
    parts = [
        f'<div class="text-xs"><span class="{cls} font-bold">{_html.escape(status)}</span>'
        f' <span class="text-slate-500">{_html.escape(summary["age_label"])}{stale_note}</span></div>',
        f'<div class="text-xs text-slate-400 mt-1">running: {summary["running_count"]} &nbsp; '
        f'backoff: {summary["backoff_count"]} &nbsp; opus: {summary["opus_count"]}</div>',
    ]
    if summary["throttle_count"]:
        parts.append(f'<div class="text-xs text-yellow-400 mt-1">'
                     f'{summary["throttle_count"]} throttle event(s) / '
                     f'{summary["throttle_window_hours"]}h</div>')
    # List up to 6 active workers, newest semantics aside (snapshot order).
    rows = []
    for l in (summary["leases"] or [])[:6]:
        if not isinstance(l, dict):
            continue
        kind = _html.escape(str(l.get("kind", "?")))
        model = _html.escape(str(l.get("model", "?")))
        skill = _html.escape(str(l.get("skill", "")))
        lstate = _html.escape(str(l.get("state", "")))
        sclr = "text-yellow-400" if lstate.startswith("backoff") else "text-slate-400"
        rows.append(f'<div class="text-xs text-slate-500 truncate" title="{kind} {model} {skill} {lstate}">'
                    f'{kind}/{model} <span class="text-slate-600">{skill}</span> '
                    f'<span class="{sclr}">{lstate}</span></div>')
    if rows:
        parts.append('<div class="mt-1">' + "".join(rows) + "</div>")
    if summary["stop_requested"]:
        parts.append('<div class="text-xs text-red-400 mt-1">STOP requested</div>')
    return "".join(parts)
