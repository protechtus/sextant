"""P6-T4: project_state MCP tool — combined git + manifest + compose snapshot."""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Optional

from atlas_plus.project_state.compose import compose_state
from atlas_plus.project_state.git import git_state
from atlas_plus.project_state.manifest import manifest_state
from atlas_plus.tickets.store import list_tickets
from ..config import safe_resolve_within
from atlas_plus import paths as atlas_paths

_CACHE: dict[str, tuple[float, dict]] = {}
_CACHE_TTL = 30.0

_PROJECTS_ROOT = atlas_paths.projects_root()


def _resolve(name: Optional[str]) -> tuple[Path, str]:
    """Resolve a project name or path to (directory, display_name)."""
    if name:
        p = Path(name)
        if p.is_absolute():
            # Absolute paths are only allowed if they are under ATLAS_PROJECTS_ROOT.
            # Rejecting arbitrary absolute paths prevents a git-hook execution chain:
            # write malicious .git/config via memory_remember → project_state(path) → git exec
            # Resolve BEFORE the containment check (audit 2026-11-09, row 16):
            # a lexical check let "<root>/../elsewhere" through.
            p = p.resolve()
            try:
                p.relative_to(_PROJECTS_ROOT.resolve())
            except ValueError:
                raise ValueError(
                    f"Absolute path {p!r} is not under PROJECTS_ROOT ({_PROJECTS_ROOT}). "
                    "Use a project name instead."
                )
            if p.is_dir():
                return p, p.name
            raise ValueError(f"Path does not exist: {p}")

        # Name relative to ATLAS_PROJECTS_ROOT -- confined (audit HIGH-2): the relative-name
        # branch previously let project_state("..") resolve to the workspace root and run git there.
        candidate = safe_resolve_within(_PROJECTS_ROOT, name, exclude=set())
        if candidate is not None and candidate.is_dir():
            return candidate, name

        # Try exact match ignoring case (children are real dirs under the root -- safe)
        if _PROJECTS_ROOT.is_dir():
            for child in _PROJECTS_ROOT.iterdir():
                if child.is_dir() and child.name.lower() == name.lower():
                    return child, child.name

        raise ValueError(f"Project '{name}' not found.")

    # No name given — walk up from cwd to find a git root
    cwd = Path.cwd()
    check = cwd
    while check != check.parent:
        if (check / ".git").exists():
            return check, check.name
        check = check.parent
    return cwd, cwd.name


def project_state(name: Optional[str] = None) -> dict:
    """Return a combined project state snapshot (git + deps + compose).

    Results are cached for 30 seconds to keep repeated calls cheap.
    """
    try:
        project_dir, display_name = _resolve(name)
    except ValueError as exc:
        return {"error": str(exc)}

    cache_key = str(project_dir)
    now = time.monotonic()
    if cache_key in _CACHE:
        ts, cached = _CACHE[cache_key]
        if now - ts < _CACHE_TTL:
            return {**cached, "cached": True}

    open_tickets = [
        t.as_dict() for t in list_tickets(project=display_name)
        if t.status in ("todo", "in-progress", "blocked")
    ]

    snapshot = {
        "project": display_name,
        "path": str(project_dir),
        "git": git_state(project_dir),
        "manifest": manifest_state(project_dir),
        "compose": compose_state(project_dir),
        "open_tickets": open_tickets,
        "cached": False,
    }
    _CACHE[cache_key] = (now, snapshot)
    return snapshot
