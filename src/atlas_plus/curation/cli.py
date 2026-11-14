"""P7-T3: Interactive curation review CLI — atlas-review-memory."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer
from atlas_plus._typer import Typer as _SextantTyper  # no Windows $VAR expansion
from rich.console import Console
from rich.table import Table

from atlas_plus.autonomous import require_autonomous_writes
from atlas_plus.mcp.memory_server.config import MEMORY_ROOT
from atlas_plus import paths as atlas_paths

app = _SextantTyper(help="Atlas memory curation hygiene tools")
console = Console()


@app.command("duplicates")
def cmd_duplicates(
    threshold: float = typer.Option(0.40, "--threshold", "-t", help="Jaccard similarity floor (0-1)"),
    scope: str = typer.Option("", "--scope", "-s", help="Top-level corpus folder to restrict to (reference, projects, identity, preference, or a sub-folder path like reference/obsidian); a `topic:name` route belongs to remember, not here"),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON"),
    facts: bool = typer.Option(False, "--facts", "-f",
                               help="repeated FACTS across notes (exact and near), not similar files"),
):
    """Find near-duplicate files in .memory/ (or, with --facts, repeated facts)."""
    from atlas_plus.curation.duplicates import find_duplicates, find_fact_duplicates
    if facts:
        rows = find_fact_duplicates(scope=scope)
        if json_output:
            import json
            typer.echo(json.dumps(rows, indent=2))
            return
        if not rows:
            console.print("[green]No repeated facts found.[/green]")
            return
        table = Table(title="Repeated facts", show_lines=True)
        table.add_column("Kind")
        table.add_column("File 1", style="cyan", overflow="fold")
        table.add_column("File 2", style="cyan", overflow="fold")
        table.add_column("Fact", overflow="fold")
        for r in rows[:60]:
            table.add_row(r["kind"], r["file1"], r["file2"], r["fact"])
        console.print(table)
        exact = sum(1 for r in rows if r["kind"] == "exact")
        console.print(f"\n[yellow]{exact} exact and {len(rows) - exact} near repeat(s)"
                      f"{' (first 60 shown; --json for all)' if len(rows) > 60 else ''}.[/yellow]")
        return
    pairs = find_duplicates(threshold=threshold, scope=scope)

    if json_output:
        import json
        typer.echo(json.dumps([p.as_dict() for p in pairs], indent=2))
        return

    if not pairs:
        console.print("[green]No duplicate files found. Repeated facts inside notes: "
                      "atlas-review-memory duplicates --facts[/green]")
        return

    table = Table(title=f"Potential Duplicates (threshold={threshold})", show_lines=True)
    table.add_column("File 1", style="cyan", overflow="fold")   # truncated paths could not be acted on (AG20)
    table.add_column("File 2", style="cyan", overflow="fold")
    table.add_column("Similarity", justify="right")
    for p in pairs:
        table.add_row(p.file1, p.file2, f"{p.similarity:.2f}")
    console.print(table)
    from atlas_plus.curation import duplicates as _dupmod
    console.print(f"\n[yellow]{len(pairs)}{'+' if _dupmod._LAST_CAPPED else ''} pair(s) found{' (capped at ' + str(_dupmod._MAX_FILE_PAIRS) + '; lower the threshold or add --json for the rest)' if _dupmod._LAST_CAPPED else ''}.[/yellow]")


@app.command("stale")
def cmd_stale(
    months: int = typer.Option(6, "--months", "-m", help="Flag files older than N months"),
    scope: str = typer.Option("", "--scope", "-s", help="Top-level corpus folder to restrict to (reference, projects, identity, preference, or a sub-folder path like reference/obsidian); a `topic:name` route belongs to remember, not here"),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON"),
):
    """Find files with stale 'updated:' frontmatter."""
    from atlas_plus.curation.stale import find_stale
    results = find_stale(months=months, scope=scope)

    if json_output:
        import json
        typer.echo(json.dumps([r.as_dict() for r in results], indent=2))
        return

    if not results:
        console.print("[green]No stale files found.[/green]")
        return

    table = Table(title=f"Stale Files (>{months} months)", show_lines=True)
    table.add_column("File", style="cyan")
    table.add_column("Last Updated")
    table.add_column("Age (days)", justify="right")
    for r in results:
        table.add_row(r.path, r.updated, str(r.age_days))
    console.print(table)
    console.print(f"\n[yellow]{len(results)} file(s) found.[/yellow]")


@app.command("archive")
def cmd_archive(
    path: str = typer.Argument(..., help="Relative .memory/ path to archive"),
    label: str = typer.Option("curation", "--label", help="Archive subfolder label"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
):
    """Archive a .memory/ file (move to archive/; never delete)."""
    from atlas_plus.curation.stale import archive_file

    if not yes:
        typer.confirm(f"Archive '{path}'? It will be moved to archive/. Continue?", abort=True)

    dest = archive_file(path, label=label)
    console.print(f"[green]Archived:[/green] {path} -> {dest}")


@app.command("contradictions")
def cmd_contradictions(
    scope: str = typer.Option("", "--scope", "-s"),
    dry_run: bool = typer.Option(False, "--dry-run", "-n", help="Show candidates without calling LLM"),
    json_output: bool = typer.Option(False, "--json"),
):
    """Detect contradictory facts between related .memory/ files (LLM-powered)."""
    from atlas_plus.curation import contradictions as _ct
    results = _ct.find_contradictions(scope=scope, dry_run=dry_run)
    stats = _ct.LAST_STATS

    if json_output:
        import json
        typer.echo(json.dumps([r.as_dict() for r in results], indent=2))
        if not dry_run and stats["unchecked"] and not stats["checked"]:
            raise typer.Exit(2)
        return

    if not dry_run and stats["unchecked"] and not stats["checked"]:
        # "No contradictions detected" used to print when nothing was checked at all (AG20).
        console.print(f"[yellow]No model answered: {stats['unchecked']} candidate pair(s) were not checked "
                      "(start Ollama at ATLAS_OLLAMA_URL, or set a provider key with ATLAS_LOCAL_ONLY=off). "
                      "Nothing was detected because nothing was checked.[/yellow]")
        raise typer.Exit(2)
    if not results:
        console.print(f"[green]No contradictions detected[/green] ({stats['checked']} pair(s) checked"
                      + (f", {stats['unchecked']} not answered" if stats['unchecked'] else "") + ").")
        return

    table = Table(title="Potential Contradictions", show_lines=True)
    table.add_column("File 1", style="cyan")
    table.add_column("File 2", style="cyan")
    table.add_column("Confidence")
    table.add_column("Explanation")
    for r in results:
        table.add_row(r.file1, r.file2, r.confidence, r.explanation)
    console.print(table)
    console.print(f"\n[yellow]{len(results)} contradiction(s) found.[/yellow]")


@app.command("suggest-facts")
def cmd_suggest_facts(
    since: str = typer.Option("today", "--since", "-s", help="Time window: 'today', 'Nh', 'Nm', or ISO timestamp"),
    source: str = typer.Option("claude-code", "--source", help="Transcript source: claude-code | atlas | auto"),
    dry_run: bool = typer.Option(False, "--dry-run", "-n", help="Print candidates without writing"),
    auto: bool = typer.Option(False, "--auto", "-a", help="Auto-approve facts with confidence >= 0.95"),
    skip_similar: float = typer.Option(0.0, "--skip-similar", help="Auto-skip candidates with Jaccard similarity >= threshold (0.0 = off, e.g. 0.6)"),
    threshold: float = typer.Option(0.0, "--threshold", help="Override confidence threshold (0.0 = use env default 0.70)"),
):
    """Scan a session transcript and propose facts to add to .memory/."""
    from atlas_plus.curation.suggest_facts import suggest_facts
    try:
        stats = suggest_facts(
            since=since,
            source=source,
            dry_run=dry_run,
            auto=auto,
            skip_similar_threshold=skip_similar,
            confidence_threshold=threshold if threshold > 0.0 else None,
        )
        rejected_lc = stats.get("rejected_low_confidence", 0)
        if stats["candidates"] > 0:
            rejected_note = f" rejected_low_conf={rejected_lc}" if rejected_lc > 0 else ""
            console.print(
                f"[dim]candidates={stats['candidates']} approved={stats['approved']} "
                f"written={stats['written']}{rejected_note}[/dim]"
            )
        elif rejected_lc > 0:
            console.print(f"[dim]No qualifying candidates (rejected {rejected_lc} low-confidence).[/dim]")
    except RuntimeError as e:
        console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(1)


def _stub_slug(query: str) -> str:
    """Convert a miss query to a safe filename slug (max 40 chars)."""
    import re
    s = query.lower().strip()
    s = re.sub(r"[^\w\s-]", "", s)
    s = re.sub(r"[\s_]+", "-", s)[:40].strip("-")
    return s or "gap"


def _create_gap_stubs(misses: list[dict], gaps_dir: Path, today_str: str) -> list[Path]:
    """Create stub .memory/gaps/ files for miss queries that lack existing coverage.

    Skips if a file with the same slug already exists. Returns list of created paths.
    """
    # ATL-018 / audit HIGH-3 completion: gate this .memory/ writer so the autonomous path
    # (the scheduled gaps_report executor running `gaps --autofile`) fail-closes when
    # ATLAS_AUTONOMOUS is off/unset, like the fact/ticket primitives already do.
    require_autonomous_writes("curation_gap_stub")
    from atlas_plus.mcp.memory_server.fts import search as mem_search
    gaps_dir.mkdir(parents=True, exist_ok=True)
    created: list[Path] = []
    for r in misses:
        query = r["query"]
        slug = _stub_slug(query)
        stub_path = gaps_dir / f"{slug}.md"
        if stub_path.exists():
            continue
        # Skip if memory search already has a decent hit for this query
        try:
            hits = mem_search(query, limit=3)
            if hits and any(h.get("score", 0) > -5 for h in hits):
                # FTS5 rank is negative (lower = better). -5 is a loose threshold;
                # if there's any non-trivial hit we don't stub.
                # Check result_count from the miss record instead.
                if r.get("total", 0) > 0 and r.get("miss_count", 0) / r["total"] < 0.8:
                    continue
        except Exception:
            pass
        import yaml as _yaml
        fm = _yaml.safe_dump(
            {
                "title": query,
                "type": "gap-stub",
                "created": str(today_str),
                "source_query": query,
                "miss_count": r["miss_count"],
                "status": "unfilled",
            },
            sort_keys=False, allow_unicode=True, default_flow_style=False, width=10**6,
        )
        safe_heading = " ".join(str(query).split())
        content = (
            f"---\n{fm}---\n\n"
            f"# {safe_heading}\n\n"
            f"<!-- Gap stub auto-created by atlas-review-memory gaps --autofile -->\n"
            f"<!-- Fill in relevant facts here and change status: unfilled -> active -->\n\n"
            f"## Notes\n\n"
            f"(no information captured yet)\n"
        )
        stub_path.write_text(content, encoding="utf-8")
        created.append(stub_path)
    return created


def _autofile_gap_tickets(misses: list[dict]) -> list:
    """Create dispatch-eligible tickets for memory gaps with no existing research report.

    For each top miss, checks research reports for coverage using similarity scoring.
    Skips if a similar report already exists (score >= 0.4) or a gap ticket with the
    same title is already open. Returns a list of created Ticket objects.
    """
    from atlas_plus.tickets.store import create_ticket, list_tickets
    from atlas_plus.brain.research import find_similar_research_reports

    # Build set of existing gap ticket titles (lowercase) to avoid duplicates
    try:
        existing = list_tickets(project="atlas-memory+", status="todo")
        existing_titles = {t.title.lower() for t in existing if "research gap" in t.title.lower()}
    except Exception:
        existing_titles = set()

    created = []
    for r in misses:
        query = r["query"]
        title = f"Research gap: {query}"

        # Skip if an open ticket already covers this gap
        if title.lower() in existing_titles:
            continue

        # Skip if existing research report covers this topic adequately
        try:
            similar = find_similar_research_reports(query, threshold=0.4, max_age_days=60)
            if similar:
                continue
        except Exception:
            pass

        body = (
            f"Auto-filed by atlas-review-memory gaps --autoticket.\n\n"
            f"Topic: {query}\n"
            f"Miss count (7d): {r.get('miss_count', '?')}\n"
            f"Total queries: {r.get('total', '?')}\n\n"
            f"No research report found for this topic. "
            f"Assign to claude-code or local to produce a .atlas/reports/ entry."
        )
        try:
            ticket = create_ticket(
                title=title,
                project="atlas-memory+",
                assignee="any",
                priority="normal",
                effort="S",
                body=body,
            )
            created.append(ticket)
        except Exception:
            continue

    return created


@app.command("gaps")
def cmd_gaps(
    since: str = typer.Option("7d", "--since", "-s", help="Time window: Nd (days) or Nh (hours). Default: 7d"),
    limit: int = typer.Option(20, "--limit", "-n", help="Max miss queries to show"),
    write_report: bool = typer.Option(False, "--write", "-w", help="Write report to .atlas/reports/"),
    autofile: bool = typer.Option(False, "--autofile", "-a", help="Create stub .memory/gaps/ files for top misses"),
    autoticket: bool = typer.Option(False, "--autoticket", "-t", help="Create ATL tickets for gaps with no existing research report"),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON"),
):
    """Show recurring memory miss queries -- queries that returned nothing useful.

    A 'miss' is a search where top_score < 0.3 or result_count == 0.
    These represent gaps in .memory/ worth documenting.

    Use --autofile to auto-create stub .memory/gaps/ files for the top misses.
    Use --autoticket to file a dispatch-eligible ticket for each gap with no existing report.
    The stubs have status: unfilled frontmatter -- edit them to add relevant facts.
    """
    import json as _json
    import re
    from datetime import datetime, timezone

    from atlas_plus.mcp.memory_server.tools.search_log import top_miss_queries

    # Parse since string
    hours = 168.0
    m = re.match(r"^(\d+(?:\.\d+)?)(d|h)$", since.strip().lower())
    if m:
        n = float(m.group(1))
        hours = n * 24 if m.group(2) == "d" else n

    misses = top_miss_queries(since_hours=hours, limit=limit)

    if json_output:
        typer.echo(_json.dumps(misses, indent=2))
        return

    if not misses:
        console.print(f"[green]No memory misses in the last {since}.[/green]")
        return

    table = Table(title=f"Memory Miss Queries (last {since})", show_lines=True)
    table.add_column("Query", style="cyan", max_width=50)
    table.add_column("Misses", justify="right")
    table.add_column("Total", justify="right")
    table.add_column("Miss %", justify="right")
    table.add_column("Suggestion")
    for r in misses:
        pct = int(r["miss_count"] / r["total"] * 100)
        words = r["query"].split()[:4]
        suggestion = f"Consider documenting: {' '.join(words)}..."
        table.add_row(r["query"][:50], str(r["miss_count"]), str(r["total"]), f"{pct}%", suggestion)
    console.print(table)
    console.print(f"\n[yellow]{len(misses)} recurring miss(es) found.[/yellow]")

    if write_report:
        import os
        from pathlib import Path
        from datetime import date
        state_dir = atlas_paths.state_dir()
        reports_dir = state_dir / "reports"
        reports_dir.mkdir(parents=True, exist_ok=True)
        today = date.today()
        week = today.isocalendar()[1]
        out = reports_dir / f"gaps-{today.year}-W{week:02d}.md"
        lines = [f"# Memory Gap Report -- {today.year} W{week:02d}", ""]
        lines.append(f"Generated: {datetime.now(timezone.utc).isoformat()}")
        lines.append(f"Window: last {since}  |  Misses shown: {len(misses)}\n")
        for r in misses:
            pct = int(r["miss_count"] / r["total"] * 100)
            lines.append(f"- **{r['query']}** -- {r['miss_count']} misses ({pct}%)  Consider documenting this topic.")
        out.write_text("\n".join(lines), encoding="utf-8")
        console.print(f"\n[green]Written:[/green] {out}")

    if autofile:
        from datetime import date
        today_str = date.today().isoformat()
        gaps_dir = MEMORY_ROOT / "gaps"
        created = _create_gap_stubs(misses, gaps_dir, today_str)
        if created:
            console.print(f"\n[green]Created {len(created)} gap stub(s) in {gaps_dir}:[/green]")
            for p in created:
                console.print(f"  {p.relative_to(MEMORY_ROOT)}")
            console.print("[dim]Edit each stub to fill in facts, then change status: unfilled -> active.[/dim]")
        else:
            console.print("\n[dim]No new stubs created (all queries already have coverage or stubs).[/dim]")

    if autoticket:
        tickets_created = _autofile_gap_tickets(misses)
        if tickets_created:
            console.print(f"\n[green]Filed {len(tickets_created)} gap ticket(s):[/green]")
            for t in tickets_created:
                console.print(f"  [{t.id}] {t.title}")
        else:
            console.print("\n[dim]No new gap tickets created (all gaps already have a research report or existing ticket).[/dim]")


@app.command("decay")
def cmd_decay(
    threshold: int = typer.Option(0, "--threshold", "-t",
        help="Default half-life days for files without a half_life: field. 0 = only explicit fields."),
    scope: str = typer.Option("", "--scope", "-s", help="Top-level corpus folder to restrict to (reference, projects, identity, preference, or a sub-folder path); a `topic:name` route belongs to remember, not here"),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON."),
):
    """Find memory files past their half-life review threshold.

    Files with `half_life: N` in frontmatter are flagged when they have not
    been updated in N days. Files with `decays: false` are always exempt.

    To mark a file as decayable, add to its frontmatter:
      half_life: 90   # flag for review after 90 days without an update
    """
    from atlas_plus.curation.decay import find_decayed_facts

    candidates = find_decayed_facts(threshold_days=threshold, scope=scope)

    if json_output:
        import json
        typer.echo(json.dumps([c.as_dict() for c in candidates], indent=2))
        return

    if not candidates:
        console.print("[green]No files past their half-life threshold.[/green]")
        return

    table = Table(title="Memory Facts Past Half-Life", show_lines=True)
    table.add_column("File", style="cyan", max_width=45)
    table.add_column("Type", style="yellow", width=12)
    table.add_column("Half-life", justify="right", width=10)
    table.add_column("Last updated", width=12)
    table.add_column("Overdue", justify="right", width=8)
    for c in candidates:
        table.add_row(c.path, c.memory_type, f"{c.half_life_days}d", c.updated, f"{c.days_overdue}d")
    console.print(table)
    console.print(f"\n[yellow]{len(candidates)} file(s) past their half-life.[/yellow]")
    console.print("[dim]Review and update, or remove the half_life: field if the fact is permanent.[/dim]")


@app.command("unconfirmed-decay")
def cmd_unconfirmed_decay(
    threshold: int = typer.Option(90, "--threshold", "-t",
        help="Days since last_confirmed before confidence is downgraded (default 90)."),
    write: bool = typer.Option(False, "--write", "-w",
        help="Write changes to files (default: dry-run report only)."),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON."),
):
    """Find (and optionally downgrade) facts whose last_confirmed is stale.

    Scans .memory/ for files that have a `last_confirmed` date older than
    --threshold days and whose `confidence` is not already 'low'. Reports them;
    with --write, sets `confidence: low` in each file's frontmatter.

    A note never confirmed counts from its last edit (`updated`) or first observation and
    is listed with `last_confirmed: never`.

    Examples:
      atlas-review-memory unconfirmed-decay               # dry-run report
      atlas-review-memory unconfirmed-decay --write       # downgrade stale facts
      atlas-review-memory unconfirmed-decay -t 180        # 180-day threshold
    """
    from atlas_plus.mcp.memory_server.routing import downgrade_stale_confirmations

    candidates = downgrade_stale_confirmations(
        threshold_days=threshold,
        dry_run=not write,
        interactive=True,   # a person at the terminal (AG20, sweep 12)
    )

    if json_output:
        import json
        typer.echo(json.dumps(candidates, indent=2))
        return

    if not candidates:
        console.print(f"[green]No stale confirmed facts found (threshold: {threshold}d).[/green]")
        return

    verb = "Downgraded" if write else "Would downgrade"
    table = Table(
        title=f"Stale Confirmed Facts (>{threshold}d) -- {verb}",
        show_lines=True,
    )
    table.add_column("File", style="cyan", max_width=45)
    table.add_column("Was", style="yellow", width=10)
    table.add_column("Last confirmed", width=14)
    table.add_column("Age", justify="right", width=8)
    for c in candidates:
        table.add_row(c["path"], c["old_confidence"], c["last_confirmed"], f"{c['age_days']}d")
    console.print(table)
    console.print(f"\n[yellow]{len(candidates)} file(s) with stale last_confirmed.[/yellow]")
    if not write:
        console.print("[dim]Run with --write to lower confidence to 'low' for these files.[/dim]")
    else:
        console.print(f"[green]Set confidence: low on {len(candidates)} file(s).[/green]")


@app.command("age-stats")
def cmd_age_stats(
    scope: str = typer.Option("", "--scope", "-s", help="Subdirectory to restrict scan (e.g. 'feedback')."),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="List all files in each bucket."),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON (includes full file lists)."),
):
    """Show age distribution of .memory/ files by days since last update.

    Groups files into buckets: <30d (fresh), 30-90d, 90-180d, 180d+ (stale).
    Useful for identifying zones that need review passes.

    Examples:
      atlas-review-memory age-stats                # summary table
      atlas-review-memory age-stats --verbose      # full file listing per bucket
      atlas-review-memory age-stats -s feedback    # feedback/ only
      atlas-review-memory age-stats --json         # JSON with full file lists
    """
    from pathlib import Path as _Path
    from atlas_plus.mcp.memory_server.config import MEMORY_ROOT as _MEM_ROOT
    import yaml as _yaml
    from datetime import date as _date

    root = _MEM_ROOT
    skip_dirs = {"archive", "_sync", "versions", "_atlas", "anti"}
    today = _date.today()
    buckets = {"<30d": [], "30-90d": [], "90-180d": [], "180d+": [], "no-date": []}
    total = 0

    for md_file in root.rglob("*.md"):
        rel_parts = md_file.relative_to(root).parts
        if any(p in skip_dirs for p in rel_parts):
            continue
        if md_file.name.startswith("_"):
            continue
        rel = str(md_file.relative_to(root)).replace("\\", "/")
        if scope and not rel.startswith(scope.rstrip("/") + "/"):
            continue
        total += 1
        updated = None
        try:
            raw = md_file.read_text(encoding="utf-8", errors="replace")
            if raw.startswith("---"):
                end = raw.find("\n---", 3)
                if end != -1:
                    fm = _yaml.safe_load(raw[3:end]) or {}
                    upd = fm.get("updated")
                    if upd:
                        updated = _date.fromisoformat(str(upd)[:10])
        except Exception:
            pass

        if updated is None:
            buckets["no-date"].append(rel)
        else:
            age = (today - updated).days
            if age < 30:
                buckets["<30d"].append(rel)
            elif age < 90:
                buckets["30-90d"].append(rel)
            elif age < 180:
                buckets["90-180d"].append(rel)
            else:
                buckets["180d+"].append(rel)

    if json_output:
        import json
        typer.echo(json.dumps({
            "total": total,
            "buckets": {k: len(v) for k, v in buckets.items()},
            "files": {k: sorted(v) for k, v in buckets.items()},
            "scope": scope or "(all)",
        }, indent=2))
        return

    console.print(f"\n[bold]Memory Age Distribution[/bold]  (scope: {scope or 'all'}, total: {total} files)\n")
    table = Table(show_header=True, header_style="bold")
    table.add_column("Age bucket", width=14)
    table.add_column("Count", justify="right", width=8)
    table.add_column("Pct", justify="right", width=7)
    if not verbose:
        table.add_column("Sample", width=50)

    _colors = {"<30d": "green", "30-90d": "cyan", "90-180d": "yellow", "180d+": "red", "no-date": "dim"}
    for bucket, paths in buckets.items():
        n = len(paths)
        pct = f"{n / total * 100:.0f}%" if total else "0%"
        color = _colors.get(bucket, "white")
        if not verbose:
            sample = ", ".join(p.split("/")[-1].replace(".md", "") for p in paths[:3])
            table.add_row(
                f"[{color}]{bucket}[/{color}]", str(n), pct,
                sample + ("..." if len(paths) > 3 else ""),
            )
        else:
            table.add_row(f"[{color}]{bucket}[/{color}]", str(n), pct)
    console.print(table)

    if verbose:
        for bucket, paths in buckets.items():
            if not paths:
                continue
            color = _colors.get(bucket, "white")
            console.print(f"\n[{color}]{bucket}[/{color}] ({len(paths)} files):")
            for p in sorted(paths):
                console.print(f"  [dim]{p}[/dim]")

    stale_count = len(buckets["180d+"]) + len(buckets["no-date"])
    if stale_count > 0:
        console.print(f"[yellow]{stale_count} file(s) are 180d+ or undated -- consider a review pass.[/yellow]")


@app.command("rederive")
def cmd_rederive(
    threshold: float = typer.Option(0.30, "--threshold", "-t",
        help="Jaccard similarity floor to flag a file as re-derivable (0-1)."),
    scope: str = typer.Option("", "--scope", "-s", help="Top-level corpus folder to restrict to (reference, projects, identity, preference, or a sub-folder path); a `topic:name` route belongs to remember, not here"),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON."),
):
    """Find .memory/ facts that duplicate information in project docs.

    Re-derivable facts are ones you could recover by reading README, AGENTS.md,
    CLAUDE.md, or similar docs. They add maintenance cost without unique value.
    This command surfaces them for review -- nothing is archived automatically.

    High similarity (>= 0.5) strongly suggests duplication.
    Moderate (0.3-0.5) is worth checking but may be coincidental.

    To archive a flagged file use:  atlas-review-memory archive <path>
    """
    from atlas_plus.curation.rederive import find_rederivable

    candidates = find_rederivable(threshold=threshold, scope=scope)

    if json_output:
        import json
        typer.echo(json.dumps([c.as_dict() for c in candidates], indent=2))
        return

    if not candidates:
        console.print("[green]No re-derivable facts found above threshold.[/green]")
        return

    table = Table(title=f"Potentially Re-derivable Facts (threshold={threshold})", show_lines=True)
    table.add_column("Memory file", style="cyan", max_width=40)
    table.add_column("Type", style="yellow", width=12)
    table.add_column("Matched doc", style="blue", max_width=40)
    table.add_column("Sim", justify="right", width=6)
    for c in candidates:
        table.add_row(c.path, c.memory_type, c.matched_doc, f"{c.similarity:.2f}")
    console.print(table)
    console.print(f"\n[yellow]{len(candidates)} file(s) may duplicate project docs.[/yellow]")
    console.print("[dim]Review each file. If the doc is the authoritative source, archive the memory.[/dim]")
    console.print("[dim]  atlas-review-memory archive <path>[/dim]")


@app.command("convergence")
def cmd_convergence(
    scope: str = typer.Option("", "--scope", "-s", help="Restrict scan to a subdirectory"),
    write_report: bool = typer.Option(False, "--write", "-w", help="Write report to .atlas/reports/"),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON"),
):
    """Show memory files with unresolved contradiction flags.

    Scans .memory/ for files that have `contradictions:` frontmatter.
    These were flagged by the nightly contradiction check (atlas-review-memory contradictions)
    and have not yet been manually resolved.

    No LLM required -- pure frontmatter scan. Use this to find divergent
    knowledge before running a full contradiction check.
    """
    import json as _json
    import os
    import re
    import yaml as _yaml
    from datetime import datetime, timezone, date

    scan_root = MEMORY_ROOT / scope if scope else MEMORY_ROOT
    results = []

    for path in sorted(scan_root.rglob("*.md")):
        rel = path.relative_to(MEMORY_ROOT)
        parts = rel.parts
        if any(p in {"archive", "_sync", "versions", "_atlas"} for p in parts):
            continue
        if rel.name.startswith("_"):
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
        contradictions = fm.get("contradictions", [])
        if not contradictions:
            continue
        if not isinstance(contradictions, list):
            contradictions = [str(contradictions)]
        results.append({
            "path": str(rel).replace("\\", "/"),
            "title": str(fm.get("title", rel.stem)),
            "type": str(fm.get("type", "unknown")),
            "contradictions": [str(c) for c in contradictions],
            "last_confirmed": str(fm.get("last_confirmed", "")),
        })

    if json_output:
        typer.echo(_json.dumps(results, indent=2))
        return

    if not results:
        console.print("[green]No files with unresolved contradiction flags found.[/green]")
        return

    table = Table(title=f"Memory Convergence Gaps ({len(results)} file(s) with contradictions)", show_lines=True)
    table.add_column("File", style="cyan", max_width=40)
    table.add_column("Type", style="yellow", width=12)
    table.add_column("Contradicts", max_width=50)
    for r in results:
        contra_list = "\n".join(r["contradictions"][:3])
        if len(r["contradictions"]) > 3:
            contra_list += f"\n  (+{len(r['contradictions']) - 3} more)"
        table.add_row(r["path"][:40], r["type"], contra_list)
    console.print(table)
    console.print(f"\n[yellow]{len(results)} file(s) with unresolved contradiction flags.[/yellow]")
    console.print("[dim]Resolve by editing the files and removing the contradictions: list.[/dim]")
    console.print("[dim]Or run: atlas-review-memory contradictions to re-check with LLM.[/dim]")

    if write_report:
        state_dir = atlas_paths.state_dir()
        reports_dir = state_dir / "reports"
        reports_dir.mkdir(parents=True, exist_ok=True)
        today = date.today()
        out = reports_dir / f"convergence-{today.isoformat()}.md"
        lines = [f"# Memory Convergence Report -- {today.isoformat()}", ""]
        lines.append(f"Generated: {datetime.now(timezone.utc).isoformat()}")
        lines.append(f"Scope: {scope or '(all)'}  |  Files with contradictions: {len(results)}\n")
        for r in results:
            lines.append(f"## {r['path']}")
            lines.append(f"- Type: {r['type']}")
            lines.append(f"- Contradicts: {', '.join(r['contradictions'])}")
            if r["last_confirmed"]:
                lines.append(f"- Last confirmed: {r['last_confirmed']}")
            lines.append("")
        out.write_text("\n".join(lines), encoding="utf-8")
        console.print(f"\n[green]Written:[/green] {out}")


@app.command("narrative")
def cmd_narrative(
    since: int = typer.Option(14, "--since", "-s", help="Analyse sessions from the last N days."),
    write: bool = typer.Option(True, "--write/--no-write", help="Write narrative to .memory/sessions/."),
    json_output: bool = typer.Option(False, "--json", help="Output summary as JSON."),
):
    """Generate a cross-session narrative: open loops, direction signals, energy.

    Analyses dated session files from the last N days and produces a weekly
    narrative report. Run this to get a "where is this work going?" summary
    rather than just "what happened?"

    Output is written to .memory/sessions/YYYY-WNN-narrative.md.
    """
    from atlas_plus.session_narrative import build_narrative, run_narrative, write_narrative

    if write:
        result = run_narrative(since_days=since)
        if json_output:
            import json
            typer.echo(json.dumps(result, indent=2))
            return
        if not result.get("ok"):
            console.print(f"[yellow]{result.get('reason', 'no sessions found')}[/yellow]")
            return
        console.print(f"[green]Narrative written:[/green] {result['path']}")
        console.print(f"  Week: {result['week']} | Sessions: {result['sessions']} | Energy: {result['energy']}")
        console.print(f"  Open loops: {result['open_loops']} | Direction signals: {result['direction_signals']}")
    else:
        report = build_narrative(since_days=since)
        if json_output:
            import json
            from dataclasses import asdict
            typer.echo(json.dumps({
                "week_label": report.week_label,
                "sessions_analyzed": report.sessions_analyzed,
                "energy": report.energy,
                "open_loops": report.open_loops,
                "direction_signals": report.direction_signals,
                "active_projects": report.active_projects,
            }, indent=2))
            return
        if report.sessions_analyzed == 0:
            console.print("[yellow]No session files found.[/yellow]")
            return
        console.print(f"[bold cyan]Narrative: {report.week_label}[/bold cyan]  "
                      f"Sessions: {report.sessions_analyzed}  Energy: [bold]{report.energy}[/bold]")
        if report.active_projects:
            console.print(f"\n[cyan]Active projects:[/cyan] {', '.join(report.active_projects[:6])}")
        if report.direction_signals:
            console.print("\n[green]Direction signals:[/green]")
            for s in report.direction_signals[:5]:
                console.print(f"  {s[:100]}")
        if report.open_loops:
            console.print("\n[yellow]Open loops:[/yellow]")
            for s in report.open_loops[:5]:
                console.print(f"  {s[:100]}")


@app.command("decisions")
def cmd_decisions(
    stale_days: int = typer.Option(90, "--stale", "-s", help="Flag decisions not reviewed in N days"),
    show_conditions: bool = typer.Option(False, "--conditions", "-c", help="Show revisit_if conditions inline"),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON"),
):
    """Surface decision records from .memory/decisions/ that may need revisiting.

    Decision files have `revisit_if:` conditions and a `made:` date. This command
    shows decisions that are either overdue for review or have no `last_reviewed:` date.

    Use `memory_remember_decision_tool` (MCP) to create new decision records.
    """
    import json as _json
    import re
    import yaml as _yaml
    from datetime import date, datetime, timezone

    decisions_dir = MEMORY_ROOT / "decisions"
    if not decisions_dir.exists():
        console.print("[dim]No .memory/decisions/ directory found. Use memory_remember_decision_tool to create decisions.[/dim]")
        return

    today_date = date.today()
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

        made_str = str(fm.get("made", "")).strip()
        last_reviewed_str = str(fm.get("last_reviewed", "")).strip()
        title = str(fm.get("title", path.stem))
        revisit_if = fm.get("revisit_if", [])
        if not isinstance(revisit_if, list):
            revisit_if = [str(revisit_if)]

        age_days = None
        if made_str and len(made_str) >= 10:
            try:
                made_date = date.fromisoformat(made_str[:10])
                age_days = (today_date - made_date).days
            except ValueError:
                pass

        days_since_review = None
        if last_reviewed_str and len(last_reviewed_str) >= 10:
            try:
                reviewed_date = date.fromisoformat(last_reviewed_str[:10])
                days_since_review = (today_date - reviewed_date).days
            except ValueError:
                pass

        # Flag if: no review date, or last review was more than stale_days ago
        is_stale = (days_since_review is None and age_days is not None and age_days > stale_days) or \
                   (days_since_review is not None and days_since_review > stale_days)

        results.append({
            "path": str(path.relative_to(MEMORY_ROOT)).replace("\\", "/"),
            "title": title,
            "made": made_str[:10] if made_str else "",
            "last_reviewed": last_reviewed_str[:10] if last_reviewed_str else "never",
            "age_days": age_days,
            "days_since_review": days_since_review,
            "revisit_if": [str(c) for c in revisit_if],
            "is_stale": is_stale,
        })

    if json_output:
        typer.echo(_json.dumps(results, indent=2))
        return

    if not results:
        console.print("[dim]No decision records found in .memory/decisions/.[/dim]")
        return

    stale_count = sum(1 for r in results if r["is_stale"])

    table = Table(title=f"Decision Records ({len(results)} total, {stale_count} need review)", show_lines=True)
    table.add_column("Decision", style="cyan", max_width=40)
    table.add_column("Made", width=12)
    table.add_column("Last Reviewed", width=14)
    table.add_column("Status")
    for r in results:
        age_str = f"{r['age_days']}d ago" if r['age_days'] is not None else "?"
        review_str = r["last_reviewed"] if r["last_reviewed"] != "never" else f"[red]never[/red]"
        status = "[red]STALE[/red]" if r["is_stale"] else "[green]ok[/green]"
        table.add_row(r["title"][:40], f"{r['made']} ({age_str})", review_str, status)
        if show_conditions and r["revisit_if"]:
            for cond in r["revisit_if"][:3]:
                table.add_row(f"  [dim]if: {cond[:60]}[/dim]", "", "", "")
    console.print(table)

    if stale_count > 0:
        console.print(f"\n[yellow]{stale_count} decision(s) need review (>{stale_days}d without review).[/yellow]")
        console.print("[dim]Update `last_reviewed:` frontmatter after reviewing a decision.[/dim]")


@app.command("ref-graph")
def cmd_ref_graph(
    scope: str = typer.Option("", "--scope", "-s",
        help="Subdirectory to restrict hub analysis (e.g. 'projects')"),
    top_n: int = typer.Option(15, "--top", "-n", help="Number of hub files to show"),
    json_out: bool = typer.Option(False, "--json"),
    show_refs: bool = typer.Option(False, "--show-refs", help="Also show outbound refs for each hub"),
):
    """Analyse the .memory/ reference graph -- show the most-referenced (hub) files.

    Scans [[wiki-link]] and [title](path.md) patterns to build a file adjacency list,
    then ranks files by inbound reference count. Hub files are the most-referenced
    anchors in the memory corpus and should be kept well-maintained.
    """
    import json as _json
    import os
    from pathlib import Path as _Path
    from atlas_plus.mcp.memory_server.config import MEMORY_ROOT
    from atlas_plus.curation.ref_graph import build_reference_graph, find_hub_files

    mem_root = _Path(os.environ.get("ATLAS_MEMORY_ROOT", str(MEMORY_ROOT)))
    scan_root = mem_root / scope if scope else mem_root

    graph = build_reference_graph(scan_root)
    hubs = find_hub_files(graph, top_n=top_n)

    if json_out:
        typer.echo(_json.dumps(hubs, indent=2))
        return

    if not hubs:
        console.print("[dim]No reference links found in .memory/ files.[/dim]")
        return

    from rich.table import Table as _Table
    t = _Table(show_header=True, header_style="bold")
    t.add_column("Stem", style="cyan", no_wrap=True, max_width=40)
    t.add_column("Inbound refs", justify="right")
    if show_refs:
        t.add_column("Outbound refs", style="dim")

    for hub in hubs:
        stem = hub["stem"]
        count = hub["inbound_count"]
        if show_refs:
            outbound = ", ".join(graph.get(stem, [])[:5])
            t.add_row(stem, str(count), outbound or "(none)")
        else:
            t.add_row(stem, str(count))

    console.print(t)
    console.print(
        f"\n  {len(graph)} file(s) contain reference links  |  "
        f"top hub: [cyan]{hubs[0]['stem']}[/cyan] ({hubs[0]['inbound_count']} inbound refs)"
        if hubs else ""
    )


@app.command("cross-links")
def cmd_cross_links(
    reports_dir: str = typer.Option("", "--dir", "-d",
        help="Directory to scan. Default: .atlas/reports/"),
    json_out: bool = typer.Option(False, "--json"),
):
    """Cross-link research report citations to .memory/reference/ files.

    For each URL in a report's `references:` frontmatter, scans .memory/reference/
    for files that mention that URL. Matching reference files are added to a
    `cross_refs:` frontmatter list so agents can navigate from research back to memory.

    Run `atlas-review-memory citations` first to populate `references:` fields.
    """
    import json as _json
    import os
    from pathlib import Path as _Path
    from atlas_plus.brain.research import inject_cross_refs
    from atlas_plus.mcp.memory_server.config import MEMORY_ROOT

    state_dir = atlas_paths.state_dir()
    scan_dir = _Path(reports_dir) if reports_dir else state_dir / "reports"

    if not scan_dir.exists():
        console.print(f"[yellow]Directory not found: {scan_dir}[/yellow]")
        return

    mem_root = _Path(os.environ.get("ATLAS_MEMORY_ROOT", str(MEMORY_ROOT)))
    results = []
    for md_file in sorted(scan_dir.rglob("*.md")):
        if md_file.name.startswith("_"):
            continue
        injected = inject_cross_refs(md_file, mem_root)
        if injected > 0:
            results.append({"file": md_file.name, "cross_refs_added": injected})

    if json_out:
        typer.echo(_json.dumps(results, indent=2))
        return

    if not results:
        console.print("[green]No new cross-references found -- either no matches in .memory/reference/ or already up to date.[/green]")
        return

    for r in results:
        console.print(f"  [cyan]{r['file']}[/cyan]  +{r['cross_refs_added']} cross-ref(s)")
    console.print(f"\n  {len(results)} report(s) updated with cross-references.")


@app.command("citations")
def cmd_citations(
    reports_dir: str = typer.Option("", "--dir", "-d",
        help="Directory to scan for research reports. Default: .atlas/reports/"),
    dry_run: bool = typer.Option(False, "--dry-run", "-n",
        help="Report discovered citations without writing them to frontmatter."),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON."),
):
    """Extract and inject citation URLs into research report frontmatter.

    Scans .atlas/reports/ (or --dir) for markdown research reports, extracts
    HTTP/HTTPS URLs from the body and ## References section, and writes them
    to the file's `references:` frontmatter field.

    Idempotent: already-complete files are skipped. Only modified files are
    reported. Use --dry-run to preview without writing.

    Example:
      atlas-review-memory citations                # inject into all reports
      atlas-review-memory citations --dry-run      # preview counts
      atlas-review-memory citations --dir .atlas/research-reports/
    """
    import json as _json
    import os

    from atlas_plus.brain.research import extract_report_citations, inject_references_frontmatter

    state_dir = atlas_paths.state_dir()
    scan_dir = Path(reports_dir) if reports_dir else state_dir / "reports"

    if not scan_dir.exists():
        console.print(f"[yellow]Directory not found: {scan_dir}[/yellow]")
        return

    results = []
    for md_file in sorted(scan_dir.rglob("*.md")):
        if md_file.name.startswith("_"):
            continue
        try:
            text = md_file.read_text(encoding="utf-8")
        except OSError:
            continue
        urls = extract_report_citations(text)
        if not urls:
            continue
        if dry_run:
            results.append({"file": str(md_file.name), "urls": len(urls), "written": False})
        else:
            injected = inject_references_frontmatter(md_file)
            if injected > 0:
                results.append({"file": str(md_file.name), "urls": injected, "written": True})

    if json_output:
        typer.echo(_json.dumps(results, indent=2))
        return

    if not results:
        verb = "would inject" if dry_run else "injected"
        console.print(f"[green]No citations {verb} -- reports are up to date or no URLs found.[/green]")
        return

    action = "Found" if dry_run else "Injected"
    table = Table(
        title=f"Research Report Citations ({action})", show_lines=True
    )
    table.add_column("Report file", style="cyan", max_width=55)
    table.add_column("URLs", justify="right", width=6)
    table.add_column("Written", width=8)
    for r in results:
        written_cell = "[dim]dry-run[/dim]" if not r["written"] else "[green]yes[/green]"
        table.add_row(r["file"][:55], str(r["urls"]), written_cell)
    console.print(table)
    total_urls = sum(r["urls"] for r in results)
    if dry_run:
        console.print(f"\n[yellow]{len(results)} report(s) would receive {total_urls} URL(s). Run without --dry-run to write.[/yellow]")
    else:
        console.print(f"\n[green]{len(results)} report(s) updated with {total_urls} citation URL(s).[/green]")


@app.command("check-links")
def cmd_check_links(
    reports_dir: str = typer.Option("", "--dir", "-d",
        help="Directory to scan. Default: .atlas/reports/"),
    fix: bool = typer.Option(False, "--fix",
        help="Remove 404/error URLs from references: frontmatter."),
    concurrency: int = typer.Option(8, "--concurrency", "-c",
        help="Max concurrent HTTP requests (default 8)."),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON."),
):
    """Check that references: URLs in research reports are still reachable.

    Reads the references: frontmatter written by 'atlas-review-memory citations'
    and verifies each URL with a HEAD request. Broken URLs (4xx/5xx/error) are
    reported; with --fix, they are removed from the frontmatter.

    Domains known to block bots (arxiv.org, linkedin.com, doi.org, etc.) are
    skipped automatically -- they may be valid but reject automated checks.
    403 responses are marked 'gated' (may be auth-required, not dead).

    Examples:
      atlas-review-memory check-links                # preview broken URLs
      atlas-review-memory check-links --fix          # remove dead URLs
      atlas-review-memory check-links --dir .atlas/reports/ --json
    """
    import json as _json
    import os

    from atlas_plus.curation.link_check import check_report_links, remove_broken_links

    state_dir = atlas_paths.state_dir()
    scan_dir = Path(reports_dir) if reports_dir else state_dir / "reports"

    if not scan_dir.exists():
        console.print(f"[yellow]Directory not found: {scan_dir}[/yellow]")
        return

    report_results = []
    for md_file in sorted(scan_dir.rglob("*.md")):
        if md_file.name.startswith("_"):
            continue
        results = check_report_links(md_file, workers=concurrency)
        broken = [r for r in results if not r["ok"] and not r.get("skipped")]
        gated  = [r for r in results if r.get("gated")]
        if not results:
            continue
        report_results.append({
            "file": md_file.name,
            "total_urls": len(results),
            "broken": [r["url"] for r in broken],
            "gated": [r["url"] for r in gated],
            "details": results,
        })
        if fix and broken:
            remove_broken_links(md_file, [r["url"] for r in broken])

    if json_output:
        typer.echo(_json.dumps(report_results, indent=2))
        return

    if not report_results:
        console.print("[dim]No research reports with references: found.[/dim]")
        return

    total_broken = sum(len(r["broken"]) for r in report_results)
    total_gated  = sum(len(r["gated"]) for r in report_results)

    if total_broken == 0 and total_gated == 0:
        console.print(f"[green]All URLs reachable across {len(report_results)} report(s).[/green]")
        return

    table = Table(
        title=f"Link Check Results ({len(report_results)} report(s)  {total_broken} broken  {total_gated} gated)",
        show_lines=True,
    )
    table.add_column("Report", style="cyan", max_width=45)
    table.add_column("Status", width=10)
    table.add_column("URL", max_width=60)

    for r in report_results:
        for url in r["broken"]:
            status_str = "broken"
            table.add_row(r["file"][:45], f"[red]{status_str}[/red]", url[:60])
        for url in r["gated"]:
            table.add_row(r["file"][:45], "[yellow]gated[/yellow]", url[:60])

    console.print(table)
    if fix and total_broken > 0:
        console.print(f"\n[green]Removed {total_broken} broken URL(s) from frontmatter.[/green]")
    elif total_broken > 0:
        console.print(f"\n[yellow]{total_broken} broken URL(s) found. Run with --fix to remove them.[/yellow]")


@app.command("health-score")
def cmd_health_score(
    scope: str = typer.Option("", "--scope", "-s",
        help="Subdirectory to restrict scan (e.g. 'feedback', 'projects')."),
    max_health: float = typer.Option(0.75, "--max-health",
        help="Only show files at or below this score (default 0.75, i.e. show imperfect files)."),
    limit: int = typer.Option(30, "--limit", "-n", help="Max files to show (default 30)."),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON."),
):
    """Show health scores for .memory/ files based on metadata completeness.

    Health score (0-1) is a composite of four metadata factors (each worth 0.25):
      tags       -- has tags: frontmatter (improves topic search ranking)
      confirmed  -- has last_confirmed: within 90 days (verifies fact accuracy)
      updated    -- has updated: date field (enables age-based decay)
      confidence -- has confidence: high|medium|low (calibrates search ranking)

    Defaults to showing files with score < 0.75 (missing at least one factor).
    Use --max-health 0.0 to show only completely unhealthy files (score = 0).
    Use --max-health 1.0 to show all files.

    Examples:
      atlas-review-memory health-score              # files missing at least one factor
      atlas-review-memory health-score --max-health 0.5   # below-half health only
      atlas-review-memory health-score -s feedback  # feedback/ directory only
      atlas-review-memory health-score --json       # machine-readable
    """
    import json as _json
    from atlas_plus.curation.health import scan_memory_health

    results = scan_memory_health(
        memory_root=MEMORY_ROOT,
        scope=scope,
        min_health=0.0,  # we filter by max_health below for display
        limit=limit * 2,  # over-fetch, filter below
    )

    # Apply max_health filter
    results = [r for r in results if r["score"] <= max_health][:limit]

    if json_output:
        typer.echo(_json.dumps(results, indent=2))
        return

    if not results:
        console.print(
            f"[green]All files have health score > {max_health:.2f} -- well maintained![/green]"
        )
        return

    table = Table(
        title=f"Memory File Health (score <= {max_health:.2f}, {len(results)} file(s))",
        show_lines=False,
    )
    table.add_column("Score", justify="right", width=7)
    table.add_column("File", style="cyan", max_width=45)
    table.add_column("Issues", max_width=40)

    for r in results:
        s = r["score"]
        color = "green" if s >= 0.75 else ("yellow" if s >= 0.5 else "red")
        issue_str = ", ".join(r["issues"][:4])
        table.add_row(f"[{color}]{s:.2f}[/{color}]", r["path"][:45], issue_str)

    console.print(table)
    avg = sum(r["score"] for r in results) / len(results) if results else 0
    console.print(f"\n  {len(results)} file(s) need attention  avg health: {avg:.2f}")
    console.print("[dim]Quick fixes: atlas-review-memory suggest-tags --write  |  atlas-review-memory unconfirmed-decay[/dim]")


@app.command("summary")
def cmd_summary(
    json_output: bool = typer.Option(False, "--json", help="Output as JSON."),
):
    """Aggregate .memory/ health dashboard -- single-command corpus overview.

    Combines health scores, orphan count, reference graph hubs, confirmation
    and tagging rates. Use this as the daily 'is my memory in good shape?' check.

    Examples:
      atlas-review-memory summary
      atlas-review-memory summary --json
    """
    import json as _json

    result: dict = {}

    # 1. Memory quality (confirmed%, tagged%, file count)
    try:
        from atlas_plus.doctor.cli import check_memory_quality
        mq = check_memory_quality()
        result["total_files"] = mq.get("total_files", 0)
        result["confirmed_pct"] = mq.get("confirmed_pct", 0)
        result["tagged_pct"] = mq.get("tagged_pct", 0)
        result["stale_confirmed"] = mq.get("stale_confirmed", 0)
        result["anti_flagged"] = mq.get("anti_flagged", 0)
    except Exception:
        result["total_files"] = 0

    # 2. Average health score + unhealthy count
    try:
        from atlas_plus.curation.health import scan_memory_health
        all_health = scan_memory_health(memory_root=MEMORY_ROOT, limit=5000)
        if all_health:
            result["avg_health"] = round(sum(r["score"] for r in all_health) / len(all_health), 3)
            result["unhealthy_count"] = sum(1 for r in all_health if r["score"] < 0.5)
        else:
            result["avg_health"] = 0.0
            result["unhealthy_count"] = 0
    except Exception:
        result["avg_health"] = 0.0
        result["unhealthy_count"] = 0

    # 3. Orphan candidates (age>180d, health<=0.5, 0 inbound refs)
    try:
        from atlas_plus.curation.orphan import find_orphan_candidates
        orphans = find_orphan_candidates(MEMORY_ROOT, min_age_days=180, max_health=0.5, limit=200)
        result["orphan_candidates"] = len(orphans)
    except Exception:
        result["orphan_candidates"] = 0

    # 4. Reference graph hubs (files with 5+ inbound refs)
    try:
        from atlas_plus.curation.ref_graph import build_reference_graph, find_hub_files
        graph = build_reference_graph(MEMORY_ROOT)
        hubs = find_hub_files(graph, top_n=50)
        result["hub_files"] = sum(1 for h in hubs if h["inbound_count"] >= 5)
        result["total_referenced"] = len(graph)
    except Exception:
        result["hub_files"] = 0
        result["total_referenced"] = 0

    if json_output:
        typer.echo(_json.dumps(result, indent=2))
        return

    # Rich table render
    tf = result.get("total_files", 0)
    conf = result.get("confirmed_pct", 0)
    tagged = result.get("tagged_pct", 0)
    avg_h = result.get("avg_health", 0.0)
    unhealthy = result.get("unhealthy_count", 0)
    orphans_n = result.get("orphan_candidates", 0)
    hubs_n = result.get("hub_files", 0)
    stale_n = result.get("stale_confirmed", 0)
    anti_n = result.get("anti_flagged", 0)

    console.print(f"[bold cyan]Memory Summary[/bold cyan]  ({tf} files)")
    console.print("")

    # Coverage row
    conf_color = "green" if conf >= 70 else ("yellow" if conf >= 40 else "red")
    tag_color = "green" if tagged >= 70 else ("yellow" if tagged >= 40 else "red")
    health_color = "green" if avg_h >= 0.7 else ("yellow" if avg_h >= 0.4 else "red")
    console.print(f"  Confirmed:   [{conf_color}]{conf}%[/{conf_color}]  (facts with last_confirmed: within 90d)")
    console.print(f"  Tagged:      [{tag_color}]{tagged}%[/{tag_color}]  (files with tags: frontmatter)")
    console.print(f"  Avg health:  [{health_color}]{avg_h:.2f}[/{health_color}]  ({unhealthy} file(s) below 0.50)")
    console.print("")

    # Hygiene signals
    if orphans_n > 0:
        console.print(f"  [yellow]Orphans:[/yellow] {orphans_n} candidate(s)  -- atlas-review-memory orphans")
    else:
        console.print(f"  [green]Orphans:[/green] none found  (age>180d, health<=0.50, no inbound refs)")
    if stale_n > 0:
        console.print(f"  [yellow]Stale confirmed:[/yellow] {stale_n} file(s) not confirmed in 90d+  -- atlas-review-memory unconfirmed-decay")
    if anti_n > 0:
        console.print(f"  [dim]Anti-flagged:[/dim] {anti_n} file(s)")
    if hubs_n > 0:
        console.print(f"  [cyan]Hubs:[/cyan] {hubs_n} file(s) with 5+ inbound refs  ({result.get('total_referenced',0)} total files with outbound links)")
    console.print("")


@app.command("suggest-tags")
def cmd_suggest_tags(
    scope: str = typer.Option("", "--scope", "-s",
        help="Subdirectory to restrict scan (e.g. 'feedback', 'projects')."),
    write: bool = typer.Option(False, "--write", "-w",
        help="Write suggested tags to frontmatter. Default: dry-run report only."),
    max_tags: int = typer.Option(4, "--max-tags",
        help="Maximum tags to suggest per file (default 4)."),
    limit: int = typer.Option(50, "--limit", "-n",
        help="Max files to process (default 50)."),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON."),
):
    """Suggest tags: frontmatter for .memory/ files that have none.

    Uses three signals: directory name, type: field, and title keywords.
    Reports suggestions; with --write applies them to the files.

    Files that already have tags: or tag: frontmatter are skipped.
    Archive, _atlas, anti, and _ prefixed files are also skipped.

    Examples:
      atlas-review-memory suggest-tags              # dry-run: see suggestions
      atlas-review-memory suggest-tags --write      # apply suggestions
      atlas-review-memory suggest-tags -s feedback  # only feedback/ directory
    """
    import json as _json
    from atlas_plus.curation.tag_suggest import suggest_tags_for_file, inject_tags

    skip_dirs = {"archive", "_sync", "versions", "_atlas", "anti"}
    scan_root = MEMORY_ROOT / scope if scope else MEMORY_ROOT

    results = []
    processed = 0
    no_suggestion_count = 0
    already_tagged_count = 0
    for md_file in sorted(scan_root.rglob("*.md")):
        if processed >= limit:
            break
        rel_parts = md_file.relative_to(MEMORY_ROOT).parts
        if any(p in skip_dirs for p in rel_parts):
            continue
        if md_file.name.startswith("_"):
            continue

        tags = suggest_tags_for_file(md_file, memory_root=MEMORY_ROOT, max_tags=max_tags)
        if not tags:
            no_suggestion_count += 1
            continue

        rel = str(md_file.relative_to(MEMORY_ROOT)).replace("\\", "/")
        applied = False
        if write:
            applied = inject_tags(md_file, tags, interactive=True)
            if not applied:
                already_tagged_count += 1

        results.append({
            "file": rel,
            "tags": tags,
            "applied": applied,
        })
        processed += 1

    if json_output:
        typer.echo(_json.dumps({
            "results": results,
            "no_suggestion_count": no_suggestion_count,
            "already_tagged_count": already_tagged_count,
        }, indent=2))
        return

    if not results:
        console.print(
            "[green]No untagged files found -- all files already have tags.[/green]"
            if not scope else
            f"[green]No untagged files found in {scope}/.[/green]"
        )
        return

    action = "Applied" if write else "Would apply"
    table = Table(
        title=f"Tag Suggestions ({action}, {len(results)} file(s))",
        show_lines=False,
    )
    table.add_column("File", style="cyan", max_width=45)
    table.add_column("Suggested tags", max_width=40)
    if write:
        table.add_column("Written", width=8)

    for r in results:
        tag_str = ", ".join(r["tags"])
        row = [r["file"][:45], tag_str]
        if write:
            row.append("[green]yes[/green]" if r["applied"] else "[dim]skip[/dim]")
        table.add_row(*row)

    console.print(table)
    total_tags = sum(len(r["tags"]) for r in results)
    if write:
        applied_n = sum(1 for r in results if r["applied"])
        console.print(f"\n[green]{applied_n} tagged[/green]  [dim]{already_tagged_count} already-tagged (skipped)  {no_suggestion_count} had no suggestions[/dim]  ({total_tags} tag(s) total)")
    else:
        console.print(f"\n[yellow]{len(results)} file(s) could receive {total_tags} tag(s).[/yellow]  [dim]{no_suggestion_count} had no suggestions. Run with --write to apply.[/dim]")


@app.command("research-index")
def cmd_research_index(
    reports_dir: str = typer.Option("", "--dir", "-d",
        help="Directory to scan. Default: .atlas/reports/"),
    min_score: float = typer.Option(0.0, "--min-score",
        help="Only show reports at or below this quality score. Default: show all."),
    since: int = typer.Option(0, "--since", "-s",
        help="Only show reports from the last N days. Default: 0 (all)."),
    sort_by: str = typer.Option("date", "--sort",
        help="Sort by: date | score | title | citations | crossrefs | words. Default: date."),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON."),
):
    """List all research reports with dates, quality scores, citations, and word counts.

    Provides a quick overview of the research corpus: what's been investigated,
    how comprehensive each report is, and which reports need improvement.

    Examples:
      atlas-review-memory research-index                    # all reports, newest first
      atlas-review-memory research-index --sort score       # sorted by quality
      atlas-review-memory research-index --since 30         # last 30 days only
      atlas-review-memory research-index --min-score 0.5    # low-quality reports only
      atlas-review-memory research-index --json             # machine-readable
    """
    import json as _json
    import os
    import re
    import yaml as _yaml
    from datetime import date as _date, timedelta

    state_dir = atlas_paths.state_dir()
    scan_dir = Path(reports_dir) if reports_dir else state_dir / "reports"

    if not scan_dir.exists():
        console.print(f"[yellow]Directory not found: {scan_dir}[/yellow]")
        return

    cutoff_date = (_date.today() - timedelta(days=since)).isoformat() if since > 0 else ""

    reports = []
    for md_file in scan_dir.rglob("*.md"):
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

        # Extract key fields
        title_raw = str(fm.get("title", md_file.stem))
        title = title_raw.replace("Research: ", "").replace("\"", "").strip()[:55]
        date_str = str(fm.get("date") or fm.get("report_date") or "")[:10]
        quality = fm.get("quality_score")
        refs = fm.get("references") or []
        citation_count = len(refs) if isinstance(refs, list) else 0
        cross_refs = fm.get("cross_refs") or []
        cross_ref_count = len(cross_refs) if isinstance(cross_refs, list) else 0
        quality_gate_passed = fm.get("quality_gate_passed")  # None=not checked, True/False

        # Age filter
        if cutoff_date and date_str and date_str < cutoff_date:
            continue

        # Min-score filter (only apply when quality_score exists)
        if min_score > 0 and quality is not None:
            try:
                if float(quality) > min_score:
                    continue
            except (TypeError, ValueError):
                pass

        # Word count from body
        body = raw[end + 4:]
        word_count = len(re.findall(r"\b\w+\b", body))

        # Freshness score (computed from date, not stored in frontmatter)
        from atlas_plus.brain.research import compute_freshness_score
        freshness = compute_freshness_score(date_str)

        reports.append({
            "file": md_file.name,
            "title": title,
            "date": date_str,
            "quality_score": float(quality) if quality is not None else None,
            "freshness_score": freshness,
            "citation_count": citation_count,
            "cross_ref_count": cross_ref_count,
            "word_count": word_count,
            "quality_gate_passed": quality_gate_passed,
        })

    # Sorting
    sort_key = sort_by.lower()
    if sort_key == "score":
        reports.sort(key=lambda r: (r["quality_score"] is None, -(r["quality_score"] or 0)))
    elif sort_key == "title":
        reports.sort(key=lambda r: r["title"].lower())
    elif sort_key == "citations":
        reports.sort(key=lambda r: -r["citation_count"])
    elif sort_key == "crossrefs":
        reports.sort(key=lambda r: -r["cross_ref_count"])
    elif sort_key == "words":
        reports.sort(key=lambda r: -r["word_count"])
    else:  # date (default)
        reports.sort(key=lambda r: r["date"], reverse=True)

    if json_output:
        typer.echo(_json.dumps(reports, indent=2))
        return

    if not reports:
        console.print("[dim]No research reports found.[/dim]")
        return

    table = Table(
        title=f"Research Index ({len(reports)} report(s))",
        show_lines=False,
    )
    table.add_column("Date", width=12)
    table.add_column("Title", style="cyan", max_width=48)
    table.add_column("Qual", justify="right", width=6)
    table.add_column("Fresh", justify="right", width=6)
    table.add_column("Cite", justify="right", width=5)
    table.add_column("XRef", justify="right", width=5)
    table.add_column("Words", justify="right", width=7)

    for r in reports:
        qs = r["quality_score"]
        if qs is None:
            score_str = "[dim]--[/dim]"
        else:
            color = "green" if qs >= 0.7 else ("yellow" if qs >= 0.4 else "red")
            score_str = f"[{color}]{qs:.2f}[/{color}]"
        fs = r.get("freshness_score")
        if fs is None:
            fresh_str = "[dim]--[/dim]"
        else:
            f_color = "green" if fs >= 0.75 else ("yellow" if fs >= 0.4 else "red")
            fresh_str = f"[{f_color}]{fs:.2f}[/{f_color}]"
        xrc = r.get("cross_ref_count", 0)
        gate = r.get("quality_gate_passed")
        title_display = r["title"]
        if gate is False:
            title_display = f"[red]![/red] {title_display}"
        table.add_row(
            r["date"] or "--",
            title_display,
            score_str,
            fresh_str,
            str(r["citation_count"]) if r["citation_count"] > 0 else "[dim]0[/dim]",
            str(xrc) if xrc > 0 else "[dim]0[/dim]",
            str(r["word_count"]) if r["word_count"] > 0 else "[dim]0[/dim]",
        )

    console.print(table)
    scored = sum(1 for r in reports if r["quality_score"] is not None)
    avg = (sum(r["quality_score"] for r in reports if r["quality_score"] is not None) / scored
           if scored else 0.0)
    console.print(
        f"\n  {len(reports)} report(s)  "
        + (f"| {scored} scored, avg quality: {avg:.2f}" if scored else "| none scored yet")
    )
    if scored < len(reports):
        unscored = len(reports) - scored
        console.print(f"  [dim]{unscored} unscored -- run: atlas-review-memory quality --write[/dim]")


@app.command("quality")
def cmd_quality(
    reports_dir: str = typer.Option("", "--dir", "-d",
        help="Directory to scan for research reports. Default: .atlas/reports/"),
    write: bool = typer.Option(False, "--write", "-w",
        help="Write quality_score and quality_factors to each report's frontmatter."),
    min_score: float = typer.Option(0.0, "--min-score",
        help="Only show reports at or below this score (0-1). Default: show all."),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON."),
):
    """Score research reports on structural quality and optionally write scores to frontmatter.

    Scoring factors (deterministic, no LLM):
      citations  (0.25): URL references found in report body
      sections   (0.30): required headings present (Summary, Key Findings, Context, ...)
      word_count (0.20): total word count (500+ words = full score)
      findings   (0.25): bullet points under ## Key Findings (3+ = full score)

    Use --write to persist scores as quality_score: and quality_factors: frontmatter.
    Use --min-score 0.6 to highlight reports that need improvement.

    Examples:
      atlas-review-memory quality --dir .atlas/reports/          # preview scores
      atlas-review-memory quality --write                        # persist to frontmatter
      atlas-review-memory quality --min-score 0.5 --write       # focus on low-quality reports
    """
    import json as _json
    import os

    from atlas_plus.brain.research import inject_quality_score, score_research_report

    state_dir = atlas_paths.state_dir()
    scan_dir = Path(reports_dir) if reports_dir else state_dir / "reports"

    if not scan_dir.exists():
        console.print(f"[yellow]Directory not found: {scan_dir}[/yellow]")
        return

    results = []
    for md_file in sorted(scan_dir.rglob("*.md")):
        if md_file.name.startswith("_"):
            continue
        try:
            raw = md_file.read_text(encoding="utf-8")
        except OSError:
            continue
        if not raw.startswith("---"):
            continue
        end = raw.find("\n---", 3)
        if end == -1:
            continue
        body = raw[end + 4:]
        scored = score_research_report(body)
        score = scored["score"]
        if min_score > 0 and score > min_score:
            continue
        entry = {
            "file": md_file.name,
            "score": score,
            "factors": scored["factors"],
            "raw": scored["raw"],
            "written": False,
        }
        if write:
            result = inject_quality_score(md_file)
            entry["written"] = bool(result)
        results.append(entry)

    if json_output:
        typer.echo(_json.dumps(results, indent=2))
        return

    if not results:
        console.print("[green]No research reports found in the scanned directory.[/green]")
        return

    table = Table(
        title=f"Research Report Quality Scores ({len(results)} report(s))",
        show_lines=True,
    )
    table.add_column("Report", style="cyan", max_width=45)
    table.add_column("Score", justify="right", width=7)
    table.add_column("Cite", justify="right", width=5)
    table.add_column("Sect", justify="right", width=5)
    table.add_column("Words", justify="right", width=6)
    table.add_column("Find", justify="right", width=5)
    if write:
        table.add_column("Written", width=8)

    for r in sorted(results, key=lambda x: x["score"]):
        s = r["score"]
        score_color = "green" if s >= 0.7 else ("yellow" if s >= 0.4 else "red")
        f = r["factors"]
        raw_r = r["raw"]
        row = [
            r["file"][:45],
            f"[{score_color}]{s:.2f}[/{score_color}]",
            str(raw_r["citation_count"]),
            str(raw_r["section_count"]),
            str(raw_r["word_count"]),
            str(raw_r["findings_count"]),
        ]
        if write:
            row.append("[green]yes[/green]" if r["written"] else "[dim]skip[/dim]")
        table.add_row(*row)

    console.print(table)
    avg_score = sum(r["score"] for r in results) / len(results)
    console.print(f"\n  Average score: {avg_score:.2f}  |  Reports scored: {len(results)}")
    if not write:
        console.print("[dim]Run with --write to persist scores to frontmatter.[/dim]")


@app.command("orphans")
def cmd_orphans(
    scope: str = typer.Option("", "--scope", "-s", help="Subdirectory to restrict search (e.g. 'observations')"),
    min_age: int = typer.Option(180, "--min-age", help="Minimum age in days (from updated: or mtime)"),
    max_health: float = typer.Option(0.5, "--max-health", help="Maximum health score to include (0-1)"),
    max_refs: int = typer.Option(0, "--max-refs", help="Maximum inbound reference count to qualify"),
    limit: int = typer.Option(50, "--limit", "-n"),
    json_out: bool = typer.Option(False, "--json"),
):
    """Find .memory/ files that are likely orphans (no inbound refs, low health, old).

    Orphan candidates are files that no other memory file references, have poor
    metadata (missing tags/confirmed/confidence), and have not been updated recently.
    These are good candidates for review and potential archival.
    """
    import json
    import os
    from pathlib import Path as _Path
    from atlas_plus.mcp.memory_server.config import MEMORY_ROOT
    from atlas_plus.curation.orphan import find_orphan_candidates

    mem_root = _Path(os.environ.get("ATLAS_MEMORY_ROOT", str(MEMORY_ROOT)))
    results = find_orphan_candidates(
        mem_root, scope=scope, min_age_days=min_age,
        max_health=max_health, max_inbound_refs=max_refs, limit=limit,
    )

    if json_out:
        print(json.dumps(results, indent=2))
        return

    if not results:
        console.print(
            f"[green]No orphan candidates found.[/green]  "
            f"(min_age={min_age}d, max_health={max_health:.2f}, max_refs={max_refs})"
        )
        return

    from rich.table import Table
    t = Table(show_header=True, header_style="bold")
    t.add_column("Path", style="cyan", no_wrap=True, max_width=50)
    t.add_column("Age", justify="right")
    t.add_column("Health", justify="right")
    t.add_column("Refs", justify="right")
    t.add_column("Issues")

    for r in results:
        age_str = f"{r['age_days']}d"
        h = r["score"]
        h_color = "red" if h <= 0.0 else ("yellow" if h < 0.5 else "white")
        ref_color = "red" if r["inbound_refs"] == 0 else "white"
        issues_str = ", ".join(r["issues"][:3])
        t.add_row(
            r["path"],
            age_str,
            f"[{h_color}]{h:.2f}[/{h_color}]",
            f"[{ref_color}]{r['inbound_refs']}[/{ref_color}]",
            f"[dim]{issues_str}[/dim]",
        )

    console.print(t)
    console.print(
        f"\n  {len(results)} orphan candidate(s)  "
        f"(min_age={min_age}d, max_health={max_health:.2f})"
    )
    console.print(
        "[dim]Review each file and archive with: "
        "atlas-review-memory archive <path>[/dim]"
    )


@app.command("next-actions")
def cmd_next_actions(
    limit: int = typer.Option(5, "--limit", "-n", help="Max items per category."),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON."),
):
    """Show a prioritized to-do list for memory corpus maintenance.

    Checks four categories in priority order:
      1. Decayed  -- files past their half-life (stale facts to review/update)
      2. Orphans  -- unreferenced low-health old files (candidates for archival)
      3. Untagged -- files missing tags (quick auto-fix: suggest-tags --write)
      4. Gaps     -- recurring miss queries with no memory coverage

    Examples:
      atlas-review-memory next-actions          # top 5 per category
      atlas-review-memory next-actions -n 3     # top 3 per category
      atlas-review-memory next-actions --json   # machine-readable
    """
    import json as _json
    from atlas_plus.curation.decay import find_decayed_facts
    from atlas_plus.curation.orphan import find_orphan_candidates
    from atlas_plus.curation.tag_suggest import suggest_tags_for_file

    result: dict = {}

    # 1. Decayed facts
    try:
        decayed = find_decayed_facts(threshold_days=0)[:limit]
        result["decayed"] = [c.as_dict() for c in decayed]
    except Exception:
        result["decayed"] = []

    # 2. Orphan candidates
    try:
        orphans = find_orphan_candidates(MEMORY_ROOT, min_age_days=90, max_health=0.5, limit=limit)
        result["orphans"] = orphans[:limit]
    except Exception:
        result["orphans"] = []

    # 3. Untagged files
    try:
        untagged = []
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
                untagged.append({
                    "path": str(md_file.relative_to(MEMORY_ROOT)).replace("\\", "/"),
                    "suggested_tags": tags,
                })
        result["untagged"] = untagged
    except Exception:
        result["untagged"] = []

    # 4. Research gaps
    try:
        from atlas_plus.mcp.memory_server.tools.search_log import top_miss_queries
        gaps = top_miss_queries(since_hours=168, limit=limit)
        result["gaps"] = gaps[:limit]
    except Exception:
        result["gaps"] = []

    if json_output:
        typer.echo(_json.dumps(result, indent=2))
        return

    has_any = any(result.values())
    if not has_any:
        console.print("[green]Memory corpus is in excellent shape -- nothing urgent to do.[/green]")
        return

    console.print("\n[bold]Memory Next Actions[/bold]\n")

    if result["decayed"]:
        console.print(f"[red]1. Decayed facts ({len(result['decayed'])} files past half-life):[/red]")
        for c in result["decayed"]:
            console.print(f"   [dim]{c['path']}[/dim]  [{c['half_life_days']}d half-life, {c['days_overdue']}d overdue]")
        console.print("   [dim]-> Review and update, or remove half_life: field if fact is permanent.[/dim]\n")

    if result["orphans"]:
        console.print(f"[yellow]2. Orphan candidates ({len(result['orphans'])} unreferenced low-health files):[/yellow]")
        for o in result["orphans"]:
            console.print(f"   [dim]{o['path']}[/dim]  [health={o['score']:.2f}, age={o['age_days']}d]")
        console.print("   [dim]-> Review and archive with: atlas-review-memory archive <path>[/dim]\n")

    if result["untagged"]:
        console.print(f"[cyan]3. Untagged files ({len(result['untagged'])} files -- quick fix):[/cyan]")
        for u in result["untagged"]:
            tags_str = ", ".join(u["suggested_tags"])
            console.print(f"   [dim]{u['path']}[/dim]  tags: {tags_str}")
        console.print("   [dim]-> Run: atlas-review-memory suggest-tags --write[/dim]\n")

    if result["gaps"]:
        console.print(f"[blue]4. Research gaps ({len(result['gaps'])} recurring miss queries):[/blue]")
        for g in result["gaps"]:
            console.print(f"   [dim]{g['query']}[/dim]  [{g['miss_count']} misses in 7d]")
        console.print("   [dim]-> Run: atlas-review-memory gaps --autoticket[/dim]\n")


@app.command("report")
def cmd_report(
    stale_months: int = typer.Option(6, "--stale-months"),
    dup_threshold: float = typer.Option(0.40, "--dup-threshold"),
    scope: str = typer.Option("", "--scope"),
    dry_run: bool = typer.Option(False, "--dry-run", "-n"),
):
    """Generate weekly curation report (no auto-edits)."""
    from atlas_plus.curation.weekly_report import generate_report
    report, out_path = generate_report(
        stale_months=stale_months,
        dup_threshold=dup_threshold,
        scope=scope,
        dry_run=dry_run,
    )
    console.print(report)
    if not dry_run:
        console.print(f"\n[green]Written:[/green] {out_path}")
    else:
        console.print("\n[dim](dry-run — not written)[/dim]")


def main() -> None:
    """Entry point for the Atlas curation CLI (Typer app)."""
    app()


if __name__ == "__main__":
    main()
