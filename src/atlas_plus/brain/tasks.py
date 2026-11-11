"""Atlas Brain task store -- persistent async job tracking in SQLite.

Tasks are the unit of work for Brain-executed jobs. They differ from
chat sessions: a task has a lifecycle (pending -> running -> done/failed),
a single instruction, and a stored result.

## Lease mechanism

The task queue uses optimistic leasing so multiple workers can run without a
central coordinator.  A worker atomically sets `lease_owner` and `lease_expires_at`
in a single SQL UPDATE ... WHERE status='pending'.  If the UPDATE affects 0 rows,
another worker already claimed it.

Leases expire after _DEFAULT_LEASE_SECONDS.  The zombie-cleanup job (every :45
via APScheduler) resets tasks whose leases expired without a heartbeat update,
making them claimable again.  This covers the case where a worker crashes mid-task.

Workers call update_heartbeat() periodically so the zombie-cleanup job can distinguish
a live long-running task from a dead one.

## SQLite WAL mode

WAL (Write-Ahead Log) is used so readers never block writers and writers never block
readers.  Without WAL, a writer acquiring an exclusive lock would stall the Brain
FastAPI server's task-list queries during inline task execution -- adding visible
latency to the Web UI and API endpoints that poll for task status.

Sources that create tasks:
  - POST /task (REST API)
  - Brain scheduler (nightly cron jobs)
  - Telegram /task command
"""

from __future__ import annotations

import os
import re as _re
import sqlite3
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from atlas_plus import paths as atlas_paths

# Matches session_id values like "ticket-ATL-009" or "ticket-PRJ-001"
_TICKET_SESSION_RE = _re.compile(r"^ticket-([A-Z]{1,8}-\d+)", _re.IGNORECASE)

_STATE_DIR = atlas_paths.state_dir()
_DB_PATH = _STATE_DIR / "state.sqlite"

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS brain_tasks (
    task_id      TEXT PRIMARY KEY,
    session_id   TEXT NOT NULL,
    instruction  TEXT NOT NULL,
    kind         TEXT NOT NULL DEFAULT 'agent',
    priority     INTEGER NOT NULL DEFAULT 5,
    agent        TEXT,
    status       TEXT NOT NULL DEFAULT 'pending',
    created_at   TEXT NOT NULL,
    started_at   TEXT,
    finished_at  TEXT,
    result       TEXT,
    error        TEXT,
    attempts     INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 3,
    lease_owner  TEXT,
    lease_expires_at TEXT,
    heartbeat_at TEXT,
    artifact_path TEXT
);
"""
_CREATE_INDEX = """
CREATE INDEX IF NOT EXISTS idx_brain_tasks_status
    ON brain_tasks(status, created_at DESC);
"""

# Per-attempt telemetry: one row per execution attempt so failures are debuggable
# without reading the task result (which is overwritten on each attempt).
_CREATE_ATTEMPTS_TABLE = """
CREATE TABLE IF NOT EXISTS task_attempts (
    attempt_id    TEXT PRIMARY KEY,
    task_id       TEXT NOT NULL,
    attempt_num   INTEGER NOT NULL,
    agent         TEXT,
    outcome       TEXT NOT NULL,
    started_at    TEXT,
    finished_at   TEXT,
    result_summary TEXT
);
"""
_CREATE_ATTEMPTS_INDEX = """
CREATE INDEX IF NOT EXISTS idx_task_attempts_task
    ON task_attempts(task_id, attempt_num);
"""

# Notification ledger: one row per (task_id, status) that has been pushed to ntfy.
# Both the inline server path and the worker path consult this before sending, so a
# single task completion can never produce duplicate phone notifications regardless of
# which path finalizes it first or whether both notification modes are enabled.
_CREATE_NTFY_LEDGER = """
CREATE TABLE IF NOT EXISTS ntfy_sent (
    task_id  TEXT NOT NULL,
    status   TEXT NOT NULL,
    sent_at  TEXT NOT NULL,
    PRIMARY KEY (task_id, status)
);
"""

# Scheduler heartbeat ledger: one row per scheduler job_id, overwritten on each run.
# The Brain process writes here from _job_event_listener (every EVENT_JOB_EXECUTED /
# EVENT_JOB_ERROR); the separate Helm web process reads it cross-process from the
# shared state.sqlite to show "last maintenance run" health. One row per job (last
# run wins) keeps the table bounded no matter how often a high-frequency job fires.
_CREATE_SCHEDULER_RUNS = """
CREATE TABLE IF NOT EXISTS scheduler_runs (
    job_id       TEXT PRIMARY KEY,
    last_run_at  TEXT NOT NULL,
    outcome      TEXT NOT NULL,
    error        TEXT,
    run_count    INTEGER NOT NULL DEFAULT 1
);
"""

# 4000 chars stores ~1000 tokens of result -- enough for a full tool-call
# response or error message while keeping the SQLite row small. Longer results
# are stored as artifacts and referenced by artifact_path.
_RESULT_MAX_CHARS = 4000

# 5 minutes: generous enough for long Qwen3 reasoning rounds (~2-3 min on RTX 3070)
# while short enough that a crashed worker doesn't lock a task for long.
# The zombie-cleanup job re-claims tasks with expired leases every :45.
_DEFAULT_LEASE_SECONDS = 300

_MIGRATIONS = {
    "kind": "TEXT NOT NULL DEFAULT 'agent'",
    "priority": "INTEGER NOT NULL DEFAULT 5",
    "agent": "TEXT",
    "attempts": "INTEGER NOT NULL DEFAULT 0",
    "max_attempts": "INTEGER NOT NULL DEFAULT 3",
    "lease_owner": "TEXT",
    "lease_expires_at": "TEXT",
    "heartbeat_at": "TEXT",
    "artifact_path": "TEXT",
    "result_digest": "TEXT",  # 2-3 sentence AI-generated summary for long results
}

# These markers appear verbatim in agent output. They are generated by the dispatcher,
# not user-controlled, so hardcoding them as exact strings is safe and intentional.
_FAILURE_TEXT_MARKERS = (
    "could not reach ollama",
    "maximum tool call rounds reached",
)

_BLOCKED_TEXT_MARKERS = (
    "requires `atlas_autonomous=on`",
    "requires atlas_autonomous=on",
    "cannot proceed with tool-based actions",
    "would you like me to",
    "please specify which action",
)


@dataclass
class Task:
    """A Brain task record stored in state.sqlite: instruction, routing metadata, lifecycle timestamps, and lease info."""

    task_id: str
    session_id: str
    instruction: str
    status: str
    created_at: str
    kind: str = "agent"
    priority: int = 5
    agent: Optional[str] = None
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    result: Optional[str] = None
    error: Optional[str] = None
    attempts: int = 0
    max_attempts: int = 3
    lease_owner: Optional[str] = None
    lease_expires_at: Optional[str] = None
    # True when create_task(dedup=True) returned an existing pending row instead of
    # inserting. Not persisted; dataclasses.asdict() does include it (TaskResponse
    # declares the field, default False).
    deduplicated: bool = False
    heartbeat_at: Optional[str] = None
    artifact_path: Optional[str] = None
    result_digest: Optional[str] = None  # 2-3 sentence summary for long results

    def as_dict(self) -> dict:
        """Serialize to a plain dict for JSON responses and task list APIs."""
        return asdict(self)


def _ensure_schema(conn: sqlite3.Connection) -> None:
    columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(brain_tasks)").fetchall()
    }
    for column, definition in _MIGRATIONS.items():
        if column not in columns:
            conn.execute(f"ALTER TABLE brain_tasks ADD COLUMN {column} {definition}")


def _connect(db_path: Optional[Path] = None) -> sqlite3.Connection:
    """Open a connection to the task store SQLite database.

    Called on every public function -- connection-per-call rather than a
    persistent pool because Brain tasks are low-frequency and SQLite
    connections are cheap.  WAL mode is set on each connection; SQLite
    persists the mode in the DB header so it only takes effect on the first
    connection but is safe to set repeatedly.

    db_path=None resolves to the module's _DB_PATH at CALL time, not def
    time, so monkeypatching tasks._DB_PATH (tests, isolation fixtures)
    actually redirects every caller that relies on the default.  The old
    `db_path: Path = _DB_PATH` defaults bound the live path at import and
    made such patches silent no-ops -- which let suite runs plant real
    tasks in the live queue (2026-10-12).
    """
    if db_path is None:
        db_path = _DB_PATH
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")  # see module docstring
    conn.execute(_CREATE_TABLE)
    _ensure_schema(conn)
    conn.execute(_CREATE_INDEX)
    conn.execute(_CREATE_ATTEMPTS_TABLE)
    conn.execute(_CREATE_ATTEMPTS_INDEX)
    conn.execute(_CREATE_NTFY_LEDGER)
    conn.execute(_CREATE_SCHEDULER_RUNS)
    conn.commit()
    return conn


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")


def _record_activity(event: str, task_id: str, **fields) -> None:
    """Best-effort pulse for the Helm live map; must never affect task state."""
    try:
        from atlas_plus import activity
        activity.record("brain", event, task_id=task_id, **fields)
    except Exception:
        pass


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")


def claim_task_notification(task_id: str, status: str, db_path: Optional[Path] = None) -> bool:
    """Atomically claim the right to send ONE ntfy push for this (task_id, status).

    Returns True exactly once per (task_id, status) pair across every process --
    the Brain server inline path and the worker path both call this before
    pushing, so a completion can never produce duplicate phone notifications even
    if both notification modes are enabled or two paths race to finalize the task.
    Returns False if a push for this pair has already been claimed.

    The claim is the INSERT itself (INSERT OR IGNORE + rowcount), so it is atomic
    under SQLite's write lock without an explicit transaction block.
    """
    conn = _connect(db_path)
    try:
        cur = conn.execute(
            "INSERT OR IGNORE INTO ntfy_sent (task_id, status, sent_at) VALUES (?, ?, ?)",
            (task_id, status, _now()),
        )
        conn.commit()
        return cur.rowcount == 1
    finally:
        conn.close()


def classify_result(result: str) -> str:
    """Classify plain-text agent output into a task lifecycle status."""
    text = (result or "").strip()
    lower = text.lower()
    if not lower:
        return "done"
    if lower.startswith(("[brain error:", "[dispatch]")):
        return "failed"
    if lower.startswith("[") and any(
        marker in lower for marker in ("] exited ", " timed out after ", " cli not found")
    ):
        return "failed"
    if any(marker in lower for marker in _FAILURE_TEXT_MARKERS):
        return "failed"
    if any(marker in lower for marker in _BLOCKED_TEXT_MARKERS):
        return "blocked"
    return "done"


def result_indicates_failure(result: str) -> bool:
    """Return true when an agent returned a known failure as plain text."""
    return classify_result(result) == "failed"


def _row_to_task(row: sqlite3.Row) -> Task:
    return Task(
        task_id=row["task_id"],
        session_id=row["session_id"],
        instruction=row["instruction"],
        kind=row["kind"],
        priority=row["priority"],
        agent=row["agent"],
        status=row["status"],
        created_at=row["created_at"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        result=row["result"],
        error=row["error"],
        attempts=row["attempts"],
        max_attempts=row["max_attempts"],
        lease_owner=row["lease_owner"],
        lease_expires_at=row["lease_expires_at"],
        heartbeat_at=row["heartbeat_at"],
        artifact_path=row["artifact_path"],
        result_digest=row["result_digest"] if "result_digest" in row.keys() else None,
    )


def find_duplicate_pending(
    instruction: str,
    agent: Optional[str],
    dedup_window_minutes: int = 5,
    db_path: Optional[Path] = None,
    kind: Optional[str] = None,
) -> Optional[Task]:
    """Return an existing pending task with the same instruction+agent (and kind, when
    given) if one exists within the dedup window. Returns None when no duplicate found.

    Used to prevent double-submission when the same instruction is queued twice
    in quick succession (e.g. two concurrent escalation workers both firing).
    """
    cutoff = _iso(
        datetime.now(timezone.utc) - timedelta(minutes=dedup_window_minutes)
    )
    with _connect(db_path) as conn:
        row = _duplicate_pending_row(conn, instruction, agent, cutoff, kind)
    return _row_to_task(row) if row else None


def _duplicate_pending_row(conn: sqlite3.Connection, instruction: str,
                           agent: Optional[str], cutoff: str,
                           kind: Optional[str] = None) -> Optional[sqlite3.Row]:
    # kind is part of the key (round C 2026-11-10, lane B F6): a verify task must not be
    # swallowed by a pending agent task with the same text.
    where = "status='pending' AND instruction=? AND created_at >= ?"
    params: tuple = (instruction, cutoff)
    if agent:
        where += " AND agent=?"
        params += (agent,)
    if kind:
        where += " AND kind=?"
        params += (kind,)
    return conn.execute(
        "SELECT * FROM brain_tasks WHERE " + where + " ORDER BY created_at DESC LIMIT 1",
        params,
    ).fetchone()


def create_task(
    instruction: str,
    session_id: Optional[str] = None,
    task_id: Optional[str] = None,
    db_path: Optional[Path] = None,
    *,
    kind: str = "agent",
    priority: int = 5,
    agent: Optional[str] = None,
    max_attempts: int = 3,
    artifact_path: Optional[str] = None,
    dedup: bool = False,
) -> Task:
    """Insert a new task row with status='pending' and return the Task object.

    task_id is auto-generated as `task-<12hex>` unless supplied (useful for idempotent
    re-submission).  session_id defaults to `brain-<task_id>` so every task has a
    deterministic session even when none is provided by the caller.

    When dedup=True, checks for an existing pending task with the same instruction+agent
    created in the last 5 minutes and returns it (``deduplicated=True``) instead of
    creating a duplicate. The lookup and the insert run in ONE write transaction, so two
    clients submitting the same task at the same moment get the same row (wave-2
    2026-11-09, TASK-03).
    """
    tid = task_id or f"task-{uuid.uuid4().hex[:12]}"
    sid = session_id or f"brain-{tid}"
    now = _now()
    with _connect(db_path) as conn:
        # An explicit task_id is the caller's own idempotency key: never fold it into
        # another row (round C 2026-11-10, lane B F6).
        if dedup and not task_id:
            conn.execute("BEGIN IMMEDIATE")
            cutoff = _iso(datetime.now(timezone.utc) - timedelta(minutes=5))
            row = _duplicate_pending_row(conn, instruction, agent, cutoff, kind)
            if row:
                conn.commit()
                existing = _row_to_task(row)
                existing.deduplicated = True
                return existing
        conn.execute(
            "INSERT INTO brain_tasks("
            "task_id, session_id, instruction, kind, priority, agent, status, created_at, "
            "max_attempts, artifact_path"
            ") VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)",
            (
                tid,
                sid,
                instruction,
                kind,
                priority,
                agent,
                now,
                max(1, max_attempts),
                artifact_path,
            ),
        )
    return Task(
        task_id=tid,
        session_id=sid,
        instruction=instruction,
        kind=kind,
        priority=priority,
        agent=agent,
        status="pending",
        created_at=now,
        max_attempts=max(1, max_attempts),  # floor of 1: claim_next_task invariant
        artifact_path=artifact_path,
    )


def update_task_agent(
    task_id: str,
    agent_name: str,
    db_path: Optional[Path] = None,
) -> None:
    """Persist the agent that actually ran the task (may differ from the requested agent)."""
    with _connect(db_path) as conn:
        conn.execute(
            "UPDATE brain_tasks SET agent=? WHERE task_id=?",
            (agent_name, task_id),
        )


def set_task_priority(
    task_id: str,
    priority: int,
    db_path: Optional[Path] = None,
) -> bool:
    """Update priority for a pending task. Returns True if row was updated."""
    with _connect(db_path) as conn:
        cur = conn.execute(
            "UPDATE brain_tasks SET priority=? WHERE task_id=? AND status='pending'",
            (max(1, priority), task_id),
        )
    return cur.rowcount == 1


def task_stats(since_days: int = 7, db_path: Optional[Path] = None) -> dict:
    """Return task counts grouped by status and agent for today + last N days.

    Keys returned:
      today: {status: count} for tasks created today
      pending_total: total pending tasks (all time)
      exhausted_total: failed/blocked tasks with no retries left
      stale_running: running tasks with heartbeat older than 30 min (zombie candidates)
      by_agent_last_Nd: {agent: {status: count}} for the last since_days days
      since_days: the window used for by_agent_last_Nd
    """
    now = datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")
    week_cutoff = _iso(now.replace(hour=0, minute=0, second=0, microsecond=0) -
                       timedelta(days=since_days - 1))
    stale_cutoff = _iso(now - timedelta(minutes=30))
    with _connect(db_path) as conn:
        # Today's counts by status
        today_rows = conn.execute(
            "SELECT status, COUNT(*) as n FROM brain_tasks "
            "WHERE created_at >= ? GROUP BY status",
            (today,),
        ).fetchall()
        # Last N days by agent
        agent_rows = conn.execute(
            "SELECT agent, status, COUNT(*) as n FROM brain_tasks "
            "WHERE created_at >= ? AND agent IS NOT NULL GROUP BY agent, status",
            (week_cutoff,),
        ).fetchall()
        # Total pending (all time)
        pending_row = conn.execute(
            "SELECT COUNT(*) FROM brain_tasks WHERE status='pending'"
        ).fetchone()
        # Exhausted: failed/blocked AND no retries left
        exhausted_row = conn.execute(
            "SELECT COUNT(*) FROM brain_tasks "
            "WHERE status IN ('failed', 'blocked') AND attempts >= max_attempts"
        ).fetchone()
        # Stale running: running with heartbeat older than 30 min (zombie candidates)
        stale_row = conn.execute(
            "SELECT COUNT(*) FROM brain_tasks WHERE status='running' "
            "AND heartbeat_at IS NOT NULL AND heartbeat_at < ?",
            (stale_cutoff,),
        ).fetchone()

    today_counts = {r["status"]: r["n"] for r in today_rows}
    by_agent: dict[str, dict] = {}
    for r in agent_rows:
        by_agent.setdefault(r["agent"], {})[r["status"]] = r["n"]

    return {
        "today": today_counts,
        "pending_total": pending_row[0] if pending_row else 0,
        "exhausted_total": exhausted_row[0] if exhausted_row else 0,
        "stale_running": stale_row[0] if stale_row else 0,
        "by_agent_last_Nd": by_agent,
        "since_days": since_days,
    }


def worker_detail(db_path: Optional[Path] = None) -> dict:
    """Return per-worker granular stats for active and recently-active workers.

    Returns:
        {
            "active_workers": [
                {
                    "worker_id": "hostname-<uuid>",
                    "running_tasks": 1,
                    "last_heartbeat": "ISO-8601",
                    "tasks": [task_id, ...]
                }
            ],
            "pending_by_agent": {"brain": 3, "claude-code": 1, ...},
            "zombie_count": 0,
            "queue_depth": 5,
        }
    """
    now = datetime.now(timezone.utc)
    stale_cutoff = _iso(now - timedelta(minutes=30))

    if db_path is None:
        db_path = _DB_PATH
    if not db_path.exists():
        return {"active_workers": [], "pending_by_agent": {}, "zombie_count": 0, "queue_depth": 0}

    conn = None
    try:
        import sqlite3 as _sl
        conn = _sl.connect(str(db_path))
        conn.row_factory = _sl.Row

        # Active workers: running tasks grouped by lease_owner
        running_rows = conn.execute(
            "SELECT task_id, agent, lease_owner, heartbeat_at "
            "FROM brain_tasks WHERE status='running' ORDER BY heartbeat_at DESC"
        ).fetchall()

        workers: dict[str, dict] = {}
        for row in running_rows:
            wid = row["lease_owner"] or "unknown"
            if wid not in workers:
                workers[wid] = {
                    "worker_id": wid,
                    "running_tasks": 0,
                    "last_heartbeat": row["heartbeat_at"] or "",
                    "tasks": [],
                }
            workers[wid]["running_tasks"] += 1
            workers[wid]["tasks"].append(row["task_id"])
            if (row["heartbeat_at"] or "") > workers[wid]["last_heartbeat"]:
                workers[wid]["last_heartbeat"] = row["heartbeat_at"]

        # Pending tasks per agent (shows what's waiting)
        pending_rows = conn.execute(
            "SELECT agent, COUNT(*) as n FROM brain_tasks WHERE status='pending' "
            "GROUP BY agent"
        ).fetchall()
        pending_by_agent = {(r["agent"] or "unassigned"): r["n"] for r in pending_rows}

        # Zombie count: running tasks with stale heartbeat
        zombie_row = conn.execute(
            "SELECT COUNT(*) FROM brain_tasks WHERE status='running' "
            "AND heartbeat_at IS NOT NULL AND heartbeat_at < ?",
            (stale_cutoff,),
        ).fetchone()
        zombie_count = zombie_row[0] if zombie_row else 0

        queue_depth_row = conn.execute(
            "SELECT COUNT(*) FROM brain_tasks WHERE status='pending'"
        ).fetchone()
        queue_depth = queue_depth_row[0] if queue_depth_row else 0
    except Exception:
        return {"active_workers": [], "pending_by_agent": {}, "zombie_count": 0, "queue_depth": 0}
    finally:
        # Always release the connection -- a query exception previously hit the bare
        # except and returned without closing, leaking the sqlite handle each time.
        if conn is not None:
            conn.close()

    return {
        "active_workers": list(workers.values()),
        "pending_by_agent": pending_by_agent,
        "zombie_count": zombie_count,
        "queue_depth": queue_depth,
    }


def mark_running(
    task_id: str,
    db_path: Optional[Path] = None,
    lease_owner: Optional[str] = None,
    lease_seconds: int = _DEFAULT_LEASE_SECONDS,
) -> None:
    """Transition a task to 'running', setting started_at (once) and a fresh lease.

    lease_owner + lease_expires_at are only written when lease_owner is provided;
    heartbeat_at is updated on every call so the zombie-cleanup query can distinguish
    stale in-flight tasks from recently-active ones.
    """
    now = datetime.now(timezone.utc)
    lease_expires_at = _iso(now + timedelta(seconds=lease_seconds)) if lease_owner else None
    with _connect(db_path) as conn:
        conn.execute(
            "UPDATE brain_tasks SET status='running', started_at=COALESCE(started_at, ?), "
            "heartbeat_at=?, lease_owner=?, lease_expires_at=? WHERE task_id=?",
            (_iso(now), _iso(now), lease_owner, lease_expires_at, task_id),
        )
    _record_activity("task_running", task_id, worker=lease_owner or "")


def update_heartbeat(
    task_id: str,
    db_path: Optional[Path] = None,
    extend_lease_seconds: int = _DEFAULT_LEASE_SECONDS,
) -> None:
    """Refresh heartbeat_at and extend the lease for a running task.

    Called periodically by the worker while a long-running executor is active.
    Without this, the zombie-cleanup job (runs every :45 via APScheduler) will
    reap tasks whose lease_expires_at passes, even if the worker is still active.

    extend_lease_seconds: how far to push lease_expires_at into the future.
    Defaults to _DEFAULT_LEASE_SECONDS so each heartbeat resets the full window.
    """
    now = datetime.now(timezone.utc)
    new_lease = _iso(now + timedelta(seconds=extend_lease_seconds))
    with _connect(db_path) as conn:
        conn.execute(
            "UPDATE brain_tasks SET heartbeat_at=?, lease_expires_at=? "
            "WHERE task_id=? AND status='running'",
            (_iso(now), new_lease, task_id),
        )


_SUMMARY_MAX_CHARS = 300  # kept short -- this is a diagnostic hint, not the full result


def record_task_attempt(
    task_id: str,
    attempt_num: int,
    outcome: str,
    agent: Optional[str] = None,
    started_at: Optional[str] = None,
    finished_at: Optional[str] = None,
    result_summary: Optional[str] = None,
    db_path: Optional[Path] = None,
) -> None:
    """Append one telemetry row to task_attempts for a completed execution attempt.

    Called by the worker in _finish_task() after each attempt so the full attempt
    history survives retries (brain_tasks.result is overwritten on each attempt).
    outcome: 'done' | 'failed' | 'blocked'
    result_summary: first _SUMMARY_MAX_CHARS chars of result/error text.
    """
    aid = f"attempt-{uuid.uuid4().hex[:12]}"
    now = finished_at or _now()
    summary = (result_summary or "")[:_SUMMARY_MAX_CHARS] if result_summary else None
    with _connect(db_path) as conn:
        conn.execute(
            "INSERT INTO task_attempts"
            "(attempt_id, task_id, attempt_num, agent, outcome, started_at, finished_at, result_summary)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (aid, task_id, attempt_num, agent, outcome, started_at, now, summary),
        )


def list_task_attempts(task_id: str, db_path: Optional[Path] = None) -> list[dict]:
    """Return all attempt rows for a task, ordered by attempt_num ascending.

    Returns an empty list when the table does not exist (e.g. fresh DB that
    predates the task_attempts migration) or when no attempts are recorded.
    """
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT attempt_id, task_id, attempt_num, agent, outcome, "
            "started_at, finished_at, result_summary "
            "FROM task_attempts WHERE task_id=? ORDER BY attempt_num ASC",
            (task_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def _try_close_ticket(task_id: str, task_outcome: str, db_path: Path) -> None:
    """Close the originating ticket when a dispatched task reaches a terminal state.

    task_outcome: 'done' | 'blocked' | 'failed'
      done    -> ticket: done
      blocked -> ticket: blocked  (needs human)
      failed  -> ticket: todo     (retry eligible)

    Called best-effort from all three mark_* functions. Never raises.
    """
    try:
        with _connect(db_path) as conn:
            row = conn.execute(
                "SELECT session_id FROM brain_tasks WHERE task_id=?", (task_id,)
            ).fetchone()
        if not row:
            return
        m = _TICKET_SESSION_RE.match(row["session_id"] or "")
        if not m:
            return
        ticket_id = m.group(1).upper()
        ticket_status = {"done": "done", "blocked": "blocked", "failed": "todo"}[task_outcome]
        from atlas_plus.tickets.store import update_ticket_status
        update_ticket_status(ticket_id, ticket_status)
    except Exception:
        pass


def _fence(owner: Optional[str], attempt: Optional[int]) -> tuple[str, tuple]:
    """Extra WHERE terms for a finalizer that must only land on ITS OWN attempt.

    A worker that claimed a task passes the lease owner it claimed under (and the
    attempt number it was handed). After its lease expired and another worker
    reclaimed the task, its late completion must change nothing: not the lease the
    new owner holds, not a result the new owner already stored (wave-2 2026-11-09,
    TASK-01). Heartbeat already had this check; finalization did not. Callers that
    never claim (inline Brain paths, cancel buttons) pass nothing and keep the
    unfenced update.
    """
    sql, params = "", ()
    if owner is None and attempt is None:
        return sql, params
    # Any fence implies the row is still the running attempt: a terminal row is never
    # re-finalized through a fenced call (round C 2026-11-10, lane B F5).
    sql += " AND status='running'"
    if owner is not None:
        sql += " AND lease_owner=?"
        params += (owner,)
    if attempt is not None:
        sql += " AND attempts=?"
        params += (attempt,)
    return sql, params


def mark_done(
    task_id: str,
    result: str,
    db_path: Optional[Path] = None,
    *,
    artifact_path: Optional[str] = None,
    owner: Optional[str] = None,
    attempt: Optional[int] = None,
) -> bool:
    """Mark a task done, store result (truncated to _RESULT_MAX_CHARS), clear lease.

    Also triggers _try_close_ticket() to auto-close any originating ticket
    whose session_id matches the ticket-{ID} pattern. Returns False (and
    changes nothing) when ``owner``/``attempt`` are given and no longer match.
    """
    fence, fparams = _fence(owner, attempt)
    with _connect(db_path) as conn:
        cur = conn.execute(
            "UPDATE brain_tasks SET status='done', finished_at=?, result=?, error=NULL, "
            "lease_owner=NULL, lease_expires_at=NULL, heartbeat_at=NULL, "
            "artifact_path=COALESCE(?, artifact_path) WHERE task_id=?" + fence,
            (_now(), result[:_RESULT_MAX_CHARS], artifact_path, task_id, *fparams),
        )
    if cur.rowcount != 1:
        _record_activity("task_finalize_refused", task_id, outcome="done", owner=owner or "")
        return False
    _try_close_ticket(task_id, "done", db_path)
    _record_activity("task_done", task_id,
                     **({"ref": str(artifact_path)} if artifact_path else {}))
    return True


def set_result_digest(task_id: str, digest: str, db_path: Optional[Path] = None) -> None:
    """Store a 2-3 sentence AI-generated summary of a completed task's result.

    Only writes if the task exists and has status='done'. The digest is shown
    in Web UI Tasks tab to avoid loading the full result for every row.
    """
    digest_truncated = digest.strip()[:500]
    with _connect(db_path) as conn:
        conn.execute(
            "UPDATE brain_tasks SET result_digest=? WHERE task_id=? AND status='done'",
            (digest_truncated, task_id),
        )


def mark_blocked(
    task_id: str,
    result: str,
    db_path: Optional[Path] = None,
    *,
    artifact_path: Optional[str] = None,
    owner: Optional[str] = None,
    attempt: Optional[int] = None,
) -> bool:
    """Mark a task blocked (human intervention needed), clear lease, close ticket as blocked.

    Returns False (and changes nothing) when the owner/attempt fence no longer matches."""
    fence, fparams = _fence(owner, attempt)
    with _connect(db_path) as conn:
        cur = conn.execute(
            "UPDATE brain_tasks SET status='blocked', finished_at=?, result=?, "
            "lease_owner=NULL, lease_expires_at=NULL, heartbeat_at=NULL, "
            "artifact_path=COALESCE(?, artifact_path) WHERE task_id=?" + fence,
            (_now(), result[:_RESULT_MAX_CHARS], artifact_path, task_id, *fparams),
        )
    if cur.rowcount != 1:
        _record_activity("task_finalize_refused", task_id, outcome="blocked", owner=owner or "")
        return False
    _try_close_ticket(task_id, "blocked", db_path)
    _record_activity("task_blocked", task_id,
                     **({"ref": str(artifact_path)} if artifact_path else {}))
    return True


def mark_failed(
    task_id: str,
    error: str,
    db_path: Optional[Path] = None,
    *,
    artifact_path: Optional[str] = None,
    owner: Optional[str] = None,
    attempt: Optional[int] = None,
) -> bool:
    """Mark a task failed, store error message, clear lease, revert ticket to todo.

    Returns False (and changes nothing) when the owner/attempt fence no longer matches."""
    fence, fparams = _fence(owner, attempt)
    with _connect(db_path) as conn:
        cur = conn.execute(
            "UPDATE brain_tasks SET status='failed', finished_at=?, error=?, "
            "lease_owner=NULL, lease_expires_at=NULL, heartbeat_at=NULL, "
            "artifact_path=COALESCE(?, artifact_path) WHERE task_id=?" + fence,
            (_now(), error[:_RESULT_MAX_CHARS], artifact_path, task_id, *fparams),
        )
    if cur.rowcount != 1:
        _record_activity("task_finalize_refused", task_id, outcome="failed", owner=owner or "")
        return False
    _try_close_ticket(task_id, "failed", db_path)
    _record_activity("task_failed", task_id,
                     **({"ref": str(artifact_path)} if artifact_path else {}))
    return True


def claim_next_task(
    worker_id: str,
    lease_seconds: int = _DEFAULT_LEASE_SECONDS,
    db_path: Optional[Path] = None,
) -> Optional[Task]:
    """Claim the highest-priority pending or expired task for a worker."""
    now = datetime.now(timezone.utc)
    now_iso = _iso(now)
    lease_expires_at = _iso(now + timedelta(seconds=lease_seconds))
    with _connect(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM brain_tasks WHERE "
            "(status='pending' OR (status='running' AND lease_expires_at IS NOT NULL "
            "AND lease_expires_at <= ?)) "
            "AND attempts < max_attempts "
            "ORDER BY priority ASC, created_at ASC LIMIT 1",
            (now_iso,),
        ).fetchone()
        if not row:
            conn.commit()
            return None
        conn.execute(
            "UPDATE brain_tasks SET status='running', started_at=COALESCE(started_at, ?), "
            "attempts=attempts+1, lease_owner=?, lease_expires_at=?, heartbeat_at=?, "
            "error=NULL WHERE task_id=?",
            (now_iso, worker_id, lease_expires_at, now_iso, row["task_id"]),
        )
        conn.commit()
        claimed = conn.execute(
            "SELECT * FROM brain_tasks WHERE task_id=?", (row["task_id"],)
        ).fetchone()
    return _row_to_task(claimed) if claimed else None


def heartbeat_task(
    task_id: str,
    worker_id: str,
    lease_seconds: int = _DEFAULT_LEASE_SECONDS,
    db_path: Optional[Path] = None,
) -> bool:
    """Extend a running task lease when the caller owns it."""
    now = datetime.now(timezone.utc)
    with _connect(db_path) as conn:
        cur = conn.execute(
            "UPDATE brain_tasks SET heartbeat_at=?, lease_expires_at=? "
            "WHERE task_id=? AND lease_owner=? AND status='running'",
            (
                _iso(now),
                _iso(now + timedelta(seconds=lease_seconds)),
                task_id,
                worker_id,
            ),
        )
    return cur.rowcount == 1


def cleanup_zombie_tasks(
    max_running_minutes: int = 30,
    db_path: Optional[Path] = None,
) -> int:
    """Recover stale running tasks.

    A task is a zombie when it has been in 'running' state for longer than
    max_running_minutes without a heartbeat update. This handles process
    crashes and killed workers that never marked their tasks done/failed.

    Recovery follows the SAME retry policy as an expired-lease claim (wave-2
    2026-11-09, TASK-02): a task a worker claimed (lease owner set, attempt
    counted) goes back to 'pending' while attempts remain, and only becomes a
    terminal 'failed' once its budget is spent. Rows that were never claimed
    through the lease (inline runs, pre-lease rows) have no attempt record to
    retry against and are failed as before. Returns the number of rows touched.
    """
    cutoff = _iso(
        datetime.now(timezone.utc) - timedelta(minutes=max_running_minutes)
    )
    stale = ("status='running' AND ((heartbeat_at IS NOT NULL AND heartbeat_at <= ?) "
             "OR (heartbeat_at IS NULL AND started_at <= ?))")
    with _connect(db_path) as conn:
        requeued = conn.execute(
            "UPDATE brain_tasks SET status='pending', "
            "error='Task timed out (no heartbeat); requeued', "
            "lease_owner=NULL, lease_expires_at=NULL, heartbeat_at=NULL "
            "WHERE lease_owner IS NOT NULL AND attempts < max_attempts AND " + stale,
            (cutoff, cutoff),
        )
        failed = conn.execute(
            "UPDATE brain_tasks SET status='failed', finished_at=?, "
            "error='Task timed out (no heartbeat)', "
            "lease_owner=NULL, lease_expires_at=NULL "
            "WHERE " + stale,
            (_now(), cutoff, cutoff),
        )
        conn.commit()
    return (requeued.rowcount or 0) + (failed.rowcount or 0)


def get_task(task_id: str, db_path: Optional[Path] = None) -> Optional[Task]:
    """Return a single Task by ID, or None if not found."""
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM brain_tasks WHERE task_id=?", (task_id,)
        ).fetchone()
    return _row_to_task(row) if row else None


_VALID_SORT_COLS = frozenset({"created_at", "priority", "status", "finished_at", "started_at"})


def list_exhausted_tasks(
    limit: int = 20,
    db_path: Optional[Path] = None,
) -> list[Task]:
    """Return tasks that have exhausted all retry attempts without succeeding.

    A task is exhausted when it has failed or blocked AND attempts >= max_attempts,
    meaning the queue will not re-claim it. These are silent failures that require
    human attention or a manual /retry.
    """
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM brain_tasks "
            "WHERE status IN ('failed', 'blocked') AND attempts >= max_attempts "
            "ORDER BY finished_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [_row_to_task(r) for r in rows]


def list_tasks(
    status: Optional[str] = None,
    agent: Optional[str] = None,
    since_hours: Optional[int] = None,
    limit: int = 20,
    db_path: Optional[Path] = None,
    sort_by: str = "created_at",
    sort_desc: bool = True,
) -> list[Task]:
    """Return tasks, optionally filtered by status, agent, and/or time window.

    sort_by:    Column to sort by. One of: created_at (default), priority,
                status, finished_at, started_at. Unknown values fall back to created_at.
    sort_desc:  Descending if True (default). Set False for ascending.
    agent:      Optional agent name filter (e.g. 'claude-code', 'brain').
    since_hours: Only return tasks created within the last N hours.
    """
    col = sort_by if sort_by in _VALID_SORT_COLS else "created_at"
    direction = "DESC" if sort_desc else "ASC"
    order = f"{col} {direction}"
    # Secondary sort ensures stable ordering when primary column has ties.
    # rowid is the SQLite auto-increment; higher rowid = later insert = more recent.
    if col != "created_at":
        order += ", created_at DESC, rowid DESC"
    else:
        order += ", rowid DESC"  # tiebreaker for same-second inserts

    filters = []
    params: list = []
    if status:
        filters.append("status=?")
        params.append(status)
    if agent:
        filters.append("agent=?")
        params.append(agent)
    if since_hours is not None and since_hours > 0:
        cutoff = _iso(datetime.now(timezone.utc) - timedelta(hours=since_hours))
        filters.append("created_at >= ?")
        params.append(cutoff)

    where = f"WHERE {' AND '.join(filters)}" if filters else ""
    params.append(limit)

    with _connect(db_path) as conn:
        rows = conn.execute(
            # bandit B608 justification: `order` comes from the _VALID_SORT_COLS allowlist
            # plus literal direction tokens; `where` is built only from
            # literal "col=?" fragments with bound params (bandit triage,
            # quality loop run 7).
            f"SELECT * FROM brain_tasks {where} ORDER BY {order} LIMIT ?",  # nosec B608
            params,
        ).fetchall()
    return [_row_to_task(r) for r in rows]


# ---------------------------------------------------------------------------
# Scheduler heartbeat ledger (scheduler_runs)
# ---------------------------------------------------------------------------
# These power Helm's "last maintenance run" health surface. The Brain process
# records a heartbeat for every scheduler job as it fires; any process (notably
# the separate Helm web process) reads the latest run per job from the shared
# state.sqlite. The staleness policy lives in atlas_plus.brain.maintenance.

def record_job_run(
    job_id: str,
    outcome: str,
    error: Optional[str] = None,
    when: Optional[str] = None,
    db_path: Optional[Path] = None,
) -> None:
    """Upsert the heartbeat for a scheduler job.

    outcome: "ok" for a clean execution, "error" for a job that raised.
    when:    ISO-8601 UTC timestamp; defaults to now. run_count increments on
             every call so the surface can show how many times a job has fired.
    Called from the scheduler's job-event listener inside the Brain process.
    """
    ts = when or _now()
    err = (error or "")[:_RESULT_MAX_CHARS] or None
    conn = _connect(db_path)
    try:
        conn.execute(
            """
            INSERT INTO scheduler_runs (job_id, last_run_at, outcome, error, run_count)
            VALUES (?, ?, ?, ?, 1)
            ON CONFLICT(job_id) DO UPDATE SET
                last_run_at = excluded.last_run_at,
                outcome     = excluded.outcome,
                error       = excluded.error,
                run_count   = scheduler_runs.run_count + 1
            """,
            (job_id, ts, outcome, err),
        )
        conn.commit()
    finally:
        conn.close()


def list_job_runs(db_path: Optional[Path] = None) -> list[dict]:
    """Return the latest heartbeat for every scheduler job, newest run first.

    Each dict: {job_id, last_run_at, outcome, error, run_count}. Returns [] when
    no job has ever recorded a heartbeat (e.g. a brand-new database).
    """
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            "SELECT job_id, last_run_at, outcome, error, run_count "
            "FROM scheduler_runs ORDER BY last_run_at DESC"
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]
