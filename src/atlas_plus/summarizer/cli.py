"""P2-T1: atlas-summarize-session CLI.

Orchestrates: load turns -> chunk -> map summarize -> reduce -> render -> review -> write.

Usage:
  atlas-summarize-session
  atlas-summarize-session --since today --source claude-code
  atlas-summarize-session --since 4h --dry-run
  atlas-summarize-session --auto-commit
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

import typer
from atlas_plus._typer import Typer as _SextantTyper  # no Windows $VAR expansion
from rich.console import Console

from .chunker import chunk_turns, needs_reduce
from .loader import load_turns
from .prompts import map_prompt, reduce_prompt, render_session_log, render_updates_entry
from .providers import ProviderError, complete
from .renderer import write_outputs
from .review import review

app = _SextantTyper(help="Summarize a Claude Code session into the Atlas session log")
console = Console()
log = logging.getLogger("atlas.summarizer")


def _redact(text: str) -> str:
    try:
        from atlas_plus.redaction import redact
        return redact(text) or ""
    except Exception:  # noqa: BLE001
        return text


def _event(kind: str, **fields) -> None:
    """One activity-log line; the Stop hook runs this detached with its output discarded, so the
    log is the only place a failure can be seen (MN28: empty, 500 and 404 replies were silent)."""
    try:
        from atlas_plus import activity
        activity.record("hook", kind, **fields)
    except Exception:  # noqa: BLE001
        pass


def _min_new_turns() -> int:
    import os as _os
    try:
        return max(1, int(_os.environ.get("ATLAS_SUMMARIZER_MIN_NEW_TURNS", "6")))
    except ValueError:
        return 6


def _acquire_session_lock(safe_id: str, stale_s: float = 900.0) -> bool:
    """One summarizer per session at a time: `<state>/summarizer/<id>.lock`, released at exit."""
    import atexit
    import os as _os
    import time as _time
    try:
        from atlas_plus import paths as _paths
        lock = _paths.state_dir() / "summarizer" / f"s-{safe_id or 'anon'}.lock"  # `CON`/`nul` never locked on Windows (AG44)
        lock.parent.mkdir(parents=True, exist_ok=True)
        try:
            if _time.time() - lock.stat().st_mtime > stale_s:
                lock.unlink()
        except OSError:
            pass
        fd = _os.open(str(lock), _os.O_CREAT | _os.O_EXCL | _os.O_WRONLY)
        _os.close(fd)
    except FileExistsError:
        return False
    except OSError:
        return True  # no state dir to lock in: proceed

    def _release():
        try:
            lock.unlink()
        except OSError:
            pass
    atexit.register(_release)
    return True


def _looks_like_summary(summary) -> bool:
    if not isinstance(summary, dict) or summary.get("_fallback"):
        return False
    title = summary.get("title")
    if not isinstance(title, str) or not title.strip() or len(title) > 160 or "\n" in title or "JSON" in title:
        return False
    return any(isinstance(summary.get(k), list) and summary.get(k) for k in ("completed", "pending", "worked_on"))


def _parse_json_response(text: str) -> dict:
    """Extract JSON object from LLM response, tolerating markdown fences."""
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        inner = [l for l in lines[1:] if not l.startswith("```")]
        text = "\n".join(inner).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # best-effort: find first { ... }
        start = text.find("{")
        end = text.rfind("}") + 1
        if start >= 0 and end > start:
            try:
                return json.loads(text[start:end])
            except json.JSONDecodeError:
                pass
        return {
            "_fallback": True,  # plain text is not a summary (AG43: a junk reply became the session's entry)
            "title": "session summary",
            "worked_on": [],
            "completed": [text[:500]],
            "pending": [],
            "files_touched": [],
            "lessons": [],
        }


@app.command()
def main(
    source: str = typer.Option("claude-code", help="Transcript source: claude-code | atlas | auto"),
    conversation_id: str | None = typer.Option(None, "--conversation-id", "-c"),
    since: str | None = typer.Option("today", "--since", "-s", help="Start time: today | Nh | ISO timestamp"),
    until: str | None = typer.Option(None, "--until"),
    model: str | None = typer.Option(None, "--model", help="Override LLM model (sets ATLAS_SUMMARIZER_MODEL)"),
    dry_run: bool = typer.Option(False, "--dry-run", "-n"),
    auto_commit: bool = typer.Option(False, "--auto-commit", help="Skip review and write immediately"),
    token_budget: int = typer.Option(6000, "--token-budget", help="Tokens per map chunk"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
):
    """Summarize a session and write to .memory/sessions/ and updates.txt."""
    if verbose:
        logging.basicConfig(level=logging.INFO, format="%(name)s %(levelname)s %(message)s")

    if model:
        import os
        os.environ["ATLAS_SUMMARIZER_MODEL"] = model

    date = datetime.now().astimezone().strftime("%Y-%m-%d")  # the user's day (MN28)
    if not dry_run:
        from atlas_plus.autonomous import is_session_autonomous as _gate
        if not _gate():
            # the gate used to run AFTER the model call: the transcript went out, then a traceback (AG38)
            console.print("[yellow]Session summaries are off: ATLAS_SESSION_AUTONOMOUS=on (or ATLAS_AUTONOMOUS=on) must reach this process (the Stop hook's --env flag).[/yellow]")
            _event("summarizer_skipped", reason="ATLAS_SESSION_AUTONOMOUS is off", conversation_id=conversation_id or "")
            raise typer.Exit(2)
    marker = ""
    replace_prefix = ""
    prior_turns = None
    safe_id = ""
    if conversation_id:
        from atlas_plus.summarizer.renderer import MEMORY_SESSIONS_DIR as _sdir, find_session_entry
        safe_id = ''.join(ch for ch in conversation_id if ch.isalnum() or ch in '-_.')[:80]
        found = find_session_entry(_sdir, safe_id)
        if found is not None:
            prior_turns = found[1]
        if since == "today":
            since = None  # the session is the unit: a session across midnight lost its first hour (AG40)
        if not dry_run and not _acquire_session_lock(safe_id):
            console.print(f"[yellow]Another summarizer run for session {conversation_id} is in progress; nothing to do.[/yellow]")
            raise typer.Exit(0)  # four Stops in one second each paid for the model (MN33)

    # --- 1. Load ---
    console.print(f"Loading turns (source={source}, since={since})...")
    try:
        turns = load_turns(source=source, conversation_id=conversation_id, since=since, until=until)
    except ValueError as exc:  # a bad --since/--source is one line, not a traceback (AG34)
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(2)
    if not turns:
        console.print("[yellow]No turns found for the given filters. Check --since or --source.[/yellow]")
        _event("summarizer_failed", reason="no turns found", conversation_id=conversation_id or "")
        raise typer.Exit(1)
    console.print(f"Loaded [bold]{len(turns)}[/bold] turns.")
    if conversation_id:
        # Claude Code fires Stop after every reply: the entry is refreshed when the session grew by
        # ATLAS_SUMMARIZER_MIN_NEW_TURNS turns, and left alone otherwise (MN33: the first summary froze)
        if prior_turns is not None and len(turns) - prior_turns < _min_new_turns():
            console.print(f"[yellow]Session {conversation_id} is summarised at {prior_turns} turns and has {len(turns)} now: nothing new.[/yellow]")
            raise typer.Exit(0)
        marker = f"<!-- session:{safe_id} turns:{len(turns)} -->"
        replace_prefix = f"<!-- session:{safe_id}" if prior_turns is not None else ""

    # --- 2. Chunk ---
    chunks = chunk_turns(turns, token_budget=token_budget)
    console.print(f"Split into [bold]{len(chunks)}[/bold] chunk(s).")

    # --- 3. Map ---
    chunk_summaries: list[str] = []
    for chunk in chunks:
        console.print(f"Summarizing chunk {chunk.index + 1}/{len(chunks)}...")
        prompt = map_prompt(_redact(chunk.text), chunk.index, len(chunks))  # keys never reach the model (MN28)
        try:
            text, provider = complete(prompt)
            console.print(f"  [dim]via {provider}[/dim]")
        except ProviderError as e:
            console.print(f"[red]LLM call failed:[/red] {e}")
            _event("summarizer_failed", reason=str(e)[:200], conversation_id=conversation_id or "")
            raise typer.Exit(1)
        chunk_summaries.append(text)

    # --- 4. Reduce ---
    if needs_reduce(chunks):
        console.print("Reducing chunk summaries...")
        prompt = reduce_prompt(chunk_summaries, date)
        try:
            final_text, provider = complete(prompt)
            console.print(f"  [dim]via {provider}[/dim]")
        except ProviderError as e:
            console.print(f"[red]Reduce call failed:[/red] {e}")
            _event("summarizer_failed", reason=str(e)[:200], conversation_id=conversation_id or "")
            raise typer.Exit(1)
        summary = _parse_json_response(final_text)
    else:
        summary = _parse_json_response(chunk_summaries[0])
    if not _looks_like_summary(summary):
        # a non-JSON reply used to be written as the session's summary and then protected by its
        # marker (MN33: "things that were finished and confirmed working Pending: ...")
        console.print("[red]The model's reply was not a summary; nothing written.[/red]")
        _event("summarizer_failed", reason="model reply was not a summary", conversation_id=conversation_id or "")
        raise typer.Exit(1)

    # --- 5. Render ---
    session_log_text = _redact(render_session_log(summary, date))
    updates_text = _redact(render_updates_entry(summary, date))
    if marker:
        # the entry names its session and its turn count: a re-fired Stop writes nothing twice, a
        # grown session replaces its entry (MN28, MN33)
        head, _, rest = session_log_text.partition("\n")
        session_log_text = head + "\n" + marker + "\n" + rest
        uhead, _, urest = updates_text.partition("\n")
        updates_text = uhead + "\n" + marker + "\n" + urest  # the updates entry is found and replaced by its marker too (AG43)

    # --- 6. Review ---
    if auto_commit or dry_run:
        accepted, session_log_text, updates_text = True, session_log_text, updates_text
    else:
        accepted, session_log_text, updates_text = review(session_log_text, updates_text)

    if not accepted:
        raise typer.Exit(0)

    # --- 7. Write ---
    result = write_outputs(session_log_text, updates_text, date, dry_run=dry_run, session_marker=marker, replace_session=replace_prefix)
    if result.get("skipped"):
        console.print(f"[yellow]{result['skipped']}[/yellow]")
        raise typer.Exit(0)
    if not dry_run:
        _event("summary_written", path=result.get("session_log", ""), conversation_id=conversation_id or "")
    dry_tag = " (dry-run)" if dry_run else ""
    for label, path in result.items():
        console.print(f"[green]Written{dry_tag}:[/green] {label} -> {path}")

    console.print("\n[bold green]Done.[/bold green]")


if __name__ == "__main__":  # the Stop hook spawns `python -m atlas_plus.summarizer.cli` (AG34: it was a no-op)
    app()
