"""Atlas Telegram bot — long-poll entry point.

Setup (one-time):
  1. Message @BotFather on Telegram: /newbot -> follow prompts -> copy the token
  2. Message @userinfobot to get your Telegram user ID
  3. Set env vars:
       ATLAS_TELEGRAM_BOT_TOKEN=<token from BotFather>
       ATLAS_TELEGRAM_ALLOWED_USERS=<your numeric user ID>
       ATLAS_NTFY_TOPIC=<your ntfy.sh topic>       # optional but recommended
       ATLAS_BRAIN_URL=http://localhost:8767        # optional, enables brain routing

Run:
  uv run atlas-telegram-bot

The bot uses long-polling — no open ports, no webhook, no firewall changes needed.
Your machine must be on and connected for the bot to respond.

Non-command messages are routed to Atlas Brain (ATLAS_BRAIN_URL) if configured.
Each Telegram user gets their own Brain session keyed by user ID.
"""

from __future__ import annotations

import logging
import os
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
)
log = logging.getLogger("atlas.telegram.bot")

BOT_TOKEN = os.environ.get("ATLAS_TELEGRAM_BOT_TOKEN", "")
ALLOWED_USERS_RAW = os.environ.get("ATLAS_TELEGRAM_ALLOWED_USERS", "")
ALLOWED_USERS: set[int] = set(
    int(x.strip()) for x in ALLOWED_USERS_RAW.split(",") if x.strip().isdigit()
)
BRAIN_URL = os.environ.get("ATLAS_BRAIN_URL", "").rstrip("/")
BRAIN_API_KEY = os.environ.get("ATLAS_BRAIN_API_KEY", "")


def _is_allowed(update) -> bool:
    # Fail-closed: an EMPTY allowlist denies everyone. main() already refuses to start
    # without ATLAS_TELEGRAM_ALLOWED_USERS, so in production this branch is unreachable --
    # but the old `return True` on an empty set was a fail-open trap for any direct caller
    # or future refactor that skips main()'s guard (same class as the ATLAS_AUTONOMOUS
    # denylist fixed 2026-07-02).
    if not ALLOWED_USERS:
        return False
    user = getattr(update, "effective_user", None)
    return user is not None and user.id in ALLOWED_USERS


def main() -> None:
    """Entry point for the Atlas Telegram bot (atlas-telegram-bot).

    Registers all command handlers with auth gating. Requires ATLAS_TELEGRAM_BOT_TOKEN
    and ATLAS_TELEGRAM_ALLOWED_USERS env vars. Fails fast if token is missing.
    """
    # --help must print usage and exit 0 BEFORE any env validation
    # (Codex artifact audit 2026-09-21).
    import argparse

    argparse.ArgumentParser(
        prog="atlas-telegram-bot",
        description="Telegram control bot (long-poll; needs ATLAS_TELEGRAM_BOT_TOKEN "
                    "and ATLAS_TELEGRAM_ALLOWED_USERS).",
    ).parse_args()

    if not BOT_TOKEN:
        print(
            "ATLAS_TELEGRAM_BOT_TOKEN is not set.\n"
            "Get a token from @BotFather on Telegram, then:\n"
            "  export ATLAS_TELEGRAM_BOT_TOKEN=your-token-here      (PowerShell: $env:ATLAS_TELEGRAM_BOT_TOKEN='...')\n"
            "  atlas-telegram-bot",
            file=sys.stderr,
        )
        sys.exit(1)

    if not ALLOWED_USERS:
        print(
            "ATLAS_TELEGRAM_ALLOWED_USERS is not set.\n"
            "The bot refuses to run without an allowlist -- this prevents it from\n"
            "responding to any Telegram user who knows the bot name.\n"
            "Get your user ID from @userinfobot, then:\n"
            "  export ATLAS_TELEGRAM_ALLOWED_USERS=your-numeric-id  (PowerShell: $env:ATLAS_TELEGRAM_ALLOWED_USERS='...')\n"
            "  atlas-telegram-bot",
            file=sys.stderr,
        )
        sys.exit(1)

    try:
        from telegram.ext import Application, CommandHandler
    except ImportError:
        print(
            "python-telegram-bot not installed. Run:\n"
            "  uv sync --extra telegram",
            file=sys.stderr,
        )
        sys.exit(1)

    from .commands import (
        cmd_agents,
        cmd_credit_reset,
        cmd_dispatch,
        cmd_doctor,
        cmd_done,
        cmd_supervisor,
        cmd_claude,
        cmd_convergence,
        cmd_decisions,
        cmd_rederive,
        cmd_suggest_tags,
        cmd_suggest_facts,
        cmd_curation_report,
        cmd_temporal_rebuild,
        cmd_temporal_search,
        cmd_temporal_stats,
        cmd_temporal_history,
        cmd_session_log,
        cmd_next_actions,
        cmd_decay,
        cmd_decay_report,
        cmd_gaps,
        cmd_help,
        cmd_status,
        cmd_plan,
        cmd_project,
        cmd_projects,
        cmd_queue,
        cmd_quick,
        cmd_remember,
        cmd_digest,
        cmd_report,
        cmd_export,
        cmd_narrative,
        cmd_priority,
        cmd_research,
        cmd_retry,
        cmd_run,
        cmd_search,
        cmd_status_update,
        cmd_stop,
        cmd_task,
        cmd_inbox,
        cmd_inspire,
        cmd_taskstatus,
        cmd_tasks,
        cmd_temporal,
        cmd_think,
        cmd_ticket,
        cmd_tickets,
        cmd_watch,
        cmd_stats,
        cmd_tasks_batch,
        cmd_memory_search,
        cmd_memory_confirm,
        cmd_brain_sessions,
        cmd_research_quality,
        cmd_research_force,
        cmd_research_index,
        cmd_dispatch_tickets,
        cmd_ticket_status,
        cmd_verify_ticket,
        cmd_orphans,
        cmd_loop_status,
        cmd_exhausted,
    )
    from .commands import cmd_dispatch_plan, cmd_recent_tasks, cmd_system_health, cmd_research_gaps, cmd_gap_report, cmd_decay_report, cmd_ticket_sync, cmd_ticket_health
    from telegram import Update
    from telegram.ext import ContextTypes, MessageHandler, filters

    app = Application.builder().token(BOT_TOKEN).build()

    # Auth-wrapping decorator applied to every handler
    def guarded(handler):
        """Wrap a Telegram handler with user-allowlist auth. Rejects unlisted users with a 403 reply."""
        async def _inner(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
            if not _is_allowed(update):
                log.warning(
                    "Rejected message from user_id=%s username=%s",
                    getattr(update.effective_user, "id", "?"),
                    getattr(update.effective_user, "username", "?"),
                )
                if update.message:
                    await update.message.reply_text("Unauthorized.")
                return
            await handler(update, ctx)
        return _inner

    commands = [
        ("help", cmd_help),
        ("start", cmd_help),
        ("ticket", cmd_ticket),
        ("queue", cmd_queue),
        ("tickets", cmd_tickets),
        ("done", cmd_done),
        ("project", cmd_project),
        ("projects", cmd_projects),
        ("search", cmd_search),
        ("remember", cmd_remember),
        ("task", cmd_task),
        ("inbox", cmd_inbox),
        ("inspire", cmd_inspire),
        ("taskstatus", cmd_taskstatus),
        ("tasks", cmd_tasks),
        ("run", cmd_run),
        ("agents", cmd_agents),
        ("dispatch", cmd_dispatch),
        ("plan", cmd_plan),
        ("quick", cmd_quick),
        ("think", cmd_think),
        ("stop", cmd_stop),
        ("retry", cmd_retry),
        ("report", cmd_report),
        ("research", cmd_research),
        ("status", cmd_status),         # /status = quick snapshot
        ("setstatus", cmd_status_update),  # /setstatus <id> <status> = ticket update
        ("digest", cmd_digest),
        ("gaps", cmd_gaps),
        ("supervisor", cmd_supervisor),
        ("claude", cmd_claude),
        ("convergence", cmd_convergence),
        ("decisions", cmd_decisions),
        ("rederive", cmd_rederive),
        ("suggest_tags", cmd_suggest_tags),
        ("suggest_facts", cmd_suggest_facts),
        ("decay", cmd_decay),
        ("decay_report", cmd_decay_report),
        ("ticket_sync", cmd_ticket_sync),
        ("ticket_health", cmd_ticket_health),
        ("temporal", cmd_temporal),
        ("doctor", cmd_doctor),
        ("credit", cmd_credit_reset),
        ("export", cmd_export),
        ("narrative", cmd_narrative),
        ("priority", cmd_priority),
        ("watch", cmd_watch),
        ("stats", cmd_stats),
        ("tasks_batch", cmd_tasks_batch),
        ("memory", cmd_memory_search),
        ("confirm", cmd_memory_confirm),
        ("brain_sessions", cmd_brain_sessions),
        ("research_quality", cmd_research_quality),
        ("research_force", cmd_research_force),
        ("research_index", cmd_research_index),
        ("dispatch_tickets", cmd_dispatch_tickets),
        ("ticketstatus", cmd_ticket_status),
        ("verify", cmd_verify_ticket),
        ("orphans", cmd_orphans),
        ("loop_status", cmd_loop_status),
        ("exhausted", cmd_exhausted),
        ("dispatch_plan", cmd_dispatch_plan),
        ("recent_tasks", cmd_recent_tasks),
        ("sys_health", cmd_system_health),
        ("health", cmd_system_health),      # alias for /sys_health
        ("research_gaps", cmd_research_gaps),
        ("gap_report", cmd_gap_report),
        ("curation_report", cmd_curation_report),
        ("temporal_rebuild", cmd_temporal_rebuild),
        ("temporal_search", cmd_temporal_search),
        ("temporal_stats", cmd_temporal_stats),
        ("temporal_history", cmd_temporal_history),
        ("session_log", cmd_session_log),
        ("next_actions", cmd_next_actions),
    ]

    for name, handler in commands:
        app.add_handler(CommandHandler(name, guarded(handler)))

    # Route plain text messages to Atlas Brain if configured
    if BRAIN_URL:
        import httpx as _httpx

        _brain_headers = {"Authorization": f"Bearer {BRAIN_API_KEY}"} if BRAIN_API_KEY else {}

        async def cmd_brain_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
            """Forward plain text messages to Brain /chat endpoint and reply with the response."""
            if not update.message or not update.message.text:
                return
            user_id = str(update.effective_user.id)
            text = update.message.text.strip()
            if not text:
                return
            # ATL-026: a message that is just a URL is a "drop this link" capture, not a chat.
            from atlas_plus.inbox.capture import build_capture_payload, is_bare_url
            if is_bare_url(text):
                try:
                    async with _httpx.AsyncClient(timeout=15.0) as client:
                        ir = await client.post(
                            f"{BRAIN_URL}/inbox",
                            json=build_capture_payload(text),
                            headers={**_brain_headers, "X-Atlas-Caller-Identity": user_id},
                        )
                    ir.raise_for_status()
                    item_id = ir.json().get("item_id", "?")
                    await update.message.reply_text(
                        f"Saved link to inbox: {item_id} (summarizing). Review in Helm or with /inbox."
                    )
                except Exception as exc:
                    await update.message.reply_text(f"[inbox error: {exc}]")
                return
            await update.message.reply_text("Thinking...")
            try:
                async with _httpx.AsyncClient(timeout=120.0) as client:
                    r = await client.post(
                        f"{BRAIN_URL}/chat",
                        json={"session_id": user_id, "message": text},
                        headers=_brain_headers,
                    )
                r.raise_for_status()
                reply = r.json().get("response", "[no response]")
            except Exception as exc:
                reply = f"[Brain error: {exc}]"
                log.error("Brain call failed: %s", exc)
            await update.message.reply_text(reply[:4096])

        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, guarded(cmd_brain_message)))
        log.info("Brain routing enabled -> %s", BRAIN_URL)
    else:
        log.info("ATLAS_BRAIN_URL not set -- plain messages will be ignored.")

    log.info("Atlas Telegram bot started (long-poll). Press Ctrl-C to stop.")
    log.info("Allowed users: %s", ALLOWED_USERS or "UNRESTRICTED")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
