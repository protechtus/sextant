"""P7-T1: Duplicate detection across .memory/ using word-overlap Jaccard similarity."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from atlas_plus.mcp.memory_server.config import EXCLUDED_DIRS, MEMORY_ROOT
from atlas_plus.curation.scan import is_excluded_mirror

DEFAULT_THRESHOLD = 0.40

# Duplicate detection targets the curatable FACT scopes (feedback, identity,
# projects, reference, decisions). Tickets, sessions, personas, visitors and
# archives are structured or historical records that legitimately share
# boilerplate -- including them swamps the report with false positives (e.g.
# ~1800 near-identical archived tickets = ~1.5M bogus pairs). This mirrors the
# non-curatable set already used by dream_mode.
_NON_CURATABLE_DIRS = EXCLUDED_DIRS | {
    "_atlas", "_archive", "sessions", "tickets", "personas", "visitors", "gaps",
}


def _tokenize(text: str) -> set[str]:
    """Lowercase word tokens, stripping punctuation and YAML frontmatter."""
    # Strip frontmatter
    if text.startswith("---"):
        end = text.find("---", 3)
        if end != -1:
            text = text[end + 3:]
    words = re.findall(r"[a-z0-9]+", text.lower())
    # Drop very common stop words and single chars
    stops = {
        "the", "a", "an", "and", "or", "of", "to", "in", "is", "it",
        "that", "this", "for", "be", "with", "on", "at", "by", "from",
        "not", "are", "was", "were", "as", "if", "but", "all", "any",
        "has", "have", "had", "do", "does", "did", "will", "would",
        "can", "could", "should", "may", "might", "must", "shall",
    }
    return {w for w in words if len(w) > 2 and w not in stops}


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _snippet(path: Path, max_chars: int = 120) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
        # strip frontmatter
        if text.startswith("---"):
            end = text.find("---", 3)
            if end != -1:
                text = text[end + 3:]
        return text.strip()[:max_chars].replace("\n", " ")
    except OSError:
        return ""


@dataclass
class DuplicatePair:
    """A pair of memory files with overlapping content above the similarity threshold."""

    file1: str
    file2: str
    similarity: float
    snippet1: str
    snippet2: str

    def as_dict(self) -> dict:
        """Return a JSON-serialisable dict including rounded similarity and content snippets."""
        return {
            "file1": self.file1,
            "file2": self.file2,
            "similarity": round(self.similarity, 3),
            "snippet1": self.snippet1,
            "snippet2": self.snippet2,
        }


def find_duplicates(
    memory_root: Optional[Path] = None,
    threshold: float = DEFAULT_THRESHOLD,
    scope: str = "",
) -> list[DuplicatePair]:
    """Return pairs of .memory/ files whose content is suspiciously similar.

    Args:
        memory_root: Root of the .memory/ corpus. Defaults to MEMORY_ROOT.
        threshold: Jaccard similarity floor (0-1). Default 0.40.
        scope: Optional subdirectory to restrict search (e.g. 'feedback').

    Returns list of DuplicatePair sorted by similarity descending.
    """
    root = memory_root or MEMORY_ROOT
    search_root = root / scope if scope else root

    files = [
        p for p in search_root.rglob("*.md")
        if not any(part in _NON_CURATABLE_DIRS for part in p.relative_to(root).parts)
        and not is_excluded_mirror(p.relative_to(root).parts)
        and not p.name.startswith("_")
    ]

    tokens: dict[Path, set[str]] = {}
    for f in files:
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
            tokens[f] = _tokenize(text)
        except OSError:
            tokens[f] = set()

    pairs: list[DuplicatePair] = []
    # Size-band prune: Jaccard(A,B) <= min(|A|,|B|) / max(|A|,|B|), so two token
    # sets whose sizes differ by more than `threshold` can never reach it. Sort
    # ascending by size and break the inner scan as soon as the ratio drops below
    # the threshold -- every later file is only larger. This keeps results EXACT
    # (no pair that could qualify is skipped) while avoiding the full O(n^2) walk.
    file_list = sorted(tokens.keys(), key=lambda f: len(tokens[f]))
    sizes = [len(tokens[f]) for f in file_list]
    for i in range(len(file_list)):
        si = sizes[i]
        if si == 0:
            continue
        f1 = file_list[i]
        toks1 = tokens[f1]
        for j in range(i + 1, len(file_list)):
            sj = sizes[j]
            if si < threshold * sj:
                break  # sj only grows from here; none can reach the threshold
            sim = _jaccard(toks1, tokens[file_list[j]])
            if sim >= threshold:
                f2 = file_list[j]
                pairs.append(DuplicatePair(
                    file1=str(f1.relative_to(root)).replace("\\", "/"),
                    file2=str(f2.relative_to(root)).replace("\\", "/"),
                    similarity=sim,
                    snippet1=_snippet(f1),
                    snippet2=_snippet(f2),
                ))

    pairs.sort(key=lambda p: p.similarity, reverse=True)
    return pairs
