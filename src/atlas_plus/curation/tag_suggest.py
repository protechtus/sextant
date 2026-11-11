"""Deterministic tag auto-suggestion for .memory/ files lacking tags: frontmatter.

Generates 2-4 tags per file based on three signals (priority order):
  1. Directory / scope: identity -> [identity], feedback -> [feedback], etc.
  2. Memory type field: type: project -> [project], type: reference -> [reference]
  3. Title key nouns: strip common prefixes, extract 4+ char non-stopwords

Tags are lowercase, hyphenated where multi-word, deduplicated and capped at 4.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Optional

# Words that carry no tag value
_STOPWORDS = {
    "the", "and", "for", "with", "that", "this", "are", "have", "from",
    "not", "use", "used", "will", "been", "into", "over", "when", "what",
    "which", "such", "more", "than", "also", "any", "all", "its", "our",
    "should", "always", "never", "can", "may", "must", "was", "how",
    "atlas", "memory", "notes", "draft", "misc", "research",
}

# Directory -> base tag mapping
_DIR_TAGS: dict[str, list[str]] = {
    "identity":     ["identity", "personal"],
    "feedback":     ["feedback", "workflow"],
    "projects":     ["project"],
    "reference":    ["reference"],
    "decisions":    ["decision"],
    "constraints":  ["constraint"],
    "observations": ["observation"],
    "sessions":     ["session"],
    "gaps":         ["gap", "missing-knowledge"],
    "runbooks":     ["runbook", "procedure"],
    "personas":     ["persona"],
    "tickets":      ["ticket"],
}

# type: field value -> tag
_TYPE_TAGS: dict[str, str] = {
    "identity":    "identity",
    "feedback":    "feedback",
    "project":     "project",
    "reference":   "reference",
    "decision":    "decision",
    "constraint":  "constraint",
    "observation": "observation",
    "session":     "session",
    "gap-stub":    "gap",
    "runbook":     "runbook",
    "persona":     "persona",
    "ticket":      "ticket",
}

_TITLE_STRIP_PREFIXES = ["research: ", "atlas: ", '"']


def _title_strip_prefixes() -> list[str]:
    """Static prefixes plus the operator's name (from ATLAS_USER_NAME), if set."""
    prefixes = list(_TITLE_STRIP_PREFIXES)
    name = os.environ.get("ATLAS_USER_NAME", "").strip().lower()
    if name:
        prefixes += [f"{name}: ", f"{name}'s "]
    return prefixes


def _slugify(word: str) -> str:
    """Convert a word to a lowercase tag slug."""
    return re.sub(r"[^a-z0-9-]", "", word.lower().replace(" ", "-").replace("_", "-"))


def _title_keywords(title: str, limit: int = 3) -> list[str]:
    """Extract up to `limit` meaningful keywords from a title for use as tags."""
    s = title
    for prefix in _title_strip_prefixes():
        if s.lower().startswith(prefix):
            s = s[len(prefix):]
    # Split into words, filter stopwords and short words
    words = re.findall(r"[a-zA-Z][a-zA-Z0-9+#-]*", s)
    keywords = []
    seen = set()
    for w in words:
        slug = _slugify(w)
        if len(slug) < 4 or slug in _STOPWORDS or slug in seen:
            continue
        seen.add(slug)
        keywords.append(slug)
        if len(keywords) >= limit:
            break
    return keywords


def suggest_tags_for_file(
    path: Path,
    memory_root: Optional[Path] = None,
    max_tags: int = 4,
) -> list[str]:
    """Return a list of suggested tags for a memory file.

    Combines directory-based tags, type-field tags, and title keywords.
    Returns at most max_tags distinct tags in priority order.
    Returns [] when the file cannot be read or already has tags.

    Does NOT write anything -- caller decides whether to apply suggestions.
    """
    import yaml as _yaml

    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []

    if not raw.startswith("---"):
        return []
    end = raw.find("\n---", 3)
    if end == -1:
        return []
    try:
        fm = _yaml.safe_load(raw[3:end]) or {}
    except Exception:
        return []

    # Already tagged -- skip
    existing = fm.get("tags") or fm.get("tag")
    if existing:
        return []

    suggested: list[str] = []
    seen: set[str] = set()

    def _add(tag: str) -> None:
        slug = _slugify(tag)
        if slug and slug not in seen and len(suggested) < max_tags:
            seen.add(slug)
            suggested.append(slug)

    # Signal 1: directory
    if memory_root:
        try:
            rel_parts = path.relative_to(memory_root).parts
            if rel_parts:
                dir_name = rel_parts[0]
                for t in _DIR_TAGS.get(dir_name, []):
                    _add(t)
        except ValueError:
            pass

    # Signal 2: type: field
    mem_type = str(fm.get("type", "")).lower()
    if mem_type and mem_type in _TYPE_TAGS:
        _add(_TYPE_TAGS[mem_type])

    # Signal 3: title keywords
    title = str(fm.get("title", path.stem))
    for kw in _title_keywords(title, limit=3):
        _add(kw)

    return suggested[:max_tags]


def inject_tags(path: Path, tags: list[str]) -> bool:
    """Write tags: [tag1, tag2, ...] into a file's frontmatter.

    Idempotent: if tags already exist, returns False without modifying the file.
    Returns True if the file was modified.
    """
    if not tags:
        return False
    # ATL-018 / audit HIGH-3 completion: gate this .memory/ frontmatter mutator (fail-closed
    # when ATLAS_AUTONOMOUS is off/unset).
    from atlas_plus.autonomous import require_autonomous_writes
    require_autonomous_writes("curation_tag_write")
    # Read-modify-write under the cross-process corpus lock, versioned, atomic: a plain
    # write_text here silently dropped a concurrent append_fact (round-B 2026-11-09, 17b).
    from atlas_plus.mcp.memory_server.routing import locked_atomic_rewrite
    from atlas_plus.mcp.memory_server.versioning import version_before_write
    changed = {"v": False}

    def _inject(raw: str) -> str:
        if not raw.startswith("---"):
            return raw
        end = raw.find("\n---", 3)
        if end == -1:
            return raw
        fm_block = raw[3:end]
        if "tags:" in fm_block or "tag:" in fm_block:
            return raw
        tag_line = f"tags: [{', '.join(tags)}]"
        new_fm_lines = fm_block.splitlines() + [tag_line]
        changed["v"] = True
        return "---" + "\n".join(new_fm_lines) + "\n---" + raw[end + 4:]

    try:
        if not path.exists():
            return False
        version_before_write(path)
        locked_atomic_rewrite(path, _inject)
        return changed["v"]
    except OSError:
        return False
