"""Anti-memory -- explicit do-not-resuggest store (ATL-R04).

Writes rejection records to .memory/anti/<scope>.md so future agents
know not to re-propose rejected ideas.

Propagation: when a rejection is recorded, existing .memory/ files whose
body contains keywords from the reject_pattern get flagged with an
`anti_flagged` frontmatter field so agents see the conflict immediately.
"""

from __future__ import annotations

import os
import re
from datetime import date
from pathlib import Path
from typing import Optional

import yaml
from atlas_plus import paths as atlas_paths
from atlas_plus.mcp.memory_server.routing import _atomic_write_text, _write_lock
from atlas_plus.mcp.memory_server.versioning import version_before_write


def _search_dirty() -> None:
    """Void the FTS freshness window after a direct corpus write (expert
    pass 2026-10-05, M1). Best-effort -- never fail a write over search."""
    try:
        from atlas_plus.mcp.memory_server.fts import mark_dirty
        mark_dirty()
    except Exception:  # pragma: no cover
        pass

_MEMORY_ROOT = atlas_paths.memory_root()
ANTI_ROOT = _MEMORY_ROOT / "anti"

VALID_APPLIES_TO = {"workspace", "project"}

# Corpus directories propagation must never rewrite: history copies,
# archives, sync staging and private config (audit 2026-11-09, row 6).
_PROPAGATION_SKIP_DIRS = {"versions", "archive", "_sync", "_atlas", "anti", ".git", ".obsidian", ".trash"}


def _skip_propagation(rel: str) -> bool:
    """Any ancestor that is a skip dir (case-insensitive, Windows) or dot-prefixed."""
    return any(part.casefold() in _PROPAGATION_SKIP_DIRS or part.startswith(".")
               for part in rel.split("/")[:-1])

_WINDOWS_DEVICE_NAMES = {
    "con", "prn", "aux", "nul",
    *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10)),
}


def _validate_scope(scope: str) -> str:
    """Normalise and confine an anti-memory scope slug.

    Mirrors routing.resolve_target's scope-parameter rules (audit 2026-11-09,
    row 5): no path separators, no '..', no ':' (drive or NTFS stream), no
    absolute path, no NUL, no leading dot, no Windows device name.
    Raises ValueError.
    """
    anti_scope = scope.strip().lower().replace(" ", "-")
    # The write grammar (`project:<name>`, `reference:<topic>`, `preference:<topic>`) is accepted
    # as a category name: the part after the colon (persona rerun 2026-11-10).
    for kind in ("project:", "reference:", "preference:", "identity:"):
        if anti_scope.startswith(kind) and len(anti_scope) > len(kind):
            anti_scope = anti_scope[len(kind):]
            break
    if (
        not anti_scope
        or any(c in anti_scope for c in ("/", "\\", "..", ":", "\x00"))
        or os.path.isabs(anti_scope)
        or anti_scope.startswith(".")
    ):
        raise ValueError(
            f"Invalid scope {scope!r}: must not contain path separators, ':' or '..', "
            "and must not start with '.'."
        )
    if anti_scope.split(".")[0] in _WINDOWS_DEVICE_NAMES:
        raise ValueError(f"Invalid scope {scope!r}: reserved device name on Windows.")
    return anti_scope


def _yaml_single_quoted(text: str, limit: int = 120) -> str:
    """Render ``text`` as a one-line YAML single-quoted scalar.

    Newlines and other control characters collapse to spaces, so the value
    can never open a new frontmatter key, and single quotes are doubled (the
    only escape single-quoted YAML has). Backslashes stay literal.
    """
    flat = "".join(c if c.isprintable() else " " for c in text)
    flat = " ".join(flat.split())[:limit]
    return "'" + flat.replace("'", "''") + "'"


# Minimum keyword length to use in propagation search (avoid noise from stopwords)
_MIN_KEYWORD_LEN = 4
# Minimum number of keywords that must match for propagation to flag a file
_PROPAGATION_MIN_MATCHES = 2


def _extract_keywords(text: str) -> list[str]:
    """Extract meaningful lowercase words from text for propagation matching."""
    stopwords = {
        "the", "and", "for", "with", "that", "this", "are", "have", "from",
        "should", "never", "always", "not", "use", "used", "using", "will",
        "been", "into", "over", "when", "what", "which", "such", "more",
        "than", "also", "any", "all", "its", "our", "your",
    }
    words = re.findall(r"[a-zA-Z][a-zA-Z0-9_-]*", text.lower())
    return [w for w in words if len(w) >= _MIN_KEYWORD_LEN and w not in stopwords]


def _flag_frontmatter(raw: str, anti_scope: str, reject_pattern: str) -> Optional[str]:
    """Add anti_flagged and anti_reason to file frontmatter. Returns new content or None if unchanged."""
    if not raw.startswith("---"):
        return None
    end = raw.find("\n---", 3)
    if end == -1:
        return None

    fm_block = raw[3:end]
    # Already flagged by this scope -- skip
    if f"anti_{anti_scope}" in fm_block or "anti_flagged: true" in fm_block:
        return None

    lines = fm_block.splitlines()
    # Append anti fields before closing ---. The reason is a single-quoted
    # scalar: the old double-quoted form turned a pattern containing a
    # Windows path into an invalid YAML escape and broke the whole
    # frontmatter (audit 2026-11-09, row 6).
    lines.append("anti_flagged: true")
    lines.append(f"anti_reason: {_yaml_single_quoted(reject_pattern)}")

    return "---" + "\n".join(lines) + "\n---" + raw[end + 4:]


def propagate_anti_rejection(
    reject_pattern: str,
    anti_scope: str,
    memory_root: Optional[Path] = None,
    applies_to: str = "workspace",
    project: str = "",
) -> dict:
    """Search existing .memory/ files for content matching the rejection and flag them.

    Args:
        reject_pattern: The rejection text to propagate.
        anti_scope:     The anti-memory scope slug (used to avoid re-flagging).
        memory_root:    Override for testing; defaults to _MEMORY_ROOT.
        applies_to:     'workspace' or 'project'.
        project:        Project name for project-scoped propagation.

    Returns {flagged: int, skipped: int, paths: list[str]}.
    """
    root = memory_root or _MEMORY_ROOT
    if not root.exists():
        return {"flagged": 0, "skipped": 0, "paths": []}

    keywords = _extract_keywords(reject_pattern)
    if len(keywords) < 2:
        # Pattern too short/generic to propagate meaningfully
        return {"flagged": 0, "skipped": 0, "paths": []}

    flagged_paths: list[str] = []
    skipped = 0

    for md_file in root.rglob("*.md"):
        # Skip anti-memory files themselves and private _atlas config
        rel = str(md_file.relative_to(root)).replace("\\", "/")
        if _skip_propagation(rel):
            continue
        # Project-scoped: only flag files under the project directory
        if applies_to == "project" and project:
            # Ordinary project facts are a FILE (projects/<slug>.md); a project may also
            # own a directory (wave-2 2026-11-09, DATA-07: the directory-only predicate
            # flagged nothing for a matching project fact).
            slug = project.strip().lower().replace(" ", "-")  # same slug routing.resolve_target makes
            if not (rel.startswith(f"projects/{slug}/") or rel == f"projects/{slug}.md"):
                continue

        try:
            raw = md_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        # Count keyword matches in the body (after frontmatter)
        body_start = 0
        if raw.startswith("---"):
            fm_end = raw.find("\n---", 3)
            if fm_end != -1:
                body_start = fm_end + 4
        body = raw[body_start:].lower()

        matches = sum(1 for kw in keywords if kw in body)
        if matches < _PROPAGATION_MIN_MATCHES:
            continue

        if _flag_frontmatter(raw, anti_scope, reject_pattern) is None:
            skipped += 1
            continue

        # Re-read and rewrite under the cross-process lock, with a version
        # copy first, through the atomic writer (audit 2026-11-09, row 6).
        try:
            with _write_lock(md_file):
                current = md_file.read_text(encoding="utf-8", errors="replace")
                updated = _flag_frontmatter(current, anti_scope, reject_pattern)
                if updated is not None:
                    version_before_write(md_file, root=root)
                    _atomic_write_text(md_file, updated)
            if updated is None:
                skipped += 1
                continue
            _search_dirty()
            flagged_paths.append(str(md_file))
        except (OSError, RuntimeError):
            skipped += 1

    return {"flagged": len(flagged_paths), "skipped": skipped, "paths": flagged_paths}


def anti_remember(
    scope: str,
    reject_pattern: str,
    applies_to: str = "workspace",
    project: str = "",
    interactive: bool = False,
) -> dict:
    """Record a rejection so agents stop re-proposing it.

    Also propagates the rejection to any existing .memory/ files that
    contain matching content, flagging them with `anti_flagged: true`.

    Args:
        scope:          Slug for the category (e.g. 'libraries', 'approaches').
        reject_pattern: What to never suggest again (1-2 sentences, plain text).
        applies_to:     'workspace' (all projects) or 'project' (project-scoped).
        project:        Required when applies_to='project'.

    Returns {ok, file, error, propagated}.
    """
    from atlas_plus.autonomous import require_write
    try:
        require_write("anti_remember", interactive=interactive)
    except RuntimeError as e:
        return {"ok": False, "file": None, "error": str(e)}

    if not scope or not scope.strip():
        return {"ok": False, "file": None, "error": "scope must not be empty"}
    if not reject_pattern or not reject_pattern.strip():
        return {"ok": False, "file": None, "error": "reject_pattern must not be empty"}
    if applies_to not in VALID_APPLIES_TO:
        return {"ok": False, "file": None, "error": f"applies_to must be one of {VALID_APPLIES_TO}"}
    if applies_to == "project" and not project:
        return {"ok": False, "file": None, "error": "project is required when applies_to='project'"}

    try:
        anti_scope = _validate_scope(scope)
    except ValueError as e:
        return {"ok": False, "file": None, "error": str(e)}
    ANTI_ROOT.mkdir(parents=True, exist_ok=True)
    target = ANTI_ROOT / f"{anti_scope}.md"
    # Belt and braces: the resolved target must sit directly in ANTI_ROOT.
    try:
        confined = target.resolve().parent == ANTI_ROOT.resolve()
    except (OSError, ValueError):
        confined = False
    if not confined:
        return {"ok": False, "file": None,
                "error": f"Invalid scope {scope!r}: escapes the anti-memory directory."}

    entry = {
        "reject_pattern": reject_pattern.strip(),
        "applies_to": applies_to,
        "added": date.today().isoformat(),
    }
    if project:
        entry["project"] = project

    already_recorded = False
    # Check-then-append under the cross-process lock, landed atomically
    # (audit 2026-11-09, row 5).
    try:
        with _write_lock(target):
            if target.exists():
                existing = target.read_text(encoding="utf-8", errors="replace")
                # Check for duplicate
                if reject_pattern.strip().lower() in existing.lower():
                    already_recorded = True
                else:
                    # Append new entry
                    new_block = "\n---\n" + yaml.safe_dump(entry, default_flow_style=False).strip()
                    _atomic_write_text(target, existing + new_block)
            else:
                header = {
                    "id": f"anti-{anti_scope}",
                    "title": f"Anti-memory: {scope}",
                    "type": "anti",
                    "status": "active",
                }
                header_text = yaml.safe_dump(header, default_flow_style=False).strip()
                first_entry = yaml.safe_dump(entry, default_flow_style=False).strip()
                _atomic_write_text(
                    target,
                    f"---\n{header_text}\n---\n\n# Do not suggest\n\n---\n{first_entry}\n",
                )
    except (OSError, RuntimeError) as e:
        return {"ok": False, "file": None, "error": f"write failed: {e}"}
    if not already_recorded:
        _search_dirty()

    if already_recorded:
        return {"ok": True, "file": str(target), "error": None, "note": "already recorded", "propagated": 0}

    # Propagate: flag existing matching memories
    prop = propagate_anti_rejection(
        reject_pattern=reject_pattern.strip(),
        anti_scope=anti_scope,
        applies_to=applies_to,
        project=project,
    )

    return {
        "ok": True,
        "file": str(target),
        "error": None,
        "propagated": prop["flagged"],
    }


def list_anti(applies_to: str = "", project: str = "") -> list[dict]:
    """Return all anti-memory entries, optionally filtered."""
    if not ANTI_ROOT.exists():
        return []
    results = []
    for md_file in sorted(ANTI_ROOT.glob("*.md")):
        text = md_file.read_text(encoding="utf-8", errors="replace")
        # Each entry is a YAML block delimited by ---
        blocks = [b.strip() for b in text.split("---") if b.strip()]
        for block in blocks:
            try:
                entry = yaml.safe_load(block)
                if not isinstance(entry, dict) or "reject_pattern" not in entry:
                    continue
                if applies_to and entry.get("applies_to") != applies_to:
                    continue
                if project and entry.get("project") and entry.get("project") != project:
                    continue
                results.append({
                    "scope": md_file.stem,
                    "reject_pattern": entry.get("reject_pattern", ""),
                    "applies_to": entry.get("applies_to", "workspace"),
                    "project": entry.get("project", ""),
                    "added": entry.get("added", ""),
                })
            except Exception:
                pass
    return results
