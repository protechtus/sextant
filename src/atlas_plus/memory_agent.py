"""Memory agent -- weekly scan that surfaces actionable memory maintenance tasks.

Runs as a Brain scheduler job (Sunday 03:50 UTC). Never writes facts directly.
Instead it:
  1. Scans for stale project files (project in recent sessions but memory not updated)
  2. Checks for decisions whose revisit_if conditions might be met
  3. Finds facts past their half-life threshold
  4. Creates tickets for anything actionable
  5. Sends a Telegram push summary via ntfy.sh

The agent is read-only with respect to .memory/. It writes only to brain_tasks
and creates tickets. This keeps the human in the loop for all memory changes.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from atlas_plus import paths as atlas_paths

STATE_DIR = atlas_paths.state_dir()
MEMORY_ROOT = atlas_paths.memory_root()
PROJECTS_ROOT = atlas_paths.projects_root()


@dataclass
class MemoryAgentReport:
    """Aggregated output of a weekly memory-agent run: stale projects, decayed facts, decisions, tickets."""

    stale_projects: list[dict] = field(default_factory=list)
    decayed_facts: list[dict] = field(default_factory=list)
    decisions_to_revisit: list[dict] = field(default_factory=list)
    tickets_created: list[str] = field(default_factory=list)
    generated_at: str = ""

    def total_issues(self) -> int:
        """Return sum of stale project, decayed fact, and decision counts."""
        return len(self.stale_projects) + len(self.decayed_facts) + len(self.decisions_to_revisit)


def _recent_projects_from_history(days: int = 14) -> list[str]:
    """Parse _history.md to find projects mentioned in the last N days."""
    import re
    history = MEMORY_ROOT / "sessions" / "_history.md"
    if not history.exists():
        return []

    now = datetime.now(timezone.utc)
    cutoff_str = (now.date().isoformat())
    projects: set[str] = set()

    known: set[str] = set()
    try:
        known = {d.name.lower() for d in PROJECTS_ROOT.iterdir() if d.is_dir()}
    except OSError:
        pass

    raw = history.read_text(encoding="utf-8", errors="replace")
    for match in re.finditer(r"^## (\d{4}-\d{2}-\d{2}) - (.+)$", raw, re.MULTILINE):
        date_str = match.group(1)
        title = match.group(2).lower()
        try:
            d = datetime.strptime(date_str, "%Y-%m-%d").date()
            age = (now.date() - d).days
            if age > days:
                continue
        except ValueError:
            continue
        for name in known:
            if name in title:
                projects.add(name)

    return sorted(projects)


def scan_stale_projects(since_days: int = 14) -> list[dict]:
    """Find projects active in recent sessions whose memory file is outdated."""
    import yaml

    recent = _recent_projects_from_history(days=since_days)
    stale: list[dict] = []
    now = datetime.now(timezone.utc)

    for project in recent:
        proj_file = MEMORY_ROOT / "projects" / f"{project}.md"
        if not proj_file.exists():
            stale.append({"project": project, "reason": "no memory file", "days_old": None})
            continue

        try:
            raw = proj_file.read_text(encoding="utf-8", errors="replace")
            if raw.startswith("---"):
                end = raw.find("\n---", 3)
                if end != -1:
                    fm = yaml.safe_load(raw[3:end]) or {}
                    updated_raw = fm.get("updated", "")
                    if updated_raw:
                        updated_dt = datetime.strptime(str(updated_raw)[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
                        age = (now - updated_dt).days
                        if age > 30:
                            stale.append({"project": project, "reason": f"not updated in {age} days", "days_old": age})
        except Exception:
            pass

    return stale


def scan_decisions_to_revisit() -> list[dict]:
    """Find decisions where revisit_if conditions might be met.

    Currently returns decisions older than 180 days that have non-empty
    revisit_if lists -- a proxy for 'conditions may have changed'.
    """
    import yaml

    decisions_dir = MEMORY_ROOT / "decisions"
    if not decisions_dir.exists():
        return []

    now = datetime.now(timezone.utc)
    candidates: list[dict] = []

    for f in decisions_dir.glob("*.md"):
        try:
            raw = f.read_text(encoding="utf-8", errors="replace")
            if not raw.startswith("---"):
                continue
            end = raw.find("\n---", 3)
            if end == -1:
                continue
            fm = yaml.safe_load(raw[3:end]) or {}

            if fm.get("status") != "active":
                continue

            revisit_if = fm.get("revisit_if") or []
            if not revisit_if:
                continue

            made_raw = str(fm.get("made", ""))[:10]
            if not made_raw:
                continue

            made_dt = datetime.strptime(made_raw, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            age = (now - made_dt).days

            if age >= 180:
                candidates.append({
                    "slug": f.stem,
                    "title": str(fm.get("title", f.stem)),
                    "project": str(fm.get("project", "")),
                    "made": made_raw,
                    "age_days": age,
                    "revisit_if": revisit_if[:2],
                })
        except Exception:
            pass

    return sorted(candidates, key=lambda x: x["age_days"], reverse=True)


def scan_decayed_facts(threshold_days: int = 90, limit: int = 10) -> list[dict]:
    """Return top decayed facts (past their half_life threshold)."""
    try:
        from atlas_plus.curation.decay import find_decayed_facts
        candidates = find_decayed_facts(threshold_days=0)
        return [c.as_dict() for c in candidates[:limit]]
    except Exception:
        return []


def create_review_tickets(report: MemoryAgentReport) -> list[str]:
    """Create tickets for actionable items. Returns list of created ticket IDs."""
    try:
        from atlas_plus.tickets.store import create_ticket
    except Exception:
        return []

    created: list[str] = []

    for sp in report.stale_projects[:3]:
        try:
            t = create_ticket(
                title=f"Review project memory: {sp['project']} ({sp['reason']})",
                project="atlas-memory+",
                assignee="human",
                priority="normal",
                body=f"Project '{sp['project']}' was active recently but memory file is stale.\nAction: update .memory/projects/{sp['project']}.md with current state.",
            )
            created.append(t.id)
        except Exception:
            pass

    for dec in report.decisions_to_revisit[:3]:
        try:
            revisit_text = "\n".join(f"- {c}" for c in dec.get("revisit_if", []))
            t = create_ticket(
                title=f"Revisit decision: {dec['title']} (made {dec['made']}, {dec['age_days']}d ago)",
                project=dec.get("project", "atlas-memory+") or "atlas-memory+",
                assignee="human",
                priority="normal",
                body=f"Decision is {dec['age_days']} days old. Check if conditions have changed.\n\nRevisit if:\n{revisit_text}\n\nSee: .memory/decisions/{dec['slug']}.md",
            )
            created.append(t.id)
        except Exception:
            pass

    return created


def format_push_summary(report: MemoryAgentReport) -> str:
    """Format a concise Telegram/ntfy push message."""
    lines = [f"Memory Agent Weekly ({report.generated_at[:10]})"]
    if report.stale_projects:
        names = ", ".join(s["project"] for s in report.stale_projects[:3])
        lines.append(f"  Stale projects: {names}")
    if report.decisions_to_revisit:
        lines.append(f"  Decisions to revisit: {len(report.decisions_to_revisit)}")
    if report.decayed_facts:
        lines.append(f"  Facts past half-life: {len(report.decayed_facts)}")
    if report.tickets_created:
        lines.append(f"  Tickets created: {len(report.tickets_created)}")
    if report.total_issues() == 0:
        lines.append("  All clear -- no maintenance needed.")
    return "\n".join(lines)


def run_weekly_agent(
    create_tickets: bool = True,
    push_ntfy: bool = True,
    run_narrative: bool = True,
) -> MemoryAgentReport:
    """Run the full weekly memory maintenance scan."""
    report = MemoryAgentReport(generated_at=datetime.now(timezone.utc).isoformat())

    report.stale_projects = scan_stale_projects(since_days=14)
    report.decisions_to_revisit = scan_decisions_to_revisit()
    report.decayed_facts = scan_decayed_facts(threshold_days=0, limit=10)

    if run_narrative:
        try:
            from atlas_plus.session_narrative import run_narrative as _run_narrative
            _run_narrative(since_days=14)
        except Exception:
            pass

    if create_tickets and (report.stale_projects or report.decisions_to_revisit):
        report.tickets_created = create_review_tickets(report)

    if push_ntfy:
        _try_push(report)

    # Write report artifact
    try:
        from datetime import date
        reports_dir = STATE_DIR / "reports"
        reports_dir.mkdir(parents=True, exist_ok=True)
        today = date.today()
        week = today.isocalendar()[1]
        out = reports_dir / f"memory-agent-{today.year}-W{week:02d}.md"
        lines = [
            f"# Memory Agent Report -- {today.year} W{week:02d}",
            f"\nGenerated: {report.generated_at}",
            f"\n## Summary\n",
            f"- Stale projects: {len(report.stale_projects)}",
            f"- Decisions to revisit: {len(report.decisions_to_revisit)}",
            f"- Decayed facts: {len(report.decayed_facts)}",
            f"- Tickets created: {len(report.tickets_created)}",
        ]
        if report.stale_projects:
            lines += ["\n## Stale Projects\n"]
            for sp in report.stale_projects:
                lines.append(f"- {sp['project']}: {sp['reason']}")
        if report.decisions_to_revisit:
            lines += ["\n## Decisions to Revisit\n"]
            for d in report.decisions_to_revisit:
                lines.append(f"- [{d['slug']}] {d['title']} (made {d['made']}, {d['age_days']}d ago)")
        if report.decayed_facts:
            lines += ["\n## Decayed Facts\n"]
            for f in report.decayed_facts:
                lines.append(f"- {f['path']}: {f['days_overdue']}d overdue")
        out.write_text("\n".join(lines), encoding="utf-8")
    except Exception:
        pass

    return report


def _try_push(report: MemoryAgentReport) -> None:
    """Send ntfy.sh push. Best-effort, never raises."""
    ntfy_url = os.environ.get("ATLAS_NTFY_URL", "")
    if not ntfy_url:
        return
    try:
        import httpx
        msg = format_push_summary(report)
        headers = {
            "Title": "Atlas Memory Agent",
            "Priority": "default" if report.total_issues() == 0 else "high",
            "Tags": "memory,brain",
        }
        _ntfy_token = os.environ.get("ATLAS_NTFY_TOKEN", "")  # ATL-007: auth for reserved topics
        if _ntfy_token:
            headers["Authorization"] = f"Bearer {_ntfy_token}"
        httpx.post(
            ntfy_url,
            content=msg.encode(),
            headers=headers,
            timeout=5.0,
        )
    except Exception:
        pass
