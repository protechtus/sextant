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

    date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # --- 1. Load ---
    console.print(f"Loading turns (source={source}, since={since})...")
    turns = load_turns(source=source, conversation_id=conversation_id, since=since, until=until)
    if not turns:
        console.print("[yellow]No turns found for the given filters. Check --since or --source.[/yellow]")
        raise typer.Exit(1)
    console.print(f"Loaded [bold]{len(turns)}[/bold] turns.")

    # --- 2. Chunk ---
    chunks = chunk_turns(turns, token_budget=token_budget)
    console.print(f"Split into [bold]{len(chunks)}[/bold] chunk(s).")

    # --- 3. Map ---
    chunk_summaries: list[str] = []
    for chunk in chunks:
        console.print(f"Summarizing chunk {chunk.index + 1}/{len(chunks)}...")
        prompt = map_prompt(chunk.text, chunk.index, len(chunks))
        try:
            text, provider = complete(prompt)
            console.print(f"  [dim]via {provider}[/dim]")
        except ProviderError as e:
            console.print(f"[red]LLM call failed:[/red] {e}")
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
            raise typer.Exit(1)
        summary = _parse_json_response(final_text)
    else:
        summary = _parse_json_response(chunk_summaries[0])

    # --- 5. Render ---
    session_log_text = render_session_log(summary, date)
    updates_text = render_updates_entry(summary, date)

    # --- 6. Review ---
    if auto_commit or dry_run:
        accepted, session_log_text, updates_text = True, session_log_text, updates_text
    else:
        accepted, session_log_text, updates_text = review(session_log_text, updates_text)

    if not accepted:
        raise typer.Exit(0)

    # --- 7. Write ---
    result = write_outputs(session_log_text, updates_text, date, dry_run=dry_run)
    dry_tag = " (dry-run)" if dry_run else ""
    for label, path in result.items():
        console.print(f"[green]Written{dry_tag}:[/green] {label} -> {path}")

    console.print("\n[bold green]Done.[/bold green]")
