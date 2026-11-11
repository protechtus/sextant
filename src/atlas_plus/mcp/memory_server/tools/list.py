"""memory.list — enumerate .memory/ files with frontmatter metadata."""

import time
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import yaml

from atlas_plus.curation.scan import is_excluded_mirror

from ..config import EXCLUDED_DIRS, LIST_CACHE_TTL, MEMORY_ROOT, safe_resolve_within


def _excluded(rel_parts: tuple) -> bool:
    """True if a path is excluded from listings: any component is an excluded dir or
    dot-prefixed (audit HIGH-2), or it is inside the reference/research mirror (which has
    its own dedicated RAG and only dilutes general memory listings)."""
    if any(part in EXCLUDED_DIRS or part.startswith(".") for part in rel_parts):
        return True
    return is_excluded_mirror(rel_parts)

_cache: dict[str, Any] = {}
_cache_ts: dict[str, float] = {}  # per-key timestamps — shared float was a bug


def _parse_frontmatter(text: str) -> dict:
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end == -1:
        return {}
    try:
        return yaml.safe_load(text[3:end].strip()) or {}
    except Exception:
        return {}


def memory_list(prefix: str = "", limit: int = 50) -> list[dict]:
    """List .memory/ files, optionally filtered by prefix directory.

    Returns list of {path, title, type, priority, updated} dicts.
    Excludes archive/, _sync/, versions/.
    """
    global _cache, _cache_ts

    limit = min(limit, 200)
    cache_key = f"{prefix}:{limit}"

    now = time.monotonic()
    if cache_key in _cache and (now - _cache_ts.get(cache_key, 0.0)) < LIST_CACHE_TTL:
        return _cache[cache_key]

    # One resolved base for the walk AND the relative paths: a corpus root spelled as an
    # 8.3 short name (%TEMP% on a profile with a space) made relative_to() raise "not in
    # the subpath" for every entry (power-user pass 2026-11-10).
    base = MEMORY_ROOT.resolve()
    root = base
    if prefix:
        # Confine the prefix to MEMORY_ROOT (audit HIGH-2): reject .. / abs / dot-dir escapes.
        confined = safe_resolve_within(MEMORY_ROOT, prefix)
        if confined is None:
            return []
        root = confined

    results = []
    try:
        candidates = sorted(root.rglob("*.md"))
    except (FileNotFoundError, OSError):
        return []

    for p in candidates:
        rel_parts = p.relative_to(base).parts
        if _excluded(rel_parts):
            continue

        rel = str(p.relative_to(base)).replace("\\", "/")
        try:
            fm = _parse_frontmatter(p.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            fm = {}

        results.append(
            {
                "path": rel,
                "title": fm.get("title", p.stem),
                "type": fm.get("type", ""),
                "priority": fm.get("priority", ""),
                "updated": str(fm.get("updated", "")),
            }
        )
        if len(results) >= limit:
            break

    _cache[cache_key] = results
    _cache_ts[cache_key] = now
    return results


def memory_list_confirmed(since_days: int = 7, limit: int = 50) -> list[dict]:
    """List .memory/ files that have been confirmed as accurate recently.

    Returns files where `last_confirmed` frontmatter is within `since_days` days of today,
    sorted newest confirmation first.  Useful for reviewing what an agent has verified
    and spotting any gaps in the confirmation coverage.

    Returns list of {path, title, last_confirmed, confidence, source} dicts.
    """
    cutoff = date.today() - timedelta(days=max(1, since_days))
    limit = min(limit, 200)

    results = []
    base = MEMORY_ROOT.resolve()
    try:
        candidates = sorted(base.rglob("*.md"))
    except (FileNotFoundError, OSError):
        return []

    for p in candidates:
        rel_parts = p.relative_to(base).parts
        if _excluded(rel_parts):
            continue

        try:
            fm = _parse_frontmatter(p.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue

        lc = fm.get("last_confirmed")
        if not lc:
            continue
        try:
            lc_date = date.fromisoformat(str(lc)[:10])
        except (ValueError, TypeError):
            continue
        if lc_date < cutoff:
            continue

        rel = str(p.relative_to(base)).replace("\\", "/")
        results.append(
            {
                "path": rel,
                "title": fm.get("title", p.stem),
                "last_confirmed": str(lc_date),
                "confidence": fm.get("confidence", ""),
                "source": fm.get("source", ""),
            }
        )

    results.sort(key=lambda x: x["last_confirmed"], reverse=True)
    return results[:limit]
