"""Session co-occurrence pattern analysis.

Reads dated session log files from .memory/sessions/ and computes which
projects appear together in the same day's sessions. The result is a JSON
model used by session_start.py to predict what context will be needed.

This never calls an LLM -- it is pure co-occurrence counting over project
names extracted from session log entry titles.
"""

from __future__ import annotations

import json
import os
import re
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from atlas_plus import paths as atlas_paths

MEMORY_ROOT = atlas_paths.memory_root()
STATE_DIR = atlas_paths.state_dir()
PATTERNS_FILE = STATE_DIR / "session-patterns.json"

# Minimum times two projects must co-occur before we record the relationship.
_MIN_COOCCURRENCE = 2
# Minimum confidence (0-1) to include in predictions.
_MIN_CONFIDENCE = 0.30


def _extract_project_from_title(title: str) -> str | None:
    """Pull the first capitalised project-like word from a session log title.

    Looks for known project directory names that appear in the title text.
    Returns the first match or None.
    """
    projects_root = atlas_paths.projects_root()
    try:
        known = {d.name.lower() for d in projects_root.iterdir() if d.is_dir() and not d.name.startswith(".")}
    except OSError:
        known = set()

    lower = title.lower()
    for name in known:
        if name in lower:
            return name
    return None


def _parse_session_history(history_file: Path) -> list[tuple[str, str]]:
    """Parse _history.md and return list of (date_str, project_name) pairs."""
    if not history_file.exists():
        return []
    raw = history_file.read_text(encoding="utf-8", errors="replace")
    results: list[tuple[str, str]] = []
    # Each entry starts with "## YYYY-MM-DD - Title"
    for match in re.finditer(r"^## (\d{4}-\d{2}-\d{2}) - (.+)$", raw, re.MULTILINE):
        date_str = match.group(1)
        title = match.group(2).strip()
        project = _extract_project_from_title(title)
        if project:
            results.append((date_str, project))
    return results


def build_cooccurrence(since_days: int = 120) -> tuple[dict, dict]:
    """Compute co-occurrence counts across dated session entries.

    Returns (cooccurrence, project_count) where:
      cooccurrence = {project_a: {project_b: count, ...}, ...}
      project_count = {project: sessions_seen, ...}
    """
    history_file = MEMORY_ROOT / "sessions" / "_history.md"
    entries = _parse_session_history(history_file)
    if not entries:
        return {}, {}

    now_date = datetime.now(timezone.utc).date()

    # Group projects by date
    by_date: dict[str, set[str]] = defaultdict(set)
    for date_str, project in entries:
        try:
            d = datetime.strptime(date_str, "%Y-%m-%d").date()
            if (now_date - d).days <= since_days:
                by_date[date_str].add(project)
        except ValueError:
            pass

    # Count co-occurrences
    cooccur: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    project_count: dict[str, int] = defaultdict(int)
    for _date, projects in by_date.items():
        proj_list = sorted(projects)
        for p in proj_list:
            project_count[p] += 1
        for i, pa in enumerate(proj_list):
            for pb in proj_list[i + 1:]:
                cooccur[pa][pb] += 1
                cooccur[pb][pa] += 1

    return {p: dict(v) for p, v in cooccur.items()}, dict(project_count)


def compute_predictions(
    current_project: str,
    cooccurrence: dict,
    project_count: dict,
) -> list[dict]:
    """Return predicted context needs for current_project, ranked by confidence.

    confidence = cooccurrence(a, b) / sessions_with_a
    """
    low = current_project.lower()
    related = cooccurrence.get(low, {})
    total_sessions = project_count.get(low, 1)

    predictions: list[dict] = []
    for other, count in related.items():
        if count < _MIN_COOCCURRENCE:
            continue
        confidence = round(count / total_sessions, 2)
        if confidence >= _MIN_CONFIDENCE:
            predictions.append({
                "project": other,
                "confidence": confidence,
                "cooccurrences": count,
            })

    return sorted(predictions, key=lambda x: x["confidence"], reverse=True)


def save_patterns(since_days: int = 120) -> dict:
    """Rebuild and persist the co-occurrence model to disk."""
    cooccurrence, project_count = build_cooccurrence(since_days)

    model = {
        "built_at": datetime.now(timezone.utc).isoformat(),
        "since_days": since_days,
        "cooccurrence": cooccurrence,
        "project_count": project_count,
    }
    PATTERNS_FILE.parent.mkdir(parents=True, exist_ok=True)
    PATTERNS_FILE.write_text(json.dumps(model, indent=2), encoding="utf-8")
    return model


def load_patterns() -> dict | None:
    """Load cached patterns model, or None if not built yet."""
    if not PATTERNS_FILE.exists():
        return None
    try:
        return json.loads(PATTERNS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return None


def predict_for_project(current_project: str) -> list[dict]:
    """Return predictions using the cached model. Returns [] if model absent."""
    model = load_patterns()
    if not model:
        return []
    return compute_predictions(
        current_project.lower(),
        model.get("cooccurrence", {}),
        model.get("project_count", {}),
    )
