"""Memory file health scoring -- composite metadata quality per .memory/ file.

Health score is a simple 0-1 composite across four metadata factors (each 0.25):
  tags        -- has tags: or tag: frontmatter (improves topic search)
  confirmed   -- has last_confirmed: within 90 days (verifies fact still accurate)
  updated     -- has updated: date field (enables age-based decay)
  confidence  -- has confidence: high|medium|low (calibrates search ranking)

Score 1.0 = fully annotated file. Score 0.0 = missing all four fields.
Designed to be run against any .memory/ subdirectory to surface files
that need attention. Does not modify files -- callers decide what to do.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Optional

from .scan import is_excluded_mirror

_CONFIRMED_STALE_DAYS = 90
_SKIP_DIRS = {"archive", "_sync", "versions", "_atlas", "anti"}


from atlas_plus._yaml import fast_safe_load as _fast_yaml_load


def compute_file_health(path: Path, today: Optional[date] = None) -> dict:
    """Compute a health dict for a single .memory/ markdown file.

    Returns:
        {
          score: float (0-1, sum of factor weights),
          factors: {tags, confirmed, updated, confidence},  # each 0.0 or 0.25
          issues: list[str],   # human-readable list of missing fields
          frontmatter: dict,   # raw parsed frontmatter (empty on parse error)
        }
    Returns {score: 0.0, ...} on unreadable or non-markdown files.
    """
    import yaml as _yaml

    today = today or date.today()
    zero = {"score": 0.0, "factors": {"tags": 0.0, "confirmed": 0.0, "updated": 0.0, "confidence": 0.0},
            "issues": ["unreadable"], "frontmatter": {}}

    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return zero

    if not raw.startswith("---"):
        return {**zero, "issues": ["no_frontmatter"]}
    end = raw.find("\n---", 3)
    if end == -1:
        return {**zero, "issues": ["no_frontmatter"]}
    try:
        fm = _fast_yaml_load(raw[3:end]) or {}
    except Exception:
        return {**zero, "issues": ["parse_error"]}

    factors = {}
    issues = []

    # Tags factor
    has_tags = bool(fm.get("tags") or fm.get("tag"))
    factors["tags"] = 0.25 if has_tags else 0.0
    if not has_tags:
        issues.append("no_tags")

    # Confirmed factor (last_confirmed within _CONFIRMED_STALE_DAYS)
    lc = fm.get("last_confirmed")
    if lc:
        try:
            lc_date = date.fromisoformat(str(lc)[:10])
            is_fresh = (today - lc_date).days <= _CONFIRMED_STALE_DAYS
            factors["confirmed"] = 0.25 if is_fresh else 0.0
            if not is_fresh:
                issues.append("stale_confirmed")
        except (ValueError, AttributeError):
            factors["confirmed"] = 0.0
            issues.append("stale_confirmed")
    else:
        factors["confirmed"] = 0.0
        issues.append("unconfirmed")

    # Updated factor
    has_updated = bool(fm.get("updated"))
    factors["updated"] = 0.25 if has_updated else 0.0
    if not has_updated:
        issues.append("no_updated")

    # Confidence factor
    conf = str(fm.get("confidence", "")).lower()
    has_confidence = conf in {"high", "medium", "low"}
    factors["confidence"] = 0.25 if has_confidence else 0.0
    if not has_confidence:
        issues.append("no_confidence")

    score = round(sum(factors.values()), 2)
    return {"score": score, "factors": factors, "issues": issues, "frontmatter": fm}


def scan_memory_health(
    memory_root: Path,
    scope: str = "",
    min_health: float = 0.0,
    limit: int = 200,
) -> list[dict]:
    """Scan .memory/ for files and return health dicts for those below min_health.

    Files with score >= 1.0 are fully healthy and excluded when min_health > 0.
    Returns list sorted by score ascending (lowest health first).
    """
    scan_root = memory_root / scope if scope else memory_root
    today = date.today()
    results = []

    for md_file in sorted(scan_root.rglob("*.md")):
        if len(results) >= limit:
            break
        rel_parts = md_file.relative_to(memory_root).parts
        if any(p in _SKIP_DIRS for p in rel_parts) or is_excluded_mirror(rel_parts):
            continue
        if md_file.name.startswith("_"):
            continue

        health = compute_file_health(md_file, today)
        score = health["score"]
        if score < min_health or min_health == 0.0:
            rel = str(md_file.relative_to(memory_root)).replace("\\", "/")
            results.append({
                "path": rel,
                "score": score,
                "issues": health["issues"],
                "factors": health["factors"],
                "title": str(health["frontmatter"].get("title", md_file.stem))[:50],
            })

    results.sort(key=lambda r: r["score"])
    return results
