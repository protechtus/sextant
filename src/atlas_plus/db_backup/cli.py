"""Atlas SQLite state database backup CLI (atlas-db-backup).

Usage:
    atlas-db-backup snapshot              # create a WAL-safe backup now
    atlas-db-backup list                  # list available snapshots
    atlas-db-backup restore <path>        # restore from snapshot (requires confirmation)
    atlas-db-backup prune [--keep N]      # delete oldest snapshots beyond N (default 14)
    atlas-db-backup status                # show last backup metadata

Designed to be called from Brain scheduler (daily 02:00 UTC) or manually.
Safe to run while Brain is running for snapshot/list/prune.
Restore requires Brain to be stopped to avoid corruption.
"""

from __future__ import annotations

import sys
from pathlib import Path

import typer
from atlas_plus._typer import Typer as _SextantTyper  # no Windows $VAR expansion
from rich.console import Console
from rich.table import Table

from .core import (
    _BACKUP_DIR,
    _DB_PATH,
    last_backup_info,
    list_backups,
    prune,
    restore,
    snapshot,
)

app = _SextantTyper(
    name="atlas-db-backup",
    help="Atlas SQLite state DB backup / restore CLI",
    add_completion=False,
)
console = Console()


@app.command(name="snapshot")
def snapshot_cmd(
    source: Path = typer.Option(_DB_PATH, "--source", "-s", help="Source state.sqlite path"),
    backup_dir: Path = typer.Option(_BACKUP_DIR, "--dir", "-d", help="Backup directory"),
):
    """Create a WAL-safe timestamped snapshot of the state database."""
    try:
        dest = snapshot(source=source, backup_dir=backup_dir)
        size_kb = dest.stat().st_size // 1024
        console.print(f"[green]Snapshot created:[/green] {dest}  ({size_kb} KB)")
    except FileNotFoundError as exc:
        console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(1)
    except Exception as exc:
        console.print(f"[red]Snapshot failed:[/red] {exc}")
        raise typer.Exit(1)


@app.command(name="list")
def list_cmd(
    backup_dir: Path = typer.Option(_BACKUP_DIR, "--dir", "-d"),
):
    """List available snapshots with timestamps and sizes."""
    backups = list_backups(backup_dir)
    if not backups:
        console.print("[dim]No snapshots found.[/dim]")
        return
    t = Table(show_header=True, header_style="bold", title=f"Snapshots ({len(backups)})")
    t.add_column("Timestamp", style="cyan", width=20)
    t.add_column("Size (KB)", width=12)
    t.add_column("Modified", width=22)
    t.add_column("Path", width=50)
    for b in backups:
        t.add_row(b["ts_str"], str(b["size_kb"]), b["modified"], str(b["path"]))
    console.print(t)


@app.command(name="restore")
def restore_cmd(
    backup_path: Path = typer.Argument(..., help="Path to the .sqlite snapshot to restore"),
    dest: Path = typer.Option(_DB_PATH, "--dest", help="Where to restore to"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation prompt"),
):
    """Restore a snapshot. Archives the current DB before overwriting.

    WARNING: Brain must be stopped before restoring. Running restore while
    Brain is active can corrupt both the current DB and the snapshot.
    """
    if not backup_path.exists():
        console.print(f"[red]Backup not found:[/red] {backup_path}")
        raise typer.Exit(1)

    console.print(f"[bold]Restore plan:[/bold]")
    console.print(f"  Source:  {backup_path}")
    console.print(f"  Target:  {dest}")
    console.print(f"  [yellow]WARNING: Stop Brain before restoring.[/yellow]")

    if not yes:
        confirm = typer.confirm("Proceed with restore?", default=False)
        if not confirm:
            console.print("[dim]Restore cancelled.[/dim]")
            return

    try:
        archive_path = restore(backup_path=backup_path, dest=dest)
        console.print(f"[green]Restore complete.[/green]")
        if archive_path:
            console.print(f"  Previous DB archived to: {archive_path}")
    except Exception as exc:
        console.print(f"[red]Restore failed:[/red] {exc}")
        raise typer.Exit(1)


@app.command(name="prune")
def prune_cmd(
    keep: int = typer.Option(14, "--keep", "-k", help="Number of snapshots to retain"),
    backup_dir: Path = typer.Option(_BACKUP_DIR, "--dir", "-d"),
):
    """Delete oldest snapshots, keeping only the most recent N."""
    deleted = prune(keep=keep, backup_dir=backup_dir)
    if deleted == 0:
        console.print(f"[dim]No snapshots pruned (already within --keep {keep}).[/dim]")
    else:
        console.print(f"[green]Pruned {deleted} snapshot(s)[/green] (retained {keep} most recent).")


@app.command()
def status(
    backup_dir: Path = typer.Option(_BACKUP_DIR, "--dir", "-d"),
    source: Path = typer.Option(_DB_PATH, "--source", "-s"),
):
    """Show last backup metadata and DB size."""
    info = last_backup_info(backup_dir)
    if info:
        console.print(f"[bold]Last snapshot:[/bold] {info['ts_str']}")
        console.print(f"  Size:     {info['size_kb']} KB")
        console.print(f"  Modified: {info['modified']}")
        console.print(f"  Path:     {info['path']}")
    else:
        console.print("[yellow]No snapshots found.[/yellow]  Run: atlas-db-backup snapshot")

    if source.exists():
        src_kb = source.stat().st_size // 1024
        console.print(f"\n[bold]Current DB:[/bold] {source}  ({src_kb} KB)")
    else:
        console.print(f"\n[dim]Source DB not found: {source}[/dim]")

    total = len(list_backups(backup_dir))
    console.print(f"[dim]Total snapshots on disk: {total}[/dim]")


def main() -> None:
    """Entry point for the db-backup CLI (Typer app)."""
    app()


if __name__ == "__main__":
    main()
