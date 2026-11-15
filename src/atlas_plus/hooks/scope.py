"""ATLAS_HOOK_SCOPE: which sessions this install's Claude Code hooks act on (AG81).

Claude Code runs EVERY Stop and SessionStart entry of a settings file for every session, so two
Sextant installs merged into one ~/.claude/settings.json both summarised every session into their
own corpus and both primed it. Unset, a hook acts on every session (one install: nothing changes).
Set to one or more directories (os.pathsep-separated: ';' on Windows, ':' elsewhere), a hook acts
only on a session whose working directory is one of them or sits under one. The Stop payload's
``cwd`` decides; without it, the transcript's project folder name (Claude Code's slug of the cwd,
every character outside A-Z a-z 0-9 turned into '-') is matched against the same slug of each
directory, which is a prefix match and therefore approximate. A session that can be placed nowhere
is out of scope.
"""
from __future__ import annotations

import os
import re
from pathlib import Path


def scope_dirs(environ=None) -> list[str]:
    env = os.environ if environ is None else environ
    raw = (env.get("ATLAS_HOOK_SCOPE") or "").strip()
    return [d.strip() for d in raw.split(os.pathsep) if d.strip()]


def _norm(p: str) -> str:
    try:
        s = str(Path(os.path.expanduser(p)).resolve(strict=False))
    except (OSError, RuntimeError, ValueError):
        s = os.path.abspath(p)
    s = s.rstrip("\\/") or s
    return s.casefold() if os.name == "nt" else s


def _slug(p: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "-", p)


def in_scope(cwd: str = "", transcript_file: str = "", environ=None) -> bool:
    """True when no scope is set, or the session belongs to one of the scope directories."""
    dirs = scope_dirs(environ)
    if not dirs:
        return True
    if cwd:
        here = _norm(cwd)
        sep = "\\" if os.name == "nt" else "/"
        for d in dirs:
            base = _norm(d)
            if here == base or here.startswith(base.rstrip(sep) + sep):
                return True
        return False
    if transcript_file:
        folder = Path(transcript_file).parent.name
        fold = (lambda s: s.casefold()) if os.name == "nt" else (lambda s: s)
        for d in dirs:
            try:
                full = str(Path(os.path.expanduser(d)).resolve(strict=False))
            except (OSError, RuntimeError, ValueError):
                full = d
            s = fold(_slug(full.rstrip("\\/")))
            if fold(folder) == s or fold(folder).startswith(s + "-"):
                return True
        return False
    return False
