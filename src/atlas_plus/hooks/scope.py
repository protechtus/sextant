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

Both hooks decide on the payload's ``cwd`` when Claude Code sends one, and on the process working
directory only when it does not (AG87: SessionStart used the process cwd while Stop used the
payload's, so one session could be primed by one install and summarised by another).
"""
from __future__ import annotations

import os
import re
from pathlib import Path


def scope_dirs(environ=None) -> list[str]:
    env = os.environ if environ is None else environ
    raw = (env.get("ATLAS_HOOK_SCOPE") or "").strip()
    return [d.strip() for d in raw.split(os.pathsep) if d.strip()]


def posix_path_for(path: str) -> str:
    """A Windows path from a Windows Claude Code, received by a WSL install, as its /mnt form (MN33)."""
    m = re.match(r"^([A-Za-z]):[\\/](.*)$", path)
    if os.name != "nt" and m:
        return f"/mnt/{m.group(1).lower()}/" + m.group(2).replace("\\", "/")
    return path


def read_payload(timeout: float = 2.0) -> dict:
    """The hook's JSON payload from stdin, or {} (a terminal, nothing piped, not JSON). Bounded: a
    pipe that never closes costs ``timeout`` seconds, never a hung session start."""
    import json
    import sys
    import threading
    try:
        if sys.stdin is None or sys.stdin.isatty():
            return {}
    except (OSError, ValueError, AttributeError):
        return {}
    box: dict = {}

    def _read() -> None:
        # The file descriptor, not sys.stdin.buffer: a reader still blocked on a pipe that never
        # closes held the BufferedReader's lock, and interpreter shutdown died on it ("Fatal Python
        # error: _enter_buffered_busy", exit 139 from `sextant context` in a shell; AG116).
        try:
            fd = sys.stdin.fileno()
        except (OSError, ValueError, AttributeError):
            fd = None
        try:
            if fd is None:
                box["raw"] = sys.stdin.read(1_000_000)
                return
            chunks: list = []
            got = 0
            while got < 1_000_000:
                chunk = os.read(fd, min(65536, 1_000_000 - got))
                if not chunk:
                    break
                chunks.append(chunk)
                got += len(chunk)
            box["raw"] = b"".join(chunks)
        except Exception:  # noqa: BLE001
            pass

    t = threading.Thread(target=_read, daemon=True)
    t.start()
    t.join(timeout)
    raw = box.get("raw")
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-16", "replace") if raw[:2] in (b"\xff\xfe", b"\xfe\xff") else raw.decode("utf-8-sig", "replace")
    if not isinstance(raw, str) or not raw.strip():
        return {}
    try:
        payload = json.loads(raw.lstrip("\ufeff"))
    except ValueError:
        return {}
    return payload if isinstance(payload, dict) else {}


def payload_cwd(payload: dict) -> str:
    """The payload's ``cwd`` as a path this host can match, or ''."""
    cwd = payload.get("cwd") if isinstance(payload, dict) else None
    if not isinstance(cwd, str) or not cwd.strip():
        return ""
    return posix_path_for(cwd.replace("\x00", ""))


def quoted_hint(environ=None) -> str:
    """One sentence when an ATLAS_HOOK_SCOPE entry carries its own quote characters, else ''.
    `--env ATLAS_HOOK_SCOPE="C:\\x"` inside single quotes keeps the double quotes, and no session
    ever matched (AG87)."""
    bad = [d for d in scope_dirs(environ) if d[:1] in "\"'" or d[-1:] in "\"'"]
    if not bad:
        return ""
    return (f"ATLAS_HOOK_SCOPE entry {bad[0]!r} carries quote characters, so no directory matches it; "
            "remove the quotes from the value.")


def _norm(p: str) -> str:
    try:
        s = str(Path(os.path.expanduser(p)).resolve(strict=False))
    except (OSError, RuntimeError, ValueError):
        s = os.path.abspath(p)
    s = s.rstrip("\\/") or s
    return s.casefold() if os.name == "nt" else s


def _slug(p: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "-", p)


def session_in_scope(cwd: str = "", transcript_file: str = "", environ=None) -> bool:
    """The decision both hooks make: the payload's cwd, else the transcript's project folder, else the
    process working directory (MN81: Stop used the folder and SessionStart the process cwd, so a payload
    without cwd was summarised by an install that had not primed it)."""
    if not cwd and not transcript_file:
        cwd = os.getcwd()
    return in_scope(cwd, transcript_file, environ)


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
