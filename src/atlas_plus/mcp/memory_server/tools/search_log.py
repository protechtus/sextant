"""Search call logger for memory_search_tool (ATL-R01).

Appends one NDJSON record per search to .atlas/logs/mcp-search.ndjson.
Miss heuristic: top_score < 0.3 or result_count == 0.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from atlas_plus.redaction import redact
from atlas_plus import paths as atlas_paths

_STATE_DIR = atlas_paths.state_dir()
LOG_FILE = _STATE_DIR / "logs" / "mcp-search.ndjson"
MISS_THRESHOLD = 0.3
QUERY_KEEP = 300            # characters of the query kept per record (AG10: a 50 MB query made a 50 MB line)
ROTATE_BYTES = 5_000_000    # the log rotates to .1 past this size; load_log reads the whole file


def _is_link(p: Path) -> bool:
    try:
        st = p.lstat()
    except OSError:
        return False
    import stat as _stat
    if _stat.S_ISLNK(st.st_mode):
        return True
    attrs = getattr(st, "st_file_attributes", 0)
    return bool(attrs & getattr(_stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


ROTATE_GUARD_STALE_S = 60   # a guard file older than this belongs to a process that died mid-rotation


def _rotate_if_large() -> None:
    """Rotate the log to `.1` past ROTATE_BYTES, one process at a time: two servers that both
    saw a 5 MB log both renamed, and the second rename moved the fresh one-line log over the
    history the first had just rotated (MN8, sweep 10). The guard is an O_EXCL file; the size
    is re-checked once it is held."""
    try:
        if not (LOG_FILE.exists() and LOG_FILE.stat().st_size > ROTATE_BYTES):
            return
        guard = LOG_FILE.with_suffix(".ndjson.rotating")
        try:
            fd = os.open(str(guard), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                import time as _time
                if _time.time() - guard.stat().st_mtime > ROTATE_GUARD_STALE_S:
                    guard.unlink()
            except OSError:
                pass
            return
        try:
            os.close(fd)
            if LOG_FILE.stat().st_size > ROTATE_BYTES:
                os.replace(LOG_FILE, LOG_FILE.with_suffix(".ndjson.1"))
        finally:
            try:
                guard.unlink()
            except OSError:
                pass
    except OSError:
        pass


def log_search(
    query: str,
    scope: str,
    top_score: float,
    result_count: int,
    caller_identity: str = "mcp-client",
) -> None:
    """Append a search record. Non-blocking: silently swallows errors."""
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        if _is_link(LOG_FILE.parent) or _is_link(LOG_FILE):
            return   # a symlinked logs dir or log file would carry the rotation outside the state dir (MN12)
        _rotate_if_large()
        is_miss = result_count == 0 or top_score < MISS_THRESHOLD
        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            # ATL-017: never persist secrets pasted into a search; and never the whole query
            # (a 50,000,000-character query made a 50 MB line, AG10 sweep 10)
            # Redact BEFORE the cut: a token straddling character 300 left its first half in the
            # log (MN8, sweep 10). The window is four times the kept length (search already caps
            # queries at 2,000 characters), so the regexes never run over a 50 MB string.
            "query": redact(query[:QUERY_KEEP * 4])[:QUERY_KEEP],
            "query_chars": len(query),
            "scope": scope or "",
            "top_score": round(top_score, 4),
            "result_count": result_count,
            "miss": is_miss,
            "caller_identity": caller_identity,
        }
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
    except Exception:
        pass


def load_log(since_hours: float = 168.0) -> list[dict]:
    """Load search log entries from the last `since_hours` hours."""
    if not LOG_FILE.exists():
        return []
    cutoff = datetime.now(timezone.utc).timestamp() - since_hours * 3600
    records = []
    try:
        for line in LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
                ts = datetime.fromisoformat(r["ts"]).timestamp()
                if ts >= cutoff:
                    records.append(r)
            except Exception:
                pass
    except Exception:
        pass
    return records


def top_miss_queries(since_hours: float = 168.0, limit: int = 20) -> list[dict]:
    """Return the most frequent miss queries, normalized and grouped."""
    records = load_log(since_hours)
    misses: dict[str, dict] = {}
    for r in records:
        q = r["query"].lower().strip()
        if q not in misses:
            misses[q] = {"query": r["query"], "miss_count": 0, "total": 0, "last_seen": r["ts"]}
        misses[q]["total"] += 1
        if r.get("miss"):
            misses[q]["miss_count"] += 1
        if r["ts"] > misses[q]["last_seen"]:
            misses[q]["last_seen"] = r["ts"]

    results = [v for v in misses.values() if v["miss_count"] > 0]
    results.sort(key=lambda x: x["miss_count"], reverse=True)
    return results[:limit]
