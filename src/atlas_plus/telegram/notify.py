"""ntfy.sh push notification helper.

Set ATLAS_NTFY_TOPIC to your chosen topic (e.g. 'atlas-myname-abc123').
Subscribe at https://ntfy.sh/<your-topic> or via the ntfy iOS/Android app.
"""

from __future__ import annotations

import logging
import os

log = logging.getLogger("atlas.telegram.notify")

NTFY_BASE = os.environ.get("ATLAS_NTFY_BASE", "https://ntfy.sh")
NTFY_TOPIC = os.environ.get("ATLAS_NTFY_TOPIC", "")


async def push(
    title: str,
    message: str,
    priority: str = "default",
    tags: list[str] | None = None,
    topic: str | None = None,
) -> bool:
    """Send a push notification via ntfy.sh. Returns True on success."""
    t = topic or NTFY_TOPIC
    if not t:
        log.debug("ATLAS_NTFY_TOPIC not set — skipping push notification")
        return False

    try:
        import httpx
        headers = {
            "Title": title,
            "Priority": priority,
        }
        if tags:
            headers["Tags"] = ",".join(tags)
        # ntfy access token (ATL-007): authenticate the publish when a reserved/ACL'd topic
        # is in use (ntfy.sh paid or self-hosted). Absent token -> legacy unauthenticated POST.
        _ntfy_token = os.environ.get("ATLAS_NTFY_TOKEN", "")
        if _ntfy_token:
            headers["Authorization"] = f"Bearer {_ntfy_token}"

        async with httpx.AsyncClient(timeout=8) as client:
            r = await client.post(f"{NTFY_BASE}/{t}", content=message, headers=headers)
            r.raise_for_status()
        return True
    except Exception as e:
        log.warning("ntfy push failed: %s", e)
        return False


async def push_ticket_created(ticket_id: str, title: str, project: str) -> None:
    """Send a low-priority ntfy notification when a new ticket is created."""
    await push(
        title=f"Ticket created: {ticket_id}",
        message=f"{title}\nProject: {project}",
        priority="low",
        tags=["memo"],
    )


async def push_job_done(ticket_id: str, title: str) -> None:
    """Send a default-priority ntfy notification when a Brain task completes successfully."""
    await push(
        title=f"Job done: {ticket_id}",
        message=title,
        priority="default",
        tags=["white_check_mark"],
    )


async def push_job_failed(ticket_id: str, error: str) -> None:
    """Send a high-priority ntfy notification when a Brain task fails (error truncated to 300 chars)."""
    await push(
        title=f"Job failed: {ticket_id}",
        message=error[:300],
        priority="high",
        tags=["x"],
    )
