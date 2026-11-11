"""Automated ticket dispatch -- creates Brain tasks from eligible todo tickets.

Stats tracking: every dispatch_all_eligible() run updates module-level _DISPATCH_STATS.
Call get_dispatch_stats() to read current state; exposed via Brain GET /dispatch-stats.

The full Atlas automation loop:
  1. Plan session creates tickets (todo, assignee=claude-code|local|any|codex)
  2. TicketDispatcher (this module) polls every 10 min and fires Brain tasks
  3. Brain worker executes, mark_done()/mark_failed() calls _try_close_ticket()
  4. Ticket status updates automatically (done/blocked/todo-retry)

Session ID convention: tasks created from tickets use session_id="ticket-{TICKET_ID}"
(e.g., "ticket-ATL-009"). _try_close_ticket() in tasks.py parses this pattern to
map task outcomes back to ticket status updates.

Agent mapping (assignee -> Brain agent):
  claude-code -> claude-code   (Claude Code CLI)
  codex       -> codex         (Codex CLI)
  local       -> brain         (local Qwen3:8b)
  any         -> None          (smart routing / brain default)
"""

from __future__ import annotations

import logging
from typing import Optional
from atlas_plus.autonomous import autonomous_enabled

log = logging.getLogger("atlas.brain.dispatcher")

# Assignees eligible for automated dispatch (human tasks are never auto-dispatched)
_DISPATCHABLE_ASSIGNEES = {"claude-code", "codex", "local", "any"}

# Ticket assignee -> Brain agent name (None = let smart routing decide)
_AGENT_MAP: dict[str, Optional[str]] = {
    "claude-code": "claude-code",
    "codex":       "codex",
    "local":       "brain",
    "any":         None,
}

# Priority mapping for ticket -> task (critical=1, high=2, normal=5, low=8)
_PRIORITY_MAP: dict[str, int] = {
    "critical": 1,
    "high":     2,
    "normal":   5,
    "low":      8,
}


def _active_task_exists(session_id: str) -> bool:
    """Return True if there is already a pending or running task for this session_id."""
    try:
        from . import tasks as task_store
        tasks = task_store.list_tasks(status="pending", limit=200)
        tasks += task_store.list_tasks(status="running", limit=200)
        return any(t.session_id == session_id for t in tasks)
    except Exception:
        return False


def dispatch_ticket(ticket) -> Optional[object]:
    """Create a Brain task for a single dispatchable todo ticket.

    Args:
        ticket: Ticket dataclass with id, title, body, assignee, priority fields.

    Returns the created Task, or None if the ticket was already dispatched or
    dispatch failed.
    """
    if ticket.status != "todo":
        return None
    if ticket.assignee not in _DISPATCHABLE_ASSIGNEES:
        return None

    session_id = f"ticket-{ticket.id.upper()}"

    # Avoid double-dispatch: skip if active task already exists for this ticket
    if _active_task_exists(session_id):
        log.debug("Ticket %s already has an active task -- skipping", ticket.id)
        return None

    # Build instruction from title + body
    instruction = ticket.title.strip()
    if ticket.body and ticket.body.strip():
        instruction += f"\n\n{ticket.body.strip()}"

    agent = _AGENT_MAP.get(ticket.assignee)
    priority = _PRIORITY_MAP.get(str(ticket.priority).lower(), 5)

    # Use research_gap executor for tickets auto-filed by the gaps report
    kind = "research_gap" if ticket.title.strip().startswith("Research gap:") else "agent"

    try:
        from . import tasks as task_store
        task = task_store.create_task(
            instruction=instruction,
            session_id=session_id,
            agent=agent,
            priority=priority,
            kind=kind,
        )
        log.info(
            "Dispatched ticket %s -> task %s (agent=%s, priority=%d)",
            ticket.id, task.task_id[:16], agent or "auto", priority,
        )
    except Exception as exc:
        log.warning("Failed to create task for ticket %s: %s", ticket.id, exc)
        return None

    # Mark the ticket in-progress so it doesn't get re-dispatched
    try:
        from atlas_plus.tickets.store import update_ticket_status
        update_ticket_status(ticket.id, "in-progress")
    except Exception as exc:
        log.warning("Failed to set ticket %s to in-progress: %s", ticket.id, exc)

    return task


_LOW_QUALITY_PATTERNS = (
    "i cannot", "i don't know", "i do not know", "no information",
    "not available", "unable to", "i'm unable", "i am unable",
    "sorry, i", "i apologize", "as an ai", "as a language model",
    "[research] failed", "[brain] error", "[agent] failed",
)
_MIN_RESULT_LENGTH = 80  # results shorter than this are suspect
_DEFAULT_TICKET_QUALITY_THRESHOLD = 4.0


def classify_result_quality(result_text: str) -> float:
    """Score ticket task result quality on a 0-10 scale (heuristic, no LLM).

    Used as a lightweight gate before marking ticket done. Pessimistic about
    very short results and known failure/refusal patterns.

    Scoring:
      0.0  -- empty result
      1.0  -- contains known failure/refusal pattern
      2.0  -- shorter than _MIN_RESULT_LENGTH chars
      7.0  -- acceptable result (default)

    Returns 7.0 for results that don't match any negative pattern.
    """
    text = (result_text or "").strip()
    if not text:
        return 0.0
    lower = text.lower()
    if any(p in lower for p in _LOW_QUALITY_PATTERNS):
        return 1.0
    if len(text) < _MIN_RESULT_LENGTH:
        return 2.0
    return 7.0


def reopen_ticket_if_low_quality(session_id: str, result_text: str) -> bool:
    """Reopen a ticket if the linked task result is low quality.

    Called from worker._finish_task() after mark_done(). If the result quality
    score is below ATLAS_TICKET_QUALITY_THRESHOLD, updates the ticket back to
    'todo' so it gets dispatched again on the next poll.

    Only acts on tasks with session_id matching the ticket-{ID} pattern.
    Returns True if the ticket was reopened, False otherwise.
    """
    import os, re as _re
    m = _re.match(r"^ticket-([A-Z]{1,8}-\d+)", session_id or "", _re.IGNORECASE)
    if not m:
        return False

    threshold = float(os.environ.get("ATLAS_TICKET_QUALITY_THRESHOLD",
                                      str(_DEFAULT_TICKET_QUALITY_THRESHOLD)))
    score = classify_result_quality(result_text)
    if score >= threshold:
        return False

    ticket_id = m.group(1).upper()
    try:
        from atlas_plus.tickets.store import update_ticket_status
        update_ticket_status(ticket_id, "todo")
        log.info(
            "Ticket %s reopened: quality score %.1f < threshold %.1f",
            ticket_id, score, threshold,
        )
        return True
    except Exception as exc:
        log.warning("Failed to reopen ticket %s: %s", ticket_id, exc)
        return False


def is_dispatch_enabled() -> bool:
    """Return True when automated ticket dispatch is active.

    Controlled by ATLAS_TICKET_DISPATCH env var:
      on  / 1 / true  -> enabled
      off / 0 / false -> disabled (default -- opt-in)
    Also requires ATLAS_AUTONOMOUS != 'off' since dispatching writes task records.
    """
    import os
    dispatch_val = os.environ.get("ATLAS_TICKET_DISPATCH", "off").lower()
    if dispatch_val not in ("on", "1", "true"):
        return False
    return autonomous_enabled()  # canonical allowlist gate (audit 2026-09-21)


def dispatch_all_eligible() -> list[dict]:
    """Scan all todo tickets and dispatch eligible ones to Brain.

    Returns list of {ticket_id, task_id, agent} for each dispatched ticket.
    Empty list if no eligible tickets or on error.
    Updates module-level _DISPATCH_STATS so get_dispatch_stats() reflects
    the most recent run.
    """
    try:
        from atlas_plus.tickets.store import list_tickets
        tickets = list_tickets(status="todo")
    except Exception as exc:
        log.warning("Ticket dispatch: failed to list tickets: %s", exc)
        return []

    dispatched = []
    for ticket in tickets:
        if ticket.assignee not in _DISPATCHABLE_ASSIGNEES:
            continue
        task = dispatch_ticket(ticket)
        if task is not None:
            dispatched.append({
                "ticket_id": ticket.id,
                "task_id": task.task_id,
                "agent": task.agent or "auto",
            })

    if dispatched:
        log.info("Ticket dispatcher: dispatched %d ticket(s)", len(dispatched))
    else:
        log.debug("Ticket dispatcher: no eligible todo tickets found")

    _record_dispatch_run(len(dispatched))
    return dispatched


# ---------------------------------------------------------------------------
# Dispatch stats -- lightweight telemetry for scheduler and Web UI
# ---------------------------------------------------------------------------

# Tracks the last dispatch run: populated by _record_dispatch_run().
_DISPATCH_STATS: dict = {
    "last_run_at": None,       # ISO-8601 UTC timestamp of last dispatch_all_eligible() call
    "last_dispatched_count": 0, # tickets dispatched on the last run
    "total_dispatched": 0,      # cumulative count since process start
}


def _record_dispatch_run(dispatched_count: int) -> None:
    """Update in-memory dispatch stats after each dispatch_all_eligible() run."""
    import datetime
    _DISPATCH_STATS["last_run_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    _DISPATCH_STATS["last_dispatched_count"] = dispatched_count
    _DISPATCH_STATS["total_dispatched"] = (
        _DISPATCH_STATS.get("total_dispatched", 0) + dispatched_count
    )


def create_verify_task(session_id: str, instruction: str, priority: int = 6, **task_kwargs) -> Optional[object]:
    """Queue a kind=verify task to run pytest after a ticket agent task completes.

    Called from worker._finish_task() when ATLAS_VERIFY_TICKETS=on and the
    completed task has a ticket-* session_id.

    The verify task gets session_id="verify-{original_session_id}" so
    _try_close_ticket() ignores it; verify failure -> ticket reopen is handled
    by _reopen_ticket_from_verify_failure() in worker._finish_task().

    Args:
        session_id:  Original task's session_id (e.g. "ticket-ATL-007").
        instruction: Original task's instruction (truncated to 200 chars for the
                     verify task instruction so logs stay readable).
        priority:    Brain queue priority for the verify task (default 6 -- slightly
                     below normal=5 so the verify doesn't jump the incoming queue).
    """
    import re as _re
    if not _re.match(r"^ticket-[A-Z]{1,8}-\d+", session_id or "", _re.IGNORECASE):
        return None
    verify_session_id = f"verify-{session_id.upper()}"
    verify_instruction = f"Verify: {instruction[:200]}"
    try:
        from . import tasks as task_store
        task = task_store.create_task(
            instruction=verify_instruction,
            session_id=verify_session_id,
            kind="verify",
            priority=priority,
            **task_kwargs,
        )
        log.info("Queued verify task %s for %s", task.task_id[:16], session_id)
        return task
    except Exception as exc:
        log.warning("Failed to create verify task for %s: %s", session_id, exc)
        return None


def _reopen_ticket_from_verify_failure(session_id: str, result_text: str = "") -> bool:
    """Reopen original ticket when a verify task fails.

    Called from worker._finish_task() when a kind=verify task finishes with
    status=failed or status=blocked. Parses the verify session_id
    "verify-ticket-{ID}" to recover the ticket ID, then sets it back to todo.

    Returns True if the ticket was reopened, False otherwise.
    """
    import re as _re
    m = _re.match(r"^verify-ticket-([A-Z]{1,8}-\d+)", session_id or "", _re.IGNORECASE)
    if not m:
        return False
    ticket_id = m.group(1).upper()
    try:
        from atlas_plus.tickets.store import update_ticket_status
        update_ticket_status(ticket_id, "todo")
        log.info("Ticket %s reopened after verify failure: %s", ticket_id, result_text[:80])
        return True
    except Exception as exc:
        log.warning("Failed to reopen ticket %s after verify failure: %s", ticket_id, exc)
        return False


def get_dispatch_stats() -> dict:
    """Return a snapshot of dispatch activity and current queue depth.

    Returned dict keys:
      enabled              -- bool: is ATLAS_TICKET_DISPATCH active?
      last_run_at          -- ISO-8601 UTC str or None
      last_dispatched_count-- int: tickets dispatched on the last run
      total_dispatched     -- int: cumulative since process start
      in_flight            -- int: tickets currently in-progress (dispatched but not done)

    in_flight is counted from the ticket store; returns 0 on any error.
    """
    stats = dict(_DISPATCH_STATS)
    stats["enabled"] = is_dispatch_enabled()

    in_flight = 0
    try:
        from atlas_plus.tickets.store import list_tickets
        in_flight = len(list_tickets(status="in-progress"))
    except Exception:
        pass
    stats["in_flight"] = in_flight
    return stats
