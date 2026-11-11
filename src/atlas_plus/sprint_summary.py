"""Sprint summary generator -- parse git log to produce structured sprint summaries.

Generates a markdown summary of commits since a given git ref or date,
grouped by sprint version tag (e.g. v0.12-A) and formatted for AGENTS.md
sprint table rows or session log entries.

Usage:
  atlas-sprint-summary                          # since yesterday
  atlas-sprint-summary --since HEAD~10          # last 10 commits
  atlas-sprint-summary --since v0.12-A          # since a tag
  atlas-sprint-summary --since 2026-05-20       # since a date
  atlas-sprint-summary --format agents-md       # AGENTS.md table rows
  atlas-sprint-summary --format session-log     # session log paragraph
"""

from __future__ import annotations

import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import date
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table
from atlas_plus import paths as atlas_paths

app = typer.Typer(help="Generate sprint summaries from git log.")
console = Console()

# Matches sprint version tags like v0.12-A or v0.12-B embedded in commit messages
_SPRINT_RE = re.compile(r"\(v(\d+\.\d+)-([A-Z]+)\)", re.IGNORECASE)
# Matches "1234 tests" or "1234 total" in commit messages
_TEST_COUNT_RE = re.compile(r"(\d{3,5})\s+(?:tests?|total)\b", re.IGNORECASE)


@dataclass
class CommitInfo:
    """Parsed git commit with extracted sprint tag (e.g. v0.12-A) and test count if present in the body."""

    hash: str
    subject: str
    body: str
    sprint_tag: Optional[str] = None    # e.g. "v0.12-A"
    test_count: Optional[int] = None


def _run_git(args: list[str]) -> str:
    """Run a git command and return stdout. Returns '' on error."""
    try:
        from atlas_plus import gitsafe
        result = gitsafe.run(args, None, timeout=30)
        return result.stdout.strip()
    except Exception:
        return ""


def _parse_commits(since: str) -> list[CommitInfo]:
    """Return CommitInfo list for commits since the given ref or date."""
    # Use %x00 as delimiter to handle multi-line bodies safely
    fmt = "%H%x00%s%x00%b%x00"
    raw = _run_git(["log", f"--since={since}" if _is_date(since) else f"{since}..HEAD",
                    f"--pretty=format:{fmt}"])
    if not raw:
        return []

    commits = []
    for record in raw.split("\x00\x00"):
        record = record.strip()
        if not record:
            continue
        parts = record.split("\x00", 2)
        if len(parts) < 2:
            continue
        h = parts[0].strip()
        subject = parts[1].strip()
        body = parts[2].strip() if len(parts) > 2 else ""

        full_text = subject + " " + body
        sprint_m = _SPRINT_RE.search(full_text)
        test_m = _TEST_COUNT_RE.search(full_text)

        commits.append(CommitInfo(
            hash=h[:8],
            subject=subject,
            body=body,
            sprint_tag=sprint_m.group(0)[1:-1] if sprint_m else None,  # strip parens
            test_count=int(test_m.group(1)) if test_m else None,
        ))
    return commits


def _is_date(ref: str) -> bool:
    """Return True if ref looks like an ISO date or datetime (YYYY-MM-DD [HH:MM:SS])."""
    return bool(re.match(r"^\d{4}-\d{2}-\d{2}(\s+\d{2}:\d{2}:\d{2})?$", ref.strip()))


def _group_by_sprint(commits: list[CommitInfo]) -> dict[str, list[CommitInfo]]:
    """Group commits by sprint tag. Untagged commits go under 'untagged'."""
    groups: dict[str, list[CommitInfo]] = {}
    for c in commits:
        key = c.sprint_tag or "untagged"
        groups.setdefault(key, []).append(c)
    return groups


def _extract_description(subject: str) -> str:
    """Strip commit prefix (feat/fix/docs/test: ...) and sprint tag for display."""
    # Remove conventional commit prefix
    s = re.sub(r"^(feat|fix|docs?|test|refactor|style|chore)(\+\w+)*:\s*", "", subject, flags=re.IGNORECASE)
    # Remove trailing sprint tag like (v0.12-A)
    s = _SPRINT_RE.sub("", s).strip()
    return s or subject


@app.command("summary")
def cmd_summary(
    since: str = typer.Option("yesterday", "--since", "-s",
        help="Git ref, tag, or ISO date (e.g. HEAD~5, v0.11-F, 2026-05-20)"),
    fmt: str = typer.Option("table", "--format", "-f",
        help="Output format: table | agents-md | session-log | json"),
    project: str = typer.Option("", "--project", "-p",
        help="Project name for session-log format (default: from git remote)"),
):
    """Generate a structured sprint summary from recent git commits.

    Groups commits by sprint version tag (v0.12-A etc.) and extracts test
    counts from commit messages. Useful for drafting AGENTS.md rows or
    session log entries without manual copy-paste from git log.
    """
    commits = _parse_commits(since)
    if not commits:
        console.print(f"[yellow]No commits found since {since!r}.[/yellow]")
        raise typer.Exit(0)

    groups = _group_by_sprint(commits)
    today = date.today().isoformat()

    if fmt == "table":
        t = Table(show_header=True, header_style="bold")
        t.add_column("Sprint", style="cyan", no_wrap=True)
        t.add_column("Description")
        t.add_column("Tests", justify="right")
        t.add_column("Hash", style="dim")

        for sprint_key, sprint_commits in sorted(groups.items()):
            latest_tests = max(
                (c.test_count for c in sprint_commits if c.test_count),
                default=None
            )
            desc = "; ".join(_extract_description(c.subject) for c in sprint_commits[:2])
            if len(sprint_commits) > 2:
                desc += f" +{len(sprint_commits) - 2} more"
            t.add_row(
                sprint_key,
                desc[:80],
                str(latest_tests) if latest_tests else "--",
                sprint_commits[-1].hash,
            )
        console.print(t)
        console.print(f"\n  {len(commits)} commit(s) since {since}")

    elif fmt == "agents-md":
        lines = []
        for sprint_key, sprint_commits in sorted(groups.items()):
            if sprint_key == "untagged":
                continue
            latest_tests = max(
                (c.test_count for c in sprint_commits if c.test_count), default=None
            )
            desc = " ".join(_extract_description(c.subject) for c in sprint_commits)
            test_str = f"; {latest_tests} tests" if latest_tests else ""
            lines.append(f"| {sprint_key} | {desc[:120]}{test_str} | COMPLETE |")
        print("\n".join(lines))

    elif fmt == "session-log":
        proj = project or _run_git(["remote", "get-url", "origin"]).split("/")[-1].replace(".git", "") or "atlas-memory+"
        sprint_parts = []
        for sprint_key, sprint_commits in sorted(groups.items()):
            if sprint_key == "untagged":
                continue
            descs = [_extract_description(c.subject) for c in sprint_commits]
            sprint_parts.append(f"{sprint_key}: {', '.join(descs[:3])}")
        all_tests = max((c.test_count for c in commits if c.test_count), default=None)
        test_note = f" {all_tests} tests total." if all_tests else ""
        print(
            f"## {today} -- {proj} sprint summary\n\n"
            f"/loop 30m autonomous sprint. " +
            " ".join(sprint_parts) + f".{test_note}"
        )

    elif fmt == "json":
        import json
        out = []
        for sprint_key, sprint_commits in sorted(groups.items()):
            out.append({
                "sprint": sprint_key,
                "commits": [{"hash": c.hash, "subject": c.subject, "test_count": c.test_count}
                            for c in sprint_commits],
                "latest_tests": max((c.test_count for c in sprint_commits if c.test_count), default=None),
            })
        print(json.dumps(out, indent=2))

    else:
        console.print(f"[red]Unknown format {fmt!r}. Use: table, agents-md, session-log, json[/red]")
        raise typer.Exit(1)


@app.command("write-session-log")
def cmd_write_session_log(
    since: str = typer.Option("yesterday", "--since", "-s",
        help="Git ref, tag, or ISO date (e.g. HEAD~5, 2026-05-20)"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Print entry without writing."),
):
    """Auto-write today's session log entry from git commits.

    Prepends a sprint summary paragraph to .memory/sessions/_history.md
    and <workspace>/updates.txt. No-op if no commits found since --since.
    """
    result = write_session_log(since=since, dry_run=dry_run)
    if not result.get("ok"):
        console.print(f"[red]Error: {result.get('error', 'unknown')}[/red]")
        raise typer.Exit(1)
    if not result.get("entry"):
        console.print(f"[yellow]No commits found since {since!r}. Nothing to write.[/yellow]")
        return
    if dry_run:
        console.print("[cyan]DRY RUN -- would write:[/cyan]")
        console.print(result["entry"])
    else:
        console.print(f"[green]Session log written ({len(result['paths_written'])} file(s)):[/green]")
        for p in result["paths_written"]:
            console.print(f"  {p}")


@app.command("last-test-count")
def cmd_last_test_count():
    """Print the most recent test count found in git commit messages."""
    raw = _run_git(["log", "--pretty=format:%s %b", "-50"])
    for line in raw.splitlines():
        m = _TEST_COUNT_RE.search(line)
        if m:
            print(m.group(1))
            return
    print("unknown")


def _atomic_write(path, text: str) -> None:
    """tmp-and-replace with fsync: a crash never truncates the history."""
    import os

    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def _version_existing(path, corpus_root) -> None:
    """Version-before-write. Corpus files go to <corpus>/versions/...; the
    updates file (not in the corpus) is copied to a versions/ dir beside it."""
    import shutil
    from datetime import date as _date

    if not path.exists():
        return
    if corpus_root is not None:
        from atlas_plus.mcp.memory_server.versioning import version_before_write

        version_before_write(path, root=corpus_root)
        return
    vdir = path.parent / "versions"
    vdir.mkdir(parents=True, exist_ok=True)
    n = 1
    while True:
        dest = vdir / f"{path.stem}_{_date.today().isoformat()}_v{n}{path.suffix}"
        if not dest.exists():
            break
        n += 1
    shutil.copy2(path, dest)


def write_session_log(
    since: str = "yesterday",
    memory_root: Optional[Path] = None,
    updates_path: Optional[Path] = None,
    dry_run: bool = False,
) -> dict:
    """Auto-write a session log entry from today's git commits.

    Generates a brief session log paragraph using the sprint summary
    session-log format, then prepends it to:
      - <memory_root>/sessions/_history.md
      - <updates_path> (e.g. <workspace>/updates.txt)
      - <memory_root>/sessions/<YYYY-MM-DD>.md (per-day detail file)

    Returns {"ok": bool, "entry": str, "paths_written": [...]}.
    No-op (ok=True, paths_written=[]) if no commits found since `since`.
    dry_run=True returns the entry text without writing.
    """
    import os
    from datetime import date, timedelta

    # Resolve natural language date references to ISO datetime strings.
    # Append 00:00:00 so git interprets the date in local time, not UTC midnight.
    _since = since.lower().strip()
    if _since == "today":
        since = date.today().isoformat() + " 00:00:00"
    elif _since == "yesterday":
        since = (date.today() - timedelta(days=1)).isoformat() + " 00:00:00"

    commits = _parse_commits(since)
    if not commits:
        return {"ok": True, "entry": "", "paths_written": [], "reason": f"no commits since {since!r}"}

    today = date.today().isoformat()
    proj = (
        _run_git(["remote", "get-url", "origin"]).split("/")[-1].replace(".git", "")
        or "atlas-memory+"
    )
    sprint_parts = []
    groups = _group_by_sprint(commits)
    for sprint_key, sprint_commits in sorted(groups.items()):
        if sprint_key == "untagged":
            continue
        descs = [_extract_description(c.subject) for c in sprint_commits]
        sprint_parts.append(f"{sprint_key}: {', '.join(descs[:3])}")
    if not sprint_parts:
        sprint_parts = [_extract_description(c.subject) for c in commits[:3]]

    all_tests = max((c.test_count for c in commits if c.test_count), default=None)
    test_note = f" {all_tests} tests total." if all_tests else ""

    entry = (
        f"## {today} -- {proj}\n\n"
        f"/loop 30m autonomous sprint. "
        + " ".join(sprint_parts)
        + f".{test_note}"
        + "\n"
    )

    paths_written: list[str] = []

    if dry_run:
        return {"ok": True, "entry": entry, "paths_written": paths_written}

    # Default paths
    import os
    from pathlib import Path
    mem_root = memory_root or atlas_paths.memory_root()
    upd_path = updates_path or atlas_paths.updates_file()

    sessions_dir = mem_root / "sessions"
    try:
        sessions_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass

    # Prepend to _history.md (versioned first, then tmp-and-replace)
    history_file = sessions_dir / "_history.md"
    try:
        existing = history_file.read_text(encoding="utf-8") if history_file.exists() else ""
        _version_existing(history_file, mem_root)
        _atomic_write(history_file, entry + "\n" + existing)
        paths_written.append(str(history_file))
    except OSError as exc:
        return {"ok": False, "entry": entry, "paths_written": paths_written, "error": str(exc)}

    # Write per-day detail file (non-destructive: only if it doesn't already start with today's header)
    day_file = sessions_dir / f"{today}.md"
    try:
        if not day_file.exists():
            _atomic_write(day_file, entry)
            paths_written.append(str(day_file))
    except OSError:
        pass

    # Prepend to updates.txt (legacy)
    try:
        if upd_path.exists():
            existing_upd = upd_path.read_text(encoding="utf-8")
            _version_existing(upd_path, None)
            _atomic_write(upd_path, entry + "\n" + existing_upd)
            paths_written.append(str(upd_path))
    except OSError:
        pass

    return {"ok": True, "entry": entry, "paths_written": paths_written}


def main() -> None:
    """Entry point for the sprint-summary Typer app."""
    app()


if __name__ == "__main__":
    main()
