"""Conversation history for Atlas Brain stored in .atlas/state.sqlite.

Schema:
  brain_conversations(id, session_id, role, content, timestamp, model, tool_calls_json)

Each row is one message turn. Sessions are identified by a string ID
(Telegram user ID, a UUID for REST clients, or a fixed ID for CLI use).
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from atlas_plus import paths as atlas_paths

_STATE_DIR = atlas_paths.state_dir()
_DB_PATH = _STATE_DIR / "state.sqlite"

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS brain_conversations (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id      TEXT    NOT NULL,
    role            TEXT    NOT NULL,
    content         TEXT    NOT NULL,
    timestamp       TEXT    NOT NULL,
    model           TEXT,
    tool_calls_json TEXT,
    caller_identity TEXT
);
"""
_CREATE_INDEX = """
CREATE INDEX IF NOT EXISTS idx_brain_conv_session
    ON brain_conversations(session_id, id);
"""
_MIGRATE_CALLER_IDENTITY = """
ALTER TABLE brain_conversations ADD COLUMN caller_identity TEXT;
"""


def _connect(db_path: Optional[Path] = None) -> sqlite3.Connection:
    if db_path is None:  # resolve at call time so tests can monkeypatch _DB_PATH
        db_path = _DB_PATH
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(_CREATE_TABLE)
    conn.execute(_CREATE_INDEX)
    # Safe migration: add caller_identity if it doesn't exist yet
    existing = {row[1] for row in conn.execute("PRAGMA table_info(brain_conversations)")}
    if "caller_identity" not in existing:
        conn.execute(_MIGRATE_CALLER_IDENTITY)
    conn.commit()
    return conn


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")


def add_message(
    session_id: str,
    role: str,
    content: str,
    model: Optional[str] = None,
    tool_calls: Optional[list] = None,
    caller_identity: Optional[str] = None,
    db_path: Optional[Path] = None,
) -> None:
    """Append one message row to brain_conversations for the given session.

    Called by agent.chat() for every user, assistant, and tool-result turn so
    subsequent calls to the same session_id have full prior context. The
    caller_identity column is used by the Brain server's per-identity rate
    limiter and session-identity-binding check.
    """
    with _connect(db_path) as conn:
        conn.execute(
            "INSERT INTO brain_conversations"
            "(session_id,role,content,timestamp,model,tool_calls_json,caller_identity) "
            "VALUES (?,?,?,?,?,?,?)",
            (
                session_id, role, content, _now_iso(), model,
                json.dumps(tool_calls) if tool_calls else None,
                caller_identity,
            ),
        )


def get_session_identity(session_id: str, db_path: Optional[Path] = None) -> Optional[str]:
    """Return the caller_identity recorded for this session, or None."""
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT caller_identity FROM brain_conversations "
            "WHERE session_id=? AND caller_identity IS NOT NULL ORDER BY id LIMIT 1",
            (session_id,),
        ).fetchone()
    return row["caller_identity"] if row else None


def get_messages(
    session_id: str,
    limit: int = 40,
    db_path: Optional[Path] = None,
) -> list[dict]:
    """Return the last `limit` messages for a session, oldest first."""
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT role, content, model, tool_calls_json FROM brain_conversations "
            "WHERE session_id=? ORDER BY id DESC LIMIT ?",
            (session_id, limit),
        ).fetchall()
    # Reverse to chronological order
    result = []
    for row in reversed(rows):
        msg: dict = {"role": row["role"], "content": row["content"]}
        if row["tool_calls_json"]:
            msg["tool_calls"] = json.loads(row["tool_calls_json"])
        result.append(msg)
    return result


def clear_session(session_id: str, db_path: Optional[Path] = None) -> int:
    """Delete all messages for a session. Returns number of rows deleted."""
    with _connect(db_path) as conn:
        cur = conn.execute(
            "DELETE FROM brain_conversations WHERE session_id=?",
            (session_id,),
        )
        return cur.rowcount


def session_export_markdown(
    session_id: str,
    limit: int = 500,
    db_path: Optional[Path] = None,
) -> str:
    """Render a Brain session as a markdown document.

    Includes all messages up to `limit`, formatted with role headers and
    an export timestamp. Returns empty string if the session has no messages.
    """
    messages = get_messages(session_id, limit=limit, db_path=db_path)
    if not messages:
        return ""

    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    lines = [
        f"# Brain Session: {session_id}",
        "",
        f"Exported: {now_str}",
        f"Messages: {len(messages)}",
        "",
        "---",
        "",
    ]
    for msg in messages:
        role = msg["role"].upper()
        content = msg.get("content", "")
        lines.append(f"## {role}")
        lines.append("")
        lines.append(content)
        lines.append("")
        lines.append("---")
        lines.append("")

    return "\n".join(lines)


def session_count(db_path: Optional[Path] = None) -> int:
    """Return the total number of distinct Brain conversation sessions on record."""
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT COUNT(DISTINCT session_id) FROM brain_conversations"
        ).fetchone()
        return row[0] if row else 0


def list_sessions(limit: int = 20, db_path: Optional[Path] = None) -> list[dict]:
    """Return recent Brain conversation sessions with message counts and last activity.

    Returns list of {session_id, message_count, last_active} sorted by most recent first.
    """
    with _connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT session_id,
                   COUNT(*) AS message_count,
                   MAX(timestamp) AS last_active
            FROM brain_conversations
            GROUP BY session_id
            ORDER BY last_active DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [
        {
            "session_id": r["session_id"],
            "message_count": r["message_count"],
            "last_active": r["last_active"],
        }
        for r in rows
    ]
