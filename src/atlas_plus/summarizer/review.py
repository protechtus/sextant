"""P2-T7: Human review loop for proposed session summaries."""

from __future__ import annotations

import os
import subprocess
import tempfile

from rich.console import Console
from rich.panel import Panel
from rich.syntax import Syntax

console = Console()


def review(session_log_text: str, updates_text: str) -> tuple[bool, str, str]:
    """Show proposed output and prompt for accept / edit / reject.

    Returns (accepted, final_session_log, final_updates).
    """
    while True:
        console.print("\n")
        console.print(Panel(session_log_text, title="Proposed session log entry", border_style="cyan"))
        console.print(Panel(updates_text, title="Proposed updates.txt entry", border_style="yellow"))

        console.print("\n[bold]Accept?[/bold]  [a] accept   [e] edit session log   [u] edit updates   [r] reject\n")
        from atlas_plus import _prompt
        choice = _prompt.ask_line("Choice [a/e/u/r]: ")
        if choice is None:  # no console, no piped answer: an EOFError traceback before (AG126, LES-082)
            import sys as _sys
            from atlas_plus import branding as _br
            print("No answer to the review question: nothing was written. Next: run "
                  f"`{_br.prog('atlas-summarize-session')}` in a terminal to review it, or add --auto-commit to write "
                  "it without the question.", file=_sys.stderr)
            return False, session_log_text, updates_text
        choice = choice.strip().lower()

        if choice in ("a", ""):
            return True, session_log_text, updates_text

        if choice == "r":
            console.print("[dim]Rejected. Nothing written.[/dim]")
            return False, session_log_text, updates_text

        if choice == "e":
            session_log_text = _open_in_editor(session_log_text, suffix=".md")
            console.print("[green]Session log updated.[/green]")

        if choice == "u":
            updates_text = _open_in_editor(updates_text, suffix=".txt")
            console.print("[green]Updates entry updated.[/green]")


def _editor_argv(editor: str) -> list[str]:
    """EDITOR split the way a shell would (`code --wait`, a quoted Windows path with spaces and its
    flags); an unquoted value that is itself an existing file (a full path with spaces, unquoted) stays whole."""
    editor = (editor or "").strip()
    if not editor:
        return []
    if os.path.isfile(editor):
        return [editor]
    from atlas_plus._cmdline import split_command
    try:
        return split_command(editor)
    except ValueError:  # an unbalanced quote
        return [editor]


def _open_in_editor(text: str, suffix: str = ".txt") -> str:
    """The text after the user edited it; the text unchanged when the editor cannot be started."""
    argv = _editor_argv(os.environ.get("EDITOR", "")) or ["notepad.exe"]
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=suffix, delete=False, encoding="utf-8"
    ) as f:
        f.write(text)
        tmp = f.name

    try:
        from atlas_plus._proc import other_party_env  # the editor is another party's program (AG127)
        try:
            subprocess.run([*argv, tmp], check=False, env=other_party_env(keep=("EDITOR", "VISUAL")))
        except OSError as exc:
            # a raw FileNotFoundError traceback, and the temp note left behind (AG136, MN98 F6)
            from rich.markup import escape as _esc
            console.print(f"[red]Could not start the editor {_esc(repr(argv[0]))}[/red] ({exc.__class__.__name__}); "
                          "the text is unchanged. Next step: set EDITOR to a program on PATH or its full path "
                          "(quote a path with spaces; flags may follow, e.g. EDITOR=\"code --wait\").")
            return text
        with open(tmp, encoding="utf-8") as fh:
            return fh.read()
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
