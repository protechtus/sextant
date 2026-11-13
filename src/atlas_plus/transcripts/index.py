"""SQLite + FTS5 index for transcript metadata and content search.

The index is a derivative view: every fact in it can be rebuilt from the
NDJSON files. We treat the NDJSON as the source of truth; the index is for
fast list/search/tail and may be dropped and rebuilt without data loss.

Schema:
    conversations(conv_id PK, source, started_at, last_seen_at, turn_count, head_hash)
    turns(turn_id PK, conv_id, source, role, ts, model, tokens_in, tokens_out, hash, parent_turn_id, content_preview)
    turns_fts(content)               -- FTS5 virtual table for content search
    cursors(source PK, path_or_id, position, last_seen_at)

The FTS table is contentless (`content=''`) so we explicitly insert into it.
"""

from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from atlas_plus.transcripts.store import TurnRecord


_SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
    conv_id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    started_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    turn_count INTEGER NOT NULL DEFAULT 0,
    head_hash TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS turns (
    turn_id TEXT PRIMARY KEY,
    conv_id TEXT NOT NULL,
    source TEXT NOT NULL,
    role TEXT NOT NULL,
    ts TEXT NOT NULL,
    model TEXT,
    tokens_in INTEGER NOT NULL DEFAULT 0,
    tokens_out INTEGER NOT NULL DEFAULT 0,
    hash TEXT NOT NULL,
    parent_turn_id TEXT,
    content_preview TEXT,
    FOREIGN KEY (conv_id) REFERENCES conversations(conv_id)
);
CREATE INDEX IF NOT EXISTS idx_turns_conv_ts ON turns(conv_id, ts);
CREATE INDEX IF NOT EXISTS idx_turns_source_ts ON turns(source, ts);

CREATE VIRTUAL TABLE IF NOT EXISTS turns_fts USING fts5(turn_id UNINDEXED, content);

CREATE TABLE IF NOT EXISTS cursors (
    source TEXT NOT NULL,
    path_or_id TEXT NOT NULL,
    position INTEGER NOT NULL DEFAULT 0,
    last_seen_at TEXT NOT NULL,
    PRIMARY KEY (source, path_or_id)
);
"""


def _content_preview(content) -> str:
    if isinstance(content, str):
        return content[:280]
    if isinstance(content, list):
        bits = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    bits.append(str(block.get("text", "")))
                elif block.get("type") == "tool_use":
                    bits.append(f"[tool_use:{block.get('name','?')}]")
                elif block.get("type") == "tool_result":
                    bits.append("[tool_result]")
        return " ".join(bits)[:280]
    return str(content)[:280]


class TranscriptIndex:
    """SQLite + FTS5 index for transcript conversations and turns: supports list, search, and cursor-based tailing."""

    def __init__(self, db_path: str | Path | None = None) -> None:
        env_path = os.environ.get("ATLAS_TRANSCRIPT_INDEX")
        from atlas_plus import paths as atlas_paths
        default = Path(
            os.environ.get("ATLAS_TRANSCRIPT_ROOT")
            or (atlas_paths.state_dir() / "transcripts")
        ) / "index.sqlite"
        self.db_path = Path(db_path or env_path or default)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._init()

    def _init(self) -> None:
        # executescript implicitly commits, so do schema setup in autocommit
        # mode without wrapping in our own transaction.
        conn = sqlite3.connect(self.db_path, timeout=10.0, isolation_level=None)
        try:
            # busy_timeout FIRST: switching journal mode needs an exclusive
            # lock, and two connections initializing the same fresh DB raced
            # to "database is locked" (deep-pass probe 2026-09-27 -- 1-in-few
            # concurrent Clip captures 500'd). With the timeout the loser
            # waits; if another connection already switched, tolerate it --
            # WAL is persistent once set.
            conn.execute("PRAGMA busy_timeout=30000")
            try:
                conn.execute("PRAGMA journal_mode=WAL")
            except sqlite3.OperationalError:
                pass
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.executescript(_SCHEMA)
        finally:
            conn.close()

    @contextmanager
    def _connect(self):
        # Use Python's default deferred-transaction mode and rely on the
        # `with conn:` pattern for commit/rollback. This is sqlite3-idiomatic
        # and avoids fighting executescript() or DDL statements.
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def upsert_turn(self, record: TurnRecord) -> None:
        """Insert or replace a TurnRecord in conversations + turns + turns_fts tables."""
        with self._lock, self._connect() as conn:
            conn.execute(
                """INSERT OR IGNORE INTO conversations
                   (conv_id, source, started_at, last_seen_at, turn_count, head_hash)
                   VALUES (?, ?, ?, ?, 0, '')""",
                (record.conversation_id, record.source, record.ts, record.ts),
            )
            conn.execute(
                """INSERT OR REPLACE INTO turns
                   (turn_id, conv_id, source, role, ts, model, tokens_in, tokens_out,
                    hash, parent_turn_id, content_preview)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    record.turn_id,
                    record.conversation_id,
                    record.source,
                    record.role,
                    record.ts,
                    record.model,
                    record.tokens_in,
                    record.tokens_out,
                    record.hash,
                    record.parent_turn_id,
                    _content_preview(record.content),
                ),
            )
            preview = _content_preview(record.content)
            if preview:
                conn.execute("DELETE FROM turns_fts WHERE turn_id = ?", (record.turn_id,))
                conn.execute(
                    "INSERT INTO turns_fts (turn_id, content) VALUES (?, ?)",
                    (record.turn_id, preview),
                )
            conn.execute(
                """UPDATE conversations
                   SET last_seen_at = ?, turn_count = turn_count + 1, head_hash = ?
                   WHERE conv_id = ?""",
                (record.ts, record.hash, record.conversation_id),
            )

    def get_cursor(self, source: str, path_or_id: str) -> int:
        """Return the last-read byte position for a source/file (0 if never read)."""
        with self._connect() as conn:
            cur = conn.execute(
                "SELECT position FROM cursors WHERE source = ? AND path_or_id = ?",
                (source, path_or_id),
            )
            row = cur.fetchone()
            return int(row[0]) if row else 0

    def set_cursor(self, source: str, path_or_id: str, position: int) -> None:
        """Upsert the read cursor position for a source/file (used for incremental tailing)."""
        now = datetime.now(timezone.utc).isoformat()
        with self._lock, self._connect() as conn:
            conn.execute(
                """INSERT INTO cursors (source, path_or_id, position, last_seen_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(source, path_or_id)
                   DO UPDATE SET position = excluded.position, last_seen_at = excluded.last_seen_at""",
                (source, path_or_id, position, now),
            )

    def list_conversations(self, source: str | None = None, limit: int = 100) -> list[dict]:
        """List conversations ordered by last_seen_at descending. Optionally filter by source."""
        sql = "SELECT conv_id, source, started_at, last_seen_at, turn_count FROM conversations"
        args: tuple = ()
        if source:
            sql += " WHERE source = ?"
            args = (source,)
        sql += " ORDER BY last_seen_at DESC LIMIT ?"
        args = args + (limit,)
        with self._connect() as conn:
            cur = conn.execute(sql, args)
            return [
                {"conv_id": r[0], "source": r[1], "started_at": r[2], "last_seen_at": r[3], "turn_count": r[4]}
                for r in cur.fetchall()
            ]

    def search(self, query: str, limit: int = 50) -> Iterator[dict]:
        """FTS5 search across turn content, yielding dicts with conv_id, turn_id, snippet.

        A query that is not valid FTS5 syntax (`app.py`, `state-dir`, an unbalanced quote) is
        retried as one literal phrase instead of raising (AG34).
        """
        import sqlite3 as _sqlite3

        with self._connect() as conn:
            try:
                cur = conn.execute(
                    """SELECT t.turn_id, t.conv_id, t.source, t.role, t.ts, t.content_preview
                       FROM turns_fts JOIN turns t ON t.turn_id = turns_fts.turn_id
                       WHERE turns_fts MATCH ? LIMIT ?""",
                    (query, limit),
                )
                rows = cur.fetchall()
            except _sqlite3.OperationalError:
                phrase = '"' + query.replace('"', '""') + '"'
                cur = conn.execute(
                    """SELECT t.turn_id, t.conv_id, t.source, t.role, t.ts, t.content_preview
                       FROM turns_fts JOIN turns t ON t.turn_id = turns_fts.turn_id
                       WHERE turns_fts MATCH ? LIMIT ?""",
                    (phrase, limit),
                )
                rows = cur.fetchall()
            for row in rows:
                yield {
                    "turn_id": row[0],
                    "conv_id": row[1],
                    "source": row[2],
                    "role": row[3],
                    "ts": row[4],
                    "preview": row[5],
                }
