"""Job poller — picks up claude-code tickets and dispatches them via Claude CLI.

Designed to run as a Windows Task Scheduler job every 60 seconds, or as a
long-running background process.

A "job" is any ticket where:
  assignee == 'claude-code'
  status   == 'todo'

On pickup:
  1. Mark ticket in-progress
  2. Run `claude --print <prompt>` with the ticket title as the prompt
  3. Mark ticket done (or failed) and push ntfy notification

Run:
  uv run atlas-job-poller                    # one-shot: process pending tickets and exit
  uv run atlas-job-poller --loop             # continuous: poll every 60s

Environment variables:
  ATLAS_JOB_POLL_INTERVAL   seconds between polls in loop mode (default: 60)
  ATLAS_CLAUDE_CLI          path to claude.exe (default: claude)
  ATLAS_JOB_TIMEOUT         max seconds per job (default: 300)
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import sys
from datetime import datetime, timezone

log = logging.getLogger("atlas.job_poller")

POLL_INTERVAL = int(os.environ.get("ATLAS_JOB_POLL_INTERVAL", "60"))
CLAUDE_CLI = os.environ.get("ATLAS_CLAUDE_CLI", "claude")
JOB_TIMEOUT = int(os.environ.get("ATLAS_JOB_TIMEOUT", "300"))
BRAIN_URL = os.environ.get("ATLAS_BRAIN_URL", "http://127.0.0.1:8767").rstrip("/")
BRAIN_API_KEY = os.environ.get("ATLAS_BRAIN_API_KEY", "")

_CREDIT_MARKERS = (
    "credit balance is too low",
    "insufficient_quota",
    "exceeded your current quota",
    "billing",
)


def _pending_jobs():
    from atlas_plus.tickets.store import list_tickets
    return [
        t for t in list_tickets()
        if t.assignee == "claude-code" and t.status == "todo"
    ]


def _sanitize(text: str, max_len: int) -> str:
    return text.replace("<", "").replace(">", "")[:max_len]


def _is_credit_error(output: str) -> bool:
    low = output.lower()
    return any(m in low for m in _CREDIT_MARKERS)


def _run_claude(ticket) -> tuple[bool, str]:
    """Run the job via claude CLI. Returns (success, output)."""
    safe_title = _sanitize(ticket.title, 200)
    safe_body = _sanitize(ticket.body or "Complete this task.", 2000)
    prompt = (
        f"<task_id>{ticket.id}</task_id>\n"
        f"<task_title>{safe_title}</task_title>\n"
        f"<task_body>{safe_body}</task_body>\n\n"
        "Perform only the task described above. Summarize what you did in 2-3 sentences."
    )
    try:
        result = subprocess.run(
            [CLAUDE_CLI, "--print", prompt],
            capture_output=True,
            text=True,
            timeout=JOB_TIMEOUT,
            encoding="utf-8",
            errors="replace",
        )
        success = result.returncode == 0
        output = (result.stdout or result.stderr or "").strip()[:500]
        return success, output
    except subprocess.TimeoutExpired:
        return False, f"Timed out after {JOB_TIMEOUT}s"
    except FileNotFoundError:
        return False, f"claude CLI not found at '{CLAUDE_CLI}'"


_BRAIN_HEADERS: dict = {}  # built lazily


def _brain_headers() -> dict:
    global _BRAIN_HEADERS
    if not _BRAIN_HEADERS and BRAIN_API_KEY:
        _BRAIN_HEADERS = {"Authorization": f"Bearer {BRAIN_API_KEY}"}
    return _BRAIN_HEADERS


async def _submit_to_brain(ticket) -> tuple[bool, str]:
    """Fallback: submit ticket to Brain when Claude credits are exhausted.
    Returns (submitted_ok, brain_task_id_or_error).
    """
    if not BRAIN_URL or not BRAIN_API_KEY:
        return False, "Brain not configured (ATLAS_BRAIN_URL / ATLAS_BRAIN_API_KEY missing)"
    import httpx
    instruction = f"Ticket {ticket.id}: {ticket.title}\n\n{ticket.body or 'Complete this task.'}"
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.post(
                f"{BRAIN_URL}/task",
                json={"instruction": instruction, "session_id": f"poller-{ticket.id}"},
                headers=_brain_headers(),
            )
        r.raise_for_status()
        task_id = r.json().get("task_id", "?")
        return True, task_id
    except Exception as exc:
        return False, str(exc)


async def _poll_brain_task(ticket_id: str, task_id: str, timeout: int = 600) -> None:
    """Poll a Brain task to completion and write the result back to the ticket.

    Runs as a fire-and-forget background coroutine. Updates ticket to done
    or blocked when Brain finishes.
    """
    import httpx
    from atlas_plus.tickets.store import update_ticket_status, list_tickets

    def _write_result(status: str, result_text: str) -> None:
        try:
            from atlas_plus.tickets.store import update_ticket_status as _upd
            _upd(ticket_id, status)
            # Append result summary to ticket body
            tickets = list_tickets()
            ticket = next((t for t in tickets if t.id == ticket_id), None)
            if ticket and hasattr(ticket, "body"):
                new_body = (ticket.body or "") + f"\n\n## Brain result ({task_id[:16]})\n{result_text[:600]}"
                # Tickets store doesn't have an update_body -- we overwrite via YAML frontmatter
                import re
                from atlas_plus import paths as atlas_paths
                ticket_file = next(
                    (p for p in atlas_paths.memory_root().glob(f"tickets/**/{ticket_id}.md")),
                    None,
                )
                if ticket_file and ticket_file.exists():
                    old = ticket_file.read_text(encoding="utf-8", errors="replace")
                    updated = old.rstrip() + f"\n\n## Brain result ({task_id[:16]})\n\n{result_text[:600]}\n"
                    ticket_file.write_text(updated, encoding="utf-8")
        except Exception as exc:
            log.warning("write-back failed for %s: %s", ticket_id, exc)

    deadline = __import__("time").monotonic() + timeout
    interval = 10  # seconds between polls
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            while __import__("time").monotonic() < deadline:
                await asyncio.sleep(interval)
                try:
                    r = await client.get(
                        f"{BRAIN_URL}/task/{task_id}",
                        headers=_brain_headers(),
                    )
                    if r.status_code == 404:
                        # The task genuinely does not exist and never will complete --
                        # leaving the ticket in-progress here made the failure invisible.
                        log.warning("Brain task %s not found -- marking ticket %s blocked", task_id, ticket_id)
                        await asyncio.to_thread(
                            _write_result, "blocked",
                            f"Brain task {task_id} not found (404); write-back impossible.",
                        )
                        return
                    data = r.json()
                    brain_status = data.get("status", "")
                    if brain_status == "done":
                        result = data.get("result", "")
                        log.info("Brain task %s done -- writing back to ticket %s", task_id, ticket_id)
                        await asyncio.to_thread(_write_result, "done", result)
                        return
                    if brain_status in ("failed", "blocked"):
                        err = data.get("error") or data.get("result", "Brain task failed.")
                        await asyncio.to_thread(_write_result, "blocked", err)
                        return
                    # still running -- keep polling
                    interval = min(interval * 1.2, 60)  # back off gently, max 60s
                except Exception as exc:
                    log.warning("Brain poll error for %s: %s", task_id, exc)
        # Timeout
        await asyncio.to_thread(_write_result, "blocked", f"Brain task {task_id} timed out after {timeout}s")
    except Exception as exc:
        log.error("_poll_brain_task crashed for %s/%s: %s", ticket_id, task_id, exc)


async def _run_job(ticket) -> tuple[bool, str, str, str]:
    """Run a job, falling back to Brain on credit exhaustion.

    Returns (success, output, agent_used, brain_task_id). brain_task_id is the Brain
    task id AS DATA when agent_used == "brain", else "". It used to be re-parsed out of
    the human-readable output string, and that parse was broken on the happy path --
    ``output.split("task ")[-1].rstrip(")")`` yielded "abc123) -- will complete async",
    so EVERY Brain-fallback watcher polled a garbage URL, hit the 404 give-up, and left
    the ticket silently stuck in-progress (found + fixed 2026-07-02, empirically
    reproduced). Never parse display strings for data.
    """
    ok, output = _run_claude(ticket)
    if not ok and _is_credit_error(output):
        log.warning("Job %s: Claude credit exhausted -- submitting to Brain", ticket.id)
        brain_ok, brain_result = await _submit_to_brain(ticket)
        if brain_ok:
            return True, f"Submitted to Brain (task {brain_result}) -- will complete async", "brain", brain_result
        log.error("Job %s: Brain fallback also failed: %s", ticket.id, brain_result)
        return False, f"Claude credit error. Brain fallback failed: {brain_result}", "failed", ""
    return ok, output, "claude-code", ""


async def process_pending() -> int:
    """Process all pending claude-code tickets. Returns count processed."""
    from atlas_plus.tickets.store import update_ticket_status
    from atlas_plus.telegram.notify import push_job_done, push_job_failed

    jobs = _pending_jobs()
    if not jobs:
        log.debug("No pending jobs.")
        return 0

    log.info("Found %d pending job(s)", len(jobs))
    processed = 0

    for ticket in jobs:
        log.info("Starting job: %s -- %s", ticket.id, ticket.title)
        update_ticket_status(ticket.id, "in-progress")

        success, output, agent_used, brain_task_id = await _run_job(ticket)

        if success:
            if agent_used == "brain":
                # Brain task is async -- stay in-progress and start write-back watcher.
                # brain_task_id arrives as DATA from _run_job (never parsed from the
                # display string -- that parse was broken and stuck every ticket).
                update_ticket_status(ticket.id, "in-progress")
                if brain_task_id and brain_task_id != "?":
                    log.info("Job %s submitted to Brain (task %s) -- launching write-back watcher",
                             ticket.id, brain_task_id)
                    asyncio.create_task(_poll_brain_task(ticket.id, brain_task_id))
                else:
                    # Brain accepted the task but returned no usable id (schema drift):
                    # the task will still run, but write-back is impossible. Say so
                    # loudly instead of polling a garbage URL.
                    log.warning("Job %s submitted to Brain but NO usable task id (%r) -- "
                                "write-back watcher NOT started; ticket needs a manual status check",
                                ticket.id, brain_task_id)
            else:
                update_ticket_status(ticket.id, "done")
                log.info("Job %s via %s: done", ticket.id, agent_used)
                await push_job_done(ticket.id, ticket.title)
        else:
            update_ticket_status(ticket.id, "blocked")
            log.error("Job failed: %s -- %s", ticket.id, output)
            await push_job_failed(ticket.id, output)

        processed += 1

    return processed


async def _loop():
    log.info("Job poller running (interval: %ds). Ctrl-C to stop.", POLL_INTERVAL)
    while True:
        try:
            await process_pending()
        except Exception as e:
            log.error("Poller error: %s", e)
        await asyncio.sleep(POLL_INTERVAL)


def main() -> None:
    """Entry point: start the Telegram job-poller daemon (polls Brain task queue and reports results via Telegram)."""
    # argparse FIRST: `--help` must be side-effect free (it used to fall
    # through and PROCESS QUEUED JOBS -- Codex artifact audit 2026-09-21).
    import argparse

    parser = argparse.ArgumentParser(
        prog="atlas-job-poller",
        description="Poll the Brain task queue once (default) or continuously (--loop).",
    )
    parser.add_argument("--loop", action="store_true", help="poll continuously")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
    )

    loop_mode = args.loop

    if loop_mode:
        asyncio.run(_loop())
    else:
        count = asyncio.run(process_pending())
        log.info("Processed %d job(s).", count)


if __name__ == "__main__":
    main()
