"""Re-derivable memory detector -- find facts that duplicate project docs.

A re-derivable fact is memory that could be obtained by reading a source
file (README, AGENTS.md, CLAUDE.md, code files) instead of .memory/.
These are candidates for archival: they add maintenance burden without
adding unique value, because the authoritative source already holds the
fact.

Detection strategy:
  - For each .memory/ file compute word-overlap (Jaccard) against all
    indexed doc files (markdown, py, txt) in the workspace.
  - If max overlap >= threshold the file is flagged as potentially
    re-derivable.
  - Never auto-archive; results are surfaced for human review only.
"""

from __future__ import annotations

import fnmatch
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from atlas_plus.mcp.memory_server.config import MEMORY_ROOT

from .scan import is_excluded_mirror
from atlas_plus import paths as atlas_paths

_WORKSPACE_ROOT = atlas_paths.workspace_root()

_DEFAULT_THRESHOLD = 0.30  # Jaccard similarity to flag

# Doc file patterns to compare against
_DOC_GLOBS = [
    "*.md",
    "*.txt",
    "projects/**/*.md",
    "projects/atlas-memory+/docs/*.md",
    "projects/atlas-memory+/AGENTS.md",
    "projects/atlas-memory+/QUICKSTART.md",
    "projects/atlas-memory+/README.md",
]

_EXCLUDED_MEMORY = {"archive", "__pycache__", ".git", "_inbox", "versions", "sessions"}

# Workspace directories never worth indexing as "project docs" -- vendored deps, version
# mirrors, build artifacts. Pruning them from the doc walk is the difference between scanning
# project READMEs/docs and recursing into every project's multi-thousand-file .venv.
_EXCLUDED_DOC_DIRS = {
    ".venv", "venv", "node_modules", "site-packages", "versions", "__pycache__",
    ".git", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".tox", "dist", "build",
    "archive", "_atlas", "_sync",
}


from atlas_plus._yaml import fast_safe_load as _fast_yaml_load


@dataclass
class RederiveCandidate:
    """A memory file that overlaps heavily with a project doc and may be rederivable from source."""

    path: str           # relative to MEMORY_ROOT
    title: str
    memory_type: str
    matched_doc: str    # workspace-relative path of highest-overlap doc
    similarity: float   # Jaccard score 0-1
    sample_overlap: list[str]  # sample shared content words

    def as_dict(self) -> dict:
        """Return a JSON-serialisable dict with rounded similarity and top-5 overlap words."""
        return {
            "path": self.path,
            "title": self.title,
            "type": self.memory_type,
            "matched_doc": self.matched_doc,
            "similarity": round(self.similarity, 3),
            "sample_overlap": self.sample_overlap[:5],
        }


def _tokenize(text: str) -> set[str]:
    """Lower-case word tokens, strip punctuation, min length 4."""
    return {w for w in re.findall(r"[a-zA-Z0-9_\-]{4,}", text.lower())}


def _parse_frontmatter_body(path: Path) -> tuple[dict, str]:
    """Return (frontmatter dict, body text)."""
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}, ""
    if not raw.startswith("---"):
        return {}, raw
    end = raw.find("\n---", 3)
    if end == -1:
        return {}, raw
    import yaml
    try:
        fm = _fast_yaml_load(raw[3:end]) or {}
    except yaml.YAMLError:
        # One malformed-frontmatter file must not crash the whole curation scan.
        return {}, raw[end + 4:]
    if not isinstance(fm, dict):
        return {}, raw[end + 4:]
    return fm, raw[end + 4:]


def _iter_pattern_files(workspace_root: Path, pattern: str):
    """Yield workspace files matching a glob WITHOUT descending into vendored/heavy dirs.

    Bounded patterns (no ``**``) use pathlib glob directly -- they cannot run away. Recursive
    ``base/**/filepat`` patterns are walked with os.walk over just ``base``, pruning
    _EXCLUDED_DOC_DIRS (and dotdirs) in place so the walk never enters .venv / versions /
    node_modules. This is what stops ``projects/**/*.md`` from recursing into every project's
    site-packages -- the workspace-side half of the rederive slowness.
    """
    if "**" not in pattern:
        yield from workspace_root.glob(pattern)
        return
    head, _, tail = pattern.partition("**")
    base = workspace_root / head.strip("/\\") if head.strip("/\\") else workspace_root
    file_glob = tail.lstrip("/\\") or "*"
    if not base.exists():
        return
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [
            d for d in dirnames
            if d not in _EXCLUDED_DOC_DIRS and not d.startswith(".")
        ]
        for fn in filenames:
            if fnmatch.fnmatch(fn, file_glob):
                yield Path(dirpath) / fn


def _index_doc_files(
    workspace_root: Path,
    globs: list[str],
    max_files: int = 200,
) -> list[tuple[Path, set[str]]]:
    """Collect (path, token_set) for doc files in the workspace.

    Each glob is expanded via _iter_pattern_files, which prunes vendored/heavy dirs so a
    ``projects/**/*.md`` pattern never recurses into .venv / versions / node_modules.
    """
    seen: set[Path] = set()
    result: list[tuple[Path, set[str]]] = []
    for pattern in globs:
        for f in _iter_pattern_files(workspace_root, pattern):
            if f in seen or not f.is_file():
                continue
            seen.add(f)
            try:
                text = f.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            tokens = _tokenize(text)
            if len(tokens) < 20:
                continue
            result.append((f, tokens))
            if len(result) >= max_files:
                return result
    return result


def find_rederivable(
    threshold: float = _DEFAULT_THRESHOLD,
    scope: str = "",
    workspace_root: Optional[Path] = None,
) -> list[RederiveCandidate]:
    """Find .memory/ files that overlap heavily with project doc files.

    Args:
        threshold: Jaccard similarity floor to flag a file (0-1).
        scope: Subdirectory within MEMORY_ROOT to restrict scan.
        workspace_root: Override workspace root (mainly for testing).

    Returns list of RederiveCandidate sorted by similarity descending.
    """
    ws_root = workspace_root or _WORKSPACE_ROOT
    doc_index = _index_doc_files(ws_root, _DOC_GLOBS)
    if not doc_index:
        return []

    search_root = MEMORY_ROOT / scope if scope else MEMORY_ROOT
    if not search_root.exists():
        return []

    candidates: list[RederiveCandidate] = []

    for md_file in search_root.rglob("*.md"):
        parts = md_file.relative_to(MEMORY_ROOT).parts
        if any(p in _EXCLUDED_MEMORY for p in parts) or is_excluded_mirror(parts):
            continue

        fm, body = _parse_frontmatter_body(md_file)
        if not fm:
            continue

        # Honor explicit opt-out: rederivable: false in frontmatter
        if str(fm.get("rederivable", "true")).lower() == "false":
            continue

        # Constraints and decisions are authoritative -- skip
        mem_type = str(fm.get("type", "unknown"))
        if mem_type in ("constraint", "decision"):
            continue

        mem_tokens = _tokenize(body)
        if len(mem_tokens) < 15:
            continue

        best_sim = 0.0
        best_doc: Optional[Path] = None
        for doc_path, doc_tokens in doc_index:
            intersection = mem_tokens & doc_tokens
            union = mem_tokens | doc_tokens
            if not union:
                continue
            sim = len(intersection) / len(union)
            if sim > best_sim:
                best_sim = sim
                best_doc = doc_path
                best_overlap = list(intersection)

        if best_sim < threshold or best_doc is None:
            continue

        try:
            doc_rel = str(best_doc.relative_to(ws_root)).replace("\\", "/")
        except ValueError:
            doc_rel = str(best_doc)

        rel = str(md_file.relative_to(MEMORY_ROOT)).replace("\\", "/")
        candidates.append(RederiveCandidate(
            path=rel,
            title=str(fm.get("title", md_file.stem)),
            memory_type=mem_type,
            matched_doc=doc_rel,
            similarity=round(best_sim, 3),
            sample_overlap=sorted(best_overlap)[:5],
        ))

    candidates.sort(key=lambda c: c.similarity, reverse=True)
    return candidates
