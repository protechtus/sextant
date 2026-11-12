"""atlas-temporal CLI -- query the Atlas temporal knowledge graph.

Commands:
    atlas-temporal build                      # index your corpus (--source fixtures loads demo data)
    atlas-temporal build --source memory      # load from .memory/ (second slice)
    atlas-temporal build --source atlas       # memory + sessions
    atlas-temporal build --source transcripts # Claude Code session metadata
    atlas-temporal build --source mining      # tool call patterns from transcripts
    atlas-temporal build --source all         # memory + sessions + transcripts + mining
    atlas-temporal query "..." --mode current
    atlas-temporal query "..." --mode current --json   # machine-readable output
    atlas-temporal query "..." --mode as-of --at 2026-05-08
    atlas-temporal history "Atlas MCP Tunnel"
    atlas-temporal history "Atlas MCP Tunnel" --json   # machine-readable output
    atlas-temporal entities                   # list all entities in the graph
    atlas-temporal entities --json            # as JSON array
    atlas-temporal stats
    atlas-temporal stats --json               # as JSON object
    atlas-temporal reset
"""

from __future__ import annotations

import typer
from atlas_plus._typer import Typer as _SextantTyper  # no Windows $VAR expansion
from pathlib import Path
from typing import Optional

from atlas_plus.temporal_graph.build import build_temporal_graph

app = _SextantTyper(
    name="atlas-temporal",
    help="Query the Atlas temporal knowledge graph.",
    add_completion=False,
)


def _get_provider(graph_root: Optional[Path] = None):
    from atlas_plus.temporal_graph.file_provider import FileTemporalProvider
    return FileTemporalProvider(graph_root=graph_root)


@app.command("build")
def build(
    source: str = typer.Option("memory", "--source", "-s", help="Source to ingest: memory (default: your corpus) | sessions | transcripts | mining | atlas | all | fixtures (built-in sample data, demo only)"),
    graph_root: Optional[Path] = typer.Option(None, "--graph-root", help="Override graph storage dir"),
    force: bool = typer.Option(False, "--force", "-f", help="Reset derived state before building"),
):
    """Ingest source data into the temporal graph."""
    from atlas_plus.temporal_graph.file_provider import FileTemporalProvider

    if force:
        provider = FileTemporalProvider(graph_root=graph_root)
        provider.reset_derived_state()
        typer.echo("Derived state reset.")

    result = build_temporal_graph(source=source, graph_root=graph_root)
    if "error" in result:
        typer.echo(str(result["error"]), err=True)
        raise typer.Exit(1)

    typer.echo(
        f"  [{result['source']}] indexed={result['files_indexed']} "
        f"skipped={result['files_skipped']} facts={result['facts_added']}"
    )
    # Surface the maintenance the build performed; silent background work
    # the operator cannot see might as well not have happened.
    retracted = result.get("files_retracted", 0)
    if retracted:
        typer.echo(
            f"  retracted={retracted} vanished file(s), "
            f"{result.get('facts_invalidated', 0)} fact(s) invalidated (kept for as-of history)"
        )
    for name, rep in sorted(result.get("compaction", {}).items()):
        typer.echo(
            f"  compacted {name}: {rep.get('bytes_before', 0):,} -> {rep.get('bytes_after', 0):,} bytes"
        )
    for warning in result.get("warnings", []):
        typer.echo(f"    Warning: {warning}", err=True)

    stats = result["index"]
    typer.echo(
        f"Index: {stats['episodes']} episodes, {stats['entities']} entities, "
        f"{stats['facts_total']} facts ({stats['facts_active']} active)."
    )


@app.command("query")
def query(
    q: str = typer.Argument(..., help="Query string"),
    mode: str = typer.Option("current", "--mode", "-m", help="Query mode: current | as-of"),
    at: Optional[str] = typer.Option(None, "--at", help="Point in time for as-of (ISO 8601 date)"),
    limit: int = typer.Option(5, "--limit", "-n"),
    graph_root: Optional[Path] = typer.Option(None, "--graph-root"),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON array"),
):
    """Query the temporal graph for facts matching a query string."""
    import json as _json

    provider = _get_provider(graph_root)
    stats = provider.stats()
    if stats["facts"] == 0:
        if json_output:
            typer.echo(_json.dumps({"error": "graph_empty", "results": []}))
        else:
            typer.echo("Temporal graph is empty. Run: atlas-temporal build")
        raise typer.Exit(1)

    if mode == "current":
        results = provider.search_current(q, limit=limit)
        label = "current"
    elif mode == "as-of":
        if not at:
            typer.echo("--at DATE is required for as-of mode.", err=True)
            raise typer.Exit(1)
        results = provider.search_as_of(q, at=at, limit=limit)
        label = f"as-of {at}"
    else:
        typer.echo(f"Unknown mode: {mode!r}. Valid: current, as-of", err=True)
        raise typer.Exit(1)

    if json_output:
        out = []
        for r in results:
            out.append({
                "score": round(r.score, 4),
                "fact": r.fact.fact_text,
                "valid_from": r.fact.valid_from,
                "invalid_at": r.fact.invalid_at,
                "provenance": r.provenance,
                "freshness_warning": r.freshness_warning,
            })
        typer.echo(_json.dumps({"query": q, "mode": label, "results": out}, indent=2))
        return

    typer.echo(f"{label.capitalize()} ({len(results)} result(s) for {q!r}):\n")
    for i, r in enumerate(results, 1):
        score_str = f"{r.score:.2f}"
        typer.echo(f"  [{i}] (score={score_str}) {r.fact.fact_text}")
        typer.echo(f"       valid_from={r.fact.valid_from}")
        if r.fact.invalid_at:
            typer.echo(f"       invalid_at={r.fact.invalid_at}")
        typer.echo(f"       provenance={r.provenance}")
        if r.freshness_warning:
            typer.echo(f"       WARNING: {r.freshness_warning}")
        typer.echo()

    if not results:
        typer.echo("  (no matching facts)")


@app.command("history")
def history(
    entity_query: str = typer.Argument(..., help="Entity name or alias to look up"),
    limit: int = typer.Option(50, "--limit", "-n"),
    graph_root: Optional[Path] = typer.Option(None, "--graph-root"),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON object"),
):
    """Show the full temporal history of an entity."""
    import json as _json

    provider = _get_provider(graph_root)
    h = provider.history(entity_query, limit=limit)

    if json_output:
        facts_out = []
        for fact in h.facts:
            facts_out.append({
                "fact_text": fact.fact_text,
                "valid_from": fact.valid_from,
                "invalid_at": fact.invalid_at,
                "confidence": round(fact.confidence, 4),
                "authority": fact.authority,
                "supersedes_fact_id": fact.supersedes_fact_id,
                "is_current": fact is h.current_fact,
            })
        typer.echo(_json.dumps({
            "entity": {
                "name": h.entity.name,
                "labels": list(h.entity.labels),
                "aliases": list(h.entity.aliases),
                "first_seen_at": h.entity.first_seen_at,
                "last_seen_at": h.entity.last_seen_at,
            },
            "facts": facts_out,
            "warnings": list(h.warnings),
        }, indent=2))
        return

    typer.echo(f"History: {h.entity.name}")
    typer.echo(f"  Labels: {', '.join(h.entity.labels) or 'none'}")
    typer.echo(f"  Aliases: {', '.join(h.entity.aliases) or 'none'}")
    typer.echo(f"  First seen: {h.entity.first_seen_at}")
    typer.echo(f"  Last seen: {h.entity.last_seen_at}")
    typer.echo()

    if not h.facts:
        typer.echo("  (no facts in history)")
    else:
        typer.echo(f"Timeline ({len(h.facts)} fact(s)):\n")
        for fact in h.facts:
            status = "ACTIVE" if fact.invalid_at is None else f"SUPERSEDED at {fact.invalid_at}"
            prefix = "  -->" if fact is h.current_fact else "  ---"
            typer.echo(f"{prefix} [{fact.valid_from[:10]}] {fact.fact_text}")
            typer.echo(f"       {status} | conf={fact.confidence:.2f} | {fact.authority}")
            if fact.supersedes_fact_id:
                typer.echo(f"       supersedes: {fact.supersedes_fact_id}")
            typer.echo()

    if h.warnings:
        for w in h.warnings:
            typer.echo(f"  Warning: {w}", err=True)


@app.command("stats")
def stats_cmd(
    graph_root: Optional[Path] = typer.Option(None, "--graph-root"),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON object"),
):
    """Show temporal graph index statistics."""
    import json as _json

    provider = _get_provider(graph_root)
    s = provider.stats()

    if json_output:
        typer.echo(_json.dumps(s, indent=2))
        return

    typer.echo(f"Episodes:      {s['episodes']}")
    typer.echo(f"Entities:      {s['entities']}")
    typer.echo(f"Facts (total): {s['facts']}")
    typer.echo(f"Facts (active): {s['facts_active']}")
    typer.echo(f"Graph root:    {s['graph_root']}")


@app.command("entities")
def entities_cmd(
    graph_root: Optional[Path] = typer.Option(None, "--graph-root"),
    limit: int = typer.Option(50, "--limit", "-n", help="Max entities to list"),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON array"),
):
    """List all entities in the temporal graph."""
    import json as _json

    provider = _get_provider(graph_root)
    s = provider.stats()
    if s["entities"] == 0:
        if json_output:
            typer.echo(_json.dumps([]))
        else:
            typer.echo("No entities in graph. Run: atlas-temporal build")
        return

    all_entities = provider.list_entities(limit=limit)

    if json_output:
        out = []
        for e in all_entities:
            out.append({
                "name": e.name,
                "labels": list(e.labels),
                "aliases": list(e.aliases),
                "first_seen_at": e.first_seen_at,
                "last_seen_at": e.last_seen_at,
            })
        typer.echo(_json.dumps(out, indent=2))
        return

    typer.echo(f"Entities ({len(all_entities)} shown, limit={limit}):\n")
    for e in all_entities:
        aliases_str = f" [{', '.join(e.aliases)}]" if e.aliases else ""
        labels_str = f" ({', '.join(e.labels)})" if e.labels else ""
        typer.echo(f"  {e.name}{aliases_str}{labels_str}")


@app.command("reset")
def reset_cmd(
    graph_root: Optional[Path] = typer.Option(None, "--graph-root"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
):
    """Delete all derived temporal graph state (safe -- rebuild from source any time)."""
    if not yes:
        typer.confirm("Reset all derived temporal graph state?", abort=True)
    provider = _get_provider(graph_root)
    provider.reset_derived_state()
    typer.echo("Derived state reset. Run: atlas-temporal build")


def main():
    """Entry point for the atlas-temporal CLI (Typer app)."""
    from atlas_plus._console import utf8_stdout_when_piped
    utf8_stdout_when_piped()
    app()


if __name__ == "__main__":
    main()
