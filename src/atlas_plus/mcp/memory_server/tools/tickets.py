"""P10-T3: Tickets MCP tool -- list and create tickets from any LLM client."""

from __future__ import annotations

import os
from typing import Optional

from atlas_plus.tickets.store import (
    create_ticket,
    get_ticket,
    list_tickets,
    update_ticket_status,
)


def tickets_list(project: Optional[str] = None, status: Optional[str] = None, tickets_root=None) -> list[dict]:
    """List tickets, optionally filtered by project and/or status."""
    tickets = list_tickets(project=project, status=status, tickets_root=tickets_root)
    return [t.as_dict() for t in tickets]


def tickets_open(project: Optional[str] = None, tickets_root=None) -> list[dict]:
    """Return only open tickets (todo + in-progress) for a project."""
    tickets = [
        t for t in list_tickets(project=project, tickets_root=tickets_root)
        if t.status in ("todo", "in-progress")
    ]
    return [t.as_dict() for t in tickets]


def tickets_create(
    title: str,
    project: str,
    assignee: str = "human",
    priority: str = "normal",
    effort: str = "M",
    body: str = "",
    requires: Optional[str] = None,
    tickets_root=None,
    interactive: bool = False,
) -> dict:
    """Create a new ticket. Returns the created ticket metadata."""
    from atlas_plus.autonomous import require_write
    try:
        require_write("tickets_create", interactive=interactive)
    except RuntimeError as e:
        return {"error": str(e)}

    # Block remote callers from queuing local agent dispatch.
    # assignee=claude-code causes the job poller to run claude CLI locally.
    # Requires ATLAS_ALLOW_REMOTE_DISPATCH=1 to override (local sessions only).
    if assignee == "claude-code":
        if not os.environ.get("ATLAS_ALLOW_REMOTE_DISPATCH", "").lower() in ("1", "true", "yes"):
            return {
                "error": (
                    "assignee='claude-code' is blocked over MCP to prevent unapproved "
                    "local agent dispatch. Set ATLAS_ALLOW_REMOTE_DISPATCH=1 on the "
                    "server to enable (local sessions only)."
                )
            }

    try:
        t = create_ticket(
            title=title,
            project=project,
            assignee=assignee,
            priority=priority,
            effort=effort,
            body=body,
            requires=requires,
            tickets_root=tickets_root,
            interactive=interactive,
        )
        return {**t.as_dict(), "path": str(t.path)}
    except ValueError as e:
        return {"error": str(e)}


def tickets_update_status(ticket_id: str, status: str, tickets_root=None, interactive: bool = False) -> dict:
    """Update a ticket's status. Returns updated ticket metadata."""
    from atlas_plus.autonomous import require_write
    try:
        require_write("tickets_status", interactive=interactive)
    except RuntimeError as e:
        return {"error": str(e)}
    try:
        t = update_ticket_status(ticket_id, status, tickets_root=tickets_root)
        return t.as_dict()
    except (FileNotFoundError, ValueError) as e:
        return {"error": str(e)}
