"""Every question a command asks goes through here (lesson LES-082, ticket SEX-048).

A terminal check that trusts `isatty()` alone is wrong on Windows: the NUL device (`< NUL` in cmd,
`< /dev/null` in Git Bash) passes it, so a question was printed to nobody and then refused. The rule,
one way everywhere:

- with a person at a console, the question is asked as it always was;
- without one, NO question is printed; a piped line still answers;
- an empty or closed stdin is a refusal that says nothing was changed and names the command that
  does it without the question (the caller builds that command with `_next_step` / `branding`).

Messages are plain ASCII.
"""
from __future__ import annotations

import os
import sys


def is_terminal(stream) -> bool:
    """A person at a console: on Windows NUL passes isatty(), so the console mode decides."""
    try:
        if not stream or not stream.isatty():
            return False
        if os.name != "nt":
            return True
        import ctypes, msvcrt  # noqa: E401
        handle, mode = msvcrt.get_osfhandle(stream.fileno()), ctypes.c_uint32()
        return bool(ctypes.windll.kernel32.GetConsoleMode(ctypes.c_void_p(handle), ctypes.byref(mode)))
    except (ValueError, OSError, AttributeError):
        return False


def _piped_line() -> str | None:
    """One line from a stdin that is not a console, without its newline; None when empty or closed."""
    stdin = sys.stdin
    try:
        line = stdin.readline() if stdin else ""
    except (ValueError, OSError):
        line = ""
    if not line:
        return None
    return line.rstrip("\r\n")


def ask_line(question: str, ask=input) -> str | None:
    """A free-text answer: asked at a console, read silently from a pipe. None means there was no
    answer (end of input, or Ctrl-C at the console): the caller ends with its next step."""
    if is_terminal(sys.stdin):
        try:
            return ask(question)
        except (EOFError, KeyboardInterrupt):
            return None
    return _piped_line()


def refuse(nothing: str, rerun: str, without: str) -> None:
    """Print the one refusal sentence to stderr and exit 1 (typer's Exit when typer is there)."""
    print(f"Aborted: {nothing}. Next: answer y at the question, or run {rerun} {without}.", file=sys.stderr)
    try:
        import typer
    except ImportError:  # pragma: no cover - typer is a dependency
        raise SystemExit(1)
    raise typer.Exit(1)


def confirm(question: str, *, rerun: str, nothing: str = "nothing was changed",
            without: str = "to do it without the question", default: bool = False) -> bool:
    """Yes or no. True for y/yes, False for any other answer. No answer at all (an empty or closed
    stdin, Ctrl-C) prints the refusal sentence naming `rerun` and exits 1."""
    if is_terminal(sys.stdin):
        import typer
        try:
            return bool(typer.confirm(question, default=default))
        except typer.Abort:
            refuse(nothing, rerun, without)
    answer = _piped_line()
    if answer is None:
        refuse(nothing, rerun, without)
    answer = answer.strip().lower()
    if not answer:
        return default
    return answer in ("y", "yes")
