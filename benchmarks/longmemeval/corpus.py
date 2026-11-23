"""One Sextant corpus per question: build it, then search it the way an agent would.

Each LongMemEval question comes with its own chat history (the haystack). This module writes
that history into a fresh Sextant memory root as plain markdown, one note per session, and
queries it through `memory_search_tool`, the same function the MCP server exposes. Nothing here
calls a hosted model: indexing is local SQLite FTS5, and the optional hybrid mode uses local
Ollama embeddings.

Run as a worker (one process per question, because Sextant reads its roots once per process):

    python corpus.py <split> <mode> <question_id> [--queries FILE]

The parent sets ATLAS_MEMORY_ROOT / ATLAS_STATE_DIR / ATLAS_WORKSPACE_ROOT for the worker.

What is deliberately kept away from the index and from every prompt: the dataset's session ids
(answer sessions are named `answer_...`) and the per-turn `has_answer` flag.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402

MODES = ("fts", "hybrid")
_DATE = re.compile(r"(\d{4})/(\d{2})/(\d{2}) \(\w+\) (\d{2}):(\d{2})")


def corpus_root(split: str, qid: str) -> Path:
    return common.work_dir() / "corpora" / split / qid


def parse_date(text: str) -> datetime:
    m = _DATE.search(text)
    if not m:
        raise ValueError(f"unrecognised date {text!r}")
    return datetime(*(int(g) for g in m.groups()))


def note_name(position: int) -> str:
    return f"sessions/s{position:03d}.md"


def render_session(date: str, turns: list[dict]) -> str:
    lines = []
    for turn in turns:
        speaker = "User" if turn["role"] == "user" else "Assistant"
        lines.append(f"**{speaker}:** {turn['content'].strip()}")
    return "\n\n".join(lines)


def build(split: str, qid: str) -> Path:
    """Write the haystack as notes. File times are set so each note is as old, relative to
    now, as the session was relative to the question date: the corpus looks the way it would
    have looked to a user asking on that day."""
    root = corpus_root(split, qid)
    memory = root / "memory"
    marker = root / ".built"
    if marker.exists():
        return memory
    entry = common.load_question(split, qid)
    asked = parse_date(entry["question_date"])
    now = time.time()
    (memory / "sessions").mkdir(parents=True, exist_ok=True)
    for position, date, turns in common.chronological(entry):
        when = parse_date(date)
        body = (
            "---\n"
            f"title: Conversation on {date}\n"
            f"date: {when:%Y-%m-%d}\n"
            "type: session\n"
            "---\n\n"
            f"# Conversation on {date}\n\n"
            f"{render_session(date, turns)}\n"
        )
        path = memory / note_name(position)
        path.write_text(body, encoding="utf-8")
        age = max(0.0, (asked - when).total_seconds())
        os.utime(path, (now - age, now - age))
    marker.write_text(datetime.now().isoformat(timespec="seconds"), encoding="utf-8")
    return memory


def _position(path: str) -> int | None:
    m = re.search(r"s(\d{3})\.md$", path.replace("\\", "/"))
    return int(m.group(1)) if m else None


def search(query: str, limit: int) -> dict:
    from atlas_plus.mcp.memory_server.server import memory_search_tool

    started = time.perf_counter()
    response = memory_search_tool(query=query, limit=limit)
    elapsed = (time.perf_counter() - started) * 1000
    hits = []
    for r in response.get("results", []):
        pos = _position(str(r.get("path", "")))
        if pos is None:
            continue
        hits.append({"position": pos, "score": round(float(r.get("score", 0)), 4),
                     "snippet": str(r.get("snippet") or "")[:400]})
    return {"query": query, "confidence": response.get("confidence"),
            "retrieval": response.get("retrieval"), "ms": round(elapsed, 1), "hits": hits}


def worker(split: str, mode: str, qid: str, queries_file: str | None, limit: int) -> dict:
    entry = common.load_question(split, qid)
    started = time.perf_counter()
    build(split, qid)
    from atlas_plus.mcp.memory_server import fts

    fts.update_incremental()
    embedded = None
    if mode == "hybrid":
        # the semantic index is opt-in in Sextant (`sextant embed-index`); build it the same way
        from atlas_plus.mcp.memory_server import embeddings

        embedded = embeddings.rebuild_index()
    index_ms = (time.perf_counter() - started) * 1000
    if queries_file:
        queries = json.loads(Path(queries_file).read_text(encoding="utf-8"))
        return {"question_id": qid, "mode": mode,
                "searches": [search(q, limit) for q in queries]}
    result = search(entry["question"], limit)
    answer_positions = sorted(
        i for i, sid in enumerate(entry["haystack_session_ids"]) if sid in set(entry["answer_session_ids"])
    )
    return {"question_id": qid, "question_type": entry["question_type"], "mode": mode,
            "sessions": len(entry["haystack_sessions"]), "index_ms": round(index_ms, 1),
            "embedded": embedded,
            "answer_positions": answer_positions, **result}


def main(argv: list[str]) -> int:
    split, mode, qid = argv[0], argv[1], argv[2]
    queries_file = argv[argv.index("--queries") + 1] if "--queries" in argv else None
    limit = int(argv[argv.index("--limit") + 1]) if "--limit" in argv else 10
    if mode not in MODES:
        raise SystemExit(f"mode must be one of {MODES}")
    out = worker(split, mode, qid, queries_file, limit)
    sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
