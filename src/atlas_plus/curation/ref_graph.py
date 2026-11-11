"""Memory reference graph -- adjacency list of cross-references between .memory/ files.

Builds a lightweight `{file_stem -> [referenced_stems]}` graph by scanning
Markdown link syntax in each file. Used by orphan detection (inbound refs),
dead-link detection, and hub analysis (most-referenced files).

Supported link patterns:
  [[slug]]          -- Obsidian-style wiki links
  [Title](path.md)  -- Markdown relative links (stem extracted, path ignored)

Short stems (< 4 chars) are excluded from the graph to avoid noise matches.
Archive, anti, gaps, and sync directories are skipped.
"""

from __future__ import annotations

import re
from pathlib import Path

from .scan import is_excluded_mirror

_SKIP_DIRS = {"archive", "_sync", "versions", "_atlas", "anti", "gaps"}
_MIN_STEM_LEN = 4

# [[wiki-link]] pattern
_WIKI_LINK_RE = re.compile(r"\[\[([^\]|#]+?)(?:\|[^\]]+)?\]\]")
# [Title](path.md) pattern -- captures the filename stem
_MD_LINK_RE = re.compile(r"\[[^\]]*\]\(([^)#]+?\.md)\)")


def build_reference_graph(memory_root: Path) -> dict[str, list[str]]:
    """Build a `{stem -> [referenced stems]}` adjacency list for .memory/ files.

    Returns:
        dict mapping each file's stem to a sorted list of stems it references.
        Only files that reference at least one other file appear in the dict.
        All memory file stems appear as keys in the inbound-ref count, even
        if they reference nothing (use get() with default []).

    Example:
        graph = build_reference_graph(MEMORY_ROOT)
        # Files that reference "sip-registration-flow":
        referrers = [stem for stem, refs in graph.items() if "sip-registration-flow" in refs]
    """
    all_files = [
        f for f in memory_root.rglob("*.md")
        if not any(p in _SKIP_DIRS for p in f.relative_to(memory_root).parts)
        and not is_excluded_mirror(f.relative_to(memory_root).parts)
        and not f.name.startswith("_")
    ]
    all_stems = {f.stem for f in all_files if len(f.stem) >= _MIN_STEM_LEN}

    graph: dict[str, list[str]] = {}
    for md_file in all_files:
        try:
            content = md_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        this_stem = md_file.stem
        refs: set[str] = set()

        # Wiki links [[stem]] or [[stem|alias]]
        for m in _WIKI_LINK_RE.finditer(content):
            target = m.group(1).strip()
            stem = Path(target).stem
            if stem != this_stem and stem in all_stems:
                refs.add(stem)

        # Markdown links [title](relative/path.md)
        for m in _MD_LINK_RE.finditer(content):
            stem = Path(m.group(1)).stem
            if stem != this_stem and stem in all_stems:
                refs.add(stem)

        if refs:
            graph[this_stem] = sorted(refs)

    return graph


def compute_inbound_counts(graph: dict[str, list[str]]) -> dict[str, int]:
    """Return `{stem -> inbound_reference_count}` from a reference graph.

    Files not referenced by anyone have count 0.
    """
    counts: dict[str, int] = {}
    for refs in graph.values():
        for target in refs:
            counts[target] = counts.get(target, 0) + 1
    return counts


def find_hub_files(
    graph: dict[str, list[str]],
    top_n: int = 10,
) -> list[dict]:
    """Return the top-N most-referenced memory files (hubs in the reference graph).

    Returns list of {stem, inbound_count} sorted by inbound_count descending.
    """
    inbound = compute_inbound_counts(graph)
    ranked = sorted(inbound.items(), key=lambda x: x[1], reverse=True)
    return [{"stem": stem, "inbound_count": count} for stem, count in ranked[:top_n]]
