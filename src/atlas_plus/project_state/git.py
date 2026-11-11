"""P6-T1: Git state reader for a project directory."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path


_GIT_TIMEOUT = 10  # seconds — prevents blocking on slow/broken repos

# Repo-local config must never execute code when we only READ state
# (data-safety audit 2026-11-09, row 16): a repo's own core.fsmonitor or
# hooks would otherwise run as us during `git status`. Empty fsmonitor
# disables it; hooksPath points at the null device (NUL on Windows).
# Hardening now lives in atlas_plus.gitsafe (one helper for every git call site; round-B
# re-audit 2026-11-09 found filters, external diff and post-commit still executing).
from atlas_plus import gitsafe

_GIT_HARDENING = gitsafe._BASE


def _harden(args: list[str], cwd: Path | str | None = None) -> list[str]:
    return gitsafe.argv(args, cwd, timeout=_GIT_TIMEOUT)


def _run(args: list[str], cwd: Path) -> tuple[str, int]:
    try:
        result = gitsafe.run(args, cwd, timeout=_GIT_TIMEOUT)
        return result.stdout.strip(), result.returncode
    except subprocess.TimeoutExpired:
        return "", 1


def git_state(project_dir: Path) -> dict:
    """Return git state snapshot for *project_dir*.

    Uses `git status --branch --porcelain=v2` to get branch + dirty +
    ahead/behind in a single subprocess call, then one more for the log.
    All calls time out after 10 seconds.
    """
    _, rc = _run(["git", "rev-parse", "--git-dir"], project_dir)
    if rc != 0:
        return {"is_git_repo": False}

    state: dict = {"is_git_repo": True}

    # Single call: branch + dirty + ahead/behind via porcelain v2
    status_out, _ = _run(
        ["git", "status", "--branch", "--porcelain=v2"],
        project_dir,
    )

    branch = "HEAD"
    ahead: int | None = None
    behind: int | None = None
    changed: list[str] = []
    untracked_count = 0

    for line in status_out.splitlines():
        if line.startswith("# branch.head "):
            branch = line.split(" ", 2)[2]
        elif line.startswith("# branch.ab "):
            parts = line.split()  # ['#', 'branch.ab', '+N', '-M']
            if len(parts) == 4:
                ahead = int(parts[2].lstrip("+"))
                behind = int(parts[3].lstrip("-"))
        elif line.startswith("1 ") or line.startswith("2 "):
            # tracked changed file: field 8 (0-indexed) is the path
            fields = line.split(" ", 8)
            if len(fields) >= 9:
                changed.append(fields[8])
        elif line.startswith("? "):
            changed.append(line[2:])
            untracked_count += 1

    state["branch"] = branch
    state["dirty"] = bool(changed)
    state["changed_files"] = changed
    state["untracked_count"] = untracked_count
    state["ahead"] = ahead
    state["behind"] = behind

    # Last 5 commits -- capped to avoid leaking sensitive commit messages to remote callers
    log_out, _ = _run(
        ["git", "log", "--oneline", "--no-decorate", "-5"],
        project_dir,
    )
    commits = []
    for line in log_out.splitlines():
        if line.strip():
            oid, _, msg = line.partition(" ")
            commits.append({"oid": oid, "message": msg})
    state["recent_commits"] = commits

    return state
