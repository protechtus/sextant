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
        is_miss = result_count == 0 or top_score < MISS_THRESHOLD
        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "query": redact(query),  # ATL-017: never persist secrets pasted into a search
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
