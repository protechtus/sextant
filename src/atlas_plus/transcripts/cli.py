"""atlas-transcripts -- thin CLI over the transcript index (list / search / recover).

The transcripts package (SQLite+FTS5 index, NDJSON store, crash recovery) predates this
CLI; the console entry point pointed at a module that was never built, so the command was
DOA on install until 2026-07-02. This is deliberately pure plumbing: every subcommand maps
1:1 onto an existing public function (TranscriptIndex.list_conversations / .search,
recovery.recover_all) with no new behavior.
"""

from __future__ import annotations

import typer
from atlas_plus._typer import Typer as _SextantTyper  # no Windows $VAR expansion
from rich.console import Console
from rich.table import Table

app = _SextantTyper(help="Transcript index: list conversations, full-text search, crash recovery.")
console = Console()


@app.command("list")
def cmd_list(
    source: str = typer.Option(None, "--source", "-s", help="Filter by source (e.g. claude-code)."),
    limit: int = typer.Option(20, "--limit", "-n"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """List recent conversations (newest first)."""
    import json as _json

    from atlas_plus.transcripts.index import TranscriptIndex

    rows = TranscriptIndex().list_conversations(source=source, limit=limit)
    if json_output:
        typer.echo(_json.dumps(rows, indent=2, default=str))
        return
    table = Table(title=f"conversations ({len(rows)})")
    for col in ("conv_id", "source", "started_at", "last_seen_at", "turn_count"):
        table.add_column(col, overflow="fold")
    for r in rows:
        table.add_row(*(str(r.get(c, "")) for c in ("conv_id", "source", "started_at", "last_seen_at", "turn_count")))
    console.print(table)


@app.command("search")
def cmd_search(
    query: str = typer.Argument(..., help="FTS5 query over turn content."),
    limit: int = typer.Option(20, "--limit", "-n"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Full-text search across transcript turns."""
    import json as _json

    from atlas_plus.transcripts.index import TranscriptIndex

    hits = list(TranscriptIndex().search(query, limit=limit))
    if json_output:
        typer.echo(_json.dumps(hits, indent=2, default=str))
        return
    if not hits:
        console.print("[dim]no matches[/dim]")
        return
    for h in hits:
        console.print(f"[bold]{h.get('conv_id', '?')}[/bold] {h.get('ts', '')} [dim]{h.get('role', '')}[/dim]")
        preview = str(h.get("content_preview", h.get("snippet", "")))[:200]
        console.print(f"  {preview}")


@app.command("recover")
def cmd_recover(
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Reconcile .partial NDJSON files after a crash (walk, validate, promote)."""
    import dataclasses
    import json as _json

    from atlas_plus.transcripts.recovery import recover_all

    report = recover_all()
    data = dataclasses.asdict(report)
    if json_output:
        typer.echo(_json.dumps(data, indent=2))
        return
    for key, value in data.items():
        console.print(f"  {key:<20} {value}")


@app.command("ingest")
def cmd_ingest(
    path: str = typer.Argument("", help="File or directory to ingest (default: the drop dir)"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Ingest dropped transcript files (.jsonl / .json / .md / .txt).

    Any tool that can write a file can feed Sextant: drop exports into the
    drop directory (see `docs/USAGE.md`) and run this. Ingested files are
    archived alongside, never deleted.
    """
    import json as _json
    from pathlib import Path as _Path

    from atlas_plus.transcripts.sources.drop import drop_dir, ingest

    target = _Path(path) if path else None
    result = ingest(target)
    if json_output:
        typer.echo(_json.dumps(result, indent=2))
        return
    if result["files"] == 0:
        console.print(
            f"Nothing to ingest. Drop .jsonl/.json/.md/.txt files into "
            f"[bold]{drop_dir()}[/bold] and re-run."
        )
    else:
        console.print(
            f"Ingested [bold]{result['turns']}[/bold] turn(s) from "
            f"{result['files']} file(s); {result['skipped']} skipped. "
            f"Originals archived to {result['archived_to']}."
        )


if __name__ == "__main__":
    app()
