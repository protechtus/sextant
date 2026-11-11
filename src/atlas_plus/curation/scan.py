"""Shared scan-exclusion predicate for memory curation walks.

Some subtrees under ``MEMORY_ROOT`` are MIRRORS of external corpora, not curatable
Atlas memory. Walking them makes every curation scan (orphan inbound-reference maps,
per-file tag suggestion, decay frontmatter parsing) pathologically slow -- the
``reference/research`` mirror of the standalone research library
(a separate private repo) is thousands of files, and one such scan
made a single web test take 96 minutes (and would slow the live Helm
``/api/memory/next-actions`` panel + the Brain's nightly curation lanes the same way).

Curation should skip these mirrors everywhere it walks ``MEMORY_ROOT``. Each scan site
keeps its own (intentionally different) top-level skip set; this predicate adds the
mirror exclusion on top, in one place, by PATH PREFIX so it never over-matches a
legitimately-curatable file that merely shares a name component (e.g. a real
``reference/research-methodology.md`` is kept; only the ``reference/research/`` subtree
is skipped).
"""
from __future__ import annotations

from typing import Iterable, Sequence

# Path prefixes (relative to MEMORY_ROOT, as tuple-of-parts) that are mirrors of external
# corpora rather than curatable memory. Extend this tuple if more mirrors are added.
_MIRROR_PREFIXES: tuple[tuple[str, ...], ...] = (
    ("reference", "research"),
)


def is_excluded_mirror(rel_parts: Iterable[str]) -> bool:
    """True if a path (its parts RELATIVE TO MEMORY_ROOT) sits inside a non-curatable mirror.

    Matches by prefix, so ``reference/research/foo/bar.md`` is excluded but
    ``reference/research-methodology.md`` and ``reference/glossary.md`` are kept.
    """
    rp: Sequence[str] = tuple(rel_parts)
    return any(tuple(rp[: len(prefix)]) == prefix for prefix in _MIRROR_PREFIXES)
