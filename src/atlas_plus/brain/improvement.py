"""Atlas Brain self-improvement -- quality scoring and observation loop.

After each task completes, Brain scores the output quality and logs it.
Over time these observations:
  - Improve routing (which agent handles which task type best)
  - Surface patterns worth adding to .memory/
  - Feed into the nightly reflexion loop

Quality scoring (0-10):
  - Length check: empty or trivial responses score low
  - Error detection: known failure markers score 0
  - Tool use: tasks that needed tools and used them score higher
  - Coherence: heuristic word/sentence ratio check
  - Routing match: did the chosen agent match the task type?

Storage: brain_observations table in state.sqlite
"""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from atlas_plus import paths as atlas_paths

_STATE_DIR = atlas_paths.state_dir()
_DB_PATH = _STATE_DIR / "state.sqlite"

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS brain_observations (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id      TEXT NOT NULL,
    agent_name   TEXT NOT NULL,
    model        TEXT NOT NULL,
    quality      REAL NOT NULL,
    signals      TEXT NOT NULL,
    created_at   TEXT NOT NULL
);
"""
_CREATE_INDEX = """
CREATE INDEX IF NOT EXISTS idx_obs_agent
    ON brain_observations(agent_name, created_at DESC);
"""


@dataclass
class Observation:
    """A scored task outcome used by the self-improvement loop to detect quality trends."""

    task_id: str
    agent_name: str
    model: str
    quality: float       # 0.0 - 10.0
    signals: dict        # breakdown of scoring components
    created_at: str

    def as_dict(self) -> dict:
        """Serialize to a plain dict; signals stays as a dict (not double-encoded)."""
        return {**asdict(self), "signals": self.signals}


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

_FAILURE_MARKERS = (
    "[brain error", "[dispatch]", "timed out after", "cli not found",
    "could not reach ollama", "exited 1:", "exited 2:",
)

_WEAK_MARKERS = (
    "i don't know", "i cannot", "i'm not sure", "i am not able",
    "as an ai", "i apologize",
)


def score(
    result_text: str,
    task_status: str,
    agent_name: str,
    instruction: str,
    tool_calls: int = 0,
) -> tuple[float, dict]:
    """Score a task result. Returns (quality_score, signals_dict)."""
    signals: dict = {}
    text = (result_text or "").strip()

    # Hard failures
    if task_status == "failed":
        signals["status"] = -5.0
        return 0.0, signals

    if task_status == "blocked":
        signals["status"] = -2.0
        return 2.0, signals

    signals["status"] = 0.0

    # Length / content
    word_count = len(text.split())
    if word_count < 5:
        signals["length"] = -3.0
    elif word_count < 20:
        signals["length"] = -1.0
    elif word_count > 50:
        signals["length"] = 1.0
    else:
        signals["length"] = 0.0

    # Known failure text in a "done" task
    lower = text.lower()
    if any(m in lower for m in _FAILURE_MARKERS):
        signals["failure_text"] = -4.0
    else:
        signals["failure_text"] = 0.0

    # Weak / hedging language
    weak_count = sum(1 for m in _WEAK_MARKERS if m in lower)
    signals["hedging"] = -0.5 * min(weak_count, 4)

    # Tool use bonus -- tasks that warranted tools and used them
    if tool_calls > 0:
        signals["tool_use"] = min(tool_calls * 0.5, 2.0)
    else:
        signals["tool_use"] = 0.0

    # Routing match heuristic -- does agent capability match instruction?
    instr_lower = instruction.lower()
    coding_task = any(p in instr_lower for p in ("code", "implement", "debug", "fix", "refactor"))
    if coding_task and agent_name in ("coder", "claude-code", "opencode", "codex"):
        signals["routing"] = 1.0
    elif not coding_task and agent_name in ("brain", "fast", "mistral"):
        signals["routing"] = 0.5
    else:
        signals["routing"] = 0.0

    raw = 5.0 + sum(signals.values())
    quality = round(max(0.0, min(10.0, raw)), 2)
    return quality, signals


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

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


def record(
    task_id: str,
    agent_name: str,
    model: str,
    result_text: str,
    task_status: str,
    instruction: str,
    tool_calls: int = 0,
    db_path: Optional[Path] = None,
) -> Observation:
    """Score a completed task result and persist the Observation to brain_observations.

    Scores use a 0-10 heuristic that considers: result length, task status, known
    failure patterns, tool-call count, and refusal detection. Results are used by
    the Brain improvement dashboard to surface low-performing agents and models.
    """
    quality, signals = score(result_text, task_status, agent_name, instruction, tool_calls)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    obs = Observation(
        task_id=task_id,
        agent_name=agent_name,
        model=model,
        quality=quality,
        signals=signals,
        created_at=now,
    )
    with _connect(db_path) as conn:
        conn.execute(
            "INSERT INTO brain_observations(task_id, agent_name, model, quality, signals, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (task_id, agent_name, model, quality, json.dumps(signals), now),
        )
    return obs


def agent_stats(agent_name: str, limit: int = 50, db_path: Optional[Path] = None) -> dict:
    """Return average quality + recent trend for an agent."""
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT quality FROM brain_observations WHERE agent_name=? "
            "ORDER BY created_at DESC LIMIT ?",
            (agent_name, limit),
        ).fetchall()
    if not rows:
        return {"agent": agent_name, "observations": 0, "avg_quality": None, "trend": None}
    scores = [r["quality"] for r in rows]
    avg = round(sum(scores) / len(scores), 2)
    # Simple trend: compare first half vs second half
    mid = len(scores) // 2
    trend = None
    if mid >= 2:
        recent = sum(scores[:mid]) / mid
        older = sum(scores[mid:]) / (len(scores) - mid)
        trend = round(recent - older, 2)
    return {
        "agent": agent_name,
        "observations": len(scores),
        "avg_quality": avg,
        "trend": trend,
    }


def agent_recent_scores(agent_name: str, limit: int = 15, db_path: Optional[Path] = None) -> list[float]:
    """Return the most recent quality scores oldest-first (for sparkline left-to-right rendering)."""
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT quality FROM brain_observations WHERE agent_name=? "
            "ORDER BY created_at DESC, id DESC LIMIT ?",
            (agent_name, limit),
        ).fetchall()
    return [r["quality"] for r in reversed(rows)]


def all_agent_recent_scores(limit: int = 15, db_path: Optional[Path] = None) -> dict[str, list[float]]:
    """Return recent quality scores for ALL agents in a single window-function query.

    Returns {agent_name: [scores oldest-first]} for sparkline rendering.
    Replaces N individual agent_recent_scores() calls with one query -- O(N) -> O(1).

    Requires SQLite >= 3.25 (window functions), available in Python >= 3.6 on all
    platforms shipping with sqlite3 3.25+ (Windows/macOS/Linux as of ~2018).
    """
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT agent_name, quality "
            "FROM ("
            "  SELECT agent_name, quality,"
            "         ROW_NUMBER() OVER (PARTITION BY agent_name"
            "                           ORDER BY created_at DESC, id DESC) AS rn"
            "  FROM brain_observations"
            ") WHERE rn <= ?"
            " ORDER BY agent_name, rn DESC",  # rn DESC = older rows first -> sparkline L->R
            (limit,),
        ).fetchall()
    result: dict[str, list[float]] = {}
    for r in rows:
        result.setdefault(r["agent_name"], []).append(r["quality"])
    return result


def all_agent_stats(db_path: Optional[Path] = None) -> list[dict]:
    """Return stats for all agents with observations using a single query.

    Fetches all quality data in one pass and computes avg + trend in Python.
    Replaces N sequential agent_stats() calls with one DB round-trip.
    """
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT agent_name, quality "
            "FROM brain_observations "
            "ORDER BY agent_name, created_at DESC, id DESC",
        ).fetchall()

    if not rows:
        return []

    agent_scores: dict[str, list[float]] = {}
    for r in rows:
        bucket = agent_scores.setdefault(r["agent_name"], [])
        if len(bucket) < 50:  # match agent_stats() default limit
            bucket.append(r["quality"])

    result = []
    for agent_name, scores in sorted(agent_scores.items()):
        avg = round(sum(scores) / len(scores), 2)
        mid = len(scores) // 2
        trend = None
        if mid >= 2:
            recent_half = sum(scores[:mid]) / mid
            older_half = sum(scores[mid:]) / (len(scores) - mid)
            trend = round(recent_half - older_half, 2)
        result.append(
            {"agent": agent_name, "observations": len(scores), "avg_quality": avg, "trend": trend}
        )
    return result


def low_quality_tasks(threshold: float = 4.0, limit: int = 10, db_path: Optional[Path] = None) -> list[dict]:
    """Return recent low-quality tasks for review."""
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT task_id, agent_name, quality, signals, created_at FROM brain_observations "
            "WHERE quality <= ? ORDER BY created_at DESC LIMIT ?",
            (threshold, limit),
        ).fetchall()
    return [
        {
            "task_id": r["task_id"],
            "agent": r["agent_name"],
            "quality": r["quality"],
            "signals": json.loads(r["signals"]),
            "created_at": r["created_at"],
        }
        for r in rows
    ]
