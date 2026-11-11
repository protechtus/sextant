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
        choice = input("Choice [a/e/u/r]: ").strip().lower()

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


def _open_in_editor(text: str, suffix: str = ".txt") -> str:
    editor = os.environ.get("EDITOR", "notepad.exe")
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=suffix, delete=False, encoding="utf-8"
    ) as f:
        f.write(text)
        tmp = f.name

    subprocess.run([editor, tmp], check=False)

    try:
        with open(tmp, encoding="utf-8") as fh:
            return fh.read()
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
