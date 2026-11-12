"""P10-T2: atlas-tickets CLI."""

from __future__ import annotations

import json as _json
from typing import Optional

import typer
from atlas_plus._typer import Typer as _SextantTyper  # no Windows $VAR expansion
from rich.console import Console
from rich.table import Table

import re as _re

_CTRL = _re.compile("[\\x00-\\x08\\x0b\\x0c\\x0e-\\x1f\\x7f-\\x9f]")


def _clean(text: str) -> str:
    """Older tickets may hold raw control bytes (FN6, final pass)."""
    from rich.markup import escape
    return escape(_CTRL.sub("", str(text)))


app = _SextantTyper(help="Atlas lightweight ticket system")
console = Console()

STATUS_COLORS = {
    "todo": "white",
    "in-progress": "yellow",
    "done": "green",
    "blocked": "red",
    "archived": "dim",
}


@app.command("list")
def cmd_list(
    project: Optional[str] = typer.Argument(None, help="Project name (omit for all)"),
    status: Optional[str] = typer.Option(None, "--status", "-s"),
    assignee: Optional[str] = typer.Option(None, "--assignee", "-a"),
    open_only: bool = typer.Option(False, "--open", "-o", help="Show only todo + in-progress"),
    json_output: bool = typer.Option(False, "--json"),
):
    """List tickets, optionally filtered."""
    from atlas_plus.tickets.store import list_tickets
    if open_only and not status:
        tickets = [
            t for t in list_tickets(project=project, assignee=assignee)
            if t.status in ("todo", "in-progress")
        ]
    else:
        tickets = list_tickets(project=project, status=status, assignee=assignee)

    if json_output:
        typer.echo(_json.dumps([t.as_dict() for t in tickets], indent=2))
        return

    if not tickets:
        console.print("[dim]No tickets found.[/dim]")
        return

    table = Table(show_lines=False, box=None, pad_edge=False)
    table.add_column("ID", style="cyan", no_wrap=True)
    table.add_column("Project", style="dim")
    table.add_column("Status", no_wrap=True)
    table.add_column("Assignee", style="dim")
    table.add_column("E", justify="center")
    table.add_column("Title")

    for t in tickets:
        color = STATUS_COLORS.get(t.status, "white")
        table.add_row(
            t.id,
            t.project,
            f"[{color}]{t.status}[/{color}]",
            t.assignee,
            t.effort,
            t.title,
        )
    console.print(table)
    console.print(f"\n[dim]{len(tickets)} ticket(s)[/dim]")


@app.command("show")
def cmd_show(ticket_id: str = typer.Argument(...)):
    """Show full detail for a ticket."""
    from atlas_plus.tickets.store import get_ticket
    t = get_ticket(ticket_id)
    if t is None:
        console.print(f"[red]Ticket '{ticket_id}' not found.[/red]")
        raise typer.Exit(1)

    console.print(f"\n[bold cyan]{t.id}[/bold cyan]  {_clean(t.title)}")
    console.print(f"Project: {t.project}  |  Status: {t.status}  |  Assignee: {t.assignee}  |  Effort: {t.effort}")
    if t.requires:
        console.print(f"Requires: {t.requires}")
    console.print(f"Updated: {t.updated}\n")
    if t.body:
        console.print(t.body)


@app.command("create")
def cmd_create(
    title: str = typer.Argument(..., help="Short imperative title"),
    project: str = typer.Option(..., "--project", "-p"),
    assignee: str = typer.Option("human", "--assignee", "-a"),
    priority: str = typer.Option("normal", "--priority"),
    effort: str = typer.Option("M", "--effort", "-e"),
    requires: Optional[str] = typer.Option(None, "--requires"),
):
    """Create a new ticket."""
    from atlas_plus.tickets.store import create_ticket
    t = create_ticket(
        title=title,
        project=project,
        assignee=assignee,
        priority=priority,
        effort=effort,
        requires=requires,
    )
    console.print(f"[green]Created:[/green] {t.id} — {t.title}")
    console.print(f"[dim]{t.path}[/dim]")


@app.command("status")
def cmd_status(
    ticket_id: str = typer.Argument(...),
    new_status: str = typer.Argument(..., help="todo | in-progress | done | blocked | archived"),
):
    """Update a ticket's status."""
    from atlas_plus.tickets.store import update_ticket_status
    try:
        t = update_ticket_status(ticket_id, new_status)
        console.print(f"[green]{t.id}[/green] -> {t.status}")
    except (FileNotFoundError, ValueError) as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1)


@app.command("open")
def cmd_open(project: Optional[str] = typer.Argument(None)):
    """Quick view: open tickets only (todo + in-progress)."""
    from atlas_plus.tickets.store import list_tickets
    tickets = [
        t for t in list_tickets(project=project)
        if t.status in ("todo", "in-progress")
    ]
    if not tickets:
        console.print("[green]No open tickets.[/green]")
        return
    for t in tickets:
        color = STATUS_COLORS.get(t.status, "white")
        console.print(f"[cyan]{t.id}[/cyan]  [{color}]{t.status}[/{color}]  [{t.assignee}]  {_clean(t.title)}")
