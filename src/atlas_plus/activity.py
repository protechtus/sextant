"""Cross-component activity event log: the system's visible pulse.

Every long-lived component (Brain tasks, MCP tool dispatch, ticket writes,
temporal builds, sync operations) appends one NDJSON line per event via
``record()``. The Helm Map tab tails this file to animate what the stack is
doing in real time.

Design rules (modeled on sync/store.py's audit trail):
  - ``record()`` NEVER raises -- observability must not break the operation
    it observes.
  - Events carry metadata only (component, event name, small labels,
    durations). Never memory content, never tool arguments: the log lives
    in state_dir, outside the corpus, and must stay safe to display.
  - String fields pass through atlas_plus.redaction before hitting disk.
  - Multiple processes append concurrently; each event is one small
    ``write()`` on an append-mode handle, which is atomic enough on one
    machine. The trim can race a concurrent append and drop a few events;
    this is a live feed, not an audit of record.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from atlas_plus import paths as atlas_paths
from atlas_plus.redaction import redact

ACTIVITY_MAX_BYTES = 5 * 1024 * 1024
ACTIVITY_KEEP_LINES = 5000

# tail() reads at most this much from the end of the file, so a poll never
# pays for the whole log.
_TAIL_READ_BYTES = 256 * 1024


def activity_path() -> Path:
    """Resolved at call time so tests can repoint ATLAS_STATE_DIR."""
    return atlas_paths.state_dir() / "logs" / "activity.ndjson"


# tail(since=cursor) filters strictly-greater, so two events sharing one
# timestamp make the second invisible to a cursor poll. Windows' system
# clock ticks at ~15.6ms, so same-tick bursts are routine there (caught by
# CI 2026-10-16: test_returns_tail_with_cursor, windows-latest). Guarantee
# strictly increasing timestamps within this process; cross-process ties
# remain possible but need two processes in the same microsecond.
_now_lock = threading.Lock()
_last_now: datetime | None = None


def _now() -> str:
    global _last_now
    with _now_lock:
        ts = datetime.now(timezone.utc)
        if _last_now is not None and ts <= _last_now:
            ts = _last_now + timedelta(microseconds=1)
        _last_now = ts
        return ts.isoformat(timespec="microseconds")


def _trim(path: Path) -> None:
    try:
        if path.stat().st_size <= ACTIVITY_MAX_BYTES:
            return
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines(True)
        tmp = path.with_suffix(".ndjson.tmp")
        tmp.write_text("".join(lines[-ACTIVITY_KEEP_LINES:]), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass


def record(source: str, event: str, **fields) -> None:
    """Append one activity event. Never raises.

    ``source`` is the component (brain, mcp, tickets, temporal, sync, hook);
    ``event`` is a short verb phrase (task_started, tool_called, ...).
    Extra fields must be small JSON-safe metadata; strings are redacted.
    """
    try:
        entry: dict = {"ts": _now(), "source": source, "event": event}
        for key, value in fields.items():
            if isinstance(value, str):
                entry[key] = redact(value)
            elif isinstance(value, (int, float, bool)) or value is None:
                entry[key] = value
            else:
                entry[key] = redact(str(value))
        path = activity_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, sort_keys=True) + "\n")
        _trim(path)
    except Exception:
        pass


def tail(limit: int = 100, since: str | None = None) -> list[dict]:
    """Newest events, oldest-first. ``since`` filters to ts strictly greater.

    Reads only the tail of the file, skips corrupt lines, returns [] on any
    problem -- the Map tab degrades to a static topology.
    """
    try:
        path = activity_path()
        size = path.stat().st_size
        with path.open("rb") as f:
            if size > _TAIL_READ_BYTES:
                f.seek(size - _TAIL_READ_BYTES)
                f.readline()  # drop the partial line we landed inside
            raw = f.read().decode("utf-8", errors="replace")
        events: list[dict] = []
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if not isinstance(entry, dict) or "ts" not in entry:
                continue
            if since is not None and str(entry["ts"]) <= since:
                continue
            events.append(entry)
        return events[-max(0, int(limit)):]
    except Exception:
        return []
