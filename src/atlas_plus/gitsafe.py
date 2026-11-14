"""One hardened way to run git on a repository we did not author.

Every git invocation in Sextant reads state from repositories the user (or a sync
peer, or an intake drop) controls. Git executes configuration-driven commands on
ordinary read paths: ``core.fsmonitor`` and ``core.hooksPath`` on ``status``,
``filter.<x>.clean`` on anything that touches the index, ``diff.external`` and
``diff.<driver>.textconv`` on ``diff``, ``post-commit`` even under ``--no-verify``.
A repo-local ``.git/config`` that a peer rewrote therefore ran code as the operator
during the next ``git status`` (round-B re-audit 2026-11-09, rows 16 and N-B/N-J).

This module neutralizes every such path in one place. Use ``run`` / ``check_output``
here instead of calling ``subprocess`` with ``git`` anywhere else.
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Sequence

_NULL = os.devnull

# Static hardening applied to every call.
_BASE = [
    "-c", "core.fsmonitor=",
    "-c", f"core.hooksPath={_NULL}",
    "-c", "diff.external=",
    "-c", "core.pager=cat",
    "-c", "core.editor=true",
    "-c", "credential.helper=",
    "-c", "commit.gpgsign=false",
    "-c", "tag.gpgsign=false",
    "-c", "log.showSignature=false",
    "-c", "core.sshCommand=",
    "-c", "protocol.allow=never",
    "-c", "protocol.file.allow=always",
]

_EXEC_KEYS = re.compile(
    r"^(filter\.[^.]+\.(clean|smudge|process)|diff\.[^.]+\.(command|textconv)|"
    r"merge\.[^.]+\.driver|sequence\.editor|core\.askpass|gpg\.program|gpg\.[^.]+\.program|"
    r"alias\.[^.]+|url\.[^.]+\.insteadof|remote\.[^.]+\.(uploadpack|receivepack)|"
    r"uploadpack\.[^.]+|receive\.[^.]+|http\.[^.]*proxy|core\.gitproxy|ssh\.variant)$",
    re.IGNORECASE,
)


def _env() -> dict:
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    # The SYSTEM config stays: it is installed by an administrator, not by a repo, and on
    # Windows it carries core.autocrlf=true; without it every CRLF file read as modified and
    # the hooks reported a clean repo as dirty (persona pass 2026-11-10, "Priya").
    env["GIT_OPTIONAL_LOCKS"] = "0"
    env["GIT_ASKPASS"] = "echo"
    env.pop("GIT_EXTERNAL_DIFF", None)
    env.pop("GIT_SSH_COMMAND", None)
    return env


def _repo_exec_keys(cwd: Path | str | None, timeout: float) -> list[str]:
    """Config keys in scope that name a command. Reading config executes nothing."""
    try:
        r = subprocess.run(
            ["git", *_BASE, "config", "--list", "--name-only"],
            cwd=str(cwd) if cwd else None, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout, env=_env(),
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if r.returncode != 0:
        return []
    return [k.strip() for k in r.stdout.splitlines() if _EXEC_KEYS.match(k.strip())]


def argv(args: Sequence[str], cwd: Path | str | None = None, *, timeout: float = 10) -> list[str]:
    """The full hardened argv for ``git <args>`` run in ``cwd``."""
    args = list(args)
    if args and args[0] == "git":
        args = args[1:]
    # `git -C <dir> ...` runs in <dir>: discover that repo's config, not the process cwd's.
    scan_cwd = cwd
    if "-C" in args:
        idx = args.index("-C")
        if idx + 1 < len(args):
            scan_cwd = args[idx + 1]
    extra: list[str] = []
    for key in _repo_exec_keys(scan_cwd, timeout):
        extra += ["-c", f"{key}="]
        if key.lower().startswith("filter."):
            extra += ["-c", key.rsplit(".", 1)[0] + ".required=false"]
    if args and args[0] in ("diff", "log", "show", "format-patch"):
        if "--no-ext-diff" not in args:
            args.insert(1, "--no-ext-diff")
        if "--no-textconv" not in args:
            args.insert(1, "--no-textconv")
    return ["git", *_BASE, *extra, *args]


def run(args: Sequence[str], cwd: Path | str | None = None, *, timeout: float = 10,
        **kwargs) -> subprocess.CompletedProcess:
    """subprocess.run for git with every config-driven exec path neutralized."""
    kwargs.setdefault("capture_output", True)
    kwargs.setdefault("stdin", subprocess.DEVNULL)  # a git child that inherits the MCP stdio pipe hangs until the timeout (MN53: every write 5 s, the guard failed open)
    kwargs.setdefault("text", True)
    if kwargs.get("text"):
        kwargs.setdefault("encoding", "utf-8")
        kwargs.setdefault("errors", "replace")
    kwargs.setdefault("timeout", timeout)
    kwargs["env"] = _env()
    return subprocess.run(argv(args, cwd, timeout=timeout), cwd=str(cwd) if cwd else None, **kwargs)


def check_output(args: Sequence[str], cwd: Path | str | None = None, *, timeout: float = 10) -> str:
    """Hardened git call that returns stdout and raises CalledProcessError on failure."""
    r = run(args, cwd, timeout=timeout)
    if r.returncode != 0:
        raise subprocess.CalledProcessError(r.returncode, r.args, r.stdout, r.stderr)
    return r.stdout
