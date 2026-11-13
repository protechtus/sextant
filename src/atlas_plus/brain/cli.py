"""Atlas Brain CLI -- local terminal chat against Qwen3.

Fallback interaction layer when Claude Code and Codex tokens are exhausted.
Runs entirely local: no cloud, no token limits, no internet required.

Usage:
    .venv\\Scripts\\python.exe -m atlas_plus.brain.cli
    .venv\\Scripts\\python.exe -m atlas_plus.brain.cli --session <id>
    .venv\\Scripts\\python.exe -m atlas_plus.brain.cli --history
    .venv\\Scripts\\python.exe -m atlas_plus.brain.cli --list-sessions

Session ID defaults to cli-YYYY-MM-DD, so the same session continues
across restarts within a day. Use --session to keep context longer.

Each conversation turn is mirrored to .atlas/cli-history/<session_id>.jsonl
in addition to the SQLite brain_conversations table. This allows grep-friendly
session review and makes long research sessions survive SQLite corruption.

Commands during chat:
    /quit /exit      -- end session
    /history         -- show last 20 messages in this session
    /tasks [N]       -- list recent Brain tasks (default 10, max 50)
    /task <id>       -- show full detail for one task
    /submit <instr>  -- queue a task to the Brain worker (default agent: brain)
    /watch  <id>     -- poll a task until done and show the result
    /pending         -- list pending fact candidates awaiting review
    /approve <id>    -- approve a pending fact (writes to memory)
    /reject  <id>    -- reject a pending fact
    /search <query>  -- full-text search memory
    /agents          -- show agent registry status
    /escalation      -- show Claude Code escalation config
    /sessions        -- list recent Brain sessions
    /kill   <id>     -- cancel a pending or running task
    /browse [scope]  -- list .memory/ files (e.g. /browse feedback)
    /save <s> <f>    -- write a fact directly to .memory/ (scope + text)
    /export [path]   -- export session to markdown (.atlas/exports/<id>.md)
    /loop            -- automation loop health (dispatch + tickets + reopen counts)
    /session-log     -- write today's session log from git commits (--dry-run to preview)
    /next-actions [N]-- prioritized memory hygiene to-do list (decayed, orphans, gaps)
    /curation-report -- weekly curation report (duplicates, stale, convergence, decisions)
    /decay-report    -- queue decay_report Brain task (half-life decay scan + artifact)
    /ticket-sync     -- auto-close tickets referenced in recent git commits
    /ticket-health   -- staleness audit + orphan reset for all open tickets
    /temporal-search <query> [--as-of DATE] -- query temporal knowledge graph
    /temporal-history <entity>  -- full fact timeline for an entity
    /temporal-entities [N]      -- list all entities in the temporal graph
    /clear           -- clear this session's history
    /help            -- show commands
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from atlas_plus import paths as atlas_paths

console = Console()

_DEFAULT_SESSION_PREFIX = "cli"
_BANNER = "[bold cyan]Atlas Brain[/bold cyan] -- local Qwen3 chat"
_HINT = "[dim]Type [bold]/help[/bold] for commands. [bold]/quit[/bold] to exit.[/dim]"

_CLI_HISTORY_DIR = atlas_paths.state_dir() / "cli-history"


def _today_session_id() -> str:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return f"{_DEFAULT_SESSION_PREFIX}-{today}"


def _cli_history_path(session_id: str) -> Path:
    """Return the JSONL file path for a CLI session's conversation log."""
    _CLI_HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    # Sanitize session_id for use as a filename
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in session_id)
    return _CLI_HISTORY_DIR / f"{safe}.jsonl"


def _append_cli_turn(session_id: str, role: str, content: str) -> None:
    """Append one conversation turn to the session's JSONL file.

    Format: {"role": "user"|"assistant", "content": "...", "ts": "ISO-8601"}
    Each line is one turn. File is append-only; never overwritten.
    """
    try:
        record = {
            "role": role,
            "content": content,
            "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00"),
        }
        path = _cli_history_path(session_id)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        pass  # non-fatal -- session still works without the JSONL log


def _load_cli_history_jsonl(session_id: str) -> list[dict]:
    """Load conversation turns from the JSONL file for a session.

    Returns list of {role, content, ts} dicts in chronological order.
    Used for display via --history flag; actual chat context comes from SQLite.
    """
    path = _cli_history_path(session_id)
    if not path.exists():
        return []
    turns = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            turns.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return turns


def _list_cli_sessions() -> list[dict]:
    """Return all saved CLI sessions from .atlas/cli-history/ sorted by most recent first.

    Returns list of {session_id, turn_count, last_ts, path}.
    """
    if not _CLI_HISTORY_DIR.exists():
        return []
    sessions = []
    for jsonl_file in sorted(_CLI_HISTORY_DIR.glob("*.jsonl")):
        lines = [
            l.strip() for l in
            jsonl_file.read_text(encoding="utf-8", errors="replace").splitlines()
            if l.strip()
        ]
        last_ts = ""
        for line in reversed(lines):
            try:
                rec = json.loads(line)
                last_ts = rec.get("ts", "")
                break
            except json.JSONDecodeError:
                pass
        sessions.append({
            "session_id": jsonl_file.stem,
            "turn_count": len(lines),
            "last_ts": last_ts,
            "path": str(jsonl_file),
        })
    # Sort by last_ts descending (ISO timestamps sort lexicographically)
    sessions.sort(key=lambda s: s["last_ts"], reverse=True)
    return sessions


def _session_message_count(session_id: str) -> int:
    """Return number of stored messages for a session (from SQLite)."""
    try:
        from atlas_plus.brain import history as hist
        sessions = hist.list_sessions(limit=200)
        for s in sessions:
            if s["session_id"] == session_id:
                return s["message_count"]
    except Exception:
        pass
    return 0


def _print_response(text: str, tool_calls: int, tool_log: list | None = None) -> None:
    if not text.strip():
        console.print("[dim](no response)[/dim]")
        return
    try:
        console.print(Markdown(text))
    except Exception:
        console.print(text)
    if tool_calls:
        console.print(f"[dim]({tool_calls} tool call{'s' if tool_calls != 1 else ''})[/dim]")
    if tool_log:
        for entry in tool_log:
            tool_name = entry.get("tool", "?")
            preview = entry.get("result_preview", "")[:80].replace("\n", " ")
            console.print(f"  [dim]-> {tool_name}: {preview}[/dim]")


def _show_history(session_id: str) -> None:
    from atlas_plus.brain import history as hist
    messages = hist.get_messages(session_id, limit=20)
    if not messages:
        console.print("[dim]No history for this session.[/dim]")
        return
    for m in messages:
        role = m["role"].upper()
        color = {"USER": "green", "ASSISTANT": "cyan", "TOOL": "yellow"}.get(role, "white")
        content = m.get("content", "")[:300]
        console.print(f"[{color}]{role}[/{color}]: {content}")


def _show_tasks(limit: int = 10) -> None:
    from atlas_plus.brain import tasks as ts
    limit = min(max(1, limit), 50)
    items = ts.list_tasks(limit=limit)
    if not items:
        console.print("[dim]No tasks.[/dim]")
        return
    t = Table(show_header=True, header_style="bold")
    t.add_column("ID", style="dim", width=22)
    t.add_column("Status", width=10)
    t.add_column("Agent", width=12)
    t.add_column("Instruction", width=44)
    t.add_column("Finished", width=20)
    _STATUS_COLORS = {
        "done": "green", "running": "yellow", "failed": "red",
        "pending": "blue", "blocked": "magenta",
    }
    for task in items:
        sc = _STATUS_COLORS.get(task.status, "white")
        t.add_row(
            task.task_id,
            f"[{sc}]{task.status}[/{sc}]",
            task.agent or "",
            task.instruction[:42],
            (task.finished_at or "")[:19],
        )
    console.print(t)


def _show_recent_tasks(limit: int = 10) -> None:
    """Show the last N completed (done/failed/blocked) tasks sorted by finish time."""
    from atlas_plus.brain import tasks as ts
    limit = min(max(1, limit), 50)
    items = ts.list_tasks(status=None, limit=limit * 3, sort_by="finished_at", sort_desc=True)
    finished = [t for t in items if t.finished_at and t.status in ("done", "failed", "blocked")][:limit]
    if not finished:
        console.print("[dim]No completed tasks.[/dim]")
        return
    t = Table(show_header=True, header_style="bold", title=f"Recent Completed ({len(finished)})")
    t.add_column("ID", style="dim", width=22)
    t.add_column("Status", width=9)
    t.add_column("Agent", width=12)
    t.add_column("Instruction", width=44)
    t.add_column("Finished", width=20)
    _SC = {"done": "green", "failed": "red", "blocked": "magenta"}
    for task in finished:
        sc = _SC.get(task.status, "white")
        t.add_row(
            task.task_id,
            f"[{sc}]{task.status}[/{sc}]",
            task.agent or "",
            (task.instruction or "")[:42],
            (task.finished_at or "")[:19],
        )
    console.print(t)


def _show_task_detail(task_id: str) -> None:
    from atlas_plus.brain import tasks as ts
    task = ts.get_task(task_id)
    if task is None:
        console.print(f"[red]Task not found: {task_id}[/red]")
        return
    _STATUS_COLORS = {
        "done": "green", "running": "yellow", "failed": "red",
        "pending": "blue", "blocked": "magenta",
    }
    sc = _STATUS_COLORS.get(task.status, "white")
    lines = [
        f"[bold]Task:[/bold] {task.task_id}",
        f"[bold]Status:[/bold] [{sc}]{task.status}[/{sc}]",
        f"[bold]Agent:[/bold]  {task.agent or '(none)'}",
        f"[bold]Kind:[/bold]   {task.kind}",
        f"[bold]Priority:[/bold] {task.priority}",
        f"[bold]Attempts:[/bold] {task.attempts}/{task.max_attempts}",
        f"[bold]Session:[/bold] {task.session_id or '(none)'}",
        f"[bold]Created:[/bold]  {task.created_at}",
        f"[bold]Started:[/bold]  {task.started_at or '(not started)'}",
        f"[bold]Finished:[/bold] {task.finished_at or '(running/pending)'}",
        "",
        f"[bold]Instruction:[/bold]\n  {task.instruction}",
    ]
    if task.result:
        preview = task.result[:600] + ("..." if len(task.result) > 600 else "")
        lines.append(f"\n[bold]Result:[/bold]\n  {preview}")
    console.print(Panel("\n".join(lines), title="Task Detail", expand=False))

    # Attempt history -- only shown when more than zero attempts recorded
    attempts = ts.list_task_attempts(task_id)
    if attempts:
        t = Table(show_header=True, header_style="bold", title=f"Attempt History ({len(attempts)})")
        t.add_column("#", width=3)
        t.add_column("Outcome", width=9)
        t.add_column("Agent", width=14)
        t.add_column("Finished", width=22)
        t.add_column("Summary", width=55)
        _OUTCOME_COLORS = {"done": "green", "failed": "red", "blocked": "magenta"}
        for a in attempts:
            oc = _OUTCOME_COLORS.get(a.get("outcome", ""), "white")
            summary = (a.get("result_summary") or "")[:53]
            if len(a.get("result_summary") or "") > 53:
                summary += ".."
            t.add_row(
                str(a.get("attempt_num", "?")),
                f"[{oc}]{a.get('outcome', '')}[/{oc}]",
                a.get("agent") or "(none)",
                (a.get("finished_at") or "")[:19],
                summary,
            )
        console.print(t)


def _show_pending_facts(limit: int = 15) -> None:
    from atlas_plus.brain.task_facts import list_pending_facts
    facts = list_pending_facts(limit=limit)
    if not facts:
        console.print("[dim]No pending facts.[/dim]")
        return
    t = Table(show_header=True, header_style="bold", title=f"Pending Facts ({len(facts)})")
    t.add_column("ID", style="dim", width=10)
    t.add_column("Conf", width=6)
    t.add_column("Scope", width=22)
    t.add_column("Fact Text", width=60)
    for f in facts:
        short_id = f.id[:8]
        conf_color = "green" if f.confidence >= 0.7 else ("yellow" if f.confidence >= 0.5 else "red")
        t.add_row(
            f"[dim]{short_id}[/dim]",
            f"[{conf_color}]{f.confidence:.2f}[/{conf_color}]",
            f.scope_hint,
            f.fact_text[:58],
        )
    console.print(t)
    console.print("[dim]Use /approve <id-prefix> or /reject <id-prefix>[/dim]")


def _approve_fact(fact_id_prefix: str) -> None:
    from atlas_plus.brain.task_facts import list_pending_facts, approve_fact
    facts = list_pending_facts(limit=100)
    matches = [f for f in facts if f.id.startswith(fact_id_prefix)]
    if not matches:
        console.print(f"[red]No pending fact with ID starting '{fact_id_prefix}'[/red]")
        return
    if len(matches) > 1:
        console.print(f"[yellow]Ambiguous: {len(matches)} facts match '{fact_id_prefix}'. Be more specific.[/yellow]")
        return
    fact = matches[0]
    result = approve_fact(fact_id=fact.id, scope=fact.scope_hint)
    if result.get("ok"):
        console.print(f"[green]Approved[/green] {fact.id[:8]}: {fact.fact_text[:80]}")
        if result.get("file"):
            console.print(f"[dim]Written to: {result['file']}[/dim]")
    else:
        error = result.get("error", str(result))
        if "autonomous" in error.lower():
            console.print(
                f"[red]Approval failed:[/red] ATLAS_AUTONOMOUS is off. "
                f"Set ATLAS_AUTONOMOUS=on in the server environment and restart the MCP server."
            )
        else:
            console.print(f"[red]Approval failed: {error}[/red]")


def _reject_fact(fact_id_prefix: str) -> None:
    from atlas_plus.brain.task_facts import list_pending_facts, reject_fact
    facts = list_pending_facts(limit=100)
    matches = [f for f in facts if f.id.startswith(fact_id_prefix)]
    if not matches:
        console.print(f"[red]No pending fact with ID starting '{fact_id_prefix}'[/red]")
        return
    if len(matches) > 1:
        console.print(f"[yellow]Ambiguous: {len(matches)} facts match '{fact_id_prefix}'. Be more specific.[/yellow]")
        return
    fact = matches[0]
    result = reject_fact(fact_id=fact.id)
    if result.get("ok"):
        console.print(f"[dim]Rejected {fact.id[:8]}: {fact.fact_text[:80]}[/dim]")
    else:
        console.print(f"[red]Rejection failed: {result}[/red]")


def _search_memory(query: str) -> None:
    if not query.strip():
        console.print("[red]Usage: /search <query>[/red]")
        return
    try:
        from atlas_plus.mcp.memory_server.tools.search import memory_search
        results = memory_search(query, limit=8)
    except Exception as exc:
        console.print(f"[red]Search error: {exc}[/red]")
        return

    items = results if isinstance(results, list) else []

    if not items:
        console.print("[dim]No results.[/dim]")
        return

    # Compute confidence band from top score (scores are now normalized 0-1 after re-ranking)
    top_score = max((float(r.get("score", 0)) for r in items), default=0.0)
    if top_score >= 0.7:
        band, band_color = "high", "green"
    elif top_score >= 0.4:
        band, band_color = "medium", "cyan"
    elif top_score >= 0.2:
        band, band_color = "low", "yellow"
    else:
        band, band_color = "starved", "red"

    console.print(
        f"[dim]{len(items)} result(s)[/dim]  "
        f"[{band_color}][bold]{band}[/bold] confidence[/{band_color}]"
    )

    t = Table(show_header=True, header_style="bold", title=f"Memory Search: {query!r}")
    t.add_column("File", style="cyan", width=32)
    t.add_column("Score", width=7)
    t.add_column("Snippet", width=55)
    for item in items:
        path = item.get("path", "")
        # Show relative path from .memory/ if possible
        if ".memory" in path:
            path = path.split(".memory")[-1].lstrip("/\\")
        score = item.get("score", 0.0)
        snippet = item.get("snippet", item.get("content", ""))[:80].replace("\n", " ")
        t.add_row(path, f"{score:.2f}", snippet)
    console.print(t)


def _submit_task(instruction: str, agent: str = "brain", session_id: str = "cli-submit") -> None:
    """Queue a task to the Brain worker. Parses --agent <name> from instruction."""
    if not instruction.strip():
        console.print("[red]Usage: /submit [--agent <name>] <instruction>[/red]")
        return
    # Allow inline agent override: /submit --agent claude-code <instruction>
    parts = instruction.strip().split()
    if len(parts) >= 3 and parts[0] == "--agent":
        agent = parts[1]
        instruction = " ".join(parts[2:])
    try:
        from atlas_plus.brain.tasks import create_task
        task = create_task(instruction=instruction, agent=agent, session_id=session_id)
        console.print(
            f"[green]Queued[/green] task [dim]{task.task_id}[/dim] "
            f"(agent: {agent}): {instruction[:60]}"
        )
    except Exception as exc:
        console.print(f"[red]Failed to queue task: {exc}[/red]")


def _watch_task(task_id: str, poll_seconds: float = 2.0, timeout_seconds: int = 300) -> None:
    """Poll a task until it reaches a terminal state, printing live status."""
    import time
    from atlas_plus.brain.tasks import get_task

    console.print(f"[dim]Watching task [bold]{task_id}[/bold] (Ctrl+C to stop)...[/dim]")
    deadline = time.time() + timeout_seconds
    last_status = None

    while time.time() < deadline:
        task = get_task(task_id)
        if task is None:
            console.print(f"[red]Task {task_id!r} not found.[/red]")
            return

        if task.status != last_status:
            _STATUS_COLORS = {
                "done": "green", "running": "yellow", "failed": "red",
                "pending": "blue", "blocked": "magenta",
            }
            sc = _STATUS_COLORS.get(task.status, "white")
            console.print(f"  [{sc}]{task.status}[/{sc}]  (agent: {task.agent or '?'})")
            last_status = task.status

        if task.status in ("done", "failed", "blocked"):
            # Show result preview
            if task.result:
                preview = task.result[:800] + ("..." if len(task.result) > 800 else "")
                console.print(Panel(preview, title=f"Result ({task.status})", expand=False))
            elif task.status == "failed":
                console.print(f"[red]Task failed with no result text.[/red]")
            return

        try:
            time.sleep(poll_seconds)
        except KeyboardInterrupt:
            console.print("[dim]\nWatch interrupted.[/dim]")
            return

    console.print(f"[yellow]Watch timeout ({timeout_seconds}s) -- task still running.[/yellow]")


def _confirm_fact(path_arg: str) -> None:
    """Update last_confirmed_at for a memory file (confirms the fact is still valid)."""
    if not path_arg.strip():
        console.print("[red]Usage: /confirm <relative-path>  (e.g. /confirm feedback/archive-not-delete.md)[/red]")
        return
    try:
        from atlas_plus.mcp.memory_server.config import MEMORY_ROOT
        from atlas_plus.mcp.memory_server.routing import confirm_fact
        target = MEMORY_ROOT / path_arg.strip()
        if not target.exists():
            console.print(f"[red]File not found: {path_arg}[/red]")
            return
        ok = confirm_fact(target, source="cli")
        if ok:
            console.print(f"[green]Confirmed[/green] {path_arg}  (last_confirmed updated to today)")
        else:
            console.print(f"[yellow]Could not confirm {path_arg} (autonomous writes may be off)[/yellow]")
    except Exception as exc:
        console.print(f"[red]Confirm error: {exc}[/red]")


def _save_fact(arg: str) -> None:
    """Write a fact directly to .memory/ without going through pending facts pipeline.

    Usage: /save <scope> <fact text>
    Example: /save feedback Always use 4-space indentation in Python code.
    """
    parts = arg.strip().split(None, 1)
    if len(parts) < 2:
        console.print(
            "[red]Usage: /save <scope> <fact>[/red]\n"
            "[dim]Examples:\n"
            "  /save feedback Always use 4-space indentation in Python.\n"
            "  /save project:atlas-memory+ Brain uses APScheduler for nightly jobs.\n"
            "  /save reference:decision Chose SQLite over Postgres for portability.[/dim]"
        )
        return
    scope, fact = parts
    try:
        from atlas_plus.mcp.memory_server.tools.remember import memory_remember
        result = memory_remember(scope=scope, fact=fact, source="cli")
        error = result.get("error", "")
        if result.get("ok") is False or error:
            if "autonomous" in error.lower():
                console.print(
                    "[red]Save failed:[/red] ATLAS_AUTONOMOUS is off. "
                    "Set ATLAS_AUTONOMOUS=on in the server environment and restart the MCP server."
                )
            else:
                console.print(f"[red]Save failed: {error or result}[/red]")
        else:
            console.print(f"[green]Saved[/green] to {result.get('file', scope)}")
    except Exception as exc:
        console.print(f"[red]Save error: {exc}[/red]")


def _browse_memory(prefix: str = "") -> None:
    """List memory files, optionally filtered by subdirectory prefix."""
    try:
        from atlas_plus.mcp.memory_server.tools.list import memory_list
        items = memory_list(prefix=prefix, limit=50)
    except Exception as exc:
        console.print(f"[red]Browse error: {exc}[/red]")
        return

    if not items:
        scope_msg = f" in {prefix!r}" if prefix else ""
        console.print(f"[dim]No memory files found{scope_msg}.[/dim]")
        return

    t = Table(show_header=True, header_style="bold",
              title=f"Memory: {prefix or '(all)'} ({len(items)} files)")
    t.add_column("Path", style="cyan", width=38)
    t.add_column("Title", width=32)
    t.add_column("Type", width=12)
    t.add_column("Updated", width=12)
    for item in items:
        t.add_row(
            item.get("path", "")[:36],
            (item.get("title") or "")[:30],
            item.get("type", ""),
            (item.get("updated") or "")[:10],
        )
    console.print(t)
    if len(items) == 50:
        console.print("[dim](showing first 50 results)[/dim]")


def _show_agents() -> None:
    from atlas_plus.brain.agents import get_agents
    try:
        agents = get_agents(refresh=True)
    except Exception as exc:
        console.print(f"[red]Agent refresh error: {exc}[/red]")
        return
    t = Table(show_header=True, header_style="bold", title="Agent Registry")
    t.add_column("Name", width=14)
    t.add_column("Model", width=24)
    t.add_column("Online", width=8)
    t.add_column("Capabilities", width=40)
    for agent in agents:
        online_str = "[green]yes[/green]" if agent.online else "[red]no[/red]"
        caps = ", ".join(agent.capabilities[:4])
        t.add_row(agent.name, agent.model, online_str, caps)
    console.print(t)


def _show_escalation() -> None:
    esc = os.environ.get("ATLAS_CLAUDE_ESCALATION", "off").lower()
    qual_esc = os.environ.get("ATLAS_CLAUDE_QUALITY_ESCALATION", "off").lower()
    threshold = os.environ.get("ATLAS_CLAUDE_QUALITY_THRESHOLD", "3.5")

    esc_color = "green" if esc == "on" else "red"
    qual_color = "green" if qual_esc == "on" else "red"

    lines = [
        f"[bold]Failure escalation:[/bold]  [{esc_color}]{esc.upper()}[/{esc_color}]  (ATLAS_CLAUDE_ESCALATION)",
        f"[bold]Quality escalation:[/bold]  [{qual_color}]{qual_esc.upper()}[/{qual_color}]  (ATLAS_CLAUDE_QUALITY_ESCALATION)",
        f"[bold]Quality threshold:[/bold]   {threshold}/10  (ATLAS_CLAUDE_QUALITY_THRESHOLD)",
        "",
    ]
    if esc == "on":
        lines.append("[green]Claude Code will auto-receive failed/blocked tasks.[/green]")
    else:
        lines.append("[dim]Set ATLAS_CLAUDE_ESCALATION=on to activate automatic failure escalation.[/dim]")
    if qual_esc == "on":
        lines.append(f"[green]Claude Code reviews tasks scoring below {threshold}/10.[/green]")

    lines += [
        "",
        "[bold]Circuit breakers (always active):[/bold]",
        "  - claude-code / opencode / codex tasks never self-escalate",
        "  - session IDs starting 'escalated-' never escalate",
        "  - scheduler tasks (temporal_build, curation_report, etc.) never escalate",
    ]
    console.print(Panel("\n".join(lines), title="Claude Code Escalation", expand=False))


def _show_stats() -> None:
    """Show a quick memory and task stats summary."""
    lines = ["\033[1mAtlas Stats\033[0m"]
    try:
        from atlas_plus.brain.tasks import task_stats, list_tasks
        from atlas_plus.brain.task_facts import pending_count
        from atlas_plus.brain.history import session_count

        stats = task_stats(since_days=7)
        pf = pending_count()
        sessions = session_count()
        total = stats.get("total", 0)
        done = stats.get("done", 0)
        failed = stats.get("failed", 0)
        running = stats.get("running", 0)
        pending = stats.get("pending", 0)

        console.print(
            f"[bold]Tasks (7d):[/bold]  total={total}  done={done}  "
            f"failed={failed}  running={running}  pending={pending}"
        )
        # Exhausted count: failed/blocked with no retries left -- show with warning if non-zero
        try:
            from atlas_plus.brain.tasks import list_exhausted_tasks
            exhausted = len(list_exhausted_tasks(limit=50))
            if exhausted > 0:
                console.print(f"[bold]Exhausted tasks:[/bold]  [yellow]{exhausted}[/yellow]  (use /exhausted to see them)")
        except Exception:
            pass
        console.print(f"[bold]Facts pending:[/bold]  {pf}")
        console.print(f"[bold]Brain sessions:[/bold]  {sessions}")
    except Exception as exc:
        console.print(f"[red]Stats error: {exc}[/red]")

    try:
        from atlas_plus.mcp.memory_server import fts as _fts
        from atlas_plus.mcp.memory_server.config import FTS_DB
        import sqlite3
        if FTS_DB.exists():
            conn = sqlite3.connect(str(FTS_DB))
            row = conn.execute("SELECT COUNT(*) FROM memory_files").fetchone()
            conn.close()
            console.print(f"[bold]Memory files indexed:[/bold]  {row[0] if row else '?'}")
    except Exception:
        pass

    try:
        from atlas_plus.doctor.cli import check_research_quality
        rq = check_research_quality()
        if rq and rq.get("total_reports", 0) > 0:
            avg = rq.get("avg_score", 0.0)
            avg_color = "green" if avg >= 0.7 else ("yellow" if avg >= 0.4 else "red")
            scored = rq.get("scored_reports", 0)
            total_r = rq.get("total_reports", 0)
            low = rq.get("low_quality_count", 0)
            console.print(
                f"[bold]Research reports:[/bold]  {total_r} total  "
                f"{scored} scored  avg: [{avg_color}]{avg:.2f}[/{avg_color}]"
                + (f"  [yellow]low: {low}[/yellow]" if low > 0 else "")
            )
    except Exception:
        pass


def _show_topics(limit_arg: str = "") -> None:
    """Show tag cloud from .memory/ frontmatter."""
    try:
        limit = max(1, min(100, int(limit_arg))) if limit_arg.strip().isdigit() else 25
        from atlas_plus.web.app import _build_topic_index
        from atlas_plus.mcp.memory_server.config import MEMORY_ROOT as _MEM_ROOT
        index = _build_topic_index(_MEM_ROOT)
    except Exception as exc:
        console.print(f"[red]Topics error: {exc}[/red]")
        return

    if not index:
        console.print("[dim]No tags found in .memory/ frontmatter.[/dim]")
        console.print("[dim]Add 'tags:' to memory file frontmatter to enable topic tracking.[/dim]")
        return

    items = list(index.items())[:limit]
    t = Table(show_header=True, header_style="bold",
              title=f"Memory Topics (top {len(items)} of {len(index)})")
    t.add_column("Tag", style="cyan", width=30)
    t.add_column("Files", width=8)
    t.add_column("Sample paths", width=50)
    for tag, paths in items:
        sample = ", ".join(p.split("/")[-1].replace(".md", "") for p in paths[:3])
        t.add_row(tag, str(len(paths)), sample)
    console.print(t)
    total_tagged = len({p for ps in index.values() for p in ps})
    console.print(f"[dim]{total_tagged} files tagged out of all .memory/ files[/dim]")


def _show_decay(days_arg: str = "") -> None:
    """Run unconfirmed decay dry-run and show candidates."""
    try:
        days = max(1, int(days_arg)) if days_arg.strip().isdigit() else 90
    except ValueError:
        days = 90
    try:
        from atlas_plus.mcp.memory_server.routing import downgrade_stale_confirmations
        candidates = downgrade_stale_confirmations(threshold_days=days, dry_run=True)
    except Exception as exc:
        console.print(f"[red]Decay check error: {exc}[/red]")
        return

    if not candidates:
        console.print(f"[green]No stale confirmed facts[/green] (threshold: {days}d)")
        return

    t = Table(show_header=True, header_style="bold",
              title=f"Stale Confirmed Facts (>{days}d, dry-run)")
    t.add_column("File", style="cyan", width=40)
    t.add_column("Was", width=10)
    t.add_column("Last confirmed", width=14)
    t.add_column("Age", width=8)
    for c in candidates[:20]:
        t.add_row(c["path"][-38:], c["old_confidence"], c["last_confirmed"], f"{c['age_days']}d")
    if len(candidates) > 20:
        console.print(f"[dim]Showing first 20 of {len(candidates)}. Run --write to apply all.[/dim]")
    console.print(t)
    console.print(
        f"[yellow]{len(candidates)} file(s) would be downgraded.[/yellow]  "
        f"Run: atlas-review-memory unconfirmed-decay --write"
    )


def _show_digest(task_id: str) -> None:
    """Show the AI-generated result digest for a completed task."""
    if not task_id.strip():
        console.print("[red]Usage: /digest <task-id>[/red]")
        return
    from atlas_plus.brain.tasks import get_task
    task = get_task(task_id.strip())
    if task is None:
        console.print(f"[red]Task not found: {task_id}[/red]")
        return
    if task.status != "done":
        console.print(f"[yellow]Task {task_id[:16]} status is '{task.status}' -- digest only available for done tasks.[/yellow]")
        return

    digest = getattr(task, "result_digest", None)
    if digest:
        console.print(f"[bold cyan]Digest:[/bold cyan] {digest}")
    else:
        console.print(f"[dim]No digest for {task_id[:16]}.[/dim]")
        if task.result:
            console.print(f"[dim]Enable ATLAS_DIGEST_RESULTS=on to auto-generate digests.[/dim]")
            console.print(f"[dim]Result preview: {task.result[:200]}{'...' if len(task.result) > 200 else ''}[/dim]")


def _show_ticket_status(ticket_id: str) -> None:
    """Show a ticket's current state plus its linked Brain task (if dispatched)."""
    if not ticket_id.strip():
        console.print("[red]Usage: /ticket-status <TICKET-ID>[/red]")
        return
    ticket_id = ticket_id.strip().upper()

    # Show ticket info
    try:
        from atlas_plus.tickets.store import list_tickets
        tickets = [t for t in list_tickets() if t.id.upper() == ticket_id]
        if not tickets:
            console.print(f"[red]Ticket {ticket_id} not found.[/red]")
            return
        t = tickets[0]
        _status_colors = {
            "todo": "yellow", "in-progress": "cyan", "done": "green",
            "blocked": "red", "archived": "dim",
        }
        sc = _status_colors.get(t.status, "white")
        reopen_count = getattr(t, "reopen_count", 0)
        reopen_str = ""
        if reopen_count > 0:
            rc_color = "red" if reopen_count >= 3 else "yellow"
            reopen_str = f"  [bold {rc_color}]reopened {reopen_count}x[/bold {rc_color}]"
        console.print(f"\n[bold]Ticket {t.id}[/bold]  [{sc}]{t.status}[/{sc}]  (assignee: {t.assignee}  priority: {t.priority}){reopen_str}")
        console.print(f"  {t.title}")
        if t.body:
            preview = t.body[:120].replace("\n", " ")
            console.print(f"  [dim]{preview}{'...' if len(t.body) > 120 else ''}[/dim]")
    except Exception as exc:
        console.print(f"[red]Ticket lookup failed: {exc}[/red]")
        return

    # Find linked Brain task via session_id="ticket-{ID}" pattern
    try:
        from atlas_plus.brain import tasks as task_store
        session_id = f"ticket-{ticket_id}"
        all_tasks = task_store.list_tasks(limit=50) + task_store.list_tasks(status="running", limit=50)
        all_tasks += task_store.list_tasks(status="done", limit=20) + task_store.list_tasks(status="failed", limit=20)
        linked = [t for t in all_tasks if t.session_id == session_id]

        if not linked:
            console.print(f"\n  [dim]No Brain task found for this ticket.[/dim]")
            if t.status == "todo" and t.assignee in {"claude-code", "local", "any", "codex"}:
                console.print(f"  [dim]Run /dispatch-tickets to queue it.[/dim]")
        else:
            # Show most recent task
            task = sorted(linked, key=lambda x: x.created_at, reverse=True)[0]
            _task_colors = {"pending": "yellow", "running": "cyan", "done": "green", "failed": "red", "blocked": "red"}
            tc = _task_colors.get(task.status, "white")
            console.print(f"\n  Brain task: [dim]{task.task_id[:20]}[/dim]  [{tc}]{task.status}[/{tc}]")
            if task.agent:
                console.print(f"  Agent: {task.agent}")
            if task.started_at:
                console.print(f"  Started: {task.started_at[:19]}")
            if task.finished_at:
                console.print(f"  Finished: {task.finished_at[:19]}")
            if task.result_digest:
                console.print(f"  [bold cyan]Digest:[/bold cyan] {task.result_digest}")
            elif task.result and task.status == "done":
                console.print(f"  Result: {task.result[:150]}{'...' if len(task.result or '') > 150 else ''}")
            if task.error:
                console.print(f"  [red]Error: {task.error[:150]}[/red]")
    except Exception as exc:
        console.print(f"[red]Task lookup failed: {exc}[/red]")
    console.print("")


def _write_session_log(arg: str) -> None:
    """Write today's session log from git commits. Usage: /session-log [--dry-run]."""
    dry_run = "--dry-run" in arg or arg.strip() == "dry"
    try:
        from atlas_plus.sprint_summary import write_session_log
        result = write_session_log(since="today", dry_run=dry_run)
    except Exception as exc:
        console.print(f"[red]Session log error: {exc}[/red]")
        return

    if not result.get("ok"):
        console.print(f"[red]Session log failed: {result.get('error', 'unknown')}[/red]")
        return

    if not result.get("entry"):
        console.print("[yellow]No commits found today -- nothing to write.[/yellow]")
        return

    if dry_run:
        console.print("[cyan]DRY RUN -- would write:[/cyan]")
        console.print(result["entry"])
    else:
        paths = result.get("paths_written", [])
        console.print(f"[green]Session log written ({len(paths)} file(s)):[/green]")
        for p in paths:
            console.print(f"  {p}")
        console.print("")
        console.print(result["entry"].split("\n")[0])


_PRIORITY_ORDER = {"critical": 0, "high": 1, "normal": 2, "low": 3}

_DISPATCH_ELIGIBLE_ASSIGNEES = {"any", "brain", "claude-code", "codex", "opencode"}


def _show_plan(limit: int = 10) -> None:
    """Show the next N dispatch-eligible todo tickets ranked by priority.

    This answers "what will the automation loop work on next?" -- useful for
    deciding whether to enable ATLAS_TICKET_DISPATCH or manually /dispatch-tickets.
    """
    try:
        from atlas_plus.tickets.store import list_tickets
        from atlas_plus.brain.dispatcher import is_dispatch_enabled
        tickets = list_tickets()
    except Exception as exc:
        console.print(f"[red]Plan unavailable: {exc}[/red]")
        return

    dispatch_on = is_dispatch_enabled()
    todo = [t for t in tickets if t.status == "todo"]
    eligible = [t for t in todo if getattr(t, "assignee", "human") in _DISPATCH_ELIGIBLE_ASSIGNEES]
    human = [t for t in todo if getattr(t, "assignee", "human") not in _DISPATCH_ELIGIBLE_ASSIGNEES]

    eligible_sorted = sorted(
        eligible,
        key=lambda t: (_PRIORITY_ORDER.get(getattr(t, "priority", "normal"), 2), t.id),
    )

    dispatch_color = "green" if dispatch_on else "yellow"
    dispatch_label = "on" if dispatch_on else "off (ATLAS_TICKET_DISPATCH=on to enable)"
    console.print(f"[bold]Automation Plan[/bold]  dispatcher: [{dispatch_color}]{dispatch_label}[/{dispatch_color}]")
    console.print(f"  {len(todo)} todo  ({len(eligible)} dispatch-eligible  {len(human)} human-only)")
    console.print("")

    if not eligible_sorted:
        console.print("[dim]No dispatch-eligible todo tickets.[/dim]")
        if human:
            console.print(f"[dim]{len(human)} human-assigned tickets require manual work.[/dim]")
        return

    t = Table(show_header=True, header_style="bold", title=f"Next {min(limit, len(eligible_sorted))} Dispatch-Eligible")
    t.add_column("ID", style="dim", width=12)
    t.add_column("P", width=8)
    t.add_column("Agent", width=14)
    t.add_column("Title", width=55)
    _PC = {"critical": "red", "high": "yellow", "normal": "green", "low": "dim"}
    for tk in eligible_sorted[:limit]:
        pri = getattr(tk, "priority", "normal")
        pc = _PC.get(pri, "white")
        agent = getattr(tk, "assignee", "any")
        t.add_row(tk.id, f"[{pc}]{pri}[/{pc}]", agent, (tk.title or "")[:53])
    console.print(t)

    if len(eligible_sorted) > limit:
        console.print(f"[dim]... and {len(eligible_sorted) - limit} more eligible tickets.[/dim]")

    if not dispatch_on:
        console.print("[dim]Run: ATLAS_TICKET_DISPATCH=on and /dispatch-tickets to queue these now.[/dim]")
    console.print("")


def _show_loop_status() -> None:
    """Show automation loop health: dispatch stats, ticket counts, reopened tickets."""
    console.print("[bold cyan]Automation Loop Status[/bold cyan]")

    # Dispatch stats
    try:
        from atlas_plus.brain.dispatcher import get_dispatch_stats
        import datetime
        stats = get_dispatch_stats()
        enabled = stats.get("enabled", False)
        in_flight = stats.get("in_flight", 0)
        total_disp = stats.get("total_dispatched", 0)
        last_run = stats.get("last_run_at")
        state_color = "green" if enabled else "yellow"
        state_str = "on" if enabled else "off (ATLAS_TICKET_DISPATCH=on to enable)"
        console.print(f"  Dispatcher: [{state_color}]{state_str}[/{state_color}]")
        if enabled:
            console.print(f"  In-flight: {in_flight}  Total dispatched (session): {total_disp}")
        if last_run:
            try:
                run_dt = datetime.datetime.fromisoformat(last_run)
                age_min = int((datetime.datetime.now(datetime.timezone.utc) - run_dt).total_seconds() / 60)
                console.print(f"  Last dispatch run: {age_min}m ago")
            except Exception:
                pass
    except Exception as exc:
        console.print(f"  [red]Dispatch stats unavailable: {exc}[/red]")

    # Ticket health
    try:
        from atlas_plus.tickets.store import list_tickets
        tickets = list_tickets()
        todo = sum(1 for t in tickets if t.status == "todo")
        in_prog = sum(1 for t in tickets if t.status == "in-progress")
        blocked = sum(1 for t in tickets if t.status == "blocked")
        console.print(f"  Tickets: [cyan]{todo}[/cyan] todo  [yellow]{in_prog}[/yellow] in-progress  [orange1]{blocked}[/orange1] blocked")
        reopened = sorted(
            [t for t in tickets if getattr(t, "reopen_count", 0) > 0],
            key=lambda t: getattr(t, "reopen_count", 0), reverse=True
        )
        if reopened:
            console.print(f"  [yellow]High reopen tickets ({len(reopened)}):[/yellow]")
            for t in reopened[:3]:
                rc_color = "red" if t.reopen_count >= 3 else "yellow"
                console.print(f"    [{rc_color}]{t.id} x{t.reopen_count}[/{rc_color}]  {t.title[:50]}")
    except Exception as exc:
        console.print(f"  [red]Ticket info unavailable: {exc}[/red]")

    console.print("")


def _show_orphans(arg: str = "") -> None:
    """List memory orphan candidates (unreferenced, low health, old)."""
    import os
    from pathlib import Path as _Path

    try:
        limit = max(1, min(100, int(arg.strip()))) if arg.strip().isdigit() else 15
    except ValueError:
        limit = 15

    try:
        from atlas_plus.mcp.memory_server.config import MEMORY_ROOT
        from atlas_plus.curation.orphan import find_orphan_candidates
        mem_root = _Path(os.environ.get("ATLAS_MEMORY_ROOT", str(MEMORY_ROOT)))
        results = find_orphan_candidates(mem_root, min_age_days=180, max_health=0.5, limit=limit)
    except Exception as exc:
        console.print(f"[red]Orphan scan failed: {exc}[/red]")
        return

    if not results:
        console.print("[green]No orphan candidates found.[/green]  (age>180d, health<=0.50, 0 inbound refs)")
        return

    from rich.table import Table
    t = Table(show_header=True, header_style="bold", show_lines=False)
    t.add_column("Path", style="dim", no_wrap=True, max_width=45)
    t.add_column("Age", justify="right")
    t.add_column("Health", justify="right")
    t.add_column("Issues", style="dim")

    for r in results:
        h = r["score"]
        h_color = "red" if h <= 0.0 else ("yellow" if h < 0.5 else "white")
        issues_str = ", ".join(r["issues"][:2])
        t.add_row(
            r["path"],
            f"{r['age_days']}d",
            f"[{h_color}]{h:.2f}[/{h_color}]",
            issues_str,
        )

    console.print(t)
    console.print(f"\n  [yellow]{len(results)} orphan candidate(s)[/yellow]  -- archive with: atlas-review-memory archive <path>")
    console.print("")


def _queue_verify_for_ticket(ticket_id_arg: str) -> None:
    """Queue a kind=verify task for a specific ticket (manual trigger).

    Parses ticket_id_arg as a ticket ID (e.g. ATL-007 or just 007).
    Creates a verify task with session_id="verify-ticket-{ID}" so worker
    failure -> ticket reopen logic is triggered if pytest fails.
    """
    ticket_id = ticket_id_arg.strip().upper()
    if not ticket_id:
        console.print("[red]Usage: /verify <ticket-id>  (e.g. /verify ATL-007)[/red]")
        return

    # Normalize: if user typed just "007", try to add a prefix
    import re as _re
    if not _re.match(r"^[A-Z]{1,8}-\d+$", ticket_id):
        console.print(f"[red]Invalid ticket ID format: {ticket_id!r}. Expected e.g. ATL-007[/red]")
        return

    # Check ticket exists
    try:
        from atlas_plus.tickets.store import list_tickets
        ticket = next((t for t in list_tickets() if t.id == ticket_id), None)
        if not ticket:
            console.print(f"[red]Ticket {ticket_id} not found.[/red]")
            return
        status_color = {"done": "green", "in-progress": "yellow", "blocked": "orange", "todo": "cyan"}.get(
            ticket.status, "white"
        )
        console.print(
            f"  Ticket [{status_color}]{ticket.id}[/{status_color}] -- [{status_color}]{ticket.status}[/{status_color}]"
            f"  {ticket.title[:60]}"
        )
    except Exception as exc:
        console.print(f"[yellow]Warning: could not verify ticket exists: {exc}[/yellow]")

    # Queue the verify task
    try:
        from . import tasks as task_store
        session_id = f"verify-ticket-{ticket_id}"
        task = task_store.create_task(
            instruction=f"Verify ticket {ticket_id}: run full test suite",
            session_id=session_id,
            kind="verify",
            priority=6,
        )
        console.print(
            f"  [bold green]Verify task queued:[/bold green]  {task.task_id[:24]}  "
            f"session_id=[dim]{session_id}[/dim]"
        )
        console.print(
            f"  [dim]Use /watch {task.task_id[:24]} to follow progress.[/dim]"
        )
    except Exception as exc:
        console.print(f"[red]Failed to queue verify task: {exc}[/red]")


def _dispatch_tickets_now() -> None:
    """Run the ticket dispatcher immediately -- creates Brain tasks from eligible todo tickets."""
    try:
        from atlas_plus.brain.dispatcher import dispatch_all_eligible
        dispatched = dispatch_all_eligible()
    except Exception as exc:
        console.print(f"[red]Dispatch failed: {exc}[/red]")
        return

    if not dispatched:
        console.print("[dim]No eligible todo tickets found (assignee=claude-code|local|any|codex).[/dim]")
        return

    console.print(f"[bold green]Dispatched {len(dispatched)} ticket(s):[/bold green]")
    for d in dispatched:
        agent_str = d.get("agent", "auto")
        console.print(f"  [cyan]{d['ticket_id']}[/cyan]  ->  task: [dim]{d['task_id'][:20]}[/dim]  agent: {agent_str}")


def _show_health() -> None:
    """Compact system health summary -- services, worker, memory quality, pending facts."""
    console.print("[bold cyan]Atlas Health[/bold cyan]")

    # Services (parallel, 3s timeout)
    try:
        from atlas_plus.doctor.cli import check_services, check_worker_status, check_memory_quality
        from concurrent.futures import ThreadPoolExecutor, as_completed

        with ThreadPoolExecutor(max_workers=2) as ex:
            f_svc = ex.submit(check_services)
            f_wk  = ex.submit(check_worker_status)
            services = f_svc.result(timeout=5)
            worker   = f_wk.result(timeout=5)

        # Services line
        _up   = sum(1 for s in services if s.get("status") == "up")
        _down = [s["name"] for s in services if s.get("status") != "up"]
        svc_color = "green" if not _down else "red"
        down_str = f"  [red]down: {', '.join(_down)}[/red]" if _down else ""
        console.print(f"  Services: [{svc_color}]{_up}/{len(services)} up[/{svc_color}]{down_str}")

        # Worker line
        assessment = worker.get("assessment", "unknown")
        _colors = {"ok": "green", "idle": "cyan", "warning": "yellow", "stale": "red", "unknown": "yellow"}
        w_color = _colors.get(assessment, "yellow")
        pending_w = worker.get("pending", "?")
        console.print(f"  Worker: [{w_color}]{assessment}[/{w_color}]  pending: {pending_w}")
    except Exception as exc:
        console.print(f"  [red]Service/worker check failed: {exc}[/red]")

    # Memory quality (fast local scan)
    try:
        from atlas_plus.doctor.cli import check_memory_quality
        mq = check_memory_quality()
        total = mq.get("total_files", 0)
        conf_pct = mq.get("confirmed_pct", 0)
        tag_pct = mq.get("tagged_pct", 0)
        untagged = mq.get("untagged_files", 0)
        conf_color = "green" if conf_pct >= 60 else ("yellow" if conf_pct >= 30 else "red")
        console.print(
            f"  Memory: {total} files  confirmed: [{conf_color}]{conf_pct}%[/{conf_color}]"
            f"  tagged: {tag_pct}%"
            + (f"  [dim]untagged: {untagged}[/dim]" if untagged > 10 else "")
        )
    except Exception:
        pass

    # Pending facts
    try:
        from atlas_plus.brain.task_facts import pending_count
        n = pending_count()
        if n > 0:
            console.print(f"  [yellow]Pending facts: {n} awaiting review[/yellow]  (/pending to review)")
    except Exception:
        pass

    # Research quality summary
    try:
        from atlas_plus.doctor.cli import check_research_quality
        rq = check_research_quality()
        if rq and rq.get("total_reports", 0) > 0:
            avg = rq.get("avg_score", 0.0)
            low = rq.get("low_quality_count", 0)
            scored = rq.get("scored_reports", 0)
            total_r = rq.get("total_reports", 0)
            avg_color = "green" if avg >= 0.7 else ("yellow" if avg >= 0.4 else "red")
            console.print(
                f"  Research: {total_r} reports  {scored} scored  avg: [{avg_color}]{avg:.2f}[/{avg_color}]"
                + (f"  [yellow]low: {low}[/yellow]" if low > 0 else "")
            )
    except Exception:
        pass

    # Next actions hint (fast checks only: decay + gaps + temporal staleness)
    try:
        from atlas_plus.curation.decay import find_decayed_facts
        from atlas_plus.mcp.memory_server.tools.search_log import top_miss_queries
        decayed = len(find_decayed_facts(threshold_days=0))
        gaps = len(top_miss_queries(since_hours=168, limit=20))
        if decayed > 0 or gaps > 0:
            parts = []
            if decayed > 0:
                parts.append(f"[red]{decayed} decayed[/red]")
            if gaps > 0:
                parts.append(f"[yellow]{gaps} gap(s)[/yellow]")
            console.print(f"  Attention: {', '.join(parts)}  [dim](run /suggest-facts or atlas-review-memory next-actions)[/dim]")
    except Exception:
        pass

    try:
        from atlas_plus.doctor.cli import check_temporal
        tg = check_temporal()
        if tg.get("stale"):
            age = tg.get("age_days")
            age_str = f" ({age}d old)" if age else " (no manifest)"
            console.print(f"  [yellow]Temporal graph stale{age_str}.[/yellow]  [dim](run: atlas-temporal build --source all)[/dim]")
    except Exception:
        pass

    console.print("")


def _show_research_index(arg: str = "") -> None:
    """List research reports sorted by date (newest first), with quality scores."""
    import re
    import yaml as _yaml
    import os
    from pathlib import Path as _Path

    try:
        limit = max(1, min(200, int(arg.strip()))) if arg.strip().isdigit() else 20
    except ValueError:
        limit = 20

    atlas_dir = atlas_paths.state_dir()
    reports_dir = atlas_dir / "reports"
    if not reports_dir.exists():
        console.print("[dim]No .atlas/reports/ directory found.[/dim]")
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

        title = str(fm.get("title", md_file.stem)).replace("Research: ", "").replace('"', "").strip()[:50]
        date_str = str(fm.get("date") or fm.get("report_date") or "")[:10]
        qs = fm.get("quality_score")
        refs = fm.get("references") or []
        cite_count = len(refs) if isinstance(refs, list) else 0
        body = raw[end + 4:]
        words = len(re.findall(r"\b\w+\b", body))
        from atlas_plus.brain.research import compute_freshness_score
        freshness = compute_freshness_score(date_str)
        gate_passed = fm.get("quality_gate_passed")  # None=unchecked, True/False
        entries.append((date_str, title, qs, cite_count, words, freshness, gate_passed))

    if not entries:
        console.print("[dim]No research reports found.[/dim]")
        return

    console.print(f"[bold]Research Index[/bold]  (newest {min(limit, len(entries))} of {len(entries)} reports)\n")
    for date_str, title, qs, cites, words, fresh, gate in entries[:limit]:
        if qs is None:
            score_str = "[dim]--[/dim]"
        else:
            color = "green" if qs >= 0.7 else ("yellow" if qs >= 0.4 else "red")
            score_str = f"[{color}]{qs:.2f}[/{color}]"
        if fresh is None:
            fresh_str = "[dim]--[/dim]"
        else:
            f_color = "green" if fresh >= 0.75 else ("yellow" if fresh >= 0.4 else "red")
            fresh_str = f"[{f_color}]{fresh:.2f}[/{f_color}]"
        cite_str = f"  cite:{cites}" if cites > 0 else ""
        words_str = f"  {words}w" if words > 0 else ""
        gate_str = "  [red]![/red]" if gate is False else ""
        console.print(f"  {date_str or '----'}  q:{score_str}  f:{fresh_str}  [cyan]{title}[/cyan]{cite_str}{words_str}{gate_str}")

    scored = sum(1 for entry in entries if entry[2] is not None)
    gate_failed = sum(1 for entry in entries if entry[6] is False)
    if gate_failed > 0:
        console.print(f"[dim]{gate_failed} report(s) marked [red]![/red] (quality gate failed). Run: atlas-review-memory quality --write[/dim]")
    if scored < len(entries):
        console.print(f"\n[dim]{len(entries) - scored} report(s) unscored. Run: atlas-review-memory quality --write[/dim]")


def _queue_gap_report(arg: str = "") -> None:
    """Queue a research_gap Brain task for a topic, then optionally watch it.

    Usage: /gap-report <topic>           -- queue and print task ID
           /gap-report --watch <topic>   -- queue and watch until done (max 5 min)

    Creates kind=research_gap task with instruction 'Research gap: <topic>'.
    The task calls run_research() via the deterministic executor, writes to
    .atlas/reports/, and closes any linked ticket.
    """
    watch = False
    topic = arg.strip()
    if topic.startswith("--watch"):
        watch = True
        topic = topic[len("--watch"):].strip()
    if not topic:
        console.print("[red]Usage: /gap-report [--watch] <topic>[/red]")
        return

    instruction = f"Research gap: {topic}"
    try:
        from atlas_plus.brain.tasks import create_task
        task = create_task(
            instruction=instruction,
            kind="research_gap",
            session_id="cli-gap-report",
        )
        console.print(
            f"[green]Queued[/green] research_gap task [dim]{task.task_id}[/dim]\n"
            f"  Topic: {topic}\n"
            f"  Monitor: [dim]/watch {task.task_id}[/dim] or [dim]/task {task.task_id}[/dim]"
        )
        if watch:
            _watch_task(task.task_id, timeout_seconds=300)
    except Exception as exc:
        console.print(f"[red]Failed to queue gap report: {exc}[/red]")


def _queue_research_bulk(arg: str = "") -> None:
    """Queue multiple research_gap tasks from a topics file or inline list.

    Usage:
      /queue-research topics.txt          -- read topics from file (one per line)
      /queue-research "SIP REGISTER\\nRTP media\\nDTMF RFC 4733"  -- inline topics

    Each non-empty, non-comment line becomes one research_gap task.
    Comment lines start with '#'. Duplicate topics are skipped.
    """
    arg = arg.strip()
    if not arg:
        console.print(
            "[red]Usage: /queue-research <file_or_topics>[/red]\n"
            "  /queue-research topics.txt         -- one topic per line\n"
            "  /queue-research 'SIP REGISTER'     -- single topic inline"
        )
        return

    from pathlib import Path
    topic_path = Path(arg)
    if topic_path.exists() and topic_path.is_file():
        raw = topic_path.read_text(encoding="utf-8", errors="replace")
    else:
        raw = arg.replace("\\n", "\n")

    topics = [
        line.strip() for line in raw.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    if not topics:
        console.print("[yellow]No topics found (empty file or all comments).[/yellow]")
        return

    seen: set[str] = set()
    unique_topics = []
    for t in topics:
        key = t.lower()
        if key not in seen:
            seen.add(key)
            unique_topics.append(t)

    console.print(f"[dim]Queueing {len(unique_topics)} research_gap task(s)...[/dim]")
    queued = []
    failed = []
    try:
        from atlas_plus.brain.tasks import create_task
        for topic in unique_topics:
            try:
                task = create_task(
                    instruction=f"Research gap: {topic}",
                    kind="research_gap",
                    session_id="cli-queue-research",
                )
                queued.append((topic, task.task_id))
            except Exception as exc:
                failed.append((topic, str(exc)))
    except Exception as exc:
        console.print(f"[red]Failed to access task store: {exc}[/red]")
        return

    for topic, tid in queued:
        console.print(f"  [green]Queued[/green] [dim]{tid}[/dim]  {topic}")
    for topic, err in failed:
        console.print(f"  [red]Failed[/red] {topic}: {err}")
    console.print(
        f"\n[bold]{len(queued)} queued[/bold], {len(failed)} failed. "
        f"Use [dim]/tasks[/dim] or [dim]/research-gaps[/dim] to monitor."
    )


def _suggest_tags_scan(arg: str = "") -> None:
    """Scan .memory/ for untagged files and suggest or apply tags.

    Usage:
      /suggest-tags           -- dry-run: list files + suggested tags, no writes
      /suggest-tags --write   -- inject tags into frontmatter (requires ATLAS_AUTONOMOUS=on)

    Uses the deterministic tag suggester (directory, type:, title keywords).
    """
    import os
    write = "--write" in (arg or "")

    try:
        from atlas_plus.mcp.memory_server.config import MEMORY_ROOT
        from atlas_plus.curation.tag_suggest import suggest_tags_for_file, inject_tags

        candidates = []
        for md_file in sorted(MEMORY_ROOT.rglob("*.md")):
            rel_parts = md_file.relative_to(MEMORY_ROOT).parts
            if rel_parts and rel_parts[0] in {"archive", "_sync", "versions", "sessions"}:
                continue
            tags = suggest_tags_for_file(md_file, memory_root=MEMORY_ROOT, max_tags=4)
            if tags:
                candidates.append((md_file, tags))

    except Exception as exc:
        console.print(f"[red]Tag scan failed: {exc}[/red]")
        return

    if not candidates:
        console.print("[green]No untagged files found that could use tags.[/green]")
        return

    if write:
        from atlas_plus.autonomous import require_autonomous_writes
        try:
            require_autonomous_writes("suggest_tags")
        except RuntimeError as exc:
            console.print(f"[red]{exc}[/red]")
            return

    from rich.table import Table
    t = Table(show_header=True, header_style="bold", show_lines=False)
    t.add_column("File", style="dim", max_width=45)
    t.add_column("Suggested Tags")
    t.add_column("Status", justify="right")

    applied = skipped = 0
    for md_file, tags in candidates[:30]:  # cap display at 30
        rel = str(md_file.relative_to(MEMORY_ROOT)).replace("\\", "/")
        tags_str = ", ".join(tags)
        if write:
            ok = inject_tags(md_file, tags)
            status = "[green]applied[/green]" if ok else "[dim]skipped[/dim]"
            if ok:
                applied += 1
            else:
                skipped += 1
        else:
            status = "[dim]dry-run[/dim]"
        t.add_row(rel, tags_str, status)

    console.print(t)
    if write:
        total = len(candidates)
        console.print(
            f"\n[bold]{applied} applied[/bold], {skipped} skipped "
            f"(already tagged or write failed). {max(0, total - 30)} more not shown."
        )
    else:
        console.print(
            f"\n[dim]{len(candidates)} untagged file(s). Add [bold]--write[/bold] to apply tags.[/dim]"
        )


def _suggest_facts_run(arg: str = "") -> None:
    """Scan today's session transcript and propose memory facts.

    Usage:
      /suggest-facts                        -- dry-run: show candidates, write nothing
      /suggest-facts --auto                 -- auto-approve facts with confidence >= 0.95
      /suggest-facts --since 4h             -- scan last 4 hours
      /suggest-facts --since today          -- scan today (default)
      /suggest-facts --skip-similar 0.6     -- skip candidates already covered in memory
      /suggest-facts --threshold 0.8        -- override confidence threshold (default 0.70)

    Requires ATLAS_AUTONOMOUS=on for --auto mode.
    """
    since = "today"
    auto = False
    skip_similar = 0.0
    threshold: float | None = None

    tokens = (arg or "").split()
    i = 0
    while i < len(tokens):
        if tokens[i] == "--auto":
            auto = True
        elif tokens[i] == "--since" and i + 1 < len(tokens):
            since = tokens[i + 1]
            i += 1
        elif tokens[i] == "--skip-similar" and i + 1 < len(tokens):
            try:
                skip_similar = float(tokens[i + 1])
            except ValueError:
                console.print(f"[red]--skip-similar expects a float (e.g. 0.6), got {tokens[i+1]!r}[/red]")
                return
            i += 1
        elif tokens[i] == "--threshold" and i + 1 < len(tokens):
            try:
                threshold = float(tokens[i + 1])
            except ValueError:
                console.print(f"[red]--threshold expects a float (e.g. 0.8), got {tokens[i+1]!r}[/red]")
                return
            i += 1
        i += 1

    if auto:
        from atlas_plus.autonomous import require_autonomous_writes
        try:
            require_autonomous_writes("suggest_facts")
        except RuntimeError as exc:
            console.print(f"[red]{exc}[/red]")
            return

    try:
        from atlas_plus.curation.suggest_facts import suggest_facts
        stats = suggest_facts(
            since=since,
            source="claude-code",
            dry_run=not auto,
            auto=auto,
            skip_similar_threshold=skip_similar,
            confidence_threshold=threshold,
        )
    except RuntimeError as exc:
        console.print(f"[red]Suggest-facts error: {exc}[/red]")
        return

    candidates = stats.get("candidates", 0)
    approved = stats.get("approved", 0)
    written = stats.get("written", 0)
    rejected_lc = stats.get("rejected_low_confidence", 0)

    if candidates == 0:
        if rejected_lc > 0:
            console.print(
                f"[green]No qualifying fact candidates[/green]  "
                f"[dim](rejected {rejected_lc} low-confidence)[/dim]"
            )
        else:
            console.print("[green]No fact candidates found in recent transcript.[/green]")
        return

    skip_note = f", skip-similar>={skip_similar:.2f}" if skip_similar > 0.0 else ""
    mode = f"--auto applied{skip_note}" if auto else f"dry-run (add --auto to write approved facts){skip_note}"
    rejected_note = f"  [dim](+{rejected_lc} rejected low-confidence)[/dim]" if rejected_lc > 0 else ""
    console.print(
        f"[bold]Suggest-facts[/bold] ({since}, {mode}): "
        f"[cyan]{candidates}[/cyan] candidates  "
        f"[green]{approved}[/green] approved  "
        f"[bold]{written}[/bold] written"
        f"{rejected_note}"
    )


def _show_next_actions(arg: str = "") -> None:
    """Show prioritized memory maintenance to-do list.

    Usage:
      /next-actions         -- top 5 per category
      /next-actions 3       -- top 3 per category
    """
    try:
        limit = max(1, min(20, int(arg.strip()))) if arg.strip().isdigit() else 5
    except (ValueError, AttributeError):
        limit = 5

    try:
        from atlas_plus.curation.decay import find_decayed_facts
        from atlas_plus.curation.orphan import find_orphan_candidates
        from atlas_plus.curation.tag_suggest import suggest_tags_for_file
        from atlas_plus.mcp.memory_server.tools.search_log import top_miss_queries
        from atlas_plus.mcp.memory_server.config import MEMORY_ROOT
    except Exception as exc:
        console.print(f"[red]Import error: {exc}[/red]")
        return

    decayed = []
    orphans = []
    untagged = []
    gaps = []

    try:
        decayed = find_decayed_facts(threshold_days=0)[:limit]
    except Exception:
        pass

    try:
        orphans = find_orphan_candidates(MEMORY_ROOT, min_age_days=90, max_health=0.5, limit=limit)
    except Exception:
        pass

    try:
        for md_file in sorted(MEMORY_ROOT.rglob("*.md")):
            if len(untagged) >= limit:
                break
            rel_parts = md_file.relative_to(MEMORY_ROOT).parts
            if rel_parts and rel_parts[0] in {"archive", "_sync", "versions", "sessions", "anti"}:
                continue
            if md_file.name.startswith("_"):
                continue
            tags = suggest_tags_for_file(md_file, memory_root=MEMORY_ROOT, max_tags=4)
            if tags:
                rel = str(md_file.relative_to(MEMORY_ROOT)).replace("\\", "/")
                untagged.append((rel, tags))
    except Exception:
        pass

    try:
        gaps = top_miss_queries(since_hours=168, limit=limit)
    except Exception:
        pass

    if not decayed and not orphans and not untagged and not gaps:
        console.print("[green]Memory corpus is in good shape -- no urgent actions found.[/green]")
        return

    console.print("\n[bold]Memory Next Actions[/bold]\n")

    if decayed:
        console.print(f"[red]Decayed facts ({len(decayed)})[/red]")
        for c in decayed:
            console.print(f"  [dim]{c.path}[/dim]  [{c.half_life_days}d hl, {c.days_overdue}d overdue]")
        console.print("  [dim]-> Review and update, or remove half_life: field.[/dim]\n")

    if orphans:
        console.print(f"[yellow]Orphan candidates ({len(orphans)})[/yellow]")
        for o in orphans:
            console.print(f"  [dim]{o['path']}[/dim]  [h={o['score']:.2f}, age={o['age_days']}d]")
        console.print("  [dim]-> atlas-review-memory archive <path>[/dim]\n")

    if untagged:
        console.print(f"[cyan]Untagged files ({len(untagged)})[/cyan]")
        for rel, tags in untagged:
            console.print(f"  [dim]{rel}[/dim]  {', '.join(tags)}")
        console.print("  [dim]-> atlas-review-memory suggest-tags --write[/dim]\n")

    if gaps:
        console.print(f"[blue]Research gaps ({len(gaps)})[/blue]")
        for g in gaps:
            console.print(f"  [dim]{g['query']}[/dim]  [{g['miss_count']} misses/7d]")
        console.print("  [dim]-> atlas-review-memory gaps --autoticket[/dim]\n")


def _temporal_rebuild(arg: str = "") -> None:
    """Rebuild the temporal knowledge graph from .memory/ files and session logs.

    Usage:
      /temporal-rebuild                -- rebuild only if stale (>7 days old)
      /temporal-rebuild --force        -- rebuild regardless of age
      /temporal-rebuild --source all   -- specify source (memory/sessions/transcripts/all)
    """
    force = "--force" in (arg or "")
    source = "all"
    tokens = (arg or "").split()
    for i, tok in enumerate(tokens):
        if tok == "--source" and i + 1 < len(tokens):
            source = tokens[i + 1]

    try:
        from atlas_plus.doctor.cli import check_temporal
        tg = check_temporal()
        stale = tg.get("stale", True)
        age_days = tg.get("age_days")
        entities = tg.get("entities", 0)
        last_built = tg.get("last_built", "never")
    except Exception as exc:
        console.print(f"[red]Failed to check temporal graph status: {exc}[/red]")
        return

    if not force and not stale:
        age_str = f" ({age_days}d old)" if age_days is not None else ""
        console.print(
            f"[green]Temporal graph is fresh[/green]{age_str}  (built {last_built}, "
            f"{entities} entities). Use --force to rebuild anyway."
        )
        return

    if stale and not force:
        age_str = f" ({age_days}d old)" if age_days is not None else " (no manifest)"
        console.print(f"[yellow]Temporal graph is stale{age_str}. Rebuilding with source={source}...[/yellow]")
    else:
        console.print(f"[dim]Force-rebuilding temporal graph with source={source}...[/dim]")

    try:
        from atlas_plus.temporal_graph.build import build_temporal_graph
        result = build_temporal_graph(source=source)
    except Exception as exc:
        console.print(f"[red]Temporal rebuild error: {exc}[/red]")
        return

    if "error" in result:
        console.print(f"[red]Build error: {result['error']}[/red]")
        return

    idx = result.get("index", {})
    console.print(
        f"[green]Temporal graph rebuilt[/green]  "
        f"[cyan]{idx.get('entities', 0)}[/cyan] entities  "
        f"[bold]{idx.get('facts_active', 0)}[/bold] active facts  "
        f"({result.get('files_indexed', 0)} indexed, {result.get('files_skipped', 0)} skipped)"
    )


def _temporal_search(arg: str = "") -> None:
    """Query the temporal knowledge graph from the Brain REPL.

    Usage:
      /temporal-search Atlas MCP Tunnel     -- current facts matching the query
      /temporal-search --as-of 2026-05-01 Atlas Brain  -- facts as of a date
      /temporal-search --limit 10 Atlas     -- return up to 10 results
    """
    tokens = (arg or "").strip().split()
    mode = "current"
    at = None
    limit = 5
    query_parts: list[str] = []

    i = 0
    while i < len(tokens):
        if tokens[i] == "--as-of" and i + 1 < len(tokens):
            mode = "as-of"
            at = tokens[i + 1]
            i += 2
        elif tokens[i] == "--limit" and i + 1 < len(tokens):
            try:
                limit = max(1, min(20, int(tokens[i + 1])))
            except ValueError:
                pass
            i += 2
        else:
            query_parts.append(tokens[i])
            i += 1

    query_str = " ".join(query_parts).strip()
    if not query_str:
        console.print("[dim]/temporal-search <query> [--as-of DATE] [--limit N][/dim]")
        return

    try:
        from atlas_plus.temporal_graph.file_provider import FileTemporalProvider
        provider = FileTemporalProvider()
        s = provider.stats()
        if s["facts"] == 0:
            console.print("[yellow]Temporal graph is empty. Run: atlas-temporal build --source fixtures[/yellow]")
            return

        if mode == "as-of" and at:
            results = provider.search_as_of(query_str, at=at, limit=limit)
            label = f"as-of {at}"
        else:
            results = provider.search_current(query_str, limit=limit)
            label = "current"
    except Exception as exc:
        console.print(f"[red]Temporal search error: {exc}[/red]")
        return

    if not results:
        console.print(f"[dim]No temporal facts found for {query_str!r} ({label})[/dim]")
        return

    console.print(f"\n[bold]Temporal[/bold] ({label}, {len(results)} result(s) for {query_str!r}):\n")
    for i, r in enumerate(results, 1):
        warn = f"  [yellow]WARNING: {r.freshness_warning}[/yellow]" if r.freshness_warning else ""
        console.print(
            f"  [{i}] [dim]score={r.score:.2f}[/dim] {r.fact.fact_text}\n"
            f"       [dim]{r.fact.valid_from[:10]} | {r.provenance}[/dim]{warn}"
        )
    console.print()


def _temporal_history(arg: str = "") -> None:
    """Show full temporal history for an entity in the Brain REPL.

    Usage:
      /temporal-history Atlas MCP Tunnel
      /temporal-history --limit 20 Atlas Brain
    """
    tokens = (arg or "").strip().split()
    limit = 50
    name_parts: list[str] = []

    i = 0
    while i < len(tokens):
        if tokens[i] == "--limit" and i + 1 < len(tokens):
            try:
                limit = max(1, min(100, int(tokens[i + 1])))
            except ValueError:
                pass
            i += 2
        else:
            name_parts.append(tokens[i])
            i += 1

    entity_name = " ".join(name_parts).strip()
    if not entity_name:
        console.print("[dim]/temporal-history <entity-name> [--limit N][/dim]")
        return

    try:
        from atlas_plus.temporal_graph.file_provider import FileTemporalProvider
        provider = FileTemporalProvider()
        h = provider.history(entity_name, limit=limit)
    except Exception as exc:
        console.print(f"[red]Temporal history error: {exc}[/red]")
        return

    console.print(f"\n[bold]History:[/bold] {h.entity.name}")
    if h.entity.aliases:
        console.print(f"  [dim]Aliases: {', '.join(h.entity.aliases)}[/dim]")
    console.print(f"  [dim]{h.entity.first_seen_at[:10]} to {h.entity.last_seen_at[:10]}[/dim]\n")

    if not h.facts:
        console.print("  [dim](no facts in history)[/dim]")
        return

    for fact in h.facts:
        status = "[green]ACTIVE[/green]" if fact.invalid_at is None else f"[dim]superseded {fact.invalid_at[:10]}[/dim]"
        prefix = "  [bold]-->[/bold]" if fact is h.current_fact else "  ---"
        console.print(f"{prefix} [{fact.valid_from[:10]}] {fact.fact_text}  {status}")
    console.print()

    if h.warnings:
        for w in h.warnings:
            console.print(f"  [yellow]Warning: {w}[/yellow]")


def _temporal_entities(arg: str = "") -> None:
    """List entities in the temporal graph from the Brain REPL.

    Usage:
      /temporal-entities           -- list up to 20 entities
      /temporal-entities 50        -- list up to 50
    """
    try:
        limit = max(1, min(100, int(arg.strip()))) if arg.strip().isdigit() else 20
    except (ValueError, AttributeError):
        limit = 20

    try:
        from atlas_plus.temporal_graph.file_provider import FileTemporalProvider
        provider = FileTemporalProvider()
        s = provider.stats()
        if s["entities"] == 0:
            console.print("[yellow]No entities in temporal graph. Run: atlas-temporal build --source fixtures[/yellow]")
            return
        entities = provider.list_entities(limit=limit)
    except Exception as exc:
        console.print(f"[red]Temporal entities error: {exc}[/red]")
        return

    console.print(f"\n[bold]Entities[/bold] ({len(entities)} shown of {s['entities']} total):\n")
    for e in entities:
        aliases_str = f" [{', '.join(e.aliases)}]" if e.aliases else ""
        console.print(f"  {e.name}[dim]{aliases_str}[/dim]")
    console.print()


def _run_curation_report(arg: str = "") -> None:
    """Run the weekly curation report (duplicates, stale, convergence flags, decisions).

    Usage:
      /curation-report           -- generate and display report summary
      /curation-report --write   -- write report to .memory/_atlas/ as well
    """
    write = "--write" in (arg or "")

    try:
        from atlas_plus.curation.weekly_report import generate_report
        report_text, output_path = generate_report(dry_run=not write)
    except Exception as exc:
        console.print(f"[red]Curation report error: {exc}[/red]")
        return

    # Extract key counts from report for compact display
    dup_count = 0
    stale_count = 0
    convergence_count = 0
    decisions_count = 0
    from atlas_plus.curation.weekly_report import curation_counts
    _c = curation_counts(report_text)
    dup_count, stale_count, convergence_count, decisions_count = _c["duplicates"], _c["stale"], _c["convergence"], _c["decisions"]

    status = "written to " + str(output_path) if write else "dry-run (add --write to save)"
    console.print(f"\n[bold]Curation Report[/bold] ({status})")
    console.print(f"  Duplicates:     [{'red' if dup_count else 'green'}]{dup_count}[/{'red' if dup_count else 'green'}]")
    console.print(f"  Stale files:    [{'yellow' if stale_count else 'green'}]{stale_count}[/{'yellow' if stale_count else 'green'}]")
    console.print(f"  Convergence:    [{'yellow' if convergence_count else 'green'}]{convergence_count}[/{'yellow' if convergence_count else 'green'}]")
    console.print(f"  Stale decisions:[{'yellow' if decisions_count else 'green'}]{decisions_count}[/{'yellow' if decisions_count else 'green'}]")
    console.print()


def _run_budget(arg: str = "") -> None:
    """Show token-budget headroom (the 5h rate-limit window), or preflight a task.

      /budget               current 5h-window snapshot (used / ceiling / headroom)
      /budget 900000        preflight a task estimated at ~900k gross tokens
      /budget large         preflight by named size (small | medium | large | xl)
    """
    from atlas_plus.budget import SIZE_PRESETS, human, preflight, preflight_size, snapshot
    a = (arg or "").strip()
    if not a:
        s = snapshot()
        console.print(
            "[bold]Token budget (5h window)[/bold]\n"
            f"  used     {human(s.used_5h)}\n"
            f"  ceiling  {human(s.ceiling)}  (empirical max 5h spend)\n"
            f"  headroom {human(s.headroom)}\n"
            f"  burn 1h  {human(s.burn_1h)}/h"
        )
        return
    try:
        v = preflight_size(a.lower()) if a.lower() in SIZE_PRESETS else preflight(int(a))
    except ValueError:
        console.print("[red]usage:[/red] /budget [<gross-estimate> | small|medium|large|xl]")
        return
    colour = {"proceed": "green", "tight": "yellow", "insufficient": "red"}[v.status]
    lines = [
        f"[{colour}]{v.status.upper()}[/{colour}] task ~{human(v.estimated_tokens)} gross "
        f"(need {human(v.required)} w/ {v.margin:.0%} margin)",
        f"  headroom {human(v.headroom)} / ceiling {human(v.snapshot.ceiling)}",
    ]
    if v.deficit:
        wait = (f"window frees it in ~{v.minutes_until_enough:.0f} min"
                if v.minutes_until_enough else "cannot free by waiting")
        lines.append(f"  short {human(v.deficit)}; {wait}")
    lines.append(f"  -> {v.recommendation}")
    console.print("\n".join(lines))


def _run_decay_report(arg: str = "") -> None:
    """Queue a decay_report Brain task and show the task ID.

    Usage:
      /decay-report             -- queue decay_report task (returns task ID immediately)
      /decay-report --watch     -- queue and poll until done, then show results

    The decay_report executor (kind=decay_report) scans .memory/ for files past their
    half_life: threshold and writes a decay-report.md artifact to .atlas/artifacts/.
    Use /watch <id> to poll if you didn't pass --watch.
    """
    watch = "--watch" in (arg or "")

    try:
        from atlas_plus.brain.tasks import create_task
        task = create_task(
            instruction="Run memory half-life decay report",
            kind="decay_report",
            session_id="cli-decay-report",
        )
    except Exception as exc:
        console.print(f"[red]Failed to queue decay report: {exc}[/red]")
        return

    console.print(
        f"[green]Decay report queued[/green]  task_id={task.task_id}\n"
        f"Poll: [dim]/watch {task.task_id}[/dim]  or  [dim]/task {task.task_id}[/dim]"
    )

    if watch:
        _watch_task(task.task_id)


def _run_ticket_sync(arg: str = "") -> None:
    """Queue a ticket_sync Brain task that auto-closes tickets referenced in git commits.

    Usage:
      /ticket-sync             -- queue ticket_sync task (returns task ID immediately)
      /ticket-sync --watch     -- queue and poll until done, then show results

    The ticket_sync executor scans the last 30 days of git log across workspace repos
    for commit messages containing ticket IDs (closes ATL-NNN, feat:/fix: prefixed with
    a ticket ID) and marks those tickets as done.
    """
    watch = "--watch" in (arg or "")

    try:
        from atlas_plus.brain.tasks import create_task
        task = create_task(
            instruction="Sync ticket statuses from git commit history",
            kind="ticket_sync",
            session_id="cli-ticket-sync",
        )
    except Exception as exc:
        console.print(f"[red]Failed to queue ticket sync: {exc}[/red]")
        return

    console.print(
        f"[green]Ticket sync queued[/green]  task_id={task.task_id}\n"
        f"Poll: [dim]/watch {task.task_id}[/dim]  or  [dim]/task {task.task_id}[/dim]"
    )

    if watch:
        _watch_task(task.task_id)


def _run_ticket_health(arg: str = "") -> None:
    """Queue a ticket_health_check Brain task: staleness audit + orphan auto-reset.

    Usage:
      /ticket-health          -- queue task (returns task ID immediately)
      /ticket-health --watch  -- queue and poll until done, then show report

    The executor flags in-progress tickets stale >7 days, blocked tickets
    stale >3 days, and auto-resets orphaned in-progress tickets (non-human,
    >14 days) back to todo so the dispatcher re-picks them up.
    """
    watch = "--watch" in (arg or "")

    try:
        from atlas_plus.brain.tasks import create_task
        task = create_task(
            instruction="Audit ticket health: flag stale in-progress/blocked, auto-reset orphans",
            kind="ticket_health_check",
            session_id="cli-ticket-health",
        )
    except Exception as exc:
        console.print(f"[red]Failed to queue ticket health check: {exc}[/red]")
        return

    console.print(
        f"[green]Ticket health check queued[/green]  task_id={task.task_id}\n"
        f"Poll: [dim]/watch {task.task_id}[/dim]  or  [dim]/task {task.task_id}[/dim]"
    )

    if watch:
        _watch_task(task.task_id)


def _show_research_gaps(arg: str = "") -> None:
    """Show top memory miss queries, cross-referenced with open research gap tickets.

    Uses the search miss log (top_miss_queries) and the ticket store to show:
    - Miss count, total queries, miss rate for each recurring gap
    - Whether a 'Research gap:' ticket already exists for that query
    - Whether a similar research report exists in .atlas/reports/

    Usage: /research-gaps [N]  (default 10)
    """
    try:
        limit = max(1, min(50, int(arg.strip()))) if arg.strip().isdigit() else 10
    except (ValueError, AttributeError):
        limit = 10

    try:
        from atlas_plus.mcp.memory_server.tools.search_log import top_miss_queries
        misses = top_miss_queries(since_hours=168.0, limit=limit)  # last 7 days
    except Exception as exc:
        console.print(f"[red]Could not load miss log: {exc}[/red]")
        return

    if not misses:
        console.print("[green]No memory miss queries in the last 7 days.[/green]")
        return

    # Build set of existing gap ticket titles for quick lookup
    try:
        from atlas_plus.tickets.store import list_tickets
        existing_gap_titles = {
            t.title.lower()
            for t in list_tickets()
            if t.status not in ("done", "archived") and "research gap" in t.title.lower()
        }
    except Exception:
        existing_gap_titles = set()

    # Build set of covered topics from research reports
    try:
        from atlas_plus.brain.research import find_similar_research_reports
        _has_report = True
    except Exception:
        _has_report = False

    from rich.table import Table
    table = Table(title=f"Research Gaps (top {limit}, last 7d)", show_lines=False)
    table.add_column("Query", style="cyan", max_width=40)
    table.add_column("Misses", justify="right", style="yellow")
    table.add_column("Rate", justify="right")
    table.add_column("Ticket", justify="center")
    table.add_column("Report", justify="center")

    for r in misses:
        query = r["query"]
        miss_count = r.get("miss_count", 0)
        total = r.get("total", 1)
        rate = f"{int(miss_count / total * 100)}%"

        ticket_icon = "[green]Y[/green]" if f"research gap: {query.lower()}" in existing_gap_titles else "[dim]--[/dim]"

        report_icon = "[dim]--[/dim]"
        if _has_report:
            try:
                similar = find_similar_research_reports(query, threshold=0.4, max_age_days=60)
                report_icon = "[green]Y[/green]" if similar else "[dim]--[/dim]"
            except Exception:
                pass

        table.add_row(query[:40], str(miss_count), rate, ticket_icon, report_icon)

    console.print(table)
    uncovered = sum(
        1 for r in misses
        if f"research gap: {r['query'].lower()}" not in existing_gap_titles
    )
    if uncovered > 0:
        console.print(
            f"\n[yellow]{uncovered} gap(s) have no open ticket.[/yellow] "
            "Run: [dim]atlas-review-memory gaps --autoticket[/dim] to file them."
        )


def _show_quality(arg: str = "") -> None:
    """Show research report quality. Args: [N] show N low-quality; [topic] filter by topic tag."""
    import os as _os
    from pathlib import Path as _Path
    import yaml as _yaml

    arg = arg.strip()

    # Determine if arg is a numeric limit or a topic filter
    topic_filter: str = ""
    limit: int = 5
    if arg:
        try:
            limit = int(arg)
        except ValueError:
            topic_filter = arg.lower()
            limit = 20  # show more when filtering by topic

    reports_dir = atlas_paths.state_dir() / "reports"
    if not reports_dir.exists():
        console.print("[dim]No .atlas/reports/ directory found.[/dim]")
        return

    # Scan all reports, optionally filtering by topic
    all_reports: list[tuple[float | None, str, str]] = []  # (score, fname, topic)
    for md_file in sorted(reports_dir.rglob("*.md")):
        if md_file.name.startswith("_"):
            continue
        try:
            raw = md_file.read_text(encoding="utf-8", errors="replace")
            if not raw.startswith("---"):
                continue
            end = raw.find("\n---", 3)
            if end == -1:
                continue
            fm = _yaml.safe_load(raw[3:end]) or {}
            topic = str(fm.get("topic", fm.get("tags", "")) or "").lower()
            if topic_filter and topic_filter not in topic and topic_filter not in md_file.name.lower():
                continue
            qs = fm.get("quality_score")
            score = float(qs) if qs is not None else None
            all_reports.append((score, md_file.name, topic))
        except Exception:
            continue

    if topic_filter:
        console.print(f"[bold]Research Quality[/bold] -- topic filter: [cyan]{topic_filter}[/cyan]  ({len(all_reports)} reports)")
    else:
        # Show global summary from doctor when no topic filter
        try:
            from atlas_plus.doctor.cli import check_research_quality
            rq = check_research_quality()
            total = rq.get("total_reports", 0)
            scored = rq.get("scored_reports", 0)
            avg = rq.get("avg_score", 0.0)
            low = rq.get("low_quality_count", 0)
            min_file = rq.get("min_score_file", "")
            avg_color = "green" if avg >= 0.7 else ("yellow" if avg >= 0.4 else "red")
            console.print(
                f"[bold]Research Quality[/bold]  "
                f"{scored}/{total} scored  "
                f"avg: [{avg_color}]{avg:.2f}[/{avg_color}]  "
                f"low-quality: {low}"
            )
            if min_file:
                console.print(f"  Lowest: [dim]{min_file}[/dim]")
            if scored < total:
                unscored = total - scored
                console.print(
                    f"[yellow]{unscored} report(s) not yet scored.[/yellow]\n"
                    "  Run: [dim]atlas-review-memory quality --write[/dim]"
                )
        except Exception as exc:
            console.print(f"[red]Quality check failed: {exc}[/red]")

    # List scored reports (low-quality first, then unscored)
    scored_reports = [(s, f, t) for s, f, t in all_reports if s is not None]
    unscored_reports = [(s, f, t) for s, f, t in all_reports if s is None]
    scored_reports.sort(key=lambda x: x[0])  # lowest score first

    if topic_filter:
        # When filtering by topic, show ALL reports with their scores
        if not all_reports:
            console.print(f"[dim]No reports match topic '{topic_filter}'.[/dim]")
            return
        for score, fname, _ in scored_reports[:limit]:
            score_color = "green" if score >= 0.7 else ("yellow" if score >= 0.4 else "red")
            console.print(f"  [{score_color}]{score:.2f}[/{score_color}] [dim]{fname}[/dim]")
        for _, fname, _ in unscored_reports[:max(0, limit - len(scored_reports))]:
            console.print(f"  [dim]---- {fname} (unscored)[/dim]")
        if len(all_reports) > limit:
            console.print(f"  (+{len(all_reports) - limit} more -- increase limit with /quality {topic_filter} {limit * 2})")
    else:
        # When no topic filter, show only low-quality
        low_reports = [(s, f) for s, f, _ in scored_reports if s < 0.5]
        if low_reports:
            console.print(f"\n[yellow]Low-quality reports (< 0.5):[/yellow]")
            for score, fname in low_reports[:limit]:
                console.print(f"  [{score:.2f}] [dim]{fname}[/dim]")
            if len(low_reports) > limit:
                console.print(f"  (+{len(low_reports) - limit} more -- increase limit with /quality <N>)")


def _bump_priority(args: str) -> None:
    """Set a task's priority. Usage: /priority <task_id> <N>"""
    parts = args.strip().split(None, 1)
    if len(parts) < 2 or not parts[1].isdigit():
        console.print("[red]Usage: /priority <task-id> <N>  (1=highest, 100=lowest)[/red]")
        return
    task_id, priority_str = parts[0], parts[1]
    priority = max(1, min(100, int(priority_str)))
    try:
        from atlas_plus.brain.tasks import set_task_priority
        set_task_priority(task_id, priority)
        console.print(f"[green]Priority set[/green] to {priority} for {task_id[:16]}")
    except Exception as exc:
        console.print(f"[red]Priority set failed: {exc}[/red]")


def _escalate_task(task_id: str) -> None:
    """Manually escalate a task to Claude Code, regardless of escalation env vars."""
    from atlas_plus.brain.tasks import get_task
    from atlas_plus.brain.escalation import create_escalation_task, ESCALATION_SESSION_PREFIX

    task = get_task(task_id)
    if task is None:
        console.print(f"[red]Task {task_id!r} not found.[/red]")
        return
    if task.agent == "claude-code":
        console.print(f"[yellow]Task {task_id[:16]} is already a claude-code task.[/yellow]")
        return

    result = (task.result or "")[:300] + ("..." if task.result and len(task.result) > 300 else "")
    esc_id = create_escalation_task(
        original_task_id=task_id,
        original_session_id=task.session_id or "cli-escalate",
        instruction=task.instruction,
        reason="manual escalation via Brain CLI",
        original_result=result,
    )
    if esc_id:
        console.print(
            f"[green]Escalated[/green] {task_id[:16]} -> [dim]{esc_id}[/dim]  "
            f"(claude-code, priority 1)"
        )
    else:
        console.print(f"[red]Escalation failed. Check logs.[/red]")


def _retry_task(task_id: str) -> None:
    """Resubmit a failed or blocked task with the same instruction and agent."""
    from atlas_plus.brain.tasks import get_task, create_task
    task = get_task(task_id)
    if task is None:
        console.print(f"[red]Task {task_id!r} not found.[/red]")
        return
    if task.status not in ("failed", "blocked"):
        console.print(
            f"[yellow]Task {task_id[:16]} has status '{task.status}' "
            f"-- only failed/blocked tasks can be retried.[/yellow]"
        )
        return
    try:
        new_task = create_task(
            instruction=task.instruction,
            agent=task.agent or "brain",
            session_id=task.session_id or "cli-retry",
            priority=task.priority,
        )
        console.print(
            f"[green]Retried[/green] as [dim]{new_task.task_id}[/dim]  "
            f"(original: {task_id[:16]})"
        )
    except Exception as exc:
        console.print(f"[red]Retry failed: {exc}[/red]")


def _kill_task(task_id: str) -> None:
    """Mark a pending or running task as failed (cancelled by user)."""
    from atlas_plus.brain.tasks import get_task, mark_failed
    task = get_task(task_id)
    if task is None:
        console.print(f"[red]Task {task_id!r} not found.[/red]")
        return
    if task.status in ("done", "failed", "blocked"):
        console.print(
            f"[yellow]Task {task_id[:16]} is already in terminal state: {task.status}[/yellow]"
        )
        return
    mark_failed(task_id, error="Cancelled by user via Brain CLI")
    console.print(f"[dim]Task [bold]{task_id[:16]}[/bold] marked as failed (cancelled).[/dim]")


def _show_exhausted_tasks(limit: int = 10) -> None:
    """Show tasks that have burned through all retry attempts."""
    from atlas_plus.brain.tasks import list_exhausted_tasks
    tasks = list_exhausted_tasks(limit=limit)
    if not tasks:
        console.print("[dim]No exhausted tasks. All retries accounted for.[/dim]")
        return
    t = Table(show_header=True, header_style="bold", title=f"Exhausted Tasks ({len(tasks)})")
    t.add_column("Task ID", style="dim", width=20)
    t.add_column("Status", width=9)
    t.add_column("Att", width=4)
    t.add_column("Agent", width=14)
    t.add_column("Instruction", width=55)
    _SC = {"done": "green", "failed": "red", "blocked": "magenta"}
    for tk in tasks:
        sc = _SC.get(tk.status, "white")
        instr = (tk.instruction or "")[:53]
        if len(tk.instruction or "") > 53:
            instr += ".."
        t.add_row(
            tk.task_id[:18],
            f"[{sc}]{tk.status}[/{sc}]",
            f"{tk.attempts}/{tk.max_attempts}",
            tk.agent or "(none)",
            instr,
        )
    console.print(t)
    console.print("[dim]Use /retry <task-id> to requeue, or /task <id> to inspect.[/dim]")


def _show_task_attempts(task_id: str) -> None:
    """Show attempt history for a task."""
    from atlas_plus.brain import tasks as ts
    task = ts.get_task(task_id)
    if task is None:
        console.print(f"[red]Task not found: {task_id}[/red]")
        return
    attempts = ts.list_task_attempts(task_id)
    if not attempts:
        console.print(f"[dim]No attempt history recorded for {task_id[:20]}.[/dim]")
        return
    _OUTCOME_COLORS = {"done": "green", "failed": "red", "blocked": "magenta"}
    t = Table(show_header=True, header_style="bold", title=f"Attempt History: {task_id[:20]} ({len(attempts)} attempts)")
    t.add_column("#", width=3)
    t.add_column("Outcome", width=9)
    t.add_column("Agent", width=14)
    t.add_column("Finished", width=22)
    t.add_column("Summary", width=55)
    for a in attempts:
        oc = _OUTCOME_COLORS.get(a.get("outcome", ""), "white")
        summary = (a.get("result_summary") or "")[:53]
        if len(a.get("result_summary") or "") > 53:
            summary += ".."
        t.add_row(
            str(a.get("attempt_num", "?")),
            f"[{oc}]{a.get('outcome', '')}[/{oc}]",
            a.get("agent") or "(none)",
            (a.get("finished_at") or "")[:19],
            summary,
        )
    console.print(t)


def _show_sessions(limit: int = 15) -> None:
    from atlas_plus.brain import history as hist
    sessions = hist.list_sessions(limit=limit)
    if not sessions:
        console.print("[dim]No Brain sessions found.[/dim]")
        return
    t = Table(show_header=True, header_style="bold", title=f"Recent Brain Sessions ({len(sessions)})")
    t.add_column("Session ID", style="cyan", width=28)
    t.add_column("Messages", width=10)
    t.add_column("Last Active", width=22)
    for s in sessions:
        t.add_row(s["session_id"], str(s["message_count"]), (s["last_active"] or "")[:19])
    console.print(t)
    console.print("[dim]Resume a session: .venv\\Scripts\\python.exe -m atlas_plus.brain.cli --session <id>[/dim]")


def _clear_session(session_id: str) -> None:
    from atlas_plus.brain import history as hist
    deleted = hist.clear_session(session_id)
    console.print(f"[dim]Cleared {deleted} message(s) from session {session_id!r}.[/dim]")


def _export_session(session_id: str, output_path_arg: str = "") -> None:
    """Export a Brain session to a markdown file.

    Combines SQLite conversation history (brain_conversations) with JSONL turns
    from .atlas/cli-history/ (if present) and writes a human-readable markdown
    document to .atlas/exports/<session_id>.md or a caller-specified path.

    Usage from CLI: /export [path]
    """
    import os
    from atlas_plus.brain import history as hist

    # Load messages from SQLite (authoritative source)
    messages = hist.get_messages(session_id, limit=200)

    # Supplement with JSONL if available (covers turns from before the current sprint)
    jsonl_turns = _load_cli_history_jsonl(session_id)
    if not messages and not jsonl_turns:
        console.print(f"[dim]No history found for session {session_id!r}.[/dim]")
        return

    # Determine output path
    if output_path_arg.strip():
        out = Path(output_path_arg.strip())
    else:
        exports_dir = _CLI_HISTORY_DIR.parent / "exports"
        exports_dir.mkdir(parents=True, exist_ok=True)
        safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in session_id)
        out = exports_dir / f"{safe}.md"

    lines = [
        f"# Brain Session: {session_id}",
        "",
        f"Exported: {datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}",
        f"Messages (SQLite): {len(messages)}",
        f"Turns (JSONL): {len(jsonl_turns)}",
        "",
        "---",
        "",
    ]

    # Prefer SQLite messages (include model/tool info); fall back to JSONL
    source = messages if messages else [
        {"role": t["role"], "content": t["content"]}
        for t in jsonl_turns
    ]
    for msg in source:
        role = msg["role"].upper()
        content = msg.get("content", "")
        lines.append(f"## {role}")
        lines.append("")
        lines.append(content)
        lines.append("")
        lines.append("---")
        lines.append("")

    try:
        out.write_text("\n".join(lines), encoding="utf-8")
        console.print(f"[green]Exported[/green] {len(source)} messages to {out}")
    except OSError as exc:
        console.print(f"[red]Export failed: {exc}[/red]")


def _handle_command(cmd_line: str, session_id: str) -> bool:
    """Handle /command input. Returns True to continue, False to exit."""
    parts = cmd_line.strip().split(None, 1)
    cmd = parts[0].lower()
    arg = parts[1] if len(parts) > 1 else ""

    if cmd in ("/quit", "/exit", "/q"):
        return False

    if cmd == "/history":
        _show_history(session_id)
    elif cmd == "/tasks":
        limit = int(arg) if arg.isdigit() else 10
        _show_tasks(limit)
    elif cmd == "/recent":
        limit = int(arg) if arg and arg.isdigit() else 10
        _show_recent_tasks(limit)
    elif cmd == "/task":
        if not arg:
            console.print("[red]Usage: /task <task-id>[/red]")
        else:
            _show_task_detail(arg.strip())
    elif cmd == "/submit":
        _submit_task(arg, session_id=session_id)
    elif cmd == "/watch":
        if not arg:
            console.print("[red]Usage: /watch <task-id>[/red]")
        else:
            _watch_task(arg.strip())
    elif cmd == "/pending":
        _show_pending_facts()
    elif cmd == "/approve":
        if not arg:
            console.print("[red]Usage: /approve <id-prefix>[/red]")
        else:
            _approve_fact(arg.strip())
    elif cmd == "/reject":
        if not arg:
            console.print("[red]Usage: /reject <id-prefix>[/red]")
        else:
            _reject_fact(arg.strip())
    elif cmd == "/search":
        _search_memory(arg)
    elif cmd == "/agents":
        _show_agents()
    elif cmd == "/escalation":
        _show_escalation()
    elif cmd == "/sessions":
        _show_sessions()
        _show_cli_sessions()
    elif cmd == "/kill":
        if not arg:
            console.print("[red]Usage: /kill <task-id>[/red]")
        else:
            _kill_task(arg.strip())
    elif cmd == "/attempts":
        if not arg:
            console.print("[red]Usage: /attempts <task-id>[/red]")
        else:
            _show_task_attempts(arg.strip())
    elif cmd == "/exhausted":
        _show_exhausted_tasks()
    elif cmd == "/retry":
        if not arg:
            console.print("[red]Usage: /retry <task-id>[/red]")
        else:
            _retry_task(arg.strip())
    elif cmd == "/escalate":
        if not arg:
            console.print("[red]Usage: /escalate <task-id>[/red]")
        else:
            _escalate_task(arg.strip())
    elif cmd == "/priority":
        _bump_priority(arg)
    elif cmd == "/stats":
        _show_stats()
    elif cmd == "/check":
        _run_check()
    elif cmd == "/browse":
        _browse_memory(arg.strip())
    elif cmd == "/confirm":
        _confirm_fact(arg)
    elif cmd == "/save":
        _save_fact(arg)
    elif cmd == "/clear":
        _clear_session(session_id)
    elif cmd == "/export":
        _export_session(session_id, arg)
    elif cmd == "/topics":
        _show_topics(arg)
    elif cmd == "/decay":
        _show_decay(arg)
    elif cmd == "/digest":
        _show_digest(arg)
    elif cmd == "/quality":
        _show_quality(arg)
    elif cmd == "/research-index":
        _show_research_index(arg)
    elif cmd == "/research-gaps":
        _show_research_gaps(arg)
    elif cmd == "/gap-report":
        _queue_gap_report(arg)
    elif cmd == "/queue-research":
        _queue_research_bulk(arg)
    elif cmd == "/suggest-tags":
        _suggest_tags_scan(arg)
    elif cmd == "/suggest-facts":
        _suggest_facts_run(arg)
    elif cmd == "/next-actions":
        _show_next_actions(arg)
    elif cmd == "/temporal-rebuild":
        _temporal_rebuild(arg)
    elif cmd == "/temporal-search":
        _temporal_search(arg)
    elif cmd == "/temporal-history":
        _temporal_history(arg)
    elif cmd == "/temporal-entities":
        _temporal_entities(arg)
    elif cmd == "/health":
        _show_health()
    elif cmd == "/dispatch-tickets":
        _dispatch_tickets_now()
    elif cmd == "/ticket-status":
        _show_ticket_status(arg)
    elif cmd == "/verify":
        _queue_verify_for_ticket(arg)
    elif cmd == "/orphans":
        _show_orphans(arg)
    elif cmd == "/plan":
        try:
            n = max(1, min(50, int(arg.strip()))) if arg.strip().isdigit() else 10
        except (ValueError, AttributeError):
            n = 10
        _show_plan(limit=n)
    elif cmd == "/loop":
        _show_loop_status()
    elif cmd == "/session-log":
        _write_session_log(arg)
    elif cmd == "/curation-report":
        _run_curation_report(arg)
    elif cmd == "/decay-report":
        _run_decay_report(arg)
    elif cmd == "/budget":
        _run_budget(arg)
    elif cmd == "/ticket-sync":
        _run_ticket_sync(arg)
    elif cmd == "/ticket-health":
        _run_ticket_health(arg)
    elif cmd == "/help":
        console.print(
            "[bold]Commands:[/bold]\n"
            "  /quit /exit                       exit\n"
            "  /history                          show session messages\n"
            "  /tasks [N]                        list recent tasks (default 10)\n"
            "  /task <id>                        show task detail\n"
            "  /submit [--agent <n>] <instr>     queue a task to the Brain worker\n"
            "  /watch <id>                       poll task until done and show result\n"
            "  /pending                          list pending fact candidates\n"
            "  /approve <id>                     approve a pending fact (writes to memory)\n"
            "  /reject  <id>                     reject a pending fact\n"
            "  /search <query>                   full-text search memory\n"
            "  /agents                           show agent registry + online status\n"
            "  /escalation                       show Claude Code escalation config\n"
            "  /sessions                         list recent Brain sessions\n"
            "  /kill <id>                        cancel a pending/running task\n"
            "  /retry <id>                       resubmit a failed/blocked task\n"
            "  /escalate <id>                    manually escalate task to Claude Code\n"
            "  /priority <id> <N>                set task priority (1=highest, 100=lowest)\n"
            "  /stats                            quick memory + task stats summary\n"
            "  /check                            run self-diagnostic (Ollama, DB, memory, escalation)\n"
            "  /browse [scope]                   list memory files (e.g. /browse feedback)\n"
            "  /confirm <path>                   mark a memory file confirmed today\n"
            "  /save <scope> <fact>              write a fact directly to .memory/\n"
            "  /export [path]                    export session to markdown file\n"
            "  /topics [N]                       show tag cloud from memory (top N, default 25)\n"
            "  /decay [days]                     dry-run unconfirmed decay (default 90d threshold)\n"
            "  /digest <task-id>                 show AI-generated result digest for a done task\n"
            "  /quality [N|topic]                show quality summary; /quality sip shows all SIP reports scored\n"
            "  /research-index [N]               list last N research reports with dates+scores (default 20)\n"
            "  /research-gaps [N]                show top N memory miss queries with ticket+report coverage status\n"
            "  /gap-report [--watch] <topic>     queue research_gap task for a topic (--watch polls until done)\n"
            "  /queue-research <file_or_topics>  bulk-queue research_gap tasks from a topics file or inline list\n"
            "  /suggest-tags [--write]           scan for untagged .memory/ files + suggest tags (--write to apply)\n"
            "  /suggest-facts [--since N] [--auto] [--skip-similar F] [--threshold F]  propose memory facts (--threshold overrides 0.70 confidence floor)\n"
            "  /next-actions [N]                  prioritized memory to-do list: decayed, orphans, untagged, gaps\n"
            "  /temporal-rebuild [--force] [--source S]  rebuild temporal graph (skips if fresh; --force to override)\n"
            "  /temporal-search <query> [--as-of DATE] [--limit N]  query temporal knowledge graph\n"
            "  /temporal-history <entity> [--limit N]               full fact timeline for an entity\n"
            "  /temporal-entities [N]             list all entities in the temporal graph (default 20)\n"
            "  /health                           compact system health: services, worker, memory, research\n"
            "  /dispatch-tickets                 manually run ticket dispatcher now (creates Brain tasks from todo tickets)\n"
            "  /ticket-status <id>               show ticket state + linked Brain task status and result\n"
            "  /verify <ticket-id>               queue a pytest verify task for a specific ticket (e.g. /verify ATL-007)\n"
            "  /orphans [N]                      list N memory orphan candidates (unreferenced + low health + old)\n"
            "  /recent [N]                       show last N completed tasks (done/failed/blocked)\n"
            "  /plan [N]                         show next N dispatch-eligible todo tickets ranked by priority\n"
            "  /loop                             show automation loop health (dispatch + tickets + reopen counts)\n"
            "  /session-log [--dry-run]          write today's session log from git commits (dry-run to preview)\n"
            "  /curation-report [--write]        run weekly curation report (duplicates, stale, convergence, decisions)\n"
            "  /decay-report [--watch]           queue decay_report task (memory half-life overdue scan + artifact)\n"
            "  /budget [<est>|size]              token-budget headroom (5h window), or preflight a task\n"
            "  /ticket-sync [--watch]            auto-close tickets referenced in recent git commits\n"
            "  /ticket-health [--watch]          queue ticket_health_check task (staleness audit + orphan reset)\n"
            "  /attempts <id>                    show per-attempt execution history for a task\n"
            "  /exhausted                        list tasks that burned through all retry attempts\n"
            "  /clear                            clear session history\n"
            "  /help                             this message"
        )
    else:
        console.print(f"[red]Unknown command: {cmd}[/red]  (type /help for commands)")
    return True


def _startup_hints() -> str:
    """Return a one-line hint string for the startup banner (pending facts, etc.)."""
    hints = []
    try:
        from atlas_plus.brain.task_facts import pending_count
        n = pending_count()
        if n > 0:
            hints.append(f"[yellow]{n} pending fact(s)[/yellow] awaiting review (type /pending)")
    except Exception:
        pass
    return "  ".join(hints) if hints else ""


def _run_check() -> None:
    """Run a quick self-diagnostic of the Brain CLI environment."""
    import os
    from pathlib import Path

    checks: list[tuple[str, str, str]] = []  # (label, status, detail)

    # Ollama
    ollama_ok = _check_ollama_reachable()
    checks.append(("Ollama", "ok" if ollama_ok else "down", "localhost:11434"))

    # State DB
    state_dir = atlas_paths.state_dir()
    db_path = state_dir / "state.sqlite"
    if db_path.exists():
        try:
            from atlas_plus.brain.task_facts import pending_count
            pf = pending_count()
            checks.append(("State DB", "ok", f"{db_path.stat().st_size // 1024} KB, {pf} pending facts"))
        except Exception as exc:
            checks.append(("State DB", "error", str(exc)[:60]))
    else:
        checks.append(("State DB", "missing", str(db_path)))

    # Memory root
    memory_root = atlas_paths.memory_root()
    if memory_root.exists():
        md_count = sum(1 for _ in memory_root.rglob("*.md"))
        checks.append(("Memory root", "ok", f"{md_count} .md files"))
    else:
        checks.append(("Memory root", "missing", str(memory_root)))

    # Escalation
    esc = os.environ.get("ATLAS_CLAUDE_ESCALATION", "off")
    qual = os.environ.get("ATLAS_CLAUDE_QUALITY_ESCALATION", "off")
    checks.append(("Escalation", esc, f"quality={qual}, threshold={os.environ.get('ATLAS_CLAUDE_QUALITY_THRESHOLD','3.5')}"))

    # SLA
    sla = os.environ.get("ATLAS_SLA_MINUTES", "15")
    ntfy = "set" if os.environ.get("ATLAS_NTFY_URL") else "not set"
    checks.append(("SLA alerting", f"{sla}min", f"ntfy: {ntfy}"))

    # CLI history dir
    history_dir = _CLI_HISTORY_DIR
    if history_dir.exists():
        jsonl_files = list(history_dir.glob("*.jsonl"))
        checks.append(("CLI history", "ok", f"{len(jsonl_files)} session(s) in {history_dir}"))
    else:
        checks.append(("CLI history", "empty", "no sessions saved yet"))

    t = Table(show_header=True, header_style="bold", title="Brain CLI Check")
    t.add_column("Check", width=16)
    t.add_column("Status", width=10)
    t.add_column("Detail", width=50)
    _STATUS_COLORS = {"ok": "green", "on": "green", "down": "red", "missing": "red", "error": "red", "off": "yellow"}
    for label, status, detail in checks:
        sc = _STATUS_COLORS.get(status.lower(), "white")
        t.add_row(label, f"[{sc}]{status}[/{sc}]", detail)
    console.print(t)


def _check_ollama_reachable() -> bool:
    """Return True if Ollama is reachable on the default port."""
    try:
        import httpx
        from atlas_plus.brain.agent import OLLAMA_BASE_URL
        r = httpx.get(f"{OLLAMA_BASE_URL}/api/tags", timeout=2.0)
        return r.status_code == 200
    except Exception:
        return False


_CLI_COMMANDS = [
    "/tasks", "/recent", "/task", "/submit", "/watch", "/retry", "/kill", "/escalate",
    "/priority", "/stats", "/pending", "/approve", "/reject", "/save",
    "/browse", "/confirm", "/search", "/agents", "/escalation",
    "/sessions", "/history", "/export", "/topics", "/decay", "/digest",
    "/quality", "/research-index", "/research-gaps", "/gap-report", "/health", "/dispatch-tickets", "/ticket-status",
    "/verify", "/orphans", "/plan", "/loop", "/attempts", "/exhausted", "/clear",
    "/suggest-tags", "/suggest-facts", "/next-actions",
    "/session-log", "/curation-report", "/decay-report", "/budget", "/ticket-sync",
    "/temporal-rebuild", "/temporal-search", "/temporal-history", "/temporal-entities",
    "/help", "/quit", "/exit",
]


def _cli_completer(text: str, state: int):
    """Tab completer for Brain CLI /commands."""
    try:
        options = [c for c in _CLI_COMMANDS if c.startswith(text)]
        return options[state] if state < len(options) else None
    except Exception:
        return None


def _setup_readline(history_file: str | None = None) -> None:
    """Enable up-arrow command history and tab completion for the Brain CLI REPL.

    Falls back gracefully on Windows (pyreadline3 may not be installed).
    History is saved to ATLAS_STATE_DIR/cli-history.txt and persists across sessions.
    """
    import os
    try:
        import readline
        if history_file is None:
            state_dir = str(atlas_paths.state_dir())
            history_file = str(Path(state_dir) / "cli-history.txt")
        # Load prior history
        try:
            readline.read_history_file(history_file)
        except FileNotFoundError:
            pass
        readline.set_history_length(500)
        # Tab completion for / commands
        readline.set_completer(_cli_completer)
        readline.set_completer_delims(" \t\n")
        readline.parse_and_bind("tab: complete")
        # Flush to file on exit
        import atexit
        atexit.register(readline.write_history_file, history_file)
    except (ImportError, AttributeError):
        pass  # readline unavailable (Windows without pyreadline3) -- silent no-op


def _show_cli_sessions() -> None:
    """Display saved CLI session list from .atlas/cli-history/."""
    sessions = _list_cli_sessions()
    if not sessions:
        console.print("[dim]No saved CLI sessions found in .atlas/cli-history/.[/dim]")
        console.print("[dim]Sessions are created automatically when you chat.[/dim]")
        return
    t = Table(show_header=True, header_style="bold",
              title=f"Saved CLI Sessions ({len(sessions)})")
    t.add_column("Session ID", style="cyan", width=30)
    t.add_column("Turns", width=8)
    t.add_column("Last Active", width=22)
    for s in sessions:
        t.add_row(s["session_id"], str(s["turn_count"]), (s["last_ts"] or "")[:19])
    console.print(t)
    console.print(
        "[dim]Resume: .venv\\Scripts\\python.exe -m atlas_plus.brain.cli --session <id>[/dim]"
    )


def run(session_id: str) -> None:
    """Start the interactive Brain REPL for the given session ID."""
    from atlas_plus.brain.agent import chat, DEFAULT_MODEL, OLLAMA_BASE_URL

    # Check if resuming an existing session
    existing_count = _session_message_count(session_id)
    resume_note = ""
    if existing_count > 0:
        resume_note = f"[dim]Resuming session with [bold]{existing_count}[/bold] prior message(s).[/dim]"

    hint_line = _startup_hints()
    banner_body = (
        f"{_BANNER}\n"
        f"[dim]session:[/dim] {session_id}  "
        f"[dim]model:[/dim] {DEFAULT_MODEL}  "
        f"[dim]ollama:[/dim] {OLLAMA_BASE_URL}\n"
        f"{_HINT}"
    )
    if resume_note:
        banner_body += f"\n{resume_note}"
    if hint_line:
        banner_body += f"\n{hint_line}"

    console.print(Panel(banner_body, expand=False))
    _setup_readline()

    # Quick Ollama liveness check
    if not _check_ollama_reachable():
        console.print(
            "[yellow]Warning:[/yellow] Ollama is not reachable. "
            "Run `ollama serve` to start it. Chat will fail until Ollama is running."
        )

    while True:
        try:
            user_input = console.input("[bold green]you>[/bold green] ").strip()
        except (KeyboardInterrupt, EOFError):
            console.print("\n[dim]Bye.[/dim]")
            break

        if not user_input:
            continue

        if user_input.startswith("/"):
            if not _handle_command(user_input, session_id):
                break
            continue

        # Save user turn to JSONL before sending (so it's logged even if Brain errors)
        _append_cli_turn(session_id, "user", user_input)

        console.print("[dim]Thinking...[/dim]")
        try:
            result = chat(session_id=session_id, user_message=user_input)
            _print_response(result.text, result.tool_calls_count, result.tool_log)
            # Save assistant turn to JSONL
            _append_cli_turn(session_id, "assistant", result.text)
        except Exception as exc:
            console.print(f"[red]Brain error: {exc}[/red]")


def main() -> None:
    """Entry point for the Brain CLI: parse args and dispatch to the interactive REPL or list/history views."""
    import argparse
    parser = argparse.ArgumentParser(
        description="Atlas Brain CLI -- local Qwen3 terminal chat",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  .venv\\Scripts\\python.exe -m atlas_plus.brain.cli\n"
            "  .venv\\Scripts\\python.exe -m atlas_plus.brain.cli --session project-planning\n"
            "  .venv\\Scripts\\python.exe -m atlas_plus.brain.cli --list-sessions\n"
            "  .venv\\Scripts\\python.exe -m atlas_plus.brain.cli --history --session project-planning\n"
        ),
    )
    parser.add_argument("--session", default=None, help="Session ID (default: cli-YYYY-MM-DD)")
    parser.add_argument(
        "--history", action="store_true",
        help="Show last 20 messages for the session and exit",
    )
    parser.add_argument(
        "--list-sessions", action="store_true",
        help="List all saved CLI sessions from .atlas/cli-history/ and exit",
    )
    args = parser.parse_args()

    session_id = args.session or _today_session_id()

    if args.list_sessions:
        _show_cli_sessions()
        return

    if args.history:
        _show_history(session_id)
        return

    run(session_id)


if __name__ == "__main__":
    main()
