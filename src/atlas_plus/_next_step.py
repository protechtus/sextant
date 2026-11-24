"""What to type next, said one way everywhere (LES-054: a first-timer is never left at an error
with no direction). Every failure exit names the next command, spelled as THIS install runs it,
or the page that explains it. Plain ASCII: cp1252 and cp932 consoles print it as is."""
from __future__ import annotations

import argparse
import os
import sys


def how_to_run(name: str) -> str:
    """The command as THIS user has to type it. Bare `sextant` only works when the venv is
    activated or its Scripts folder is on PATH; a first-timer who ran `.\\.venv\\Scripts\\sextant.exe`
    and was told to type `sextant init` got "not recognized" (operator's by-hand install,
    2026-11-21). So: the bare name when the shell would find this install's script, else the
    script's path, relative to the current folder when it is inside it."""
    import shutil
    from pathlib import Path

    script = Path(sys.executable).parent / (name + (".exe" if os.name == "nt" else ""))
    try:
        found = shutil.which(name)
        if not script.exists() or (found and Path(found).resolve() == script.resolve()):
            return name
        try:
            rel = script.resolve().relative_to(Path.cwd().resolve())
            shown = f".{os.sep}{rel}"
        except ValueError:
            shown = str(script)
    except OSError:
        return name
    if " " in shown:
        return f'& "{shown}"' if os.name == "nt" else f'"{shown}"'
    return shown


def umbrella() -> str:
    """`sextant` for the public family, `atlas-plus` when invoked through an atlas-* name."""
    from pathlib import Path

    invoked = Path(sys.argv[0]).stem.lower() if sys.argv and sys.argv[0] else ""
    if invoked.startswith("sextant"):
        return "sextant"
    if invoked.startswith("atlas") or not (invoked == "__main__" and Path(sys.argv[0]).parent.name == "atlas_plus"):
        return "atlas-plus" if invoked.startswith("atlas") else "sextant"
    from importlib.metadata import PackageNotFoundError, distribution
    try:  # `python -m atlas_plus ...`: the installed distribution decides
        distribution("sextant-ai")
        return "sextant"
    except PackageNotFoundError:
        return "atlas-plus"


def command(sub: str = "") -> str:
    """`sextant <sub>` as this install must type it, backticked."""
    u = umbrella()
    return f"`{how_to_run(u)}{' ' + sub if sub else ''}`"


def help_hint(prog: str) -> str:
    return f"Run `{prog} --help` to see what it takes."


def say(message: str, next_step: str, code: int = 1) -> int:
    """Print a failure and its next step to stderr; return the exit code for `return say(...)`."""
    print(f"{message}\nNext: {next_step}", file=sys.stderr)
    return code


class ArgumentParser(argparse.ArgumentParser):
    """argparse whose usage errors end with the pointer to --help instead of a bare
    `error: unrecognized arguments`. Subparsers inherit it (argparse uses type(self))."""

    def error(self, message: str):  # type: ignore[override]
        self.print_usage(sys.stderr)
        self.exit(2, f"{self.prog}: error: {message}\n{help_hint(self.prog)}\n")
