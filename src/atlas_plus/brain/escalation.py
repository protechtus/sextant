"""Claude Code escalation layer -- auto-route failed/blocked/low-quality tasks to claude-code.

Two escalation triggers:
  1. Failure/block escalation (Phase 1): when a local agent returns failed or blocked,
     automatically create a new high-priority task routed to claude-code.
  2. Quality gate escalation (Phase 3): when improvement.record() scores a completed
     task below ATLAS_CLAUDE_QUALITY_THRESHOLD, queue a claude-code review/retry.

Environment variables:
  ATLAS_CLAUDE_ESCALATION=on          enable failure/block escalation (default: off)
  ATLAS_CLAUDE_QUALITY_ESCALATION=on  enable quality-score escalation (default: off)
  ATLAS_CLAUDE_QUALITY_THRESHOLD      minimum quality score before escalation (default: 3.5)

Escalation loop prevention:
  - Tasks already running as claude-code or opencode are never escalated.
  - Tasks whose session_id starts with 'escalated-' are never escalated (circuit breaker).
  - Non-agent kind tasks (scheduler, temporal_build, etc.) are never escalated --
    those have their own deterministic executors and should not be re-routed.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

log = logging.getLogger("atlas.brain.escalation")

# Session prefix that marks tasks already spawned by this module.
# Any task with this prefix will never trigger further escalation.
ESCALATION_SESSION_PREFIX = "escalated-"

# Agents that are themselves the escalation target -- escalating them would loop.
_TERMINAL_AGENTS = {"claude-code", "opencode", "codex"}

# Kinds of tasks that should never be escalated (deterministic executors, scheduler jobs).
# verify: failure is handled by _reopen_ticket_from_verify_failure() -- escalating would be
#   redundant and would generate an extra claude-code task for every pytest failure.
# claude_supervisor: IS the escalation target; escalating it would create an infinite loop.
_NON_AGENT_KINDS = {
    "temporal_build", "curation_report", "suggest_facts_review",
    "gaps_report", "convergence_report", "decay_report", "research_runner",
    "research_runner_api", "narrative_weekly", "memory_agent",
    "patterns_rebuild", "pytest", "noop", "fail", "context_pack",
    "research_digest", "verify", "claude_supervisor",
}


def _failure_escalation_enabled() -> bool:
    return os.environ.get("ATLAS_CLAUDE_ESCALATION", "off").lower() in ("on", "1", "true")


def _quality_escalation_enabled() -> bool:
    return os.environ.get("ATLAS_CLAUDE_QUALITY_ESCALATION", "off").lower() in ("on", "1", "true")


def _quality_threshold() -> float:
    try:
        return float(os.environ.get("ATLAS_CLAUDE_QUALITY_THRESHOLD", "3.5"))
    except ValueError:
        return 3.5


def should_escalate_failure(
    agent_name: Optional[str],
    session_id: str,
    kind: str,
) -> bool:
    """Return True if a failed/blocked task should be escalated to Claude."""
    if not _failure_escalation_enabled():
        return False
    if kind in _NON_AGENT_KINDS or kind != "agent":
        return False
    if session_id.startswith(ESCALATION_SESSION_PREFIX):
        return False
    if agent_name in _TERMINAL_AGENTS:
        return False
    return True


def should_escalate_quality(
    agent_name: Optional[str],
    session_id: str,
    kind: str,
    quality: float,
) -> bool:
    """Return True if a low-quality completed task should be escalated to Claude."""
    if not _quality_escalation_enabled():
        return False
    if kind in _NON_AGENT_KINDS or kind != "agent":
        return False
    if session_id.startswith(ESCALATION_SESSION_PREFIX):
        return False
    if agent_name in _TERMINAL_AGENTS:
        return False
    if quality >= _quality_threshold():
        return False
    return True


def build_escalation_instruction(
    original_task_id: str,
    original_instruction: str,
    reason: str,
    original_result: str = "",
) -> str:
    """Build the instruction text for an escalated task."""
    header = (
        f"[ESCALATED from {original_task_id} -- {reason}]\n\n"
        f"The original task was handled by a local model but needs your review.\n"
    )
    if original_result:
        preview = original_result[:800].rstrip()
        header += f"\nOriginal result (preview):\n---\n{preview}\n---\n\n"
    header += "Please provide a complete, high-quality response to the following:\n\n"
    return header + original_instruction


def create_escalation_task(
    original_task_id: str,
    original_session_id: str,
    instruction: str,
    reason: str,
    original_result: str = "",
) -> Optional[str]:
    """Create a claude-code escalation task. Returns new task_id or None on error."""
    try:
        from . import tasks as task_store
        escalated_instruction = build_escalation_instruction(
            original_task_id, instruction, reason, original_result
        )
        task = task_store.create_task(
            instruction=escalated_instruction,
            session_id=f"{ESCALATION_SESSION_PREFIX}{original_session_id}",
            agent="claude-code",
            kind="agent",
            priority=1,
            dedup=True,  # prevent duplicate escalations from concurrent workers
        )
        log.info(
            "Escalated %s -> %s (reason=%s, agent=claude-code)",
            original_task_id, task.task_id, reason,
        )
        return task.task_id
    except Exception as exc:
        log.warning("Escalation task creation failed for %s: %s", original_task_id, exc)
        return None
