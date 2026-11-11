"""Memory half-life decay -- surface facts past their review threshold.

Facts with a `half_life: N` frontmatter field (days) are flagged for
review when they have not been referenced for N days. Constraints
(decays: false) are exempt.

The 'last_referenced' proxy is the file's updated: frontmatter date.
A proper access log (from memory_read calls) would be more accurate,
but the file's updated: date is sufficient for the first pass.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional

import yaml

from atlas_plus.mcp.memory_server.config import MEMORY_ROOT
from atlas_plus.temporal_graph.decay_model import DecayAction, assess

from .scan import is_excluded_mirror


@dataclass
class DecayCandidate:
    """A memory file whose content has exceeded its type-specific half-life and needs review."""

    path: str            # relative to MEMORY_ROOT
    title: str
    memory_type: str
    half_life_days: int
    updated: str         # last updated date (YYYY-MM-DD)
    days_overdue: int    # how far past the half_life threshold
    state: str = ""              # epistemic state: aging | stale | expired
    action: str = ""             # prescribed action: surface | reverify | quarantine
    effective_confidence: float = 1.0   # base confidence eroded by age, in [0, 1]

    def as_dict(self) -> dict:
        """Return a JSON-serialisable dict of the decay candidate fields."""
        return {
            "path": self.path,
            "title": self.title,
            "type": self.memory_type,
            "half_life_days": self.half_life_days,
            "updated": self.updated,
            "days_overdue": self.days_overdue,
            "state": self.state,
            "action": self.action,
            "effective_confidence": round(self.effective_confidence, 4),
        }


def _parse_frontmatter(path: Path) -> dict:
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
        if not raw.startswith("---"):
            return {}
        end = raw.find("\n---", 3)
        if end == -1:
            return {}
        return yaml.safe_load(raw[3:end]) or {}
    except Exception:
        return {}


def find_decayed_facts(
    threshold_days: int = 30,
    scope: str = "",
) -> list[DecayCandidate]:
    """Find memory files past their half_life threshold.

    Args:
        threshold_days: Default half-life to apply when no `half_life:` field
                        is present. Set to 0 to only flag files with explicit
                        half_life fields.
        scope: Subdirectory to restrict scan (e.g. 'feedback', 'decisions').

    Returns list of DecayCandidate sorted by days_overdue descending.
    """
    today = date.today()
    candidates: list[DecayCandidate] = []

    search_root = MEMORY_ROOT / scope if scope else MEMORY_ROOT
    if not search_root.exists():
        return []

    excluded = {"archive", "__pycache__", ".git", "_inbox", "versions"}

    for md_file in search_root.rglob("*.md"):
        parts = md_file.relative_to(MEMORY_ROOT).parts
        if any(p in excluded for p in parts):
            continue
        if is_excluded_mirror(parts):
            continue

        fm = _parse_frontmatter(md_file)
        if not fm:
            continue

        # Constraints never decay
        if fm.get("decays") is False or fm.get("type") == "constraint":
            continue

        half_life = fm.get("half_life")
        if half_life is None:
            if threshold_days <= 0:
                continue
            half_life = threshold_days
        else:
            try:
                half_life = int(half_life)
            except (ValueError, TypeError):
                continue

        updated_raw = fm.get("updated", "")
        if not updated_raw:
            continue

        try:
            updated_date = date.fromisoformat(str(updated_raw)[:10])
        except ValueError:
            continue

        age_days = (today - updated_date).days
        days_overdue = age_days - half_life
        if days_overdue <= 0:
            continue

        base_conf = fm.get("confidence", 1.0)
        try:
            base_conf = float(base_conf)
        except (ValueError, TypeError):
            base_conf = 1.0
        verdict = assess(base_conf, age_days, half_life)

        rel = str(md_file.relative_to(MEMORY_ROOT)).replace("\\", "/")
        candidates.append(DecayCandidate(
            path=rel,
            title=str(fm.get("title", md_file.stem)),
            memory_type=str(fm.get("type", "unknown")),
            half_life_days=half_life,
            updated=str(updated_raw)[:10],
            days_overdue=days_overdue,
            state=verdict.state.value,
            action=verdict.action.value,
            effective_confidence=verdict.effective_confidence,
        ))

    candidates.sort(key=lambda c: c.days_overdue, reverse=True)
    return candidates


def group_by_action(candidates: list[DecayCandidate]) -> dict[str, list[DecayCandidate]]:
    """Bucket decay candidates by their prescribed action (surface | reverify | quarantine)."""
    groups: dict[str, list[DecayCandidate]] = {}
    for c in candidates:
        groups.setdefault(c.action or DecayAction.SURFACE.value, []).append(c)
    return groups


def reverify_queue(candidates: list[DecayCandidate]) -> list[DecayCandidate]:
    """Candidates whose decay action calls for autonomous re-verification (stale or expired),
    most-overdue first. This is the behavioral trigger the Brain drains into reverify tasks."""
    actionable = {DecayAction.REVERIFY.value, DecayAction.QUARANTINE.value}
    return [c for c in candidates if c.action in actionable]
