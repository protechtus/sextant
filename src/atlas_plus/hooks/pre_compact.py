"""P9-T2: PreCompact hook — save session state before context compaction.

Fires before both /compact and auto-compact. Writes a snapshot to
.atlas/session-state.json so SessionStart (on resume) can re-inject
the key context that would otherwise be lost.

Never blocks — exits 0 even on failure.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from atlas_plus import paths as atlas_paths


def _under_projects(path: Path) -> bool:
    """Resolved containment (a string prefix made projects-old/ count as under projects/)."""
    try:
        return path.resolve().is_relative_to(Path(PROJECTS_ROOT).resolve())
    except (OSError, ValueError):
        return False


def _git_out(args, cwd, timeout=10) -> str:
    """Hardened git read: repo config may not execute anything (round-B 2026-11-09, N-B)."""
    from atlas_plus import gitsafe
    return subprocess.check_output(gitsafe.argv(args, cwd, timeout=timeout), cwd=str(cwd),
                                   stderr=subprocess.DEVNULL, text=True, timeout=timeout, env=gitsafe._env())

STATE_DIR = atlas_paths.state_dir()
PROJECTS_ROOT = atlas_paths.projects_root()
SESSION_STATE_FILE = STATE_DIR / "session-state.json"


def _detect_project() -> tuple[str, Path] | None:
    cwd = Path.cwd()
    check = cwd
    while check != check.parent:
        if (check / ".git").exists() and _under_projects(check):
            return check.name, check
        check = check.parent
    return None


def _git_oneliner(project_dir: Path) -> str:
    try:
        branch = _git_out(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"], project_dir).strip()
        dirty = _git_out(
            ["git", "status", "--porcelain"], project_dir).strip()
        log = _git_out(
            ["git", "log", "--oneline", "-1", "--no-decorate"], project_dir).strip()
        return f"{branch} | {'dirty' if dirty else 'clean'} | last: {log}"
    except Exception:
        return "(git unavailable)"


def main() -> None:
    """Claude Code pre-compact hook: snapshot session state before the context window is compressed."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)

    state: dict = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "project": None,
        "git": None,
        "summary": "",
    }

    project_info = _detect_project()
    if project_info:
        name, project_dir = project_info
        state["project"] = name
        state["git"] = _git_oneliner(project_dir)
        state["summary"] = (
            f"At compaction: working in '{name}'. "
            f"Git: {state['git']}. "
            f"Resume from where you left off — check project state and open tickets."
        )

    SESSION_STATE_FILE.write_text(
        json.dumps(state, indent=2), encoding="utf-8"
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"[atlas-pre-compact] error: {e}", file=sys.stderr)
        sys.exit(0)
