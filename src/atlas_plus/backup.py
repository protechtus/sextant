"""Atlas backup CLI — daily sync + archive to Google Drive via rclone.

Commands:
  atlas-backup verify   check rclone is installed and gdrive remote exists
  atlas-backup now      run backup immediately
  atlas-backup status   show last backup result
  atlas-backup install  register Windows Task Scheduler daily job (requires elevation)
"""

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import typer
from atlas_plus._typer import Typer as _SextantTyper  # no Windows $VAR expansion
from rich.console import Console
from rich.table import Table

from atlas_plus import paths as atlas_paths

from atlas_plus.branding import product

app = _SextantTyper(help=f"{product()} backup -- mirror the corpus with rclone")
console = Console()

# ---------------------------------------------------------------------------
# Config (override via environment if needed)
# ---------------------------------------------------------------------------
#
# Per ADR-016 (data classification) and the 2026-05-18 audit (CRITICAL-2):
# the rclone remote should point at a CRYPT remote that wraps the underlying
# gdrive remote. Configure once:
#
#   rclone config
#     -> n (new remote) -> name=gdrive_crypt -> storage=crypt
#     -> remote=gdrive:Atlas
#     -> set passphrase + salt; store passphrase in
#        <state dir>\.crypt-passphrase (NTFS user-only ACL)
#
# Then set ATLAS_RCLONE_REMOTE=gdrive_crypt in your env.
#
# Default below points at gdrive_crypt. To migrate from a pre-crypt remote,
# wrap it per https://rclone.org/crypt/ (rationale: ADR-016, docs/adr/).

RCLONE_REMOTE = os.environ.get("ATLAS_RCLONE_REMOTE", "")  # no default: the remote is yours to name
GDRIVE_ROOT = os.environ.get("ATLAS_RCLONE_ROOT", "Atlas")

LOG_DIR = atlas_paths.state_dir() / "logs"
BACKUP_LOG = LOG_DIR / "backup.log"
BACKUP_STATE = LOG_DIR / "backup_state.json"

SCRIPT_DIR = Path(__file__).parent.parent.parent / "scripts"
PS_BACKUP = SCRIPT_DIR / "backup.ps1"
PS_INSTALL = SCRIPT_DIR / "install_backup_task.ps1"

# Hard-excluded file patterns that never leave the workstation regardless of
# crypt remote. Defense in depth: if the crypt config is ever wrong, these
# still do not upload. See ADR-016 Tier 3 (Secrets).
_SECRET_PATTERNS = [
    "**/*.key",
    "**/*.secret",
    "**/*.pem",
    "**/*.pfx",
    "**/*.p12",
    "**/enrollment.secret",
    "**/jwt.secret",
    "**/vault.key",
    "**/event_signing_private.pem",
    "**/.crypt-passphrase",
    "**/.env",
    "**/.env.*",
]

# What to back up
SYNC_TARGETS = [
    {
        "name": "memory corpus",
        "source": str(atlas_paths.memory_root()),
        "dest": f"{RCLONE_REMOTE}:{GDRIVE_ROOT}/memory",
        "exclude": list(_SECRET_PATTERNS),
    },
    {
        "name": "atlas state",
        "source": str(atlas_paths.state_dir()),
        "dest": f"{RCLONE_REMOTE}:{GDRIVE_ROOT}/state",
        # logs are noisy and may capture sensitive content; secrets never leave.
        "exclude": ["logs/**", *_SECRET_PATTERNS],
    },
]

ARCHIVE_TARGETS = [
    {
        "name": "memory archive",
        "source": str(atlas_paths.memory_root()),
        "dest_template": f"{RCLONE_REMOTE}:{GDRIVE_ROOT}/archive/{{date}}/memory",
        "exclude": list(_SECRET_PATTERNS),
    },
    {
        # transcripts, task DB and intake ledger are only copies; the mirror alone gives
        # them no history (round-B 2026-11-09, row 23 residual 3).
        "name": "state archive",
        "source": str(atlas_paths.state_dir()),
        "dest_template": f"{RCLONE_REMOTE}:{GDRIVE_ROOT}/archive/{{date}}/state",
        "exclude": ["logs/**", *_SECRET_PATTERNS],
    },
]

# Identity, not a file count: the mirror must come from OUR corpus / state dir, not from
# whatever a misconfigured root points at (round-B 2026-11-09, row 23 residual 1).
_SOURCE_SIGNATURES = {
    "memory corpus": ("identity", "projects"),
    "atlas state": (".sextant-state", "state.sqlite", "transcripts"),
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _remote_problem() -> str:
    """'' when RCLONE_REMOTE is a configured rclone remote, else what exactly is missing."""
    if not RCLONE_REMOTE:
        return "ATLAS_RCLONE_REMOTE is not set"
    r = _rclone("listremotes", capture=True)
    if r.returncode == 127:
        return "rclone is not on PATH"
    if r.returncode != 0:
        return f"rclone listremotes failed (exit {r.returncode}): {(r.stderr or '').strip()[:120]}"
    names = {line.strip().rstrip(":") for line in (r.stdout or "").splitlines() if line.strip()}
    if RCLONE_REMOTE not in names:
        return f"rclone has no remote named '{RCLONE_REMOTE}' (it has: {', '.join(sorted(names)) or 'none'})"
    return ""


def _remote_configured() -> bool:
    return _remote_problem() == ""


def _rclone(*args: str, capture: bool = False) -> subprocess.CompletedProcess:
    # --ask-password=false: an encrypted rclone config must fail, not stop a backup run
    # (or a scheduled task) at an interactive prompt (persona pass 2026-11-10, "Dana").
    cmd = ["rclone", "--ask-password=false", *args]
    try:
        return subprocess.run(
            cmd,
            capture_output=capture,
            text=True,
        )
    except FileNotFoundError:
        # rclone is an optional external tool; its absence is a reportable state, not a crash
        # (deep install lane 2026-11-09: `sextant-backup verify` showed a raw traceback).
        return subprocess.CompletedProcess(cmd, 127, stdout="", stderr="rclone executable not found on PATH")


def _write_state(result: dict) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    BACKUP_STATE.write_text(json.dumps(result, indent=2), encoding="utf-8")


def _read_state() -> dict | None:
    if not BACKUP_STATE.exists():
        return None
    try:
        return json.loads(BACKUP_STATE.read_text(encoding="utf-8"))
    except Exception:
        return None


def _log(msg: str) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    line = f"[{ts}] {msg}\n"
    with BACKUP_LOG.open("a", encoding="utf-8") as f:
        f.write(line)
    console.print(f"[dim]{ts}[/dim] {msg}")


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

@app.command()
def verify():
    """Check rclone is installed, the remote is configured, and it is a crypt wrapper.

    Per ADR-016 (data classification) and the 2026-05-18 audit (CRITICAL-2),
    the active remote must be a crypt-type remote. Verify warns if you are
    still pointing at a plain gdrive remote.
    """
    if not RCLONE_REMOTE:
        # The `now` path had this branch; verify printed "Remote '' not found" (FN1, final pass).
        console.print("[red]ATLAS_RCLONE_REMOTE is not set.[/red] Name the rclone remote to verify "
                      "(see docs/CONFIGURATION.md, backup).")
        raise typer.Exit(1)
    result = _rclone("version", capture=True)
    if result.returncode != 0:
        import platform as _pf
        hint = {"Windows": "winget install Rclone.Rclone", "Darwin": "brew install rclone"}.get(
            _pf.system(), "your distro package, or https://rclone.org/install/")
        console.print(f"[red]rclone not found on PATH.[/red] Install it first: {hint}")
        raise typer.Exit(1)
    version_line = result.stdout.splitlines()[0] if result.stdout else "unknown"
    console.print(f"[green]rclone OK[/green]  {version_line}")

    result = _rclone("listremotes", "--long", capture=True)
    # rclone listremotes --long emits "name: type" per line.
    remotes: dict[str, str] = {}
    for line in result.stdout.splitlines():
        if ":" in line:
            n, _, t = line.partition(":")
            remotes[n.strip()] = t.strip()

    if RCLONE_REMOTE not in remotes:
        # Public-facing message: name the env var and the upstream docs, not
        # a private runbook path (polish sweep, quality loop run 29).
        console.print(
            f"[red]Remote '{RCLONE_REMOTE}' not found.[/red] "
            f"Create it with `rclone config` (use a crypt remote for cloud "
            f"targets: https://rclone.org/crypt/), or point "
            f"ATLAS_RCLONE_REMOTE at an existing remote (docs/CONFIGURATION.md)."
        )
        console.print(f"Configured remotes: {list(remotes) or '(none)'}")
        raise typer.Exit(1)

    remote_type = remotes.get(RCLONE_REMOTE, "unknown")
    console.print(f"[green]Remote '{RCLONE_REMOTE}' OK[/green] (type: {remote_type})")
    if remote_type != "crypt":
        console.print(
            f"[yellow]WARNING:[/yellow] remote type is '{remote_type}', not 'crypt'. "
            f"Cleartext at rest on the upstream. Wrap it in a crypt remote "
            f"(https://rclone.org/crypt/) and point ATLAS_RCLONE_REMOTE at "
            f"the crypt wrapper; see ADR-016 (docs/adr/) for why."
        )

    # Quick connectivity test
    result = _rclone("lsd", f"{RCLONE_REMOTE}:", capture=True)
    if result.returncode != 0:
        console.print(f"[yellow]Remote exists but connectivity test failed:[/yellow] {result.stderr.strip()}")
        raise typer.Exit(1)

    console.print("[green]Remote connection OK[/green]")


_SYNC_MAX_DELETE = 50
_MIN_SYNC_SOURCE_FILES = 5


def _count_files_capped(src, cap: int) -> int | None:
    """Count files under a LOCAL source directory, stopping at `cap`.

    Returns None when the source is not a directory,
    in which case the guard does not apply.
    """
    try:
        p = Path(str(src))
        if not p.exists():
            return 0
        if not p.is_dir():
            return None
        n = 0
        for _root, _dirs, files in os.walk(p):
            n += len(files)
            if n >= cap:
                return n
        return n
    except OSError:
        return None


@app.command()
def now(
    dry_run: bool = typer.Option(False, "--dry-run", "-n", help="Show what would be synced, don't transfer"),
    skip_archive: bool = typer.Option(False, "--skip-archive", help="Skip dated archive snapshots"),
):
    """Run backup immediately."""
    started = datetime.now(timezone.utc)
    date_str = started.strftime("%Y-%m-%d")
    errors: list[str] = []

    problem = _remote_problem()
    if problem:
        console.print(
            f"[red]Backup refused: {problem}.[/red] "
            "sextant-backup copies your corpus to an rclone remote: install rclone, create a remote "
            "(a crypt remote for any cloud target: https://rclone.org/crypt/), then set "
            "ATLAS_RCLONE_REMOTE=<name> and optionally ATLAS_RCLONE_ROOT=<folder>. "
            "To back up without rclone, copy the corpus folder with any tool you trust "
            "(docs/GETTING-STARTED.md, 'Backing up')."
        )
        raise typer.Exit(1)

    _log(f"Backup started (dry_run={dry_run})")

    # --- sync targets (current-state mirror) ---
    for t in SYNC_TARGETS:
        src = t["source"]
        dest = t["dest"]
        name = t["name"]
        extra = []
        for exc in t.get("exclude", []):
            extra += ["--exclude", exc]

        # Guard: a misconfigured/empty source would mirror emptiness onto the
        # remote. Refuse to sync a local directory holding almost nothing.
        n_files = _count_files_capped(src, _MIN_SYNC_SOURCE_FILES)
        if n_files is not None and n_files < _MIN_SYNC_SOURCE_FILES:
            var = "ATLAS_STATE_DIR" if "state" in name else "ATLAS_MEMORY_ROOT"
            msg = (f"SYNC REFUSED {name}: source {src} has only {n_files} file(s) "
                   f"(< {_MIN_SYNC_SOURCE_FILES}); check {var}")
            _log(msg)
            errors.append(msg)
            continue
        signature = _SOURCE_SIGNATURES.get(name)
        if signature and not any((Path(str(src)) / s).exists() for s in signature):
            msg = (f"SYNC REFUSED {name}: source {src} shows none of {signature}; "
                   "it does not look like this install's data, check ATLAS_MEMORY_ROOT / ATLAS_STATE_DIR")
            _log(msg)
            errors.append(msg)
            continue

        _log(f"Syncing {name}: {src} -> {dest}")
        # Deletions AND overwrites on the remote go to a dated trash, never straight away.
        trash = f"{RCLONE_REMOTE}:{GDRIVE_ROOT}/trash/{date_str}/{name.replace(' ', '-')}"
        guard = ["--max-delete", str(_SYNC_MAX_DELETE), "--backup-dir", trash]
        cmd = ["sync", src, dest, "--progress"] + guard + extra
        if dry_run:
            cmd = ["sync", src, dest, "--dry-run"] + guard + extra
        r = _rclone(*cmd)
        if r.returncode != 0:
            msg = f"SYNC FAILED {name}: exit {r.returncode}"
            _log(msg)
            errors.append(msg)
        else:
            _log(f"Sync OK: {name}")

    # --- archive targets (dated snapshots) ---
    if not skip_archive:
        for t in ARCHIVE_TARGETS:
            src = t["source"]
            dest = t["dest_template"].format(date=date_str)
            name = t["name"]
            extra = []
            for exc in t.get("exclude", []):
                extra += ["--exclude", exc]
            _log(f"Archiving {name}: {src} -> {dest}")
            cmd = ["copy", src, dest, "--progress"] + extra
            if dry_run:
                cmd = ["copy", src, dest, "--dry-run"] + extra
            r = _rclone(*cmd)
            if r.returncode != 0:
                msg = f"ARCHIVE FAILED {name}: exit {r.returncode}"
                _log(msg)
                errors.append(msg)
            else:
                _log(f"Archive OK: {name} -> {dest}")

    finished = datetime.now(timezone.utc)
    elapsed = (finished - started).total_seconds()
    success = len(errors) == 0

    # A dry run is reported as what it is and never replaces the record of the last REAL
    # run (wave-2 2026-11-09: `status` after a rehearsal claimed a backup had just
    # succeeded and the real timestamp was gone).
    prev = _read_state() or {}
    prev_real_run = prev.get("last_real_run") or (None if prev.get("dry_run") else prev.get("last_run"))
    prev_real_success = prev.get("last_real_success") or (
        prev.get("last_run") if prev.get("success") and not prev.get("dry_run") else None
    )
    state = {
        "last_run": started.isoformat(),
        "duration_s": round(elapsed, 1),
        "success": success,
        "errors": errors,
        "dry_run": dry_run,
        "last_real_run": prev_real_run if dry_run else started.isoformat(),
        "last_real_success": prev_real_success if (dry_run or not success) else started.isoformat(),
    }
    _write_state(state)

    if success:
        _log(f"Backup complete in {elapsed:.1f}s")
        console.print(f"\n[green]Backup complete[/green] ({elapsed:.1f}s)")
    else:
        _log(f"Backup finished with {len(errors)} error(s)")
        console.print(f"\n[red]{len(errors)} error(s)[/red] -- check {BACKUP_LOG}")
        raise typer.Exit(1)


@app.command()
def status():
    """Show last backup result."""
    state = _read_state()
    if not isinstance(state, dict) or not state:
        console.print("[yellow]No backup has run yet.[/yellow] Run: sextant-backup now")
        return

    t = Table(show_header=False, box=None)
    t.add_column("key", style="dim")
    t.add_column("value")

    ok = state.get("success", False)
    dry = bool(state.get("dry_run", False))
    verdict = "[green]OK[/green]" if ok else "[red]FAILED[/red]"
    t.add_row("Status", verdict + (" [yellow](dry run, nothing transferred)[/yellow]" if dry else ""))
    t.add_row("Last run", str(state.get("last_run") or "unknown") + (" (dry run)" if dry else ""))
    t.add_row("Last real run", str(state.get("last_real_run") or "never"))
    t.add_row("Last real success", str(state.get("last_real_success") or "never"))
    t.add_row("Duration", f"{state.get('duration_s', '?')}s")
    t.add_row("Dry run", str(dry))

    errors = state.get("errors") or []
    if isinstance(errors, list) and errors:
        t.add_row("Errors", "\n".join(str(e) for e in errors))

    console.print(t)
    console.print(f"\nFull log: {BACKUP_LOG}")


@app.command(hidden=not PS_INSTALL.exists())  # the installer is not part of the public build
def install(
    time: str = typer.Option("02:00", help="Daily run time in HH:MM (24h)"),
):
    """Register the daily backup as a scheduled task (installer not shipped in the public build)."""
    if not PS_INSTALL.exists():
        # The installer script lives in the maintainer's private deploy tree;
        # the public distribution ships without it (P10 sysadmin persona,
        # 2026-10-16: the bare "not found" read as a broken install).
        console.print(
            "[yellow]The scheduled-task installer is not part of the public "
            "distribution.[/yellow]\n"
            "Run backups manually (`sextant-backup now`) or register your own "
            "Task Scheduler entry / cron job for that command."
        )
        raise typer.Exit(1)

    result = subprocess.run(
        [
            "powershell.exe",
            "-NonInteractive",
            "-ExecutionPolicy", "Bypass",
            "-File", str(PS_INSTALL),
            "-RunTime", time,
        ],
        text=True,
    )
    if result.returncode != 0:
        console.print("[red]Task Scheduler registration failed.[/red]")
        console.print("Tip: run this command from an elevated PowerShell prompt.")
        raise typer.Exit(1)
    console.print(f"[green]Scheduled task registered.[/green] Daily at {time}.")


if __name__ == "__main__":
    app()
