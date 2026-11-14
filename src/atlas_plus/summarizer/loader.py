"""P2-T2: Transcript loader -- reads turns from Claude Code JSONL files.

Supports:
  source='claude-code'  -- reads ~/.claude/projects/**/*.jsonl directly
  source='atlas'        -- reads TranscriptStore NDJSON files (requires capture daemon)

since / until accept:
  ISO timestamp string  -- 2026-04-30T15:00:00
  'today'               -- midnight UTC today
  'Nh' / 'Nm'          -- N hours or minutes ago
  None                  -- no filter
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

from atlas_plus.transcripts.store import TurnRecord
from atlas_plus.transcripts.sources.claude_code import _line_to_record
from atlas_plus import paths as atlas_paths

CLAUDE_CODE_ROOT = Path(
    os.environ.get("ATLAS_CLAUDE_CODE_ROOT")
    or (Path.home() / ".claude" / "projects")
)
ATLAS_TRANSCRIPT_ROOT = Path(
    os.environ.get("ATLAS_TRANSCRIPT_ROOT")
    or (atlas_paths.state_dir() / "transcripts")
)


def _parse_since(s: str | None) -> datetime | None:
    if not s:
        return None
    s = s.strip().lower()
    if s == "today":
        now = datetime.now().astimezone()  # the user's midnight, not UTC's (MN28: an evening session was dropped)
        return now.replace(hour=0, minute=0, second=0, microsecond=0)
    if s.endswith("h") and s[:-1].isdigit():
        return datetime.now(timezone.utc) - timedelta(hours=int(s[:-1]))
    if s.endswith("m") and s[:-1].isdigit():
        return datetime.now(timezone.utc) - timedelta(minutes=int(s[:-1]))
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        raise ValueError(f"Cannot parse since/until value: {s!r}. Use ISO timestamp, 'today', or 'Nh'/'Nm'.")


def _ts_to_dt(ts: str) -> datetime | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def _load_claude_code(
    conversation_id: str | None,
    since: datetime | None,
    until: datetime | None,
) -> Iterator[TurnRecord]:
    if not CLAUDE_CODE_ROOT.exists():
        return

    jsonl_files = sorted(CLAUDE_CODE_ROOT.rglob("*.jsonl"), key=lambda p: p.stat().st_mtime)
    _one = __import__("os").environ.get("ATLAS_TRANSCRIPT_FILE", "")
    if _one:
        try:
            _p = Path(_one).resolve()
            if _p.is_file() and _p.is_relative_to(CLAUDE_CODE_ROOT.resolve()):
                jsonl_files = [_p]  # the Stop payload names the transcript: every Stop had parsed every transcript under the root (AG64: 200 MB, 70 s cold)
        except Exception:  # noqa: BLE001
            pass

    for path in jsonl_files:
        # Skip files not modified since our window
        if since and path.stat().st_mtime < since.timestamp():
            continue

        try:
            lines = path.read_bytes().decode("utf-8", errors="replace").splitlines()
        except OSError:
            continue

        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue

            rec = _line_to_record(obj)
            if rec is None or not rec.conversation_id:
                continue
            if conversation_id and rec.conversation_id != conversation_id:
                continue

            ts_dt = _ts_to_dt(rec.ts)
            if since and ts_dt and ts_dt < since:
                continue
            if until and ts_dt and ts_dt > until:
                continue

            yield rec


def _load_atlas(
    conversation_id: str | None,
    since: datetime | None,
    until: datetime | None,
) -> Iterator[TurnRecord]:
    if not ATLAS_TRANSCRIPT_ROOT.exists():
        return

    for ndjson in sorted(ATLAS_TRANSCRIPT_ROOT.rglob("*.ndjson"), key=lambda p: p.stat().st_mtime):
        if since and ndjson.stat().st_mtime < since.timestamp():
            continue
        try:
            lines = ndjson.read_bytes().decode("utf-8", errors="replace").splitlines()
        except OSError:
            continue

        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
                rec = TurnRecord(**{k: v for k, v in data.items() if k != "hash"})
            except Exception:
                continue

            if conversation_id and rec.conversation_id != conversation_id:
                continue

            ts_dt = _ts_to_dt(rec.ts)
            if since and ts_dt and ts_dt < since:
                continue
            if until and ts_dt and ts_dt > until:
                continue

            yield rec


def load_turns(
    source: str = "claude-code",
    conversation_id: str | None = None,
    since: str | None = None,
    until: str | None = None,
) -> list[TurnRecord]:
    """Load transcript turns, sorted chronologically."""
    since_dt = _parse_since(since)
    until_dt = _parse_since(until)

    if source == "claude-code":
        turns = list(_load_claude_code(conversation_id, since_dt, until_dt))
    elif source == "atlas":
        turns = list(_load_atlas(conversation_id, since_dt, until_dt))
    elif source == "auto":
        seen = set()
        turns = []
        for t in _load_claude_code(conversation_id, since_dt, until_dt):
            if t.turn_id not in seen:
                seen.add(t.turn_id)
                turns.append(t)
        for t in _load_atlas(conversation_id, since_dt, until_dt):
            if t.turn_id not in seen:
                seen.add(t.turn_id)
                turns.append(t)
    else:
        raise ValueError(f"Unknown source: {source!r}. Use 'claude-code', 'atlas', or 'auto'.")

    turns.sort(key=lambda t: t.ts or "")
    return turns
