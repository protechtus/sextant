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


def single_transcript() -> Path | None:
    """The transcript ATLAS_TRANSCRIPT_FILE names (the Stop hook sets it), when it is a file under the root."""
    _one = os.environ.get("ATLAS_TRANSCRIPT_FILE", "")
    if not _one:
        return None
    try:
        _p = Path(_one).resolve()
        if _p.is_file() and _p.is_relative_to(CLAUDE_CODE_ROOT.resolve()):
            return _p
    except Exception:  # noqa: BLE001
        pass
    return None


def _count_lines(text: str, conversation_id: str) -> int:
    n = 0
    for line in text.split("\n"):  # the same split _load_claude_code uses, so the counts agree
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        rec = _line_to_record(obj) if isinstance(obj, dict) else None
        if rec is not None and rec.conversation_id and rec.conversation_id == conversation_id:
            n += 1
    return n


_TAIL = 4096


def count_session_turns(path: Path, conversation_id: str, cache_file: Path) -> int:
    """len(load_turns(conversation_id=...)) for one transcript with no time filter, reading only the bytes
    written since the last count. Claude Code fires Stop after every reply and the summarizer parsed the
    whole transcript each time just to find that fewer than six turns were new (AG84: 1.4 s and 0.6 GB on a
    100 MB session per reply). The cache keeps the offset of the last complete line, the turns before it and
    a hash of the 4 KB before it; a file that shrank, or whose last 4 KB before the offset changed, is counted from the start (a
    same-size rewrite further back is not noticed: Claude Code only appends)."""
    import hashlib
    try:
        cache = json.loads(cache_file.read_text(encoding="utf-8"))
        if not isinstance(cache, dict):
            cache = {}
    except (OSError, ValueError):
        cache = {}
    start, count = 0, 0
    with open(path, "rb") as f:
        size = os.fstat(f.fileno()).st_size
        off = cache.get("offset")
        if (cache.get("path") == str(path) and cache.get("session") == conversation_id
                and isinstance(off, int) and 0 < off <= size and isinstance(cache.get("count"), int)):
            f.seek(max(0, off - _TAIL))
            if hashlib.sha256(f.read(off - max(0, off - _TAIL))).hexdigest() == cache.get("tail"):
                start, count = off, cache["count"]
        f.seek(start)
        data = f.read()
    cut = data.rfind(b"\n") + 1  # complete lines only are cached; a line still being written is counted, not cached
    count += _count_lines(data[:cut].decode("utf-8", errors="replace"), conversation_id)
    end = start + cut
    if cut:
        tail_from = max(0, end - _TAIL)
        if tail_from >= start:
            tail = data[tail_from - start:cut]
        else:
            with open(path, "rb") as f:
                f.seek(tail_from)
                tail = f.read(end - tail_from)
        try:
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            cache_file.write_text(json.dumps({"path": str(path), "session": conversation_id, "offset": end,
                                              "count": count, "tail": hashlib.sha256(tail).hexdigest()}), encoding="utf-8")
        except OSError:
            pass
    return count + _count_lines(data[cut:].decode("utf-8", errors="replace"), conversation_id)


def _load_claude_code(
    conversation_id: str | None,
    since: datetime | None,
    until: datetime | None,
) -> Iterator[TurnRecord]:
    if not CLAUDE_CODE_ROOT.exists():
        return

    _p = single_transcript()
    if _p is not None:
        jsonl_files = [_p]  # the Stop payload names the transcript: every Stop had parsed every transcript under the root (AG64: 200 MB, 70 s cold)
    else:  # the walk only when no single file is named (AG84: it ran before the named file was honoured)
        jsonl_files = sorted(CLAUDE_CODE_ROOT.rglob("*.jsonl"), key=lambda p: p.stat().st_mtime)

    for path in jsonl_files:
        # Skip files not modified since our window
        if since and path.stat().st_mtime < since.timestamp():
            continue

        try:
            lines = path.read_bytes().decode("utf-8", errors="replace").split("\n")  # a raw U+2028 inside a JSON string is not a line break (AG84: that turn vanished)
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
            lines = ndjson.read_bytes().decode("utf-8", errors="replace").split("\n")  # as above (AG84)
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
