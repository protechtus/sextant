"""Atlas Telegram bot command handlers."""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

import httpx
from telegram import Update
from telegram.ext import ContextTypes

from .formatting import fmt_git_state, fmt_ticket, fmt_ticket_list
from .notify import push_ticket_created
from atlas_plus import paths as atlas_paths

log = logging.getLogger("atlas.telegram.commands")

PROJECTS_ROOT = atlas_paths.projects_root()
BRAIN_URL = os.environ.get("ATLAS_BRAIN_URL", "").rstrip("/")
BRAIN_API_KEY = os.environ.get("ATLAS_BRAIN_API_KEY", "")
_BRAIN_HEADERS = {"Authorization": f"Bearer {BRAIN_API_KEY}"} if BRAIN_API_KEY else {}

_SCHEDULER_JOBS = ("temporal-build", "suggest-facts", "curation")


_TG_MAX = 4096  # Telegram hard limit for message text


def _args(context: ContextTypes.DEFAULT_TYPE) -> list[str]:
    return context.args or []


async def _reply_long(
    update: Update,
    text: str,
    parse_mode: str | None = None,
) -> None:
    """Send text, splitting into multiple messages if it exceeds Telegram's 4096-char limit."""
    if not text:
        await update.message.reply_text("(empty)")
        return
    chunks: list[str] = []
    while len(text) > _TG_MAX:
        split = text.rfind("\n", 0, _TG_MAX)
        if split == -1:
            split = _TG_MAX
        chunks.append(text[:split])
        text = text[split:].lstrip("\n")
    if text:
        chunks.append(text)
    for chunk in chunks:
        await update.message.reply_text(chunk, parse_mode=parse_mode)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show available commands."""
    text = (
        "*Atlas Bot*\n\n"
        "*Agents*\n"
        "/agents -- who is online and what they are doing\n"
        "/claude <instr> -- direct to Claude Code (priority 1)\n"
        "/supervisor -- Claude Code weekly review of failed/blocked tasks\n"
        "/dispatch <agent> <task> -- send task to specific agent\n"
        "/plan <goal> -- Brain plans and dispatches subtasks\n"
        "/quick <q> -- fast answer via qwen3:1.7b\n"
        "/think <q> -- deep reasoning via qwen3:8b\n"
        "/research <topic> -- autonomous research + report\n"
        "/stop <id> -- cancel a running task\n"
        "/retry <id> -- retry a failed task\n"
        "/stats -- quick summary (tasks, pending facts, escalation)\n\n"
        "*Brain tasks*\n"
        "/task <instr> -- submit async task (auto-routed)\n"
        "/taskstatus <id> -- poll task status and result\n"
        "/watch <id> -- watch a task until done\n"
        "/tasks [status] -- list recent Brain tasks\n"
        "/run <job> -- trigger scheduled job now\n"
        "  jobs: temporal-build suggest-facts curation\n\n"
        "*Tickets*\n"
        "/ticket <project> <title> -- create ticket (human)\n"
        "/queue <project> <title> -- queue job (claude-code)\n"
        "/tickets [project] -- list open tickets\n"
        "/done <id> -- mark ticket done\n"
        "/setstatus <id> <status> -- update ticket status\n\n"
        "*Memory*\n"
        "/search <query> -- full-text search .memory/\n"
        "/remember <scope> <fact> -- write fact to memory\n"
        "/gaps -- top miss queries (last 7d)\n"
        "/next_actions [N] -- prioritized hygiene to-do (decayed/orphans/gaps)\n"
        "/suggest_facts -- dry-run fact extraction from today's session\n"
        "/suggest_tags -- scan for untagged memory files\n"
        "/curation_report -- weekly report (duplicates/stale/convergence/decisions)\n"
        "/decay_report -- queue decay_report Brain task (half-life overdue scan)\n"
        "/ticket_sync -- auto-close tickets referenced in recent git commits\n"
        "/ticket_health -- staleness report (stale in-progress/blocked)\n"
        "/convergence -- files with unresolved contradiction flags\n"
        "/decisions -- decision records stale >90 days\n"
        "/decay -- facts past their half-life threshold\n\n"
        "*Temporal graph*\n"
        "/temporal <query> -- search temporal knowledge graph\n"
        "/temporal_search <query> -- current-fact search with scores\n"
        "/temporal_history <entity> -- full fact timeline for an entity\n"
        "/temporal_stats -- episode/entity/fact counts\n"
        "/temporal_rebuild [--force] -- rebuild graph (skips if fresh)\n\n"
        "*Projects*\n"
        "/project <name> -- git state for a project\n"
        "/projects -- list known projects\n\n"
        "*System*\n"
        "/status -- one-line task + ticket snapshot\n"
        "/credit -- clear credit exhaustion\n"
        "/digest -- daily work summary via Brain\n"
        "/report -- daily task activity summary\n"
        "/doctor -- full stack health dashboard\n"
        "/session_log [--dry-run] -- write today session log from git commits\n"
        "/help -- this message"
    )
    await update.message.reply_text(text)


async def cmd_ticket(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/ticket <project> <title...> — create a ticket assigned to human."""
    args = _args(context)
    if len(args) < 2:
        await update.message.reply_text("Usage: /ticket <project> <title...>")
        return

    project = args[0]
    title = " ".join(args[1:])

    try:
        from atlas_plus.tickets.store import create_ticket
        t = create_ticket(title=title, project=project, assignee="human")
        await update.message.reply_text(
            f"*{t.id}* created\n`{t.title}`\nProject: {t.project}",
            parse_mode="Markdown",
        )
        await push_ticket_created(t.id, t.title, t.project)
    except ValueError as e:
        await update.message.reply_text(f"Error: {e}")


async def cmd_queue(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/queue <project> <title...> — queue a job for claude-code."""
    args = _args(context)
    if len(args) < 2:
        await update.message.reply_text("Usage: /queue <project> <title...>")
        return

    project = args[0]
    title = " ".join(args[1:])

    try:
        from atlas_plus.tickets.store import create_ticket
        t = create_ticket(title=title, project=project, assignee="claude-code", priority="high")
        await update.message.reply_text(
            f"*{t.id}* queued for claude-code\n`{t.title}`\nProject: {t.project}",
            parse_mode="Markdown",
        )
    except ValueError as e:
        await update.message.reply_text(f"Error: {e}")


async def cmd_tickets(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/tickets [project] — list open tickets."""
    args = _args(context)
    project = args[0] if args else None

    try:
        from atlas_plus.tickets.store import list_tickets
        tickets = [
            t for t in list_tickets(project=project)
            if t.status in ("todo", "in-progress", "blocked")
        ]
        header = f"Open tickets -- {project}" if project else "Open tickets (all projects)"
        text = fmt_ticket_list(tickets, header)
        await _reply_long(update, text, parse_mode="Markdown")
    except Exception as exc:
        await update.message.reply_text(f"Error loading tickets: {exc}")


async def cmd_done(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/done <id> — mark ticket done."""
    args = _args(context)
    if not args:
        await update.message.reply_text("Usage: /done <ticket-id>")
        return

    ticket_id = args[0].upper()
    try:
        from atlas_plus.tickets.store import update_ticket_status
        from .notify import push_job_done
        t = update_ticket_status(ticket_id, "done")
        await update.message.reply_text(f"*{t.id}* marked done.", parse_mode="Markdown")
        if t.assignee == "claude-code":
            await push_job_done(t.id, t.title)
    except FileNotFoundError:
        await update.message.reply_text(f"Ticket `{ticket_id}` not found.")
    except ValueError as e:
        await update.message.reply_text(f"Error: {e}")


async def cmd_priority(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/priority <ticket-id> <priority> -- update ticket priority.

    Valid priorities: critical, high, normal, low.
    Example: /priority ATL-007 high
    """
    args = _args(context)
    if len(args) < 2:
        await update.message.reply_text(
            "Usage: /priority <ticket-id> <priority>\n"
            "Priorities: critical | high | normal | low"
        )
        return

    ticket_id = args[0].upper()
    new_priority = args[1].lower()
    valid = {"critical", "high", "normal", "low"}
    if new_priority not in valid:
        await update.message.reply_text(
            f"Invalid priority '{new_priority}'. Use: {', '.join(sorted(valid))}"
        )
        return

    try:
        from atlas_plus.tickets.store import get_ticket, TICKETS_ROOT
        from datetime import date
        import yaml

        t = get_ticket(ticket_id)
        if t is None:
            await update.message.reply_text(f"Ticket `{ticket_id}` not found.", parse_mode="Markdown")
            return

        old_priority = t.priority
        # Through the store: versioned, atomic, unknown frontmatter keys kept
        # (round-B re-audit 2026-11-09, row 9).
        from atlas_plus.tickets.store import update_ticket_fields
        update_ticket_fields(ticket_id, {"priority": new_priority})
        await update.message.reply_text(
            f"*{t.id}* priority: {old_priority} -> *{new_priority}*",
            parse_mode="Markdown",
        )
    except Exception as exc:
        await update.message.reply_text(f"Error: {exc}")


async def cmd_status_update(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/status <id> <new-status> — update ticket status."""
    args = _args(context)
    if len(args) < 2:
        await update.message.reply_text("Usage: /status <ticket-id> <new-status>")
        return

    ticket_id = args[0].upper()
    new_status = args[1].lower()

    try:
        from atlas_plus.tickets.store import update_ticket_status
        t = update_ticket_status(ticket_id, new_status)
        await update.message.reply_text(
            f"*{t.id}* -> `{t.status}`", parse_mode="Markdown"
        )
    except (FileNotFoundError, ValueError) as e:
        await update.message.reply_text(f"Error: {e}")


async def cmd_project(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/project <name> — show git state + open tickets for a project."""
    args = _args(context)
    if not args:
        await update.message.reply_text("Usage: /project <name>")
        return

    name = args[0]
    try:
        from atlas_plus.mcp.memory_server.tools.project_state import project_state
        state = project_state(name)
        if "error" in state:
            await update.message.reply_text(f"Error: {state['error']}")
            return

        git_text = fmt_git_state(state.get("git", {}), state["project"])
        open_tickets = state.get("open_tickets", [])
        ticket_text = ""
        if open_tickets:
            ticket_text = "\n\n*Open tickets:*\n" + "\n".join(
                f"  `{t['id']}` {t['title']}" for t in open_tickets[:5]
            )
            if len(open_tickets) > 5:
                ticket_text += f"\n  _(+{len(open_tickets)-5} more)_"

        await _reply_long(update, git_text + ticket_text, parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"Error: {e}")


async def cmd_projects(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/projects — list known project directories."""
    try:
        if not PROJECTS_ROOT.exists():
            await update.message.reply_text("Projects root not found.")
            return

        projects = sorted(
            d.name for d in PROJECTS_ROOT.iterdir()
            if d.is_dir() and not d.name.startswith(".")
        )
        if not projects:
            await update.message.reply_text("No projects found.")
            return

        text = "*Projects:*\n" + "\n".join(f"  `{p}`" for p in projects)
        await _reply_long(update, text, parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"Error: {e}")


async def cmd_search(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/search <query> — full-text search over .memory/."""
    args = _args(context)
    if not args:
        await update.message.reply_text("Usage: /search <query>")
        return

    query = " ".join(args)[:500]
    try:
        from atlas_plus.mcp.memory_server.fts import search
        results = search(query, limit=5)
        if not results:
            await update.message.reply_text(f"No results for: `{query}`", parse_mode="Markdown")
            return
        lines = [f"*Search:* `{query}`\n"]
        for r in results:
            lines.append(f"*{r['path']}*\n_{r['snippet']}_\n")
        await _reply_long(update, "\n".join(lines), parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"Search error: {e}")


async def cmd_remember(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/remember <scope> <fact...> — write a fact to .memory/."""
    args = _args(context)
    if len(args) < 2:
        await update.message.reply_text(
            "Usage: /remember <scope> <fact>\n"
            "Scopes: identity, preference:topic, project:name, reference:topic"
        )
        return

    scope = args[0][:100]
    fact = " ".join(args[1:])[:2000]

    try:
        from atlas_plus.mcp.memory_server.tools.remember import memory_remember
        result = memory_remember(scope=scope, fact=fact)
        if result.get("ok"):
            await update.message.reply_text(
                f"Remembered in `{result['file']}`", parse_mode="Markdown"
            )
        else:
            await update.message.reply_text(f"Error: {result.get('error', 'unknown')}")
    except Exception as e:
        await update.message.reply_text(f"Error: {e}")


async def cmd_task(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/task <instruction...> — submit an async task to Brain."""
    args = _args(context)
    if not args:
        await update.message.reply_text("Usage: /task <instruction>")
        return
    if not BRAIN_URL:
        await update.message.reply_text("ATLAS_BRAIN_URL is not set.")
        return

    instruction = " ".join(args)[:4000]
    user_id = str(update.effective_user.id)
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.post(
                f"{BRAIN_URL}/task",
                json={"instruction": instruction, "session_id": f"tg-{user_id}"},
                headers=_BRAIN_HEADERS,
            )
        r.raise_for_status()
        data = r.json()
        await update.message.reply_text(
            f"Task queued: `{data['task_id']}`\nStatus: {data['status']}\n\n"
            f"Use /watch `{data['task_id']}` to be notified when it completes.\n"
            f"Or /taskstatus `{data['task_id']}` to poll manually.",
            parse_mode="Markdown",
        )
    except Exception as exc:
        await update.message.reply_text(f"Error submitting task: {exc}")


async def cmd_inbox(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/inbox <url or text> -- drop a link or blurb into the triage inbox (auto-summarized).

    Prefix with 'research' (or 'deep'/'dig') to also flag it for a deep-research pass:
      /inbox research https://example.com/paper
    """
    from atlas_plus.inbox.capture import build_capture_payload

    args = _args(context)
    if not args:
        await update.message.reply_text("Usage: /inbox <url or text>   (prefix 'research' for a deep dive)")
        return
    if not BRAIN_URL:
        await update.message.reply_text("ATLAS_BRAIN_URL is not set.")
        return
    payload = build_capture_payload(" ".join(args))
    if not payload:
        await update.message.reply_text("Nothing to capture.")
        return
    user_id = str(update.effective_user.id)
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.post(
                f"{BRAIN_URL}/inbox",
                json=payload,
                headers={**_BRAIN_HEADERS, "X-Atlas-Caller-Identity": user_id},
            )
        r.raise_for_status()
        data = r.json()
        flag = " (flagged for research)" if payload.get("research") else ""
        await update.message.reply_text(
            f"Saved to inbox: `{data['item_id']}`{flag}\nSummarizing now -- review and promote in Helm.",
            parse_mode="Markdown",
        )
    except Exception as exc:
        await update.message.reply_text(f"Error saving to inbox: {exc}")


async def cmd_inspire(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/inspire <prompt...> — generate divergent ideas via Brain (kind=inspire).

    Ideas are captured UNVERIFIED in the quarantine store and are never counted as
    facts until promoted by a verifier. Runs on the worker, so the Brain worker must
    be up (and loaded with the inspire executor).
    """
    args = _args(context)
    if not args:
        await update.message.reply_text("Usage: /inspire <prompt>")
        return
    if not BRAIN_URL:
        await update.message.reply_text("ATLAS_BRAIN_URL is not set.")
        return

    instruction = " ".join(args)[:4000]
    user_id = str(update.effective_user.id)
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.post(
                f"{BRAIN_URL}/task",
                json={"instruction": instruction, "session_id": f"tg-{user_id}", "kind": "inspire"},
                headers=_BRAIN_HEADERS,
            )
        r.raise_for_status()
        data = r.json()
        await update.message.reply_text(
            f"Inspiration queued: `{data['task_id']}`\n"
            f"Ideas are captured UNVERIFIED (not facts) until promoted.\n"
            f"Use /watch `{data['task_id']}` to see results.",
            parse_mode="Markdown",
        )
    except Exception as exc:
        await update.message.reply_text(f"Error submitting inspire task: {exc}")


async def cmd_taskstatus(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/taskstatus <task_id> — poll a Brain task for status and result."""
    args = _args(context)
    if not args:
        await update.message.reply_text("Usage: /taskstatus <task-id>")
        return
    if not BRAIN_URL:
        await update.message.reply_text("ATLAS_BRAIN_URL is not set.")
        return

    task_id = args[0]
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(
                f"{BRAIN_URL}/task/{task_id}",
                headers=_BRAIN_HEADERS,
            )
        if r.status_code == 404:
            await update.message.reply_text(f"Task `{task_id}` not found.", parse_mode="Markdown")
            return
        r.raise_for_status()
        data = r.json()
        status = data["status"]
        lines = [f"*{task_id}* -- {status}"]
        if data.get("started_at"):
            lines.append(f"Started: {data['started_at'][:19]}")
        if data.get("finished_at"):
            lines.append(f"Finished: {data['finished_at'][:19]}")
        if data.get("result"):
            lines.append(f"\n{data['result'][:3000]}")
        if data.get("error"):
            lines.append(f"\nError: {data['error'][:500]}")
        await _reply_long(update, "\n".join(lines), parse_mode="Markdown")
    except Exception as exc:
        await update.message.reply_text(f"Error: {exc}")


async def cmd_watch(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/watch <task_id> — poll a Brain task until done and notify via Telegram."""
    args = _args(context)
    if not args:
        await update.message.reply_text("Usage: /watch <task-id>")
        return
    if not BRAIN_URL:
        await update.message.reply_text("ATLAS_BRAIN_URL is not set.")
        return

    task_id = args[0]
    chat_id = update.effective_chat.id

    # Verify task exists and is not already terminal
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(f"{BRAIN_URL}/task/{task_id}", headers=_BRAIN_HEADERS)
        if r.status_code == 404:
            await update.message.reply_text(f"Task `{task_id}` not found.", parse_mode="Markdown")
            return
        r.raise_for_status()
        data = r.json()
        status = data.get("status", "unknown")
    except Exception as exc:
        await update.message.reply_text(f"Error checking task: {exc}")
        return

    if status in ("done", "failed", "blocked"):
        result = (data.get("result") or "")[:500]
        msg = f"Task `{task_id}` is already *{status}*."
        if result:
            msg += f"\n\n{result}"
        await update.message.reply_text(msg, parse_mode="Markdown")
        return

    await update.message.reply_text(
        f"Watching task `{task_id}` (status: {status}).\nYou will be notified when it completes.",
        parse_mode="Markdown",
    )

    async def _poll(ctx: ContextTypes.DEFAULT_TYPE) -> None:
        job = ctx.job
        tid: str = job.data["task_id"]
        cid: int = job.data["chat_id"]
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(f"{BRAIN_URL}/task/{tid}", headers=_BRAIN_HEADERS)
            if resp.status_code != 200:
                return
            d = resp.json()
            st = d.get("status", "")
            if st not in ("done", "failed", "blocked"):
                return  # still running

            result_text = (d.get("result") or "")
            preview = result_text[:600] + ("..." if len(result_text) > 600 else "")
            status_icon = {"done": "[DONE]", "failed": "[FAILED]", "blocked": "[BLOCKED]"}
            notify = f"{status_icon.get(st, st)} Task `{tid}`\n\n{preview}" if preview else f"{status_icon.get(st, st)} Task `{tid}` completed."
            await ctx.bot.send_message(chat_id=cid, text=notify[:4096], parse_mode="Markdown")
        except Exception:
            pass
        finally:
            job.schedule_removal()

    if context.job_queue is not None:
        context.job_queue.run_repeating(
            _poll,
            interval=15,
            first=15,
            data={"task_id": task_id, "chat_id": chat_id},
            name=f"watch-{task_id}",
        )
    else:
        await update.message.reply_text(
            "Job queue not available. Use /taskstatus to manually poll.",
        )


async def cmd_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/tasks [status] — list recent Brain tasks."""
    args = _args(context)
    status_filter = args[0].lower() if args else None
    if not BRAIN_URL:
        await update.message.reply_text("ATLAS_BRAIN_URL is not set.")
        return

    params = {"limit": "10"}
    if status_filter:
        params["status"] = status_filter
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(
                f"{BRAIN_URL}/tasks",
                params=params,
                headers=_BRAIN_HEADERS,
            )
        r.raise_for_status()
        data = r.json()
        tasks = data.get("tasks", [])
        if not tasks:
            label = f" ({status_filter})" if status_filter else ""
            await update.message.reply_text(f"No tasks found{label}.")
            return
        lines = [f"*Brain tasks* ({data['count']} shown)\n"]
        for t in tasks:
            icon = {"pending": "...", "running": ">>", "done": "ok", "failed": "!!", "blocked": "~~"}.get(t["status"], "?")
            agent_tag = f"[{t['agent']}] " if t.get("agent") else ""
            lines.append(f"`{t['task_id'][:16]}` [{icon}] {agent_tag}{t['instruction'][:50]}")
        await _reply_long(update, "\n".join(lines), parse_mode="Markdown")
    except Exception as exc:
        await update.message.reply_text(f"Error: {exc}")


async def cmd_run(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/run <job> — manually trigger a scheduled Brain job."""
    args = _args(context)
    if not args:
        await update.message.reply_text(
            f"Usage: /run <job>\nJobs: {', '.join(_SCHEDULER_JOBS)}"
        )
        return
    if not BRAIN_URL:
        await update.message.reply_text("ATLAS_BRAIN_URL is not set.")
        return

    job = args[0].lower()
    instructions = {
        "temporal-build": (
            "Run the temporal graph build: call temporal_build to ingest recent session logs "
            "and update the temporal knowledge graph. Report how many entities and facts were updated."
        ),
        "suggest-facts": (
            "Run suggest-facts review: search recent session transcripts for high-confidence facts "
            "not yet in .memory/. Report any candidates with confidence >= 0.95. "
            "Do not write anything unless ATLAS_AUTONOMOUS=on."
        ),
        "curation": (
            "Run memory curation: check .memory/ for duplicate entries, contradictions, and stale "
            "project records. Summarize findings concisely. Do not write anything unless ATLAS_AUTONOMOUS=on."
        ),
    }
    if job not in instructions:
        await update.message.reply_text(
            f"Unknown job: `{job}`\nValid jobs: {', '.join(_SCHEDULER_JOBS)}",
            parse_mode="Markdown",
        )
        return

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.post(
                f"{BRAIN_URL}/task",
                json={"instruction": instructions[job], "session_id": f"scheduler-{job}"},
                headers=_BRAIN_HEADERS,
            )
        r.raise_for_status()
        data = r.json()
        await update.message.reply_text(
            f"Job `{job}` dispatched.\nTask: `{data['task_id']}`\n\n"
            f"Poll with /taskstatus `{data['task_id']}`",
            parse_mode="Markdown",
        )
    except Exception as exc:
        await update.message.reply_text(f"Error dispatching job: {exc}")


# ---------------------------------------------------------------------------
# Agent commands
# ---------------------------------------------------------------------------

async def cmd_agents(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/agents -- all agents with status, model, and active task."""
    if not BRAIN_URL:
        await update.message.reply_text("ATLAS_BRAIN_URL is not set.")
        return
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(f"{BRAIN_URL}/agents", headers=_BRAIN_HEADERS)
        r.raise_for_status()
        agents = r.json().get("agents", [])
        if not agents:
            await update.message.reply_text("No agents registered.")
            return
        online = [a for a in agents if a.get("online")]
        offline = [a for a in agents if not a.get("online")]
        lines = [f"*Agents* ({len(online)}/{len(agents)} online)\n"]
        for a in online:
            task_note = f"\n  > {a['current_task'][:50]}" if a.get("current_task") else ""
            caps_str = ", ".join(a.get("capabilities", [])[:4])
            lines.append(f"OK  `{a['name']}` ({a['model']})\n  {caps_str}{task_note}")
        if offline:
            lines.append("\nOffline:")
            for a in offline:
                lines.append(f"  `{a['name']}` ({a['model']})")
        await _reply_long(update, "\n".join(lines), parse_mode="Markdown")
    except Exception as exc:
        await update.message.reply_text(f"Error: {exc}")


async def cmd_dispatch(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/dispatch <agent> <instruction> -- send task to a specific agent."""
    args = _args(context)
    if len(args) < 2:
        await update.message.reply_text("Usage: /dispatch <agent> <instruction>\nAgents: fast, brain, mistral, claude-code, codex")
        return
    if not BRAIN_URL:
        await update.message.reply_text("ATLAS_BRAIN_URL is not set.")
        return
    agent = args[0][:100]
    instruction = " ".join(args[1:])[:4000]
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.post(
                f"{BRAIN_URL}/task",
                json={"instruction": instruction, "agent": agent, "session_id": f"tg-dispatch-{agent}"},
                headers=_BRAIN_HEADERS,
            )
        r.raise_for_status()
        data = r.json()
        await update.message.reply_text(
            f"Dispatched to {agent}\nTask: {data['task_id']}\n\nPoll: /taskstatus {data['task_id']}"
        )
    except Exception as exc:
        await update.message.reply_text(f"Error: {exc}")


async def cmd_plan(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/plan <goal> -- Brain plans and dispatches subtasks."""
    args = _args(context)
    if not args:
        await update.message.reply_text("Usage: /plan <goal>")
        return
    if not BRAIN_URL:
        await update.message.reply_text("ATLAS_BRAIN_URL is not set.")
        return
    goal = " ".join(args)
    instruction = (
        f"You are a planning agent. Break the following goal into 2-5 concrete subtasks. "
        f"For each subtask, state: the task description, and which agent is best suited "
        f"(fast, brain, mistral, claude-code, codex). Format as a numbered list.\n\nGoal: {goal}"
    )
    await update.message.reply_text(f"Planning: {goal[:80]}...")
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.post(
                f"{BRAIN_URL}/task",
                json={"instruction": instruction, "agent": "brain", "session_id": "tg-planner"},
                headers=_BRAIN_HEADERS,
            )
        r.raise_for_status()
        data = r.json()
        await update.message.reply_text(
            f"Plan queued: {data['task_id']}\n\nPoll: /taskstatus {data['task_id']}"
        )
    except Exception as exc:
        await update.message.reply_text(f"Error: {exc}")


async def cmd_quick(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/quick <question> -- fast answer via qwen3:1.7b."""
    args = _args(context)
    if not args:
        await update.message.reply_text("Usage: /quick <question>")
        return
    if not BRAIN_URL:
        await update.message.reply_text("ATLAS_BRAIN_URL is not set.")
        return
    question = " ".join(args)
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.post(
                f"{BRAIN_URL}/task",
                json={"instruction": question, "agent": "fast", "session_id": "tg-quick"},
                headers=_BRAIN_HEADERS,
            )
        r.raise_for_status()
        data = r.json()
        await update.message.reply_text(f"Queued (fast): {data['task_id']}\nPoll: /taskstatus {data['task_id']}")
    except Exception as exc:
        await update.message.reply_text(f"Error: {exc}")


async def cmd_think(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/think <question> -- deep reasoning via qwen3:8b."""
    args = _args(context)
    if not args:
        await update.message.reply_text("Usage: /think <question>")
        return
    if not BRAIN_URL:
        await update.message.reply_text("ATLAS_BRAIN_URL is not set.")
        return
    question = " ".join(args)
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.post(
                f"{BRAIN_URL}/task",
                json={"instruction": question, "agent": "brain", "session_id": "tg-think"},
                headers=_BRAIN_HEADERS,
            )
        r.raise_for_status()
        data = r.json()
        await update.message.reply_text(f"Queued (brain): {data['task_id']}\nPoll: /taskstatus {data['task_id']}")
    except Exception as exc:
        await update.message.reply_text(f"Error: {exc}")


async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/stop <task_id> -- mark a task as failed/cancelled."""
    args = _args(context)
    if not args:
        await update.message.reply_text("Usage: /stop <task-id>")
        return
    task_id = args[0]
    try:
        from atlas_plus.brain import tasks as ts
        task = ts.get_task(task_id)
        if not task:
            await update.message.reply_text(f"Task {task_id} not found.")
            return
        if task.status in ("done", "failed"):
            await update.message.reply_text(f"Task {task_id} already {task.status}.")
            return
        ts.mark_failed(task_id, "Cancelled by user via Telegram.")
        await update.message.reply_text(f"Task {task_id} cancelled.")
    except Exception as exc:
        await update.message.reply_text(f"Error: {exc}")


async def cmd_retry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/retry <task_id> -- resubmit a failed task."""
    args = _args(context)
    if not args:
        await update.message.reply_text("Usage: /retry <task-id>")
        return
    if not BRAIN_URL:
        await update.message.reply_text("ATLAS_BRAIN_URL is not set.")
        return
    task_id = args[0]
    try:
        from atlas_plus.brain import tasks as ts
        task = ts.get_task(task_id)
        if not task:
            await update.message.reply_text(f"Task {task_id} not found.")
            return
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.post(
                f"{BRAIN_URL}/task",
                json={"instruction": task.instruction, "session_id": task.session_id},
                headers=_BRAIN_HEADERS,
            )
        r.raise_for_status()
        data = r.json()
        await update.message.reply_text(
            f"Retried as {data['task_id']}\nOriginal: {task_id}\n\nPoll: /taskstatus {data['task_id']}"
        )
    except Exception as exc:
        await update.message.reply_text(f"Error: {exc}")


async def cmd_doctor(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/doctor -- system health dashboard (services, tickets, Brain state)."""
    await update.message.reply_text("Checking system health...")
    try:
        from atlas_plus.doctor.cli import collect
        data = await asyncio.to_thread(collect)

        lines = ["*Atlas Health*\n"]

        # Services
        for svc in data.get("services", []):
            icon = "green" if svc["status"] == "up" else "red"
            sym = "✅" if svc["status"] == "up" else "❌"
            detail = f" {svc['detail']}" if svc.get("detail") else ""
            lines.append(f"{sym} {svc['name']}{detail}")

        # Brain model
        diag = data.get("brain_diagnostic", {})
        if diag:
            lines.append(f"\nModel: {diag.get('model','?')} | autonomous: {'on' if diag.get('autonomous') else 'off'}")

        # Tickets
        tk = data.get("tickets", {})
        if tk:
            by_s = tk.get("by_status", {})
            open_c = by_s.get("todo", 0) + by_s.get("in-progress", 0) + by_s.get("blocked", 0)
            by_a = tk.get("by_assignee", {})
            lines.append(f"\nTickets: {open_c} open | cc:{by_a.get('claude-code',0)} human:{by_a.get('human',0)}")

        # Nightly jobs
        nightly = data.get("nightly", {})
        if nightly:
            lines.append("\nNightly:")
            for job, info in nightly.items():
                sym = "✅" if info.get("status") == "done" else "❌"
                lines.append(f"  {sym} {job}: {info.get('last','never')[:16]}")

        # Memory gaps
        gaps = data.get("memory_gaps", {})
        if gaps and gaps.get("unique_miss_queries", 0) > 0:
            lines.append(f"\nMemory gaps: {gaps['unique_miss_queries']} miss queries (7d)")
            for g in gaps.get("top", [])[:2]:
                lines.append(f"  {g['miss_rate_pct']}% {g['query'][:30]}")

        # Pending facts
        pf = data.get("pending_facts", {})
        pf_count = pf.get("count", 0)
        if pf_count > 0:
            oldest = (pf.get("oldest") or "")[:10]
            lines.append(f"\n⏳ {pf_count} pending fact(s) await review (oldest: {oldest})")

        # SLA breaches
        sla = data.get("sla_breaches", {})
        sla_count = sla.get("count", 0)
        if sla_count > 0:
            lines.append(f"\n🔴 {sla_count} task(s) past SLA threshold")

        # Disk
        disk = data.get("disk", {})
        if disk:
            lines.append(f"\nDisk: {disk.get('free_gb')}GB free ({disk.get('pct_used')}% used)")

        await update.message.reply_text("\n".join(lines))
    except Exception as exc:
        await update.message.reply_text(f"Doctor error: {exc}")


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/status -- one-line snapshot: running/pending/done today/failed today + open tickets."""
    try:
        from atlas_plus.brain import tasks as ts
        from atlas_plus.tickets.store import list_tickets
        from datetime import date

        today = date.today().isoformat()

        def _gather():
            from datetime import timezone, timedelta
            cutoff = (date.fromisoformat(today) - timedelta(0)).isoformat()  # used below
            cutoff30 = (__import__("datetime").datetime.now(__import__("datetime").timezone.utc) - timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%S")
            all_tasks = ts.list_tasks(limit=200)
            running  = sum(1 for t in all_tasks if t.status == "running")
            pending  = sum(1 for t in all_tasks if t.status == "pending")
            stale    = sum(1 for t in all_tasks if t.status == "running" and t.heartbeat_at and t.heartbeat_at < cutoff30)
            done_t   = sum(1 for t in all_tasks if t.status == "done"   and (t.finished_at or "").startswith(today))
            failed_t = sum(1 for t in all_tasks if t.status == "failed" and (t.finished_at or "").startswith(today))
            open_tk  = sum(1 for t in list_tickets() if t.status in ("todo", "in-progress", "blocked"))
            return running, pending, stale, done_t, failed_t, open_tk

        running, pending, stale, done_t, failed_t, open_tk = await asyncio.to_thread(_gather)
        stale_line = f"Worker: {stale} STALE RUNNING (heartbeat >30m old)" if stale else ""
        lines = [
            f"*Atlas Status* ({today})\n",
            f"Tasks: {running} running | {pending} pending",
            f"Today: {done_t} done | {failed_t} failed",
            f"Tickets: {open_tk} open",
        ]
        if stale_line:
            lines.append(stale_line)
        await _reply_long(update, "\n".join(lines), parse_mode="Markdown")
    except Exception as exc:
        await update.message.reply_text(f"Error: {exc}")


async def cmd_gaps(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/gaps -- top memory miss queries (searches that found nothing)."""
    try:
        from atlas_plus.mcp.memory_server.tools.search_log import top_miss_queries
        gaps = await asyncio.to_thread(top_miss_queries, 168.0, 12)
        if not gaps:
            await update.message.reply_text("No miss data yet -- run some memory searches first.")
            return
        lines = ["*Memory Gaps* (last 7d)\n"]
        for g in gaps[:10]:
            pct = round(g["miss_count"] / g["total"] * 100) if g["total"] else 0
            sym = "!!" if pct >= 80 else ("!" if pct >= 50 else "~")
            lines.append(f"{sym} `{g['query'][:40]}`  {g['miss_count']}/{g['total']} ({pct}%)")
        await _reply_long(update, "\n".join(lines), parse_mode="Markdown")
    except Exception as exc:
        await update.message.reply_text(f"Error: {exc}")


async def cmd_temporal(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/temporal <query> -- search the temporal knowledge graph."""
    args = _args(context)
    if not args:
        await update.message.reply_text(
            "Usage: /temporal <query>\n"
            "Example: /temporal Atlas MCP tunnel\n"
            "Returns current facts matching the query."
        )
        return
    query = " ".join(args)
    try:
        def _do_query():
            from atlas_plus.temporal_graph.file_provider import FileTemporalProvider
            p = FileTemporalProvider()
            if p.stats()["facts"] == 0:
                return None
            return p.search_current(query, limit=8)

        results = await asyncio.to_thread(_do_query)
        if results is None:
            await update.message.reply_text(
                "Temporal graph is empty.\n"
                "Run: `atlas-temporal build --source all`",
                parse_mode="Markdown",
            )
            return
        if not results:
            await update.message.reply_text(f"No temporal facts for: `{query}`", parse_mode="Markdown")
            return
        lines = [f"*Temporal:* `{query}`\n"]
        for r in results:
            f = r.fact
            e = r.entity
            subj = e.name if e else f.subject_id
            lines.append(f"`{subj}` {f.predicate} {f.object_value}")
        await _reply_long(update, "\n".join(lines), parse_mode="Markdown")
    except Exception as exc:
        await update.message.reply_text(f"Error: {exc}")


async def cmd_decisions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/decisions -- surface decision records that may need revisiting."""
    try:
        def _do_check():
            from atlas_plus.curation.cli import MEMORY_ROOT
            import yaml as _yaml
            from datetime import date
            decisions_dir = MEMORY_ROOT / "decisions"
            if not decisions_dir.exists():
                return [], 0
            today = date.today()
            results = []
            for path in sorted(decisions_dir.glob("*.md")):
                if path.name.startswith("_"):
                    continue
                try:
                    text = path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                if not text.startswith("---"):
                    continue
                end = text.find("\n---", 3)
                if end == -1:
                    continue
                try:
                    fm = _yaml.safe_load(text[3:end]) or {}
                except Exception:
                    continue
                title = str(fm.get("title", path.stem))
                made_str = str(fm.get("made", ""))[:10]
                lr_str = str(fm.get("last_reviewed", ""))[:10]
                age_days = None
                if made_str:
                    try:
                        age_days = (today - date.fromisoformat(made_str)).days
                    except ValueError:
                        pass
                lr_days = None
                if lr_str:
                    try:
                        lr_days = (today - date.fromisoformat(lr_str)).days
                    except ValueError:
                        pass
                is_stale = (lr_days is None and age_days is not None and age_days > 90) or \
                           (lr_days is not None and lr_days > 90)
                results.append((title, made_str, lr_str or "never", is_stale))
            stale_count = sum(1 for _, _, _, s in results if s)
            return results, stale_count

        records, stale = await asyncio.to_thread(_do_check)
        if not records:
            await update.message.reply_text("No decision records found in .memory/decisions/.")
            return
        lines = [f"*Decision Records* ({len(records)} total, {stale} stale)\n"]
        for title, made, reviewed, is_stale in records[:10]:
            flag = " [STALE]" if is_stale else ""
            lines.append(f"  `{title[:40]}`{flag}\n    made: {made}  reviewed: {reviewed}")
        if len(records) > 10:
            lines.append(f"  (+{len(records) - 10} more)")
        lines.append("\nRun `atlas-review-memory decisions` for full details.")
        await _reply_long(update, "\n".join(lines), parse_mode="Markdown")
    except Exception as exc:
        await update.message.reply_text(f"Error: {exc}")


async def cmd_convergence(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/convergence -- show memory files with unresolved contradiction flags."""
    try:
        def _do_check():
            from atlas_plus.doctor.cli import check_convergence
            return check_convergence()

        result = await asyncio.to_thread(_do_check)
        flagged = result.get("flagged", 0)
        if flagged == 0:
            await update.message.reply_text("No memory convergence issues found.")
            return
        paths = result.get("paths", [])
        lines = [f"*Convergence: {flagged} file(s) with contradiction flags*\n"]
        for p in paths[:5]:
            lines.append(f"  `{p}`")
        if flagged > 5:
            lines.append(f"  (+{flagged - 5} more)")
        lines.append("\nRun: `atlas-review-memory convergence --write` for full report.")
        await _reply_long(update, "\n".join(lines), parse_mode="Markdown")
    except Exception as exc:
        await update.message.reply_text(f"Error: {exc}")


async def cmd_supervisor(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/supervisor -- trigger the weekly Claude supervisor review immediately."""
    if not BRAIN_URL:
        await update.message.reply_text("ATLAS_BRAIN_URL is not set.")
        return
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.post(
                f"{BRAIN_URL}/task",
                json={
                    "instruction": (
                        "Run the Claude supervisor pass: review all failed and blocked Brain tasks "
                        "from the last 7 days, identify failure patterns, root causes, and recommended "
                        "actions. Check pending fact count. Write supervisor report."
                    ),
                    "kind": "claude_supervisor",
                    "priority": 1,
                    "session_id": "tg-claude-supervisor",
                },
                headers=_BRAIN_HEADERS,
            )
        r.raise_for_status()
        data = r.json()
        await update.message.reply_text(
            f"*Claude Supervisor* review queued\n"
            f"ID: `{data['task_id']}`\n"
            f"Claude will review the last 7 days of failed/blocked tasks and produce a report.\n\n"
            f"Poll: /taskstatus `{data['task_id']}`\n"
            f"Report: `.atlas/reports/supervisor-*.md`",
            parse_mode="Markdown",
        )
    except Exception as exc:
        await update.message.reply_text(f"Error: {exc}")


async def cmd_claude(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/claude <instruction> -- send task directly to Claude Code (highest priority, bypasses routing)."""
    args = _args(context)
    if not args:
        await update.message.reply_text(
            "Usage: /claude <instruction>\n"
            "Routes directly to Claude Code at priority 1, bypassing Qwen3.\n"
            "Use for complex coding, architecture, multi-file work, or anything\n"
            "where you need the full Claude context window."
        )
        return
    if not BRAIN_URL:
        await update.message.reply_text("ATLAS_BRAIN_URL is not set.")
        return
    instruction = " ".join(args)
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.post(
                f"{BRAIN_URL}/task",
                json={
                    "instruction": instruction,
                    "agent": "claude-code",
                    "priority": 1,
                    "session_id": "tg-claude-direct",
                },
                headers=_BRAIN_HEADERS,
            )
        r.raise_for_status()
        data = r.json()
        await update.message.reply_text(
            f"*Claude Code* task queued at priority 1\n"
            f"ID: `{data['task_id']}`\n"
            f"Instruction: {instruction[:80]}{'...' if len(instruction) > 80 else ''}\n\n"
            f"Poll: /taskstatus `{data['task_id']}`",
            parse_mode="Markdown",
        )
    except Exception as exc:
        await update.message.reply_text(f"Error: {exc}")


async def cmd_rederive(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/rederive -- show memory files that overlap heavily with project docs."""
    try:
        def _do_check():
            from atlas_plus.curation.rederive import find_rederivable
            from atlas_plus.mcp.memory_server.config import MEMORY_ROOT as _MEM
            ws_root = _MEM.parent
            return find_rederivable(threshold=0.40, workspace_root=ws_root)

        candidates = await asyncio.to_thread(_do_check)
        if not candidates:
            await update.message.reply_text(
                "No re-derivable facts found -- memory is clean (threshold 0.40)."
            )
            return
        lines = [f"*Re-derivable Facts* ({len(candidates)} above threshold 0.40)\n"]
        for c in candidates[:8]:
            lines.append(
                f"  `{c.path[:40]}`\n"
                f"    sim={c.similarity:.2f}  matches: `{c.matched_doc[:35]}`"
            )
        if len(candidates) > 8:
            lines.append(f"  (+{len(candidates) - 8} more)")
        lines.append("\nRun `atlas-review-memory rederive` for full report.")
        await _reply_long(update, "\n".join(lines), parse_mode="Markdown")
    except Exception as exc:
        await update.message.reply_text(f"Error: {exc}")


async def cmd_suggest_tags(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/suggest_tags -- show untagged .memory/ files with suggested tags (dry-run)."""
    try:
        def _do_scan():
            from atlas_plus.mcp.memory_server.config import MEMORY_ROOT
            from atlas_plus.curation.tag_suggest import suggest_tags_for_file
            candidates = []
            for md_file in sorted(MEMORY_ROOT.rglob("*.md")):
                rel_parts = md_file.relative_to(MEMORY_ROOT).parts
                if rel_parts and rel_parts[0] in {"archive", "_sync", "versions", "sessions"}:
                    continue
                tags = suggest_tags_for_file(md_file, memory_root=MEMORY_ROOT, max_tags=4)
                if tags:
                    candidates.append((str(md_file.relative_to(MEMORY_ROOT)).replace("\\", "/"), tags))
            return candidates

        candidates = await asyncio.to_thread(_do_scan)
        if not candidates:
            await update.message.reply_text("All memory files are tagged.")
            return
        lines = [f"*Untagged Files* ({len(candidates)} found -- top 10)\n"]
        for path, tags in candidates[:10]:
            tags_str = ", ".join(f"`{t}`" for t in tags)
            lines.append(f"  `{path[:45]}`\n    -> {tags_str}")
        if len(candidates) > 10:
            lines.append(f"\n(+{len(candidates) - 10} more)")
        lines.append("\nRun `/suggest-tags --write` in Brain CLI to apply tags.")
        await _reply_long(update, "\n".join(lines), parse_mode="Markdown")
    except Exception as exc:
        await update.message.reply_text(f"Error: {exc}")


async def cmd_suggest_facts(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/suggest_facts -- dry-run: extract fact candidates from today's session transcript."""
    await update.message.reply_text("Scanning today's session transcript for fact candidates...")
    try:
        def _do_scan():
            from atlas_plus.curation.suggest_facts import suggest_facts
            return suggest_facts(since="today", source="claude-code", dry_run=True, auto=False)

        stats = await asyncio.to_thread(_do_scan)
        candidates = stats.get("candidates", 0)
        approved = stats.get("approved", 0)
        if candidates == 0:
            await update.message.reply_text(
                "No fact candidates found in today's session transcript."
            )
            return
        msg = (
            f"*Suggest-Facts (dry-run)*\n\n"
            f"Candidates found: `{candidates}`\n"
            f"High-confidence (>= 0.85): `{approved}`\n\n"
            f"Run `/suggest-facts --auto` in Brain CLI to write approved facts."
        )
        await update.message.reply_text(msg, parse_mode="Markdown")
    except RuntimeError as exc:
        await update.message.reply_text(f"Error: {exc}")
    except Exception as exc:
        await update.message.reply_text(f"Error: {exc}")


async def cmd_decay(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/decay -- show memory files past their half-life review threshold."""
    try:
        def _do_check():
            from atlas_plus.curation.decay import find_decayed_facts
            return find_decayed_facts(threshold_days=0)

        candidates = await asyncio.to_thread(_do_check)
        if not candidates:
            await update.message.reply_text(
                "No memory files past their half-life threshold.\n"
                "Add `half_life: N` to frontmatter to enable decay tracking."
            )
            return
        lines = [f"*Memory Decay* ({len(candidates)} file(s) overdue)\n"]
        for c in candidates[:8]:
            lines.append(
                f"  `{c.path[:40]}`\n"
                f"    half-life={c.half_life_days}d  overdue={c.days_overdue}d  updated={c.updated}"
            )
        if len(candidates) > 8:
            lines.append(f"  (+{len(candidates) - 8} more)")
        lines.append("\nRun `atlas-review-memory decay` for full report.")
        await _reply_long(update, "\n".join(lines), parse_mode="Markdown")
    except Exception as exc:
        await update.message.reply_text(f"Error: {exc}")


async def cmd_research(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/research <topic> -- autonomous investigation: searches memory, synthesizes, writes report."""
    args = _args(context)
    if not args:
        await update.message.reply_text(
            "Usage: /research <topic>\n"
            "Example: /research SIP registration flow\n"
            "Report written to `.atlas/reports/research-*.md`"
        )
        return
    if not BRAIN_URL:
        await update.message.reply_text("ATLAS_BRAIN_URL is not set.")
        return
    topic = " ".join(args)
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.post(
                f"{BRAIN_URL}/task",
                json={
                    "instruction": topic,
                    "agent": "research",
                    "session_id": "tg-research",
                },
                headers=_BRAIN_HEADERS,
            )
        r.raise_for_status()
        data = r.json()
        await update.message.reply_text(
            f"Research queued: `{data['task_id']}`\n"
            f"Topic: {topic[:80]}\n\n"
            f"Poll: /taskstatus `{data['task_id']}`\n"
            f"Report -> `.atlas/reports/research-*.md`",
            parse_mode="Markdown",
        )
    except Exception as exc:
        await update.message.reply_text(f"Error: {exc}")


async def cmd_research_gaps(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/research_gaps [N] -- top N memory miss queries with ticket and report coverage."""
    args = _args(context)
    try:
        limit = max(1, min(20, int(args[0]))) if args else 8
    except (ValueError, IndexError):
        limit = 8

    lines = ["*Research Gaps* (last 7d)\n"]
    try:
        from atlas_plus.mcp.memory_server.tools.search_log import top_miss_queries
        misses = top_miss_queries(since_hours=168.0, limit=limit)
    except Exception as exc:
        await update.message.reply_text(f"Error loading miss log: {exc}")
        return

    if not misses:
        await update.message.reply_text("No memory miss queries in the last 7 days.")
        return

    # Build existing gap ticket set
    try:
        from atlas_plus.tickets.store import list_tickets
        gap_titles = {
            t.title.lower()
            for t in list_tickets()
            if t.status not in ("done", "archived") and "research gap" in t.title.lower()
        }
    except Exception:
        gap_titles = set()

    for r in misses:
        query = r["query"]
        miss_count = r.get("miss_count", 0)
        total = r.get("total", 1)
        rate = int(miss_count / total * 100)
        has_ticket = "T" if f"research gap: {query.lower()}" in gap_titles else "-"
        lines.append(f"`{has_ticket}` {query[:35]} ({miss_count}/{total} {rate}%)")

    uncovered = sum(1 for r in misses if f"research gap: {r['query'].lower()}" not in gap_titles)
    if uncovered:
        lines.append(f"\n_{uncovered} gap(s) without ticket. Run `gaps --autoticket` to file._")

    await _reply_long(update, "\n".join(lines), parse_mode="Markdown")


async def cmd_decay_report(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/decay_report -- queue a decay_report Brain task from mobile.

    Scans .memory/ for files past their half_life: threshold and writes
    a decay-report.md artifact to .atlas/artifacts/. Monitor with /task <id>.
    """
    try:
        from atlas_plus.brain.tasks import create_task
        task = await asyncio.to_thread(
            lambda: create_task(
                instruction="Run memory half-life decay report",
                kind="decay_report",
                session_id="tg-decay-report",
            )
        )
        await update.message.reply_text(
            f"Decay report queued: `{task.task_id}`\n"
            f"Monitor: /task `{task.task_id}`\n"
            f"Artifact -> `.atlas/artifacts/decay-report.md`",
            parse_mode="Markdown",
        )
    except Exception as exc:
        await update.message.reply_text(f"Error: {exc}")


async def cmd_ticket_sync(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/ticket_sync -- auto-close tickets referenced in recent git commits.

    Queues a ticket_sync Brain task that scans git log for ticket IDs
    and marks referenced tickets done. Uses ATLAS_TICKET_SYNC_DAYS (default 30)
    and ATLAS_TICKET_SYNC_REPOS env vars. Monitor with /task <id>.
    """
    try:
        from atlas_plus.brain.tasks import create_task
        task = await asyncio.to_thread(
            lambda: create_task(
                instruction="Sync ticket statuses from git commit history",
                kind="ticket_sync",
                session_id="tg-ticket-sync",
            )
        )
        await update.message.reply_text(
            f"Ticket sync queued: `{task.task_id}`\n"
            f"Monitor: /task `{task.task_id}`\n"
            f"Scans recent commits and closes referenced tickets.",
            parse_mode="Markdown",
        )
    except Exception as exc:
        await update.message.reply_text(f"Error queuing ticket sync: {exc}")


async def cmd_ticket_health(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/ticket_health -- staleness report for all open tickets.

    Flags:
    - in-progress tickets not updated in > 7 days
    - blocked tickets not updated in > 3 days
    Shows age in days, assignee, and suggested remediation commands.
    """
    from datetime import date

    try:
        from atlas_plus.tickets.store import list_tickets
        tickets = [t for t in list_tickets() if t.status in ("todo", "in-progress", "blocked")]
    except Exception as exc:
        await update.message.reply_text(f"Error loading tickets: {exc}")
        return

    if not tickets:
        await update.message.reply_text("No open tickets found.")
        return

    today = date.today()

    def _age(t) -> int:
        try:
            return (today - date.fromisoformat(t.updated)).days
        except Exception:
            return 0

    ages = {t.id: _age(t) for t in tickets}
    stale_ip = sorted(
        [(t, ages[t.id]) for t in tickets if t.status == "in-progress" and ages[t.id] > 7],
        key=lambda x: x[1], reverse=True,
    )
    stale_bl = sorted(
        [(t, ages[t.id]) for t in tickets if t.status == "blocked" and ages[t.id] > 3],
        key=lambda x: x[1], reverse=True,
    )

    lines = ["*Ticket Health Report*", ""]

    if stale_ip:
        lines.append(f"*Stale in-progress* (>7d) -- {len(stale_ip)} ticket(s):")
        for t, d in stale_ip:
            lines.append(f"  `{t.id}` {t.title[:50]} [{d}d, {t.assignee}]")
        lines.append("")

    if stale_bl:
        lines.append(f"*Needs attention* blocked (>3d) -- {len(stale_bl)} ticket(s):")
        for t, d in stale_bl:
            lines.append(f"  `{t.id}` {t.title[:50]} [{d}d, {t.assignee}]")
        lines.append("")

    if not stale_ip and not stale_bl:
        lines.append("All tickets are healthy (no stale in-progress or blocked).")
    else:
        lines.append("Remediation: `/done <id>` or `/setstatus <id> <status>`")

    n_ip = sum(1 for t in tickets if t.status == "in-progress")
    n_bl = sum(1 for t in tickets if t.status == "blocked")
    n_td = sum(1 for t in tickets if t.status == "todo")
    lines += [
        "",
        f"Total open: {len(tickets)} ({n_ip} in-progress, {n_bl} blocked, {n_td} todo)",
    ]

    await _reply_long(update, "\n".join(lines), parse_mode="Markdown")


async def cmd_gap_report(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/gap_report <topic> -- queue a research_gap Brain task for a topic from mobile.

    Creates kind=research_gap task with instruction 'Research gap: <topic>'.
    The task runs run_research() via the deterministic executor and writes a
    report to .atlas/reports/. Monitor with /task <task_id>.
    """
    args = _args(context)
    if not args:
        await update.message.reply_text(
            "Usage: /gap\\_report <topic>\n"
            "Example: /gap\\_report SIP REGISTER flow\n"
            "Queues a research\\_gap Brain task to produce a `.atlas/reports/` entry.",
            parse_mode="Markdown",
        )
        return

    topic = " ".join(args).strip()
    instruction = f"Research gap: {topic}"

    try:
        from atlas_plus.brain.tasks import create_task
        task = create_task(
            instruction=instruction,
            kind="research_gap",
            session_id="tg-gap-report",
        )
        await update.message.reply_text(
            f"Research gap queued: `{task.task_id}`\n"
            f"Topic: {topic[:80]}\n\n"
            f"Monitor: /task `{task.task_id}`\n"
            f"Report -> `.atlas/reports/research-*.md`",
            parse_mode="Markdown",
        )
    except Exception as exc:
        await update.message.reply_text(f"Error: {exc}")


async def cmd_loop_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/loop_status -- show automation loop health (dispatch stats + ticket loop counts)."""
    lines = ["*Automation Loop Status*\n"]

    # Dispatch stats
    try:
        from atlas_plus.brain.dispatcher import get_dispatch_stats
        stats = get_dispatch_stats()
        enabled = stats.get("enabled", False)
        in_flight = stats.get("in_flight", 0)
        total_disp = stats.get("total_dispatched", 0)
        last_run = stats.get("last_run_at")
        status_str = "on" if enabled else "off (ATLAS_TICKET_DISPATCH=on to enable)"
        lines.append(f"Dispatcher: {status_str}")
        if enabled:
            lines.append(f"In-flight: {in_flight}  Total dispatched (session): {total_disp}")
        if last_run:
            import datetime
            try:
                run_dt = datetime.datetime.fromisoformat(last_run)
                age_min = int(
                    (datetime.datetime.now(datetime.timezone.utc) - run_dt).total_seconds() / 60
                )
                lines.append(f"Last run: {age_min}m ago")
            except Exception:
                pass
    except Exception as exc:
        lines.append(f"Dispatch stats unavailable: {exc}")

    # Ticket loop health
    try:
        from atlas_plus.tickets.store import list_tickets
        tickets = list_tickets()
        todo = sum(1 for t in tickets if t.status == "todo")
        in_prog = sum(1 for t in tickets if t.status == "in-progress")
        blocked = sum(1 for t in tickets if t.status == "blocked")
        reopened = [t for t in tickets if getattr(t, "reopen_count", 0) > 0]
        lines.append(f"\nTickets: {todo} todo | {in_prog} in-progress | {blocked} blocked")
        if reopened:
            high = sorted(reopened, key=lambda t: getattr(t, "reopen_count", 0), reverse=True)
            lines.append(f"Reopened tickets ({len(reopened)}):")
            for t in high[:3]:
                lines.append(f"  `{t.id}` x{t.reopen_count} -- {t.title[:40]}")
    except Exception as exc:
        lines.append(f"Ticket info unavailable: {exc}")

    # Exhausted tasks (no retries left)
    try:
        from atlas_plus.brain.tasks import list_exhausted_tasks
        exhausted = list_exhausted_tasks(limit=5)
        if exhausted:
            lines.append(f"\n*Exhausted tasks* ({len(exhausted)} need attention):")
            for t in exhausted[:3]:
                lines.append(f"  `{t.task_id[:16]}` [{t.status}] {t.agent or 'auto'} -- {(t.instruction or '')[:35]}")
            lines.append("Use /exhausted for full list or /retry <id> to requeue.")
    except Exception:
        pass

    await _reply_long(update, "\n".join(lines), parse_mode="Markdown")


async def cmd_dispatch_tickets(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/dispatch_tickets -- immediately dispatch eligible todo tickets to Brain tasks."""
    try:
        from atlas_plus.brain.dispatcher import dispatch_all_eligible
        import asyncio as _asyncio
        dispatched = await _asyncio.get_event_loop().run_in_executor(None, dispatch_all_eligible)
    except Exception as exc:
        await update.message.reply_text(f"Dispatch error: {exc}")
        return

    if not dispatched:
        await update.message.reply_text(
            "No eligible tickets found.\n"
            "Eligible: status=todo, assignee=claude-code|local|any|codex"
        )
        return

    lines = [f"*Dispatched {len(dispatched)} ticket(s)*\n"]
    for d in dispatched:
        lines.append(f"`{d['ticket_id']}` -> task `{d['task_id'][:16]}` (agent: {d['agent']})")
    await _reply_long(update, "\n".join(lines), parse_mode="Markdown")


async def cmd_research_index(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/research_index [N] -- list last N research reports with quality scores (default 10)."""
    import os
    import re
    import yaml as _yaml
    from pathlib import Path

    args = _args(context)
    try:
        limit = max(1, min(30, int(args[0]))) if args else 10
    except (ValueError, IndexError):
        limit = 10

    atlas_dir = atlas_paths.state_dir()
    reports_dir = atlas_dir / "reports"
    if not reports_dir.exists():
        await update.message.reply_text("No .atlas/reports/ directory found.")
        return

    entries = []
    for md_file in sorted(reports_dir.rglob("*.md"), reverse=True):
        if md_file.name.startswith("_"):
            continue
        try:
            raw = md_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if not raw.startswith("---"):
            continue
        end = raw.find("\n---", 3)
        if end == -1:
            continue
        try:
            fm = _yaml.safe_load(raw[3:end]) or {}
        except Exception:
            continue
        title = str(fm.get("title", md_file.stem)).replace("Research: ", "").replace('"', "").strip()[:45]
        date_str = str(fm.get("date") or fm.get("report_date") or "")[:10]
        qs = fm.get("quality_score")
        refs = fm.get("references") or []
        abstract = str(fm.get("abstract", "")).strip()[:120]
        entries.append((date_str, title, qs, len(refs) if isinstance(refs, list) else 0, abstract))
        if len(entries) >= limit:
            break

    if not entries:
        await update.message.reply_text("No research reports found.")
        return

    lines = [f"*Research Index* (newest {len(entries)})\n"]
    for date_str, title, qs, cites, abstract in entries:
        if qs is None:
            score_str = "--"
        else:
            score_str = f"{qs:.2f}"
        cite_str = f" | {cites} cite(s)" if cites > 0 else ""
        lines.append(f"{date_str or '----'} `{score_str}` {title}{cite_str}")
        if abstract:
            lines.append(f"  _{abstract}_")

    await _reply_long(update, "\n".join(lines), parse_mode="Markdown")


async def cmd_research_force(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/research_force <topic> -- like /research but always creates a new report, ignoring dedup."""
    args = _args(context)
    if not args:
        await update.message.reply_text(
            "Usage: /research_force <topic>\n"
            "Creates a new research report even if a similar one was written recently.\n"
            "Example: /research_force SIP registration flow"
        )
        return
    if not BRAIN_URL:
        await update.message.reply_text("ATLAS_BRAIN_URL is not set.")
        return
    topic = " ".join(args)
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.post(
                f"{BRAIN_URL}/task",
                json={
                    "instruction": f"--force {topic}",
                    "agent": "research",
                    "session_id": "tg-research",
                },
                headers=_BRAIN_HEADERS,
            )
        r.raise_for_status()
        data = r.json()
        await update.message.reply_text(
            f"Research queued (force): `{data['task_id']}`\n"
            f"Topic: {topic[:80]}\n\n"
            f"Poll: /taskstatus `{data['task_id']}`\n"
            f"Report -> `.atlas/reports/research-*.md`",
            parse_mode="Markdown",
        )
    except Exception as exc:
        await update.message.reply_text(f"Error: {exc}")


async def cmd_credit_reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/credit -- clear credit exhaustion state on Brain so CLI agents can be dispatched again."""
    if not BRAIN_URL:
        await update.message.reply_text("ATLAS_BRAIN_URL is not set.")
        return
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.post(f"{BRAIN_URL}/credit/reset", headers=_BRAIN_HEADERS)
        r.raise_for_status()
        data = r.json()
        cleared = data.get("cleared", [])
        if cleared:
            await update.message.reply_text(
                f"Credit exhaustion cleared.\nReleased agents: {', '.join(cleared)}"
            )
        else:
            await update.message.reply_text("No agents were credit-exhausted.")
    except Exception as exc:
        await update.message.reply_text(f"Error: {exc}")


async def cmd_digest(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/digest -- ask Brain to generate a daily summary of work done today."""
    if not BRAIN_URL:
        await update.message.reply_text("ATLAS_BRAIN_URL is not set.")
        return
    from datetime import date
    today = date.today().isoformat()
    instruction = (
        f"Generate a concise daily digest for {today}. "
        f"Include: 1) Brain tasks completed today (from the task store), "
        f"2) any tickets closed today, "
        f"3) any new memory facts written to .memory/ today, "
        f"4) notable failures or blocks. "
        f"Keep it to 10 lines max. Be specific -- list task IDs and ticket IDs where known. "
        f"Skip empty sections."
    )
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.post(
                f"{BRAIN_URL}/task",
                json={"instruction": instruction, "agent": "brain", "session_id": "tg-digest"},
                headers=_BRAIN_HEADERS,
            )
        r.raise_for_status()
        data = r.json()
        await update.message.reply_text(
            f"Digest queued: `{data['task_id']}`\n"
            f"Poll: /taskstatus `{data['task_id']}`",
            parse_mode="Markdown",
        )
    except Exception as exc:
        await update.message.reply_text(f"Error: {exc}")


async def cmd_export(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/export [json] -- send Brain task list as CSV (default) or JSON file attachment."""
    args = _args(context)
    fmt = "json" if args and args[0].lower() == "json" else "csv"
    if not BRAIN_URL:
        await update.message.reply_text("ATLAS_BRAIN_URL is not set.")
        return
    await update.message.reply_text(f"Fetching task export ({fmt})...")
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            r = await client.get(
                f"{BRAIN_URL}/tasks/export",
                params={"fmt": fmt, "limit": "200"},
                headers=_BRAIN_HEADERS,
            )
        r.raise_for_status()
    except Exception as exc:
        await update.message.reply_text(f"Export failed: {exc}")
        return

    from datetime import date
    from io import BytesIO
    today = date.today().isoformat()
    filename = f"atlas-tasks-{today}.{fmt}"
    payload = BytesIO(r.content)
    payload.name = filename
    caption = f"Atlas tasks export -- {today} ({fmt})"
    await update.message.reply_document(document=payload, filename=filename, caption=caption)


async def cmd_narrative(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/narrative -- weekly cross-session momentum summary (open loops + direction)."""
    await update.message.reply_text("Analysing session narrative...")
    try:
        from atlas_plus.session_narrative import build_narrative
        report = await asyncio.to_thread(build_narrative, 14)
        if report.sessions_analyzed == 0:
            await update.message.reply_text("No session files found in the last 14 days.")
            return
        lines = [f"*Session Narrative: {report.week_label}*", f"Energy: {report.energy} | {report.sessions_analyzed} sessions\n"]
        if report.active_projects:
            lines.append("*Projects*")
            lines.extend(f"  {p}" for p in report.active_projects[:5])
        if report.direction_signals:
            lines.append("\n*Direction*")
            lines.extend(f"  {s[:80]}" for s in report.direction_signals[:4])
        if report.open_loops:
            lines.append("\n*Open loops*")
            lines.extend(f"  {s[:80]}" for s in report.open_loops[:5])
        await _reply_long(update, "\n".join(lines), parse_mode="Markdown")
    except Exception as exc:
        await update.message.reply_text(f"Error: {exc}")


async def cmd_report(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/report -- daily summary of task activity."""
    try:
        from atlas_plus.brain import tasks as ts
        all_tasks = ts.list_tasks(limit=50)
        total = len(all_tasks)
        done = sum(1 for t in all_tasks if t.status == "done")
        failed = sum(1 for t in all_tasks if t.status == "failed")
        running = sum(1 for t in all_tasks if t.status == "running")
        pending = sum(1 for t in all_tasks if t.status == "pending")
        lines = [
            f"Report (last {total} tasks)",
            f"done: {done}  failed: {failed}  running: {running}  pending: {pending}",
            "",
        ]
        recent_done = [t for t in all_tasks if t.status == "done"][:3]
        if recent_done:
            lines.append("Recent completions:")
            for t in recent_done:
                lines.append(f"  {t.task_id}: {t.instruction[:50]}")
        recent_failed = [t for t in all_tasks if t.status == "failed"][:3]
        if recent_failed:
            lines.append("\nFailed:")
            for t in recent_failed:
                lines.append(f"  {t.task_id}: {(t.error or '')[:60]}")
        await update.message.reply_text("\n".join(lines))
    except Exception as exc:
        await update.message.reply_text(f"Error: {exc}")


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/stats -- quick summary of tasks, pending facts, and escalation state."""
    try:
        from atlas_plus.brain import tasks as ts
        from atlas_plus.brain.task_facts import pending_count
        from atlas_plus.brain.history import session_count
        import os

        stats = ts.task_stats(since_days=7)
        pf = pending_count()
        sessions = session_count()

        esc_on = os.environ.get("ATLAS_CLAUDE_ESCALATION", "off").lower() == "on"
        qual_on = os.environ.get("ATLAS_CLAUDE_QUALITY_ESCALATION", "off").lower() == "on"
        sla = os.environ.get("ATLAS_SLA_MINUTES", "15")

        lines = [
            "*Atlas Stats*",
            "",
            f"*Tasks (7d)*: {stats.get('total',0)} total"
            f"  done:{stats.get('done',0)}"
            f"  failed:{stats.get('failed',0)}"
            f"  running:{stats.get('running',0)}",
            f"*Pending facts*: {pf}",
            f"*Brain sessions*: {sessions}",
            "",
            f"*Escalation*: {'on' if esc_on else 'off'}"
            f" | quality: {'on' if qual_on else 'off'}"
            f" | SLA: {sla}min",
        ]
        await _reply_long(update, "\n".join(lines), parse_mode="Markdown")
    except Exception as exc:
        await update.message.reply_text(f"Stats error: {exc}")


async def cmd_tasks_batch(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/tasks_batch -- submit multiple tasks in one message (newline-separated).

    Usage:
      /tasks_batch
      Research async Python patterns
      Write tests for the temporal graph module
      Summarize last 3 sessions

    Or inline:
      /tasks_batch Research async patterns\\nWrite temporal tests\\nSummarize sessions

    Each non-empty line becomes a separate Brain task. Reports all queued task IDs.
    Max 10 tasks per batch to prevent accidental queue floods.
    """
    if not BRAIN_URL:
        await update.message.reply_text("ATLAS_BRAIN_URL is not set.")
        return

    # Extract raw text: everything after the /tasks_batch command
    raw = update.message.text or ""
    # Strip the command itself (first word)
    parts = raw.split(None, 1)
    body = parts[1] if len(parts) > 1 else ""

    # Split on newlines (also handle literal \\n in inline form)
    instructions = [
        line.strip()
        for line in body.replace("\\n", "\n").splitlines()
        if line.strip()
    ]

    if not instructions:
        await update.message.reply_text(
            "Usage: /tasks_batch\n<instruction 1>\n<instruction 2>\n..."
        )
        return

    # Cap batch size to prevent accidental floods
    _MAX_BATCH = 10
    if len(instructions) > _MAX_BATCH:
        await update.message.reply_text(
            f"Batch too large: {len(instructions)} instructions (max {_MAX_BATCH}). "
            "Split into multiple /tasks_batch calls."
        )
        return

    user_id = str(update.effective_user.id)
    session_id = f"tg-batch-{user_id}"
    queued: list[str] = []
    errors: list[str] = []

    async with httpx.AsyncClient(timeout=15.0) as client:
        for instr in instructions:
            try:
                r = await client.post(
                    f"{BRAIN_URL}/task",
                    json={"instruction": instr[:4000], "session_id": session_id},
                    headers=_BRAIN_HEADERS,
                )
                r.raise_for_status()
                data = r.json()
                queued.append(f"`{data['task_id']}` -- {instr[:60]}")
            except Exception as exc:
                errors.append(f"FAILED: {instr[:40]} -- {exc}")

    lines = [f"*Batch submitted {len(queued)}/{len(instructions)} task(s)*", ""]
    lines.extend(queued)
    if errors:
        lines.append("")
        lines.extend(errors)
    lines.append("")
    lines.append("Use /tasks to see queue or /watch <id> to track one.")

    await _reply_long(update, "\n".join(lines), parse_mode="Markdown")


async def cmd_memory_search(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/memory <query> -- quick FTS search of .memory/ from Telegram.

    Returns top 5 results with confidence band, file path, and snippet.
    Uses the same FTS5 + semantic re-ranking as the Web UI Memory tab.
    """
    args = _args(context)
    if not args:
        await update.message.reply_text("Usage: /memory <search query>")
        return

    query = " ".join(args)[:500]
    try:
        from atlas_plus.mcp.memory_server.tools.search import memory_search
        results = memory_search(query, limit=5)
    except Exception as exc:
        await update.message.reply_text(f"Search error: {exc}")
        return

    if not results:
        await update.message.reply_text(f"No results for: `{query}`", parse_mode="Markdown")
        return

    top_score = max((float(r.get("score", 0)) for r in results), default=0.0)
    if top_score >= 0.7:
        band = "high"
    elif top_score >= 0.4:
        band = "medium"
    elif top_score >= 0.2:
        band = "low"
    else:
        band = "starved"

    lines = [f"*Memory search:* `{query}`  ({band} confidence, {len(results)} result(s))", ""]
    for r in results:
        path = r.get("path", "")
        score = float(r.get("score", 0))
        snippet = (r.get("snippet") or r.get("content", ""))[:120].replace("\n", " ")
        lines.append(f"`{path}` ({score:.2f})")
        lines.append(f"  _{snippet}_")
        lines.append("")

    await _reply_long(update, "\n".join(lines), parse_mode="Markdown")


async def cmd_memory_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/confirm <relative-path> -- mark a memory file as confirmed (last_confirmed = today).

    Tells Atlas that you have reviewed this fact and it is still accurate.
    Path is relative to .memory/ (e.g. feedback/archive-not-delete.md).
    """
    args = _args(context)
    if not args:
        await update.message.reply_text(
            "Usage: /confirm <relative-path>\n"
            "Example: /confirm feedback/archive-not-delete.md"
        )
        return

    path_arg = " ".join(args).strip()
    try:
        from atlas_plus.mcp.memory_server.config import MEMORY_ROOT
        from atlas_plus.mcp.memory_server.routing import confirm_fact
        target = MEMORY_ROOT / path_arg.replace("\\", "/")
        if not target.exists():
            await update.message.reply_text(f"File not found: `{path_arg}`", parse_mode="Markdown")
            return
        ok = confirm_fact(target, source="telegram")
        if ok:
            await update.message.reply_text(
                f"Confirmed `{path_arg}` -- last_confirmed set to today.",
                parse_mode="Markdown",
            )
        else:
            await update.message.reply_text(
                f"Could not confirm `{path_arg}` (autonomous writes may be off).",
                parse_mode="Markdown",
            )
    except Exception as exc:
        await update.message.reply_text(f"Confirm error: {exc}")


async def cmd_brain_sessions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/brain_sessions -- list recent Brain conversation sessions.

    Shows the 10 most recent sessions from Brain's SQLite conversation store,
    with message counts and last activity. Sessions are color-labeled by prefix:
      cli- = Brain CLI    tg- = Telegram    escalated- = Claude Code escalation
    """
    try:
        from atlas_plus.brain.history import list_sessions
        sessions = list_sessions(limit=10)
    except Exception as exc:
        await update.message.reply_text(f"Error reading sessions: {exc}")
        return

    if not sessions:
        await update.message.reply_text("No Brain sessions found.")
        return

    lines = ["*Recent Brain Sessions*", ""]
    for s in sessions:
        sid = s["session_id"]
        count = s.get("message_count", 0)
        last = (s.get("last_active") or "")[:16]
        # Emoji label by session type
        if sid.startswith("cli-"):
            icon = "terminal"
        elif sid.startswith("tg-"):
            icon = "phone"
        elif sid.startswith("escalated-"):
            icon = "lightning"
        else:
            icon = "brain"
        lines.append(f"_{icon}_ `{sid}` -- {count} msg -- {last}")

    lines.append("")
    lines.append("Use `/export-session <id>` in Brain CLI to export as markdown.")
    await _reply_long(update, "\n".join(lines), parse_mode="Markdown")


async def cmd_ticket_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/ticketstatus <id> -- show a ticket's state and linked Brain task."""
    args = _args(context)
    if not args:
        await update.message.reply_text("Usage: /ticketstatus <ticket-id>  (e.g. /ticketstatus ATL-007)")
        return

    ticket_id = args[0].upper()
    try:
        from atlas_plus.tickets.store import list_tickets
        ticket = next((t for t in list_tickets() if t.id == ticket_id), None)
    except Exception as exc:
        await update.message.reply_text(f"Error loading ticket: {exc}")
        return

    if not ticket:
        await update.message.reply_text(f"Ticket {ticket_id} not found.")
        return

    # Status emoji
    st_icons = {
        "todo": "o", "in-progress": "...", "done": "done",
        "blocked": "blocked", "archived": "archived",
    }
    st = st_icons.get(ticket.status, ticket.status)
    lines = [
        f"*{ticket.id}* -- {st}",
        f"`{ticket.title}`",
        f"Project: {ticket.project}  |  Assignee: {ticket.assignee}  |  Priority: {ticket.priority}",
    ]

    # Find linked Brain task (session_id = "ticket-{ID}")
    try:
        from atlas_plus.brain import tasks as task_store
        session_id = f"ticket-{ticket_id}"
        all_tasks = task_store.list_tasks(limit=50)
        # Also check active tasks
        all_tasks += task_store.list_tasks(status="running", limit=20)
        linked = [t for t in all_tasks if t.session_id == session_id]
        # Also check verify tasks
        vsession_id = f"verify-ticket-{ticket_id}"
        verify_tasks = [t for t in task_store.list_tasks(limit=50) if t.session_id == vsession_id]

        if linked:
            latest = max(linked, key=lambda t: t.created_at or "")
            task_icon = {"done": "ok", "failed": "fail", "blocked": "blocked",
                         "pending": "...", "running": "run"}.get(latest.status, latest.status)
            lines.append(f"\nBrain task: `{latest.task_id[:20]}` -- {task_icon}")
            if latest.status == "done" and latest.result:
                lines.append(f"Result: {latest.result[:100]}{'...' if len(latest.result) > 100 else ''}")
            elif latest.status in ("failed", "blocked") and latest.error:
                lines.append(f"Error: {latest.error[:100]}")
        else:
            lines.append("\nNo Brain task linked yet.")

        if verify_tasks:
            vt = max(verify_tasks, key=lambda t: t.created_at or "")
            v_icon = {"done": "pass", "failed": "FAIL"}.get(vt.status, vt.status)
            lines.append(f"Verify: `{vt.task_id[:20]}` -- {v_icon}")
    except Exception as exc:
        lines.append(f"\n(task lookup failed: {exc})")

    await _reply_long(update, "\n".join(lines), parse_mode="Markdown")


async def cmd_verify_ticket(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/verify <ticket-id> -- queue a pytest verify task for a specific ticket."""
    args = _args(context)
    if not args:
        await update.message.reply_text("Usage: /verify <ticket-id>  (e.g. /verify ATL-007)")
        return

    ticket_id = args[0].upper()
    import re as _re
    if not _re.match(r"^[A-Z]{1,8}-\d+$", ticket_id):
        await update.message.reply_text(f"Invalid ticket ID format: `{ticket_id}`. Expected e.g. ATL-007.")
        return

    try:
        from atlas_plus.brain import tasks as task_store
        session_id = f"verify-ticket-{ticket_id}"
        task = task_store.create_task(
            instruction=f"Verify ticket {ticket_id}: run full test suite",
            session_id=session_id,
            kind="verify",
            priority=6,
        )
        await update.message.reply_text(
            f"Verify task queued for *{ticket_id}*\n"
            f"Task ID: `{task.task_id[:24]}`\n"
            f"Session: `{session_id}`\n\n"
            f"Use /watch `{task.task_id[:24]}` to follow progress.",
            parse_mode="Markdown",
        )
    except Exception as exc:
        await update.message.reply_text(f"Failed to queue verify task: {exc}")


async def cmd_orphans(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/orphans [N] -- list top N memory orphan candidates (unreferenced + low health + old)."""
    args = _args(context)
    try:
        limit = max(1, min(30, int(args[0]))) if args else 10
    except (ValueError, IndexError):
        limit = 10

    try:
        import os
        from pathlib import Path as _Path
        from atlas_plus.mcp.memory_server.config import MEMORY_ROOT
        from atlas_plus.curation.orphan import find_orphan_candidates
        mem_root = _Path(os.environ.get("ATLAS_MEMORY_ROOT", str(MEMORY_ROOT)))
        results = find_orphan_candidates(
            mem_root, min_age_days=180, max_health=0.5, limit=limit
        )
    except Exception as exc:
        await update.message.reply_text(f"Orphan scan failed: {exc}")
        return

    if not results:
        await update.message.reply_text(
            "No orphan candidates found. (min age: 180d, max health: 0.50)"
        )
        return

    lines = [f"*Memory Orphans* ({len(results)} candidates)"]
    for r in results[:limit]:
        age = r["age_days"]
        h = r["score"]
        lines.append(f"`{r['path'][:45]}`  age:{age}d  health:{h:.2f}")

    lines.append("\nReview and archive with: `atlas-review-memory archive <path>`")
    await _reply_long(update, "\n".join(lines), parse_mode="Markdown")


async def cmd_research_quality(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/research_quality [topic] -- quality summary; with topic, filters reports by topic tag."""
    args = _args(context)
    topic_filter = " ".join(args).strip().lower() if args else ""

    if topic_filter:
        # Topic-filtered mode: scan reports matching topic in frontmatter or filename
        try:
            import os
            import yaml as _yaml
            from pathlib import Path
            reports_dir = atlas_paths.state_dir() / "reports"
            if not reports_dir.exists():
                await update.message.reply_text("No .atlas/reports/ directory found.")
                return
            matches = []
            for md_file in sorted(reports_dir.rglob("*.md")):
                if md_file.name.startswith("_"):
                    continue
                try:
                    raw = md_file.read_text(encoding="utf-8", errors="replace")
                    if not raw.startswith("---"):
                        continue
                    end = raw.find("\n---", 3)
                    fm = _yaml.safe_load(raw[3:end]) if end != -1 else {}
                    topic = str((fm or {}).get("topic", (fm or {}).get("tags", "")) or "").lower()
                    if topic_filter not in topic and topic_filter not in md_file.name.lower():
                        continue
                    qs = (fm or {}).get("quality_score")
                    score = float(qs) if qs is not None else None
                    matches.append((score, md_file.name))
                except Exception:
                    continue
            if not matches:
                await update.message.reply_text(f"No reports match topic: {topic_filter}")
                return
            lines = [f"*Research Quality: {topic_filter}* ({len(matches)} reports)\n"]
            for score, fname in sorted(matches, key=lambda x: (x[0] is None, x[0] or 0)):
                s = f"`{score:.2f}`" if score is not None else "`----`"
                lines.append(f"{s} {fname[:35]}")
            await _reply_long(update, "\n".join(lines), parse_mode="Markdown")
        except Exception as exc:
            await update.message.reply_text(f"Error: {exc}")
        return

    # Global summary mode
    try:
        from atlas_plus.doctor.cli import check_research_quality
        rq = check_research_quality()
    except Exception as exc:
        await update.message.reply_text(f"Error reading research quality: {exc}")
        return

    if not rq:
        await update.message.reply_text("No research reports directory found.")
        return

    total = rq.get("total_reports", 0)
    scored = rq.get("scored_reports", 0)
    avg = rq.get("avg_score", 0.0)
    low = rq.get("low_quality_count", 0)
    min_file = rq.get("min_score_file", "")

    if scored == 0:
        text = (
            f"*Research Quality*\n"
            f"{total} report(s), none scored yet.\n\n"
            "Run `atlas-review-memory quality --write` to score them."
        )
    else:
        quality_label = "good" if avg >= 0.7 else ("fair" if avg >= 0.4 else "poor")
        text = (
            f"*Research Quality*\n"
            f"{scored}/{total} scored | avg: `{avg:.2f}` ({quality_label})\n"
        )
        if low > 0:
            text += f"Low-quality (<0.5): {low}\n"
            if min_file:
                text += f"Lowest: `{min_file}`\n"
            text += "\nRun `atlas-review-memory citations --write` then `atlas-review-memory quality --write` to improve."
        else:
            text += "All scored reports meet the quality threshold."
        text += "\n\n_Tip: /research\\_quality sip for topic-filtered view_"

    await _reply_long(update, text, parse_mode="Markdown")


async def cmd_exhausted(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/exhausted -- show tasks that burned through all retry attempts."""
    try:
        from atlas_plus.brain.tasks import list_exhausted_tasks
        tasks = list_exhausted_tasks(limit=10)
    except Exception as exc:
        await update.message.reply_text(f"Error reading exhausted tasks: {exc}")
        return

    if not tasks:
        await update.message.reply_text("No exhausted tasks. All retries accounted for.")
        return

    lines = [f"*Exhausted Tasks* ({len(tasks)} with no retries left)\n"]
    for t in tasks:
        short_id = t.task_id[:18]
        instr = (t.instruction or "")[:40]
        lines.append(
            f"`{short_id}` [{t.status}]  {t.agent or 'auto'}  `{instr}`"
        )
    lines.append("\nUse /retry <id> to requeue, or investigate the error with /task <id>.")
    await _reply_long(update, "\n".join(lines), parse_mode="Markdown")


_PRIORITY_ORDER_TG = {"critical": 0, "high": 1, "normal": 2, "low": 3}
_DISPATCH_ELIGIBLE_ASSIGNEES_TG = {"any", "brain", "claude-code", "codex", "opencode"}


async def cmd_dispatch_plan(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/dispatch_plan [N] -- next N dispatch-eligible todo tickets ranked by priority."""
    args = _args(context)
    try:
        limit = max(1, min(20, int(args[0]))) if args else 8
    except (ValueError, IndexError):
        limit = 8

    try:
        from atlas_plus.tickets.store import list_tickets
        from atlas_plus.brain.dispatcher import is_dispatch_enabled
        tickets = list_tickets()
    except Exception as exc:
        await update.message.reply_text(f"Plan unavailable: {exc}")
        return

    dispatch_on = is_dispatch_enabled()
    todo = [t for t in tickets if t.status == "todo"]
    eligible = sorted(
        [t for t in todo if getattr(t, "assignee", "human") in _DISPATCH_ELIGIBLE_ASSIGNEES_TG],
        key=lambda t: (_PRIORITY_ORDER_TG.get(getattr(t, "priority", "normal"), 2), t.id),
    )
    human_count = len(todo) - len(eligible)

    dispatch_label = "on" if dispatch_on else "off"
    lines = [f"*Automation Plan* (dispatcher: {dispatch_label})\n"]
    lines.append(f"{len(todo)} todo | {len(eligible)} dispatch-eligible | {human_count} human-only\n")

    if not eligible:
        lines.append("No dispatch-eligible tickets. All require human action.")
    else:
        _EMOJI = {"critical": "!", "high": "^", "normal": "-", "low": "."}
        for t in eligible[:limit]:
            pri = getattr(t, "priority", "normal")
            icon = _EMOJI.get(pri, "-")
            lines.append(f"`{icon}` `{t.id}` [{pri}] {getattr(t, 'assignee', 'any')} -- {t.title[:40]}")
        if len(eligible) > limit:
            lines.append(f"... and {len(eligible) - limit} more.")

    if not dispatch_on:
        lines.append("\nEnable with: `ATLAS_TICKET_DISPATCH=on` + /dispatch_tickets")

    await _reply_long(update, "\n".join(lines), parse_mode="Markdown")


async def cmd_recent_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/recent_tasks [N] -- show last N completed (done/failed/blocked) Brain tasks."""
    args = _args(context)
    try:
        limit = max(1, min(20, int(args[0]))) if args else 8
    except (ValueError, IndexError):
        limit = 8

    try:
        from atlas_plus.brain.tasks import list_tasks
        items = list_tasks(status=None, limit=limit * 3, sort_by="finished_at", sort_desc=True)
        finished = [t for t in items if t.finished_at and t.status in ("done", "failed", "blocked")][:limit]
    except Exception as exc:
        await update.message.reply_text(f"Error: {exc}")
        return

    if not finished:
        await update.message.reply_text("No completed tasks found.")
        return

    _ICON = {"done": "v", "failed": "x", "blocked": "!"}
    lines = [f"*Recent Completed Tasks* ({len(finished)})\n"]
    for t in finished:
        icon = _ICON.get(t.status, "-")
        instr = (t.instruction or "")[:45]
        lines.append(
            f"`{icon}` `{t.task_id[:16]}` {t.agent or 'auto'} -- {instr}"
        )
    await _reply_long(update, "\n".join(lines), parse_mode="Markdown")


async def cmd_system_health(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/sys_health -- compact Atlas system health snapshot (mirrors Brain CLI /health)."""
    lines = ["*Atlas System Health*\n"]

    # Services
    try:
        from atlas_plus.doctor.cli import check_services
        services = check_services()
        up = sum(1 for s in services if s.get("status") == "up")
        down = [s["name"] for s in services if s.get("status") != "up"]
        svc_line = f"Services: {up}/{len(services)} up"
        if down:
            svc_line += f" | DOWN: {', '.join(down)}"
        lines.append(svc_line)
    except Exception as exc:
        lines.append(f"Services: unavailable ({exc})")

    # Worker
    try:
        from atlas_plus.doctor.cli import check_worker_status
        wk = check_worker_status()
        assessment = wk.get("assessment", "unknown")
        pending = wk.get("pending", "?")
        lines.append(f"Worker: {assessment} | pending: {pending}")
    except Exception:
        lines.append("Worker: unavailable")

    # Memory quality
    try:
        from atlas_plus.doctor.cli import check_memory_quality
        mq = check_memory_quality()
        total = mq.get("total_files", 0)
        conf_pct = mq.get("confirmed_pct", 0)
        tag_pct = mq.get("tagged_pct", 0)
        lines.append(f"Memory: {total} files | confirmed {conf_pct}% | tagged {tag_pct}%")
    except Exception:
        lines.append("Memory: unavailable")

    # Pending facts
    try:
        from atlas_plus.brain.task_facts import pending_count
        n = pending_count()
        if n > 0:
            lines.append(f"Pending facts: {n} (use /pending in Brain CLI)")
    except Exception:
        pass

    # Exhausted tasks
    try:
        from atlas_plus.brain.tasks import list_exhausted_tasks
        ex = list_exhausted_tasks(limit=10)
        if ex:
            lines.append(f"Exhausted tasks: {len(ex)} (no retries left -- /exhausted)")
    except Exception:
        pass

    await _reply_long(update, "\n".join(lines), parse_mode="Markdown")


async def cmd_curation_report(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/curation_report -- run weekly curation report and show summary counts.

    Runs generate_report() in a thread (blocking I/O scan of .memory/).
    """
    await update.message.reply_text("Running curation report...")

    def _run_report() -> tuple[int, int, int, int]:
        from atlas_plus.curation.weekly_report import generate_report
        import re
        report_text, _ = generate_report(dry_run=True)
        dup = stale = convergence = decisions = 0
        for line in report_text.splitlines():
            if "Repeated facts (" in line:
                m = re.search(r"\((\d+) found", line)
                if m:
                    counts["duplicates"] = counts.get("duplicates", 0) + int(m.group(1))
            if "Potential Duplicates (" in line:
                m = re.search(r"\((\d+) found", line)
                if m:
                    dup = int(m.group(1))
            elif "Stale Files (" in line:
                m = re.search(r"\((\d+) found", line)
                if m:
                    stale = int(m.group(1))
            elif "Convergence Flags (" in line:
                m = re.search(r"\((\d+)", line)
                if m:
                    convergence = int(m.group(1))
            elif "Stale Decisions (" in line:
                m = re.search(r"\((\d+)", line)
                if m:
                    decisions = int(m.group(1))
        return dup, stale, convergence, decisions

    try:
        dup_count, stale_count, convergence_count, decisions_count = await asyncio.to_thread(_run_report)
    except Exception as exc:
        await update.message.reply_text(f"Error: {exc}")
        return

    lines = [
        "*Curation Report*\n",
        f"Duplicates: {dup_count}",
        f"Stale files: {stale_count}",
        f"Convergence: {convergence_count}",
        f"Stale decisions: {decisions_count}",
    ]
    await _reply_long(update, "\n".join(lines), parse_mode="Markdown")


async def cmd_temporal_rebuild(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/temporal_rebuild [--force] -- rebuild the temporal graph (skips if fresh, use --force to override)."""
    force = "--force" in (context.args or [])
    source = "all"
    for i, tok in enumerate(context.args or []):
        if tok == "--source" and i + 1 < len(context.args):
            source = context.args[i + 1]

    try:
        from atlas_plus.doctor.cli import check_temporal
        tg = await asyncio.to_thread(check_temporal)
        stale = tg.get("stale", True)
        age_days = tg.get("age_days")
        last_built = tg.get("last_built", "never")
    except Exception as exc:
        await update.message.reply_text(f"Error checking temporal graph: {exc}")
        return

    if not force and not stale:
        age_str = f" ({age_days}d old)" if age_days is not None else ""
        await update.message.reply_text(
            f"Temporal graph is fresh{age_str} (built {last_built}). Use /temporal_rebuild --force to rebuild."
        )
        return

    age_str = f" ({age_days}d old)" if age_days is not None else ""
    prefix = "Graph is stale" if stale else "Force rebuild requested"
    await update.message.reply_text(f"{prefix}{age_str}. Rebuilding with source={source}...")

    try:
        from atlas_plus.temporal_graph.build import build_temporal_graph
        result = await asyncio.to_thread(build_temporal_graph, source)
    except Exception as exc:
        await update.message.reply_text(f"Rebuild error: {exc}")
        return

    if "error" in result:
        await update.message.reply_text(f"Build failed: {result['error']}")
        return

    idx = result.get("index", {})
    await update.message.reply_text(
        f"*Temporal graph rebuilt*\n"
        f"Entities: {idx.get('entities', 0)}\n"
        f"Active facts: {idx.get('facts_active', 0)}\n"
        f"Indexed: {result.get('files_indexed', 0)} | Skipped: {result.get('files_skipped', 0)}",
        parse_mode="Markdown"
    )


async def cmd_temporal_search(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/temporal_search <query> -- search the temporal knowledge graph for current facts."""
    query_str = " ".join(context.args or []).strip()
    if not query_str:
        await update.message.reply_text(
            "Usage: /temporal_search <query>\nExample: /temporal_search Atlas MCP Tunnel"
        )
        return

    await update.message.reply_text(f"Searching temporal graph for: {query_str}...")

    try:
        from atlas_plus.temporal_graph.file_provider import FileTemporalProvider
        provider = FileTemporalProvider()
        stats = provider.stats()
        if stats["facts"] == 0:
            await update.message.reply_text(
                "Temporal graph is empty. Run: atlas-temporal build --source fixtures"
            )
            return

        results = provider.search_current(query_str, limit=5)
    except Exception as exc:
        await update.message.reply_text(f"Error: {exc}")
        return

    if not results:
        await update.message.reply_text(f"No temporal facts found for: {query_str}")
        return

    lines = [f"*Temporal: {query_str}* ({len(results)} result(s))\n"]
    for i, r in enumerate(results, 1):
        warn = " _(stale)_" if r.freshness_warning else ""
        lines.append(
            f"{i}. {r.fact.fact_text}{warn}\n"
            f"   _{r.fact.valid_from[:10]} | {r.provenance}_"
        )
    await _reply_long(update, "\n".join(lines), parse_mode="Markdown")


async def cmd_temporal_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/temporal_stats -- show temporal knowledge graph statistics."""
    try:
        from atlas_plus.temporal_graph.file_provider import FileTemporalProvider
        provider = FileTemporalProvider()
        s = provider.stats()
    except Exception as exc:
        await update.message.reply_text(f"Error: {exc}")
        return

    lines = [
        "*Temporal Graph Stats*\n",
        f"Episodes: {s['episodes']}",
        f"Entities: {s['entities']}",
        f"Facts (total): {s['facts']}",
        f"Facts (active): {s['facts_active']}",
    ]
    await _reply_long(update, "\n".join(lines), parse_mode="Markdown")


async def cmd_temporal_history(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/temporal_history <entity> -- show the fact timeline for an entity in the temporal graph."""
    entity_name = " ".join(context.args or []).strip()
    if not entity_name:
        await update.message.reply_text(
            "Usage: /temporal_history <entity>\nExample: /temporal_history Atlas MCP Tunnel"
        )
        return

    try:
        from atlas_plus.temporal_graph.file_provider import FileTemporalProvider
        provider = FileTemporalProvider()
        h = provider.history(entity_name, limit=10)
    except Exception as exc:
        await update.message.reply_text(f"Error: {exc}")
        return

    if not h.facts:
        await update.message.reply_text(f"No temporal history found for: {entity_name}")
        return

    lines = [
        f"*History: {h.entity.name}*\n"
        f"_{h.entity.first_seen_at[:10]} to {h.entity.last_seen_at[:10]}_\n"
    ]
    for fact in h.facts[:8]:
        status = "(active)" if fact.invalid_at is None else f"(until {fact.invalid_at[:10]})"
        marker = "->" if fact is h.current_fact else "--"
        lines.append(f"{marker} [{fact.valid_from[:10]}] {fact.fact_text} _{status}_")
    await _reply_long(update, "\n".join(lines), parse_mode="Markdown")


async def cmd_next_actions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/next_actions [N] -- show prioritized memory maintenance to-do list.

    Categories (in urgency order):
      1. Decayed facts (past half-life threshold)
      2. Orphan candidates (unreferenced low-health old files)
      3. Memory gaps (recurring miss queries with no coverage)

    Default N=5 items per category. Useful for quick hygiene triage from mobile.
    """
    try:
        limit = max(1, min(20, int((context.args or ["5"])[0])))
    except (ValueError, IndexError):
        limit = 5

    def _gather() -> tuple[list, list, list]:
        from atlas_plus.curation.decay import find_decayed_facts
        from atlas_plus.curation.orphan import find_orphan_candidates
        from atlas_plus.mcp.memory_server.tools.search_log import top_miss_queries
        from atlas_plus.mcp.memory_server.config import MEMORY_ROOT

        _decayed = _orphans = _gaps = []
        try:
            _decayed = find_decayed_facts(threshold_days=0)[:limit]
        except Exception:
            pass
        try:
            _orphans = find_orphan_candidates(MEMORY_ROOT, min_age_days=90, max_health=0.5, limit=limit)
        except Exception:
            pass
        try:
            _gaps = top_miss_queries(since_hours=168, limit=limit)
        except Exception:
            pass
        return _decayed, _orphans, _gaps

    try:
        decayed, orphans, gaps = await asyncio.to_thread(_gather)
    except Exception as exc:
        await update.message.reply_text(f"Error: {exc}")
        return

    if not decayed and not orphans and not gaps:
        await update.message.reply_text("Memory corpus is in good shape -- no urgent actions found.")
        return

    lines = ["*Next Actions*\n"]

    if decayed:
        lines.append(f"*{len(decayed)} Decayed* (past half-life):")
        for c in decayed[:limit]:
            lines.append(f"  `{c.path[:50]}` -- {c.days_overdue}d overdue")

    if orphans:
        lines.append(f"\n*{len(orphans)} Orphans* (unreferenced):")
        for o in orphans[:limit]:
            p = o.get("path", "")[:50]
            h = o.get("score", 0)
            lines.append(f"  `{p}` -- health={h:.2f}")

    if gaps:
        lines.append(f"\n*{len(gaps)} Gaps* (recurring miss queries):")
        for g in gaps[:limit]:
            lines.append(f"  `{g['query'][:45]}` -- {g['miss_count']}x")

    text = "\n".join(lines)
    await _reply_long(update, text, parse_mode="Markdown")


async def cmd_session_log(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/session_log [--dry-run] -- write today's session log from git commits.

    Calls write_session_log(since='today'). Requires ATLAS_AUTONOMOUS=on to write.
    Add --dry-run to preview the entry without writing.
    """
    dry_run = "--dry-run" in (context.args or [])

    mode = "dry-run preview" if dry_run else "writing"
    await update.message.reply_text(f"Session log ({mode})...")

    try:
        from atlas_plus.sprint_summary import write_session_log
        _fn = lambda: write_session_log(since="today", dry_run=dry_run)
        result = await asyncio.to_thread(_fn)
    except Exception as exc:
        await update.message.reply_text(f"Error: {exc}")
        return

    if not result.get("ok"):
        await update.message.reply_text(f"Session log failed: {result.get('error', 'unknown')}")
        return

    entry = result.get("entry", "")
    if not entry:
        await update.message.reply_text("No commits found today -- nothing to log.")
        return

    if dry_run:
        preview = entry[:800] + ("..." if len(entry) > 800 else "")
        await update.message.reply_text(f"*DRY RUN*\n{preview}", parse_mode="Markdown")
    else:
        paths = result.get("paths_written", [])
        first_line = entry.split("\n")[0] if entry else ""
        await update.message.reply_text(
            f"Session log written ({len(paths)} file(s)).\n{first_line}"
        )
