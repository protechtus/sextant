"""Walk the README's Quickstart as a literal reader would (SEX-054 item 3).

Usage: python scripts/readme_walk.py [CHECKOUT_ROOT] [--readme FILE] [--workdir DIR] [--timeout S] [--list]

The script reads the fenced command blocks of the README's "## Quickstart" section in order and runs
the ones meant for this platform, one line at a time, in a temporary HOME with a fresh venv. The one
substitution: `git clone <url> [dir]` copies CHECKOUT_ROOT instead (so the README's own
`pip install -e` installs the package from the checkout under test, not from GitHub or PyPI). Skipped,
with the reason printed: blocks the README marks optional, blocks for the other platform (powershell
on POSIX, bash on Windows), and lines that run a client program (claude, codex, opencode: never run,
never looked up). A line fails the walk when it exits non-zero, times out, prints a Python traceback
or names a program that does not exist; the walk stops at the first failure and exits 1 naming it.
The transcript goes to stdout and, under GitHub Actions, to the job summary.

Only the standard library; no shell is started (each line is split and run with plain subprocess),
so a line that needs shell syntax ($VAR, pipes, redirection) fails closed instead of running half.
"""
from __future__ import annotations

import argparse
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

CLIENT_PROGRAMS = ("claude", "codex", "opencode")
POSIX_LANGS = {"bash", "sh", "shell", "console", "zsh"}
WINDOWS_LANGS = {"powershell", "ps1", "pwsh", "bat", "cmd"}
TRACEBACK = "Traceback (most recent call last)"
_FENCE = re.compile(r"^```\s*([A-Za-z0-9_+-]*)\s*$")
_SHELL_SYNTAX = re.compile(r"[$|<>`]")
_CLONE_URL = re.compile(r"^(?:https://|git@|ssh://)")
_COPY_IGNORE = shutil.ignore_patterns(".git", ".venv", "build", "dist", "__pycache__", "*.egg-info",
                                      ".pytest_cache", ".mypy_cache", ".ruff_cache")


@dataclass
class Step:
    line: int          # 1-based line in the README
    command: str       # the line as the reader types it (trailing comment removed)
    reason: str = ""   # non-empty: skipped, and why


def this_platform() -> str:
    return "windows" if os.name == "nt" else "posix"


def find_readme(root: Path) -> Path:
    """The private tree keeps the public README as docs/README-PUBLIC.md; try it first."""
    for rel in ("docs/README-PUBLIC.md", "README.md"):
        if (root / rel).is_file():
            return root / rel
    raise SystemExit(f"readme_walk: no docs/README-PUBLIC.md or README.md under {root}")


def _strip_comment(line: str) -> tuple[str, str]:
    """Split `cmd   # comment` outside quotes; a `#` must follow whitespace to start a comment."""
    quote = ""
    for i, ch in enumerate(line):
        if quote:
            if ch == quote:
                quote = ""
        elif ch in "\"'":
            quote = ch
        elif ch == "#" and (i == 0 or line[i - 1].isspace()):
            return line[:i].rstrip(), line[i + 1:].strip()
    return line.rstrip(), ""


def _line_platform(comment: str) -> str:
    c = comment.lower()
    if c.startswith("windows"):
        return "windows"
    if c.startswith(("linux", "macos", "posix")):
        return "posix"
    return ""


def _paragraph_before(lines: list[str], fence_index: int) -> str:
    """The prose paragraph that introduces a fenced block (blank lines in between are skipped)."""
    i = fence_index - 1
    while i >= 0 and not lines[i].strip():
        i -= 1
    para: list[str] = []
    while i >= 0 and lines[i].strip() and not lines[i].startswith("```"):
        para.insert(0, lines[i].strip())
        i -= 1
    return " ".join(para)


def extract(text: str, platform: str, section: str = "Quickstart") -> list[Step]:
    """Every command line of the section's fenced blocks, in README order, with skip reasons."""
    lines = text.splitlines()
    start = next((i for i, l in enumerate(lines) if l.strip() == f"## {section}"), None)
    if start is None:
        raise SystemExit(f"readme_walk: the README has no '## {section}' section")
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")), len(lines))
    steps: list[Step] = []
    i = start + 1
    while i < end:
        m = _FENCE.match(lines[i])
        if not m:
            i += 1
            continue
        lang = m.group(1).lower()
        close = next((j for j in range(i + 1, end) if lines[j].startswith("```")), end)
        intro = _paragraph_before(lines, i)
        block_reason = ""
        if intro.lower().startswith("optional"):
            block_reason = "the README marks this block optional: " + intro[:70]
        elif lang in WINDOWS_LANGS and platform != "windows":
            block_reason = f"a {lang} block: Windows only"
        elif lang in POSIX_LANGS and platform != "posix":
            block_reason = f"a {lang} block: Linux / macOS only"
        elif lang and lang not in WINDOWS_LANGS | POSIX_LANGS:
            block_reason = f"a {lang} block, not commands"
        for j in range(i + 1, close):
            command, comment = _strip_comment(lines[j])
            if not command.strip():
                continue
            reason = block_reason
            words = command.split()
            first = re.split(r"[\\/]", words[0])[-1].lower() if words else ""
            if first.endswith(".exe"):
                first = first[:-4]
            if not reason and first in CLIENT_PROGRAMS:
                reason = f"runs the client program `{first}` (never run by this walk)"
            if not reason:
                lp = _line_platform(comment)
                if lp and lp != platform:
                    reason = f"the README marks this line for {comment}"
            steps.append(Step(j + 1, command.strip(), reason))
        i = close + 1
    return steps


def split_line(command: str) -> list[list[str]]:
    """`a; b` and `a && b` are two commands; each is split into argv (quotes removed)."""
    parts = re.split(r"\s*(?:;|&&)\s*", command)
    out = []
    for part in parts:
        if not part:
            continue
        argv = shlex.split(part, posix=(os.name != "nt"))
        out.append([a[1:-1] if len(a) > 1 and a[0] == a[-1] and a[0] in "\"'" else a for a in argv])
    return out


def reader_env(home: Path) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items()
           if not k.upper().startswith(("ATLAS_", "SEXTANT_", "VIRTUAL_ENV", "PYTHONHOME", "PYTHONPATH"))}
    env["HOME"] = str(home)
    if os.name == "nt":
        env["USERPROFILE"] = str(home)
    env["PIP_NO_INPUT"] = "1"
    env["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
    return env


class Walk:
    def __init__(self, root: Path, workdir: Path, timeout: float):
        self.root = root
        self.home = workdir / "home"
        self.home.mkdir(parents=True, exist_ok=True)
        self.cwd = self.home
        self.env = reader_env(self.home)
        self.timeout = timeout
        self.log: list[str] = []

    def say(self, text: str = "") -> None:
        print(text, flush=True)
        self.log.append(text)

    def _resolve(self, prog: str) -> str | None:
        if "/" in prog or "\\" in prog:
            p = Path(prog)
            p = p if p.is_absolute() else self.cwd / p
            return str(p) if p.exists() else None
        return shutil.which(prog, path=self.env.get("PATH"))

    def run_argv(self, argv: list[str]) -> tuple[bool, str]:
        if argv[0] == "cd":
            target = Path(argv[1]) if len(argv) > 1 else self.home
            target = target if target.is_absolute() else self.cwd / target
            if not target.is_dir():
                return False, f"cd: no such directory: {target}"
            self.cwd = target.resolve()
            return True, f"(cwd is now {self.cwd})"
        if argv[0] == "git" and len(argv) >= 3 and argv[1] == "clone" and _CLONE_URL.match(argv[2]):
            name = argv[3] if len(argv) > 3 else re.sub(r"\.git$", "", argv[2].rstrip("/").rsplit("/", 1)[-1])
            dest = self.cwd / name
            shutil.copytree(self.root, dest, ignore=_COPY_IGNORE)
            return True, f"SUBSTITUTED: `git clone {argv[2]}` -> a copy of the checkout {self.root} at {dest}"
        exe = self._resolve(argv[0])
        if exe is None:
            return False, f"command not found: {argv[0]}"
        env = self.env
        note = ""
        if os.name == "nt" and Path(exe).stem.lower() == "py" and "USERPROFILE" in os.environ:
            # The Windows `py` launcher exits 109 when USERPROFILE points at a fresh folder: it gets the
            # reader's real profile (it only picks an interpreter; nothing is written to the profile).
            env = {**self.env, "USERPROFILE": os.environ["USERPROFILE"]}
            note = "(the py launcher runs with the real USERPROFILE: it exits 109 under a fresh one)\n"
        try:
            proc = subprocess.run([exe, *argv[1:]], cwd=self.cwd, env=env, stdin=subprocess.DEVNULL,
                                  stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=self.timeout)
        except subprocess.TimeoutExpired:
            return False, f"timed out after {self.timeout:.0f} s"
        out = note + proc.stdout.decode("utf-8", errors="replace")
        if proc.returncode != 0:
            return False, f"{out}\nexit {proc.returncode}"
        if TRACEBACK in out:
            return False, f"{out}\nexit 0 but the output holds a Python traceback"
        return True, out

    def step(self, n: int, step: Step) -> bool:
        if step.reason:
            self.say(f"[{n}] SKIP README:{step.line}  {step.command}\n      reason: {step.reason}")
            return True
        if _SHELL_SYNTAX.search(step.command):
            self.say(f"[{n}] FAIL README:{step.line}  {step.command}\n      the line needs a shell "
                     "($, |, <, >, `) and this walk starts none: mark it optional or platform-specific")
            return False
        self.say(f"[{n}] RUN  README:{step.line}  {step.command}   (in {self.cwd})")
        for argv in split_line(step.command):
            t0 = time.monotonic()
            ok, out = self.run_argv(argv)
            tail = out.strip().splitlines()[-15:]
            for line in tail:
                self.say(f"      | {line}")
            self.say(f"      {'ok' if ok else 'FAILED'} in {time.monotonic() - t0:.1f} s: {' '.join(argv)}")
            if not ok:
                return False
        return True


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("root", nargs="?", default=str(Path(__file__).resolve().parents[1]),
                    help="the checkout to install from (default: the one holding this script)")
    ap.add_argument("--readme", help="read this README instead of the checkout's")
    ap.add_argument("--workdir", help="where the temporary HOME goes (default: a new temp folder)")
    ap.add_argument("--timeout", type=float, default=900.0, help="seconds per command (default 900)")
    ap.add_argument("--list", action="store_true", help="print the extracted steps and run nothing")
    args = ap.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):  # a cp1252 console or pipe must not kill the walk on a child's box glyph
            stream.reconfigure(errors="backslashreplace")
    root =Path(args.root).resolve()
    readme = Path(args.readme).resolve() if args.readme else find_readme(root)
    steps = extract(readme.read_text(encoding="utf-8"), this_platform())
    workdir = Path(args.workdir).resolve() if args.workdir else Path(tempfile.mkdtemp(prefix="readme-walk-"))
    if workdir == root or root in workdir.parents:
        raise SystemExit(f"readme_walk: --workdir {workdir} is inside the checkout; the clone step would copy it into itself")
    walk = Walk(root, workdir, args.timeout)
    walk.say(f"README walk: {readme} on {this_platform()} (Python {sys.version.split()[0]})")
    walk.say(f"checkout: {root}; temporary HOME: {walk.home}")
    walk.say("substitution: `git clone <url>` copies the checkout, so the package is installed from it, "
             "not from GitHub or PyPI")
    walk.say(f"{len(steps)} command lines in the Quickstart, "
             f"{sum(1 for s in steps if not s.reason)} to run on this platform")
    failed = None
    for n, step in enumerate(steps, 1):
        if args.list:
            walk.say(f"[{n}] {'SKIP' if step.reason else 'RUN '} README:{step.line}  {step.command}"
                     + (f"\n      reason: {step.reason}" if step.reason else ""))
            continue
        if not walk.step(n, step):
            failed = step
            break
    verdict = ("listed only, nothing run" if args.list
               else f"FAILED at README:{failed.line}: {failed.command}" if failed
               else "every command a reader is told to type succeeded")
    walk.say(f"README walk: {verdict}")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write("## README walk\n\n```text\n" + "\n".join(walk.log) + "\n```\n")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
