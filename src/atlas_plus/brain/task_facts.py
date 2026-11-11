"""Task-to-memory pipeline: extract fact candidates from completed Brain tasks.

After a task completes with substantial result text, this module extracts
candidate facts and stores them as 'pending' in the SQLite DB. The user
approves or rejects each candidate via the Web UI Memory tab.

Approved facts are written to .memory/ via memory_remember with source='agent'.
This closes the loop: Brain work produces memory, with human approval gate.
"""

from __future__ import annotations

import os
import re
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import os
from atlas_plus import paths as atlas_paths

_STATE_DIR = atlas_paths.state_dir()
_DB_PATH = _STATE_DIR / "state.sqlite"

_MIN_RESULT_CHARS = 300  # don't process short task results
_MAX_CANDIDATES = 5      # cap extracted candidates per task
_MIN_SENTENCE_WORDS = 6  # filter out very short sentences

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS pending_facts (
    id          TEXT PRIMARY KEY,
    task_id     TEXT NOT NULL,
    fact_text   TEXT NOT NULL,
    scope_hint  TEXT NOT NULL DEFAULT 'reference:task-result',
    confidence  REAL NOT NULL DEFAULT 0.5,
    status      TEXT NOT NULL DEFAULT 'pending',
    created_at  TEXT NOT NULL,
    reviewed_at TEXT,
    result_file TEXT
);
"""
_CREATE_INDEX = "CREATE INDEX IF NOT EXISTS idx_pending_facts_status ON pending_facts(status, created_at DESC);"


@dataclass
class PendingFact:
    """A fact candidate extracted by an agent task, awaiting human review before promotion to .memory/."""

    id: str
    task_id: str
    fact_text: str
    scope_hint: str
    confidence: float
    status: str
    created_at: str
    reviewed_at: Optional[str] = None
    result_file: Optional[str] = None

    def as_dict(self) -> dict:
        """Serialize to plain dict for JSON responses (pending facts API)."""
        return {
            "id": self.id,
            "task_id": self.task_id,
            "fact_text": self.fact_text,
            "scope_hint": self.scope_hint,
            "confidence": self.confidence,
            "status": self.status,
            "created_at": self.created_at,
            "reviewed_at": self.reviewed_at,
            "result_file": self.result_file,
        }


def _connect(db_path: Optional[Path] = None) -> sqlite3.Connection:
    if db_path is None:  # resolve at call time so tests can monkeypatch _DB_PATH
        db_path = _DB_PATH
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(_CREATE_TABLE)
    conn.execute(_CREATE_INDEX)
    conn.commit()
    return conn


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")


def _extract_sentences(text: str) -> list[str]:
    """Extract clean declarative sentences from result text."""
    # Strip markdown headers, code blocks, bullet points
    lines = text.splitlines()
    cleaned: list[str] = []
    in_code = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("```"):
            in_code = not in_code
            continue
        if in_code:
            continue
        if stripped.startswith("#") or stripped.startswith("```"):
            continue
        # Strip bullet/list markers
        stripped = re.sub(r"^[-*+]\s+", "", stripped)
        stripped = re.sub(r"^\d+\.\s+", "", stripped)
        if stripped:
            cleaned.append(stripped)

    # Split into sentences and filter
    full_text = " ".join(cleaned)
    sentences = re.split(r"(?<=[.!?])\s+", full_text)
    result: list[str] = []
    for s in sentences:
        s = s.strip()
        word_count = len(s.split())
        # Keep declarative sentences of reasonable length
        if (word_count >= _MIN_SENTENCE_WORDS
                and not s.endswith("?")
                and not s.lower().startswith(("note:", "warning:", "example:", "e.g."))
                and len(s) < 300):
            result.append(s)
    return result


def _score_sentence(sentence: str, instruction: str) -> float:
    """Heuristic quality score 0-1. Higher = more fact-like."""
    score = 0.5
    words = sentence.split()
    # Longer sentences are more specific
    if len(words) >= 15:
        score += 0.1
    if len(words) >= 25:
        score += 0.1
    # Sentences with numbers or version strings tend to be more concrete
    if re.search(r"\b\d+\.?\d*\b", sentence):
        score += 0.1
    # Sentences that reference the instruction topic are more relevant
    inst_words = set(w.lower() for w in instruction.split() if len(w) > 4)
    sent_words = set(w.lower() for w in words if len(w) > 4)
    overlap = len(inst_words & sent_words) / max(len(inst_words), 1)
    score += min(0.2, overlap * 0.4)
    return min(1.0, score)


def _infer_scope(instruction: str) -> str:
    """Infer a memory scope from the task instruction.

    Falls back to 'reference:task-result' when no pattern matches.
    """
    instr_lower = instruction.lower()
    # Known projects: the engine's own names plus whatever the operator
    # registers via ATLAS_KNOWN_PROJECTS ("name:synonym|name2" format).
    # The previous hardcoded list was the maintainer's private project
    # roster and shipped publicly (paranoid stranger pass 2026-09-23).
    _KNOWN_PROJECTS: list[tuple[str, ...]] = [
        ("atlas-memory", "atlas"),
        ("sextant",),
    ]
    extra = os.environ.get("ATLAS_KNOWN_PROJECTS", "")
    for spec in extra.split("|"):
        parts = tuple(p.strip().lower() for p in spec.split(":") if p.strip())
        if parts:
            _KNOWN_PROJECTS.append(parts)
    for synonyms in _KNOWN_PROJECTS:
        if any(s in instr_lower for s in synonyms):
            return f"project:{synonyms[0]}"
    # Semantic patterns
    if any(w in instr_lower for w in ("decision", "decided", "architecture", "chose", "choice", "tradeoff")):
        return "reference:decision"
    if any(w in instr_lower for w in ("preference", "feedback", "style", "convention", "rule", "always", "never")):
        return "feedback"
    return "reference:task-result"


def extract_candidates(
    task_id: str,
    instruction: str,
    result_text: str,
    db_path: Optional[Path] = None,
) -> list[PendingFact]:
    """Extract and persist fact candidates from a task result.

    Returns the persisted candidates (empty if result is too short).
    Only called when result_text is substantial (> _MIN_RESULT_CHARS).
    """
    if len(result_text) < _MIN_RESULT_CHARS:
        return []

    sentences = _extract_sentences(result_text)
    if not sentences:
        return []

    # Score and rank
    scored = [(s, _score_sentence(s, instruction)) for s in sentences]
    scored.sort(key=lambda x: x[1], reverse=True)
    top = scored[:_MAX_CANDIDATES]

    now = _now()
    scope_hint = _infer_scope(instruction)
    facts: list[PendingFact] = []
    with _connect(db_path) as conn:
        for sentence, score in top:
            fid = f"pf-{uuid.uuid4().hex[:10]}"
            conn.execute(
                "INSERT INTO pending_facts(id,task_id,fact_text,scope_hint,confidence,status,created_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (fid, task_id, sentence, scope_hint, round(score, 3), "pending", now),
            )
            facts.append(PendingFact(
                id=fid, task_id=task_id, fact_text=sentence,
                scope_hint=scope_hint, confidence=round(score, 3),
                status="pending", created_at=now,
            ))

    return facts


def list_pending_facts(
    status: str = "pending",
    limit: int = 50,
    db_path: Optional[Path] = None,
) -> list[PendingFact]:
    """Return pending_facts rows filtered by status, newest first. Status: pending/approved/rejected."""
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM pending_facts WHERE status=? ORDER BY created_at DESC LIMIT ?",
            (status, limit),
        ).fetchall()
    return [
        PendingFact(
            id=r["id"], task_id=r["task_id"], fact_text=r["fact_text"],
            scope_hint=r["scope_hint"], confidence=r["confidence"],
            status=r["status"], created_at=r["created_at"],
            reviewed_at=r["reviewed_at"], result_file=r["result_file"],
        )
        for r in rows
    ]


def approve_fact(
    fact_id: str,
    scope: Optional[str] = None,
    db_path: Optional[Path] = None,
) -> dict:
    """Write an approved fact to .memory/ and mark it approved."""
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM pending_facts WHERE id=?", (fact_id,)
        ).fetchone()

    if not row:
        return {"ok": False, "error": f"Pending fact {fact_id!r} not found"}

    target_scope = scope or row["scope_hint"] or "reference:task-result"
    fact_text = row["fact_text"]

    try:
        from atlas_plus.mcp.memory_server.tools.remember import memory_remember
        result = memory_remember(scope=target_scope, fact=fact_text, source="agent")
    except Exception as exc:
        return {"ok": False, "error": str(exc)}

    now = _now()
    with _connect(db_path) as conn:
        conn.execute(
            "UPDATE pending_facts SET status='approved', reviewed_at=?, result_file=? WHERE id=?",
            (now, result.get("file"), fact_id),
        )

    return {"ok": True, "file": result.get("file"), "fact_id": fact_id}


def reject_fact(fact_id: str, db_path: Optional[Path] = None) -> dict:
    """Mark a pending fact as rejected."""
    with _connect(db_path) as conn:
        cur = conn.execute(
            "UPDATE pending_facts SET status='rejected', reviewed_at=? WHERE id=?",
            (_now(), fact_id),
        )
    return {"ok": cur.rowcount == 1, "fact_id": fact_id}


def pending_count(db_path: Optional[Path] = None) -> int:
    """Return count of facts with status='pending' -- used by Memory tab badge and weekly digest."""
    with _connect(db_path) as conn:
        row = conn.execute("SELECT COUNT(*) FROM pending_facts WHERE status='pending'").fetchone()
    return row[0] if row else 0
