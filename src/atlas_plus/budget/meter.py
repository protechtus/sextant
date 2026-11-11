"""Token-usage meter: account-wide Claude Code consumption from local transcripts.

Reads ~/.claude/projects/**/*.jsonl -- the per-message usage records the CLI writes -- and reports
rolling-window consumption plus an EMPIRICAL ceiling (the largest 5h-rolling spend ever seen), so a
task can be sized against the remaining headroom BEFORE it starts. The 5h rolling window is the
rate-limit pressure window; gross tokens (input + output + cache_creation + cache_read) track that
pressure best. No network, no API -- the quota on the Max plan is account-wide, so this scans all
projects.

Provenance: the proven scanning logic is the workspace's research/usage_meter.py. This is a fresh, injectable
copy for atlas_plus (the glob and the clock are parameters, so the core is unit-testable without
touching real transcripts).
"""

from __future__ import annotations

import glob as _glob
import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

HOME = os.path.expanduser("~")
# Recursive: top-level session transcripts AND nested subagent/workflow transcripts, where
# fan-out workloads spend most of their tokens. Overridable via env so a service running under a
# different HOME (e.g. the MCP server) can still point at the real ~/.claude transcripts.
DEFAULT_GLOB = os.environ.get("ATLAS_TRANSCRIPTS_GLOB") or os.path.join(
    HOME, ".claude", "projects", "**", "*.jsonl")

# The rate-limit pressure window, in hours. Empirically the Max-plan 5h rolling window.
WINDOW_HOURS = 5.0


@dataclass
class UsageEvent:
    ts: datetime
    total: int          # gross = output + input + cache_creation + cache_read
    output: int
    input: int
    cache_creation: int
    cache_read: int
    model: str = "?"
    web: int = 0


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse_ts(s: str) -> Optional[datetime]:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def load_events(lookback_hours: float, *, glob_pattern: Optional[str] = None,
                now: Optional[datetime] = None) -> list[UsageEvent]:
    """Parse usage events from transcripts touched within the lookback window, sorted by time."""
    now = now or _utcnow()
    pattern = glob_pattern or DEFAULT_GLOB
    cutoff = now - timedelta(hours=lookback_hours)
    cutoff_epoch = cutoff.timestamp()
    events: list[UsageEvent] = []
    for path in _glob.glob(pattern, recursive=True):
        try:
            if os.path.getmtime(path) < cutoff_epoch - 3600:  # skip files untouched in window
                continue
        except OSError:
            continue
        try:
            with open(path, encoding="utf-8") as fh:
                for line in fh:
                    if '"usage"' not in line:
                        continue
                    try:
                        d = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    msg = d.get("message")
                    if not isinstance(msg, dict):
                        continue
                    u = msg.get("usage")
                    if not isinstance(u, dict):
                        continue
                    ts = _parse_ts(d.get("timestamp", ""))
                    if ts is None or ts < cutoff:
                        continue
                    events.append(_event_from_usage(ts, u, msg.get("model", "?")))
        except OSError:
            continue
    events.sort(key=lambda e: e.ts)
    return events


def _event_from_usage(ts: datetime, u: dict, model: str = "?") -> UsageEvent:
    out = u.get("output_tokens", 0) or 0
    inp = u.get("input_tokens", 0) or 0
    cc = u.get("cache_creation_input_tokens", 0) or 0
    cr = u.get("cache_read_input_tokens", 0) or 0
    stu = u.get("server_tool_use", {}) or {}
    web = (stu.get("web_search_requests", 0) or 0) + (stu.get("web_fetch_requests", 0) or 0)
    return UsageEvent(ts=ts, total=out + inp + cc + cr, output=out, input=inp,
                      cache_creation=cc, cache_read=cr, model=model, web=web)


def window_sum(events: list[UsageEvent], hours: float, field: str = "total",
               *, now: Optional[datetime] = None) -> int:
    """Sum a field over the trailing `hours` window."""
    now = now or _utcnow()
    cut = now - timedelta(hours=hours)
    return sum(getattr(e, field) for e in events if e.ts >= cut)


def max_rolling_window(events: list[UsageEvent], hours: float = WINDOW_HOURS,
                       field: str = "total") -> int:
    """Empirical ceiling: the largest `field` total within any `hours`-rolling window in history."""
    if not events:
        return 0
    best = 0
    j = 0
    run = 0
    window = timedelta(hours=hours)
    for i in range(len(events)):
        run += getattr(events[i], field)
        while events[i].ts - events[j].ts > window:
            run -= getattr(events[j], field)
            j += 1
        best = max(best, run)
    return best


def minutes_until_freed(events: list[UsageEvent], needed: int, hours: float = WINDOW_HOURS,
                        *, now: Optional[datetime] = None) -> Optional[float]:
    """How many minutes until enough of the current window's spend rolls off to free `needed`
    gross tokens. Events leave the window `hours` after their timestamp; we accumulate the oldest
    in-window events until their mass >= needed, and return when the last needed one exits.

    Returns 0.0 if `needed` <= 0, or None if even emptying the whole window cannot free it
    (i.e. `needed` exceeds everything currently in the window)."""
    if needed <= 0:
        return 0.0
    now = now or _utcnow()
    window = timedelta(hours=hours)
    cut = now - window
    in_window = [e for e in events if e.ts >= cut]  # oldest-first (events are sorted)
    freed = 0
    for e in in_window:
        freed += e.total
        if freed >= needed:
            exit_at = e.ts + window           # when this event leaves the rolling window
            return max(0.0, (exit_at - now).total_seconds() / 60.0)
    return None  # not enough mass in the window to ever free `needed` by waiting


def human(n: float) -> str:
    """Compact token count: 1234 -> 1.23K, 1_200_000 -> 1.20M."""
    for unit in ("", "K", "M", "B"):
        if abs(n) < 1000:
            return f"{n:.0f}{unit}" if unit == "" else f"{n:.2f}{unit}"
        n /= 1000
    return f"{n:.2f}T"
