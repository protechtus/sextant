"""Cross-session momentum and narrative analysis (Angle 7, WhereFromHere.md).

Asks "where is this work going?" rather than "what happened?"

A narrative summary captures:
  - Open loops: mentions of things not yet resolved (need to, blocked on, next session,
    TODO, should, will need, pending)
  - Direction signals: what's being built toward (implementing, building, adding,
    designing, planning, working on)
  - Energy: are sessions converging on a goal or diverging across problems?
  - Unresolved tensions: topics mentioned with both positive and negative sentiment

Output is written to .memory/sessions/YYYY-WNN-narrative.md.
The memory_agent weekly scan calls run_narrative() and includes it in the push summary.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Optional
from atlas_plus import paths as atlas_paths

_MEMORY_ROOT = atlas_paths.memory_root()
_SESSIONS_DIR = _MEMORY_ROOT / "sessions"

# Phrases that signal an open loop
_OPEN_LOOP_PATTERNS = [
    r"\bneed to\b", r"\bblocked on\b", r"\bnext session\b", r"\btodo\b",
    r"\bshould\b", r"\bwill need\b", r"\bpending\b", r"\bstill need\b",
    r"\bnot yet\b", r"\bdeferred\b", r"\bfollow.?up\b", r"\brevisit\b",
]

# Phrases that signal active direction
_DIRECTION_PATTERNS = [
    r"\bimplementing\b", r"\bbuilding\b", r"\badding\b", r"\bdesigning\b",
    r"\bplanning\b", r"\bworking on\b", r"\bin progress\b", r"\bstarted\b",
    r"\bbegun\b", r"\bwiring\b", r"\bshipping\b", r"\brolling out\b",
]

_OPEN_RE = re.compile("|".join(_OPEN_LOOP_PATTERNS), re.IGNORECASE)
_DIR_RE  = re.compile("|".join(_DIRECTION_PATTERNS), re.IGNORECASE)


@dataclass
class SessionEntry:
    """A single parsed dated session record from .memory/sessions/."""

    date_str: str
    title: str
    body: str


@dataclass
class NarrativeReport:
    """Weekly narrative synthesis: open loops, direction signals, active projects, and energy reading."""

    week_label: str         # e.g. "2026-W21"
    sessions_analyzed: int
    open_loops: list[str]   # deduped phrase fragments
    direction_signals: list[str]
    active_projects: list[str]
    energy: str             # "converging" | "diverging" | "neutral"
    generated_at: str


def _parse_dated_sessions(since_days: int = 14) -> list[SessionEntry]:
    """Read individual dated session files from .memory/sessions/."""
    cutoff = date.today() - timedelta(days=since_days)
    entries: list[SessionEntry] = []
    if not _SESSIONS_DIR.exists():
        return entries
    for f in sorted(_SESSIONS_DIR.glob("20??-??-??.md")):
        try:
            d = date.fromisoformat(f.stem)
        except ValueError:
            continue
        if d < cutoff:
            continue
        try:
            body = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        # Extract title from first H1/H2 line
        title_match = re.search(r"^#{1,2}\s+(.+)", body, re.MULTILINE)
        title = title_match.group(1).strip() if title_match else f.stem
        entries.append(SessionEntry(date_str=f.stem, title=title, body=body))
    return entries


def _extract_open_loops(entries: list[SessionEntry]) -> list[str]:
    """Extract unique open-loop sentence fragments across all sessions."""
    seen: set[str] = set()
    result: list[str] = []
    for e in entries:
        for line in e.body.splitlines():
            line = line.strip()
            if _OPEN_RE.search(line) and 10 < len(line) < 200:
                # Normalise + deduplicate by first 60 chars
                key = re.sub(r"\s+", " ", line.lower())[:60]
                if key not in seen:
                    seen.add(key)
                    result.append(line[:120])
    return result[:15]


def _extract_direction_signals(entries: list[SessionEntry]) -> list[str]:
    """Extract unique direction-signal sentence fragments."""
    seen: set[str] = set()
    result: list[str] = []
    for e in entries:
        for line in e.body.splitlines():
            line = line.strip()
            if _DIR_RE.search(line) and 10 < len(line) < 200:
                key = re.sub(r"\s+", " ", line.lower())[:60]
                if key not in seen:
                    seen.add(key)
                    result.append(line[:120])
    return result[:15]


def _extract_active_projects(entries: list[SessionEntry]) -> list[str]:
    """Extract project names mentioned across sessions using known project dirs."""
    try:
        projects_root = atlas_paths.projects_root()
        known = {d.name.lower() for d in projects_root.iterdir() if d.is_dir()} if projects_root.exists() else set()
    except OSError:
        known = set()
    counts: dict[str, int] = {}
    for e in entries:
        text_lower = e.body.lower()
        for proj in known:
            if len(proj) >= 4 and proj in text_lower:
                counts[proj] = counts.get(proj, 0) + 1
    return [p for p, _ in sorted(counts.items(), key=lambda x: -x[1])][:8]


def _compute_energy(entries: list[SessionEntry]) -> str:
    """Heuristic energy indicator: converging vs diverging vs neutral."""
    if len(entries) < 2:
        return "neutral"
    # Count unique active projects per session
    try:
        projects_root = atlas_paths.projects_root()
        known = {d.name.lower() for d in projects_root.iterdir() if d.is_dir()} if projects_root.exists() else set()
    except OSError:
        known = set()

    per_session = []
    for e in entries:
        text_lower = e.body.lower()
        mentioned = sum(1 for p in known if len(p) >= 4 and p in text_lower)
        per_session.append(mentioned)

    if not per_session:
        return "neutral"

    avg = sum(per_session) / len(per_session)
    # If recent sessions (last half) touch fewer projects than earlier, converging
    mid = len(per_session) // 2
    early_avg = sum(per_session[:mid]) / max(mid, 1)
    late_avg  = sum(per_session[mid:]) / max(len(per_session) - mid, 1)

    if late_avg < early_avg * 0.75:
        return "converging"
    if late_avg > early_avg * 1.25:
        return "diverging"
    return "neutral"


def build_narrative(since_days: int = 14) -> NarrativeReport:
    """Analyse sessions from the last N days and return a NarrativeReport."""
    from datetime import datetime, timezone
    entries = _parse_dated_sessions(since_days=since_days)
    today = date.today()
    week = today.isocalendar()[1]
    week_label = f"{today.year}-W{week:02d}"

    return NarrativeReport(
        week_label=week_label,
        sessions_analyzed=len(entries),
        open_loops=_extract_open_loops(entries),
        direction_signals=_extract_direction_signals(entries),
        active_projects=_extract_active_projects(entries),
        energy=_compute_energy(entries),
        generated_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M+00:00"),
    )


def write_narrative(report: NarrativeReport) -> Path:
    """Write narrative report to .memory/sessions/YYYY-WNN-narrative.md."""
    _SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    out = _SESSIONS_DIR / f"{report.week_label}-narrative.md"

    lines = [
        f"---",
        f"id: narrative-{report.week_label}",
        f"title: Weekly Narrative -- {report.week_label}",
        f"type: session",
        f"status: active",
        f"updated: {date.today().isoformat()}",
        f"---",
        f"",
        f"# Weekly Narrative: {report.week_label}",
        f"",
        f"Generated: {report.generated_at}",
        f"Sessions analysed (last 14 days): {report.sessions_analyzed}",
        f"Energy: **{report.energy}**",
        f"",
    ]

    if report.active_projects:
        lines += [
            "## Active projects",
            "",
            *[f"- {p}" for p in report.active_projects],
            "",
        ]

    if report.direction_signals:
        lines += [
            "## Direction signals",
            "",
            "What the work is building toward:",
            "",
            *[f"- {s}" for s in report.direction_signals],
            "",
        ]

    if report.open_loops:
        lines += [
            "## Open loops",
            "",
            "Things mentioned but not yet resolved:",
            "",
            *[f"- {s}" for s in report.open_loops],
            "",
        ]

    out.write_text("\n".join(lines), encoding="utf-8")
    return out


def run_narrative(since_days: int = 14) -> dict:
    """Build and write the narrative report. Returns summary dict."""
    report = build_narrative(since_days=since_days)
    if report.sessions_analyzed == 0:
        return {"ok": False, "reason": "no sessions found", "week": report.week_label}
    path = write_narrative(report)
    return {
        "ok": True,
        "week": report.week_label,
        "sessions": report.sessions_analyzed,
        "energy": report.energy,
        "open_loops": len(report.open_loops),
        "direction_signals": len(report.direction_signals),
        "path": str(path),
    }
