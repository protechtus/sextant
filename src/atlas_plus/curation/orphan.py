"""Memory orphan detection -- finds .memory/ files with no inbound references.

An orphan candidate is a file that:
  - Has no other .memory/ file mentioning its stem or path (no inbound refs)
  - Has a low health score (missing metadata like tags, confirmed, confidence)
  - Is old (no updated: date, or updated date older than min_age_days)

These are candidates for review and potential archival -- they represent
accumulated knowledge that no other part of the memory system points to,
and that has not been confirmed or updated in a long time.

This module does NOT delete or move files. Callers decide what to do.
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path
from typing import Optional

from .scan import is_excluded_mirror

_SKIP_DIRS = {"archive", "_sync", "versions", "_atlas", "anti", "gaps"}
_DEFAULT_MIN_AGE_DAYS = 180
_DEFAULT_MAX_HEALTH = 0.5


def _build_inbound_refs(memory_root: Path) -> dict[str, int]:
    """Return {file_stem -> inbound_ref_count} for all .md files in memory_root.

    An inbound ref is counted when any OTHER .memory/ file's body mentions the
    file's stem (case-insensitive whole-word match). This is a simple but
    effective heuristic -- true cross-file links ([[slug]]) and plain-text
    mentions both count.
    """
    all_files = [
        f for f in memory_root.rglob("*.md")
        if not any(p in _SKIP_DIRS for p in f.relative_to(memory_root).parts)
        and not is_excluded_mirror(f.relative_to(memory_root).parts)
        and not f.name.startswith("_")
    ]
    stems = {f.stem: f for f in all_files}
    inbound: dict[str, int] = {s: 0 for s in stems}

    # Split stems (>= 4 chars, to avoid spurious matches on short names) into a fast path and a
    # rare slow path. The OLD implementation ran one compiled regex per stem against EVERY file's
    # full text -- O(files x stems) full-content scans, which on the real corpus took minutes (and
    # timed out the weekly curation task). Instead, tokenize each file ONCE into its set of
    # [A-Za-z0-9_-] words and set-intersect with the stem tokens: O(total_content), same result.
    #
    # A stem matches a file exactly when it appears as a maximal [A-Za-z0-9_-] token -- precisely
    # what the old boundary regex (?<![A-Za-z0-9_-])stem(?![A-Za-z0-9_-]) means -- so token-set
    # membership is equivalent for stems that are themselves all [A-Za-z0-9_-]. The (rare) stems
    # containing other characters keep the exact boundary-regex path.
    _alnum = re.compile(r"^[A-Za-z0-9_-]+$")
    token_re = re.compile(r"[A-Za-z0-9_-]+")
    fast_stems: dict[str, list[str]] = {}     # lowercased token -> original stem(s) (handles case collisions)
    gram_stems: dict[tuple[str, ...], list[str]] = {}  # lowercased token tuple -> stems with spaces/punctuation
    slow_patterns: dict[str, re.Pattern] = {}  # stems with no Latin token at all (CJK names): one regex each
    for stem in stems:
        if len(stem) < 4:
            continue
        if _alnum.match(stem):
            fast_stems.setdefault(stem.lower(), []).append(stem)
            continue
        grams = tuple(t.lower() for t in token_re.findall(stem))
        # only a stem made of tokens and spaces is its token n-gram; `会議 notes` or `Café notes`
        # matched every "notes" (MN25), so anything else keeps the exact regex
        if 1 <= len(grams) <= 12 and re.fullmatch(r"[A-Za-z0-9_\- ]+", stem):
            # `Meeting Notes` is the token pair (meeting, notes): matched as an adjacent n-gram of the
            # file's tokens, one pass per file. The per-stem regex made doctor take 8 minutes on a
            # 3,000-note Obsidian vault, every stem having a space (AG35, sweep 14).
            gram_stems.setdefault(grams, []).append(stem)
        else:
            try:
                slow_patterns[stem] = re.compile(
                    r"(?<![a-zA-Z0-9_-])" + re.escape(stem) + r"(?![a-zA-Z0-9_-])",
                    re.IGNORECASE,
                )
            except re.error:
                pass

    fast_token_set = set(fast_stems)
    gram_lengths = sorted({len(g) for g in gram_stems})

    for md_file in all_files:
        try:
            content = md_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        this_stem = md_file.stem
        toks = [t.lower() for t in token_re.findall(content)]
        tokens = set(toks)
        for tok in tokens & fast_token_set:
            for stem in fast_stems[tok]:
                if stem != this_stem:
                    inbound[stem] += 1
        if gram_stems:
            seen_grams: set[tuple[str, ...]] = set()
            for n in gram_lengths:
                if n == 1:
                    seen_grams.update((t,) for t in tokens)
                    continue
                for i in range(len(toks) - n + 1):
                    g = tuple(toks[i:i + n])
                    if g in gram_stems:
                        seen_grams.add(g)
            for g in seen_grams:
                for stem in gram_stems.get(g, ()):
                    if stem != this_stem:
                        inbound[stem] += 1
        for stem, pattern in slow_patterns.items():
            if stem == this_stem:
                continue
            if pattern.search(content):
                inbound[stem] += 1

    return inbound


def _file_age_days(path: Path, frontmatter: dict, today: date) -> Optional[int]:
    """Return file age in days, using updated: frontmatter or file mtime fallback."""
    updated = frontmatter.get("updated")
    if updated:
        try:
            upd_date = date.fromisoformat(str(updated)[:10])
            return (today - upd_date).days
        except (ValueError, AttributeError):
            pass
    try:
        mtime_date = date.fromtimestamp(path.stat().st_mtime)
        return (today - mtime_date).days
    except OSError:
        return None


def find_orphan_candidates(
    memory_root: Path,
    scope: str = "",
    min_age_days: int = _DEFAULT_MIN_AGE_DAYS,
    max_health: float = _DEFAULT_MAX_HEALTH,
    max_inbound_refs: int = 0,
    limit: int = 200,
) -> list[dict]:
    """Find .memory/ files that are likely orphans.

    A file is included when ALL conditions hold:
      - inbound_refs <= max_inbound_refs (default 0 = completely unreferenced)
      - health_score  <= max_health      (default 0.5 = poor metadata)
      - age_days      >= min_age_days    (default 180 = not updated recently)

    Args:
        memory_root:      Root of .memory/ corpus.
        scope:            Optional subdirectory to restrict the scan (e.g. "observations").
        min_age_days:     Minimum age in days (uses updated: or file mtime).
        max_health:       Maximum health score to include (0=all four missing, 1=fully healthy).
        max_inbound_refs: Maximum number of inbound references to qualify as orphan.
        limit:            Maximum results to return.

    Returns:
        list[dict] sorted by orphan_score DESC (most orphan-like first):
          path          -- relative path from memory_root
          title         -- file title from frontmatter (fallback: stem)
          score         -- health score 0-1 (lower = worse metadata)
          inbound_refs  -- count of other memory files that mention this file
          age_days      -- days since updated: or mtime
          issues        -- list of health issue strings
          orphan_score  -- composite: (1 - health_score) + age_factor + (1 if no_refs)
    """
    from .health import compute_file_health

    today = date.today()
    scan_root = memory_root / scope if scope else memory_root

    # Build inbound reference map for the full memory root (not just scope)
    inbound_map = _build_inbound_refs(memory_root)

    results = []

    for md_file in sorted(scan_root.rglob("*.md")):
        rel_parts = md_file.relative_to(memory_root).parts
        if any(p in _SKIP_DIRS for p in rel_parts):
            continue
        if is_excluded_mirror(rel_parts):
            continue
        if md_file.name.startswith("_"):
            continue

        # One read and one YAML parse per file: compute_file_health already returns the
        # frontmatter (the second parse doubled doctor's orphan scan, AG11 sweep 10).
        health = compute_file_health(md_file, today)
        fm: dict = health.get("frontmatter") or {}
        h_score = health["score"]
        stem = md_file.stem
        inbound = inbound_map.get(stem, 0)
        age = _file_age_days(md_file, fm, today)

        # Apply filters
        if h_score > max_health:
            continue
        if inbound > max_inbound_refs:
            continue
        if age is None or age < min_age_days:
            continue

        # Composite orphan score (higher = more orphan-like)
        age_factor = min(1.0, age / 730)  # saturates at ~2 years
        no_ref_bonus = 1.0 if inbound == 0 else 0.0
        orphan_score = round((1.0 - h_score) + age_factor + no_ref_bonus, 3)

        rel = str(md_file.relative_to(memory_root)).replace("\\", "/")
        results.append({
            "path": rel,
            "title": str(fm.get("title", md_file.stem))[:60],
            "score": h_score,
            "inbound_refs": inbound,
            "age_days": age,
            "issues": health["issues"],
            "orphan_score": orphan_score,
        })

        if len(results) >= limit:
            break

    results.sort(key=lambda r: r["orphan_score"], reverse=True)
    return results
