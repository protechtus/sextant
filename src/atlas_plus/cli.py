"""atlas-plus -- the namesake umbrella CLI.

Atlas ships ~30 focused console scripts (atlas-doctor, atlas-brain, atlas-tickets, ...).
This umbrella is deliberately thin: it answers "what is installed here and where do I
start?" -- version, key paths/ports, and discovery of every installed atlas-* command.
It does not duplicate any subsystem CLI. (The entry point existed since early pyproject
drafts but pointed at a module that was never built -- the namesake command was DOA on
install until 2026-07-02.)
"""

from __future__ import annotations

import os

import typer

from atlas_plus import __version__
from atlas_plus._console import utf8_stdout_when_piped as _utf8_when_piped

_utf8_when_piped()  # captured output keeps non-ASCII paths intact (AG1, pipeline sweep 2026-11-11)
from atlas_plus import paths as atlas_paths

app = typer.Typer(
    help="Umbrella command: version, overview, and discovery of the whole command family.",
    invoke_without_command=True,
    no_args_is_help=False,
)
class _LazyConsole:
    """Defer rich.console import to first print: rich costs ~69ms of the
    ~400ms first-touch (`sextant --version`) and half the commands never
    print through it (measured, quality loop 2026-09-27)."""

    _real = None

    def __getattr__(self, name):
        if _LazyConsole._real is None:
            from rich.console import Console
            _LazyConsole._real = Console()
        return getattr(_LazyConsole._real, name)


console = _LazyConsole()


def _dist_name() -> str:
    """The installed distribution's name (sextant-ai publicly, atlas-plus privately)."""
    # Two direct lookups instead of packages_distributions(), which reads every installed
    # package's RECORD (0.4 s on a 60-package venv, AG11 sweep 10).
    try:
        from importlib.metadata import PackageNotFoundError, distribution
        for name in ("sextant-ai", "atlas-plus"):
            try:
                distribution(name)
                return name
            except PackageNotFoundError:
                continue
    except Exception:  # noqa: BLE001
        pass
    return "atlas-plus"


def _brand() -> tuple[str, str]:
    """(display name, command prefix) based on how the CLI was invoked.

    The public install exposes sextant-* aliases; invoked as `sextant`, speak
    Sextant. Invoked as `atlas-plus` (the private/internal name), speak Keel.
    Same engine; the first touchpoint should not leak the other identity.
    """
    import sys
    from pathlib import Path

    invoked = Path(sys.argv[0]).stem.lower() if sys.argv else ""
    if invoked.startswith("sextant"):
        return "Sextant", "sextant"
    return "Keel", "atlas"


def _overview() -> None:
    """Print the at-a-glance overview (default action)."""
    name, prefix = _brand()
    doctor = "sextant-doctor" if prefix == "sextant" else "atlas-doctor"
    memory_root = str(atlas_paths.memory_root())
    state_dir = str(atlas_paths.state_dir())
    console.print(f"[bold]{name}[/bold] v{__version__} -- self-hosted memory + orchestration for your AI sessions")
    console.print(f"  memory root : {memory_root}")
    # The installed-and-tested-but-zero-corpus landing state gave no next
    # step (impatient persona, quality loop run 20) -- flag it right where
    # the user already is. Own line: appended to the path line, rich's
    # wrapping could split the hint at any path length (ubuntu CI caught it).
    try:
        corpus_missing = not any(atlas_paths.memory_root().iterdir())
    except (FileNotFoundError, NotADirectoryError, OSError):
        corpus_missing = True
    if corpus_missing:
        init_cmd = "sextant-init" if prefix == "sextant" else "atlas-init"
        console.print(f"  [yellow]no corpus yet -- run: {init_cmd}[/yellow]")
    console.print(f"  state dir   : {state_dir}")
    ports = service_ports()
    console.print(f"  services    : MCP :{ports['mcp']} | Web UI (Helm) :{ports['web']} | Brain :{ports['brain']} | Sync :{ports['sync']}")
    console.print(f"  health      : run [bold]{doctor}[/bold]")
    console.print(f"  components  : run [bold]{prefix if prefix == 'sextant' else 'atlas-plus'} components[/bold] -- what's here, what's running, what's optional")
    umbrella = prefix if prefix == "sextant" else "atlas-plus"
    console.print(f"  connect     : run [bold]{umbrella} setup[/bold] -- the MCP registration for this install, filled in; then [bold]{umbrella} verify[/bold]")
    # A package install (pipx, uv tool, PyPI) has no docs/ on disk: point at the published copies.
    from pathlib import Path as _P
    # The clone's docs/, found from the package (an editable install), not from the current
    # directory: a package user inside some other project's `docs/` was pointed at that (AG7).
    _clone_docs = _P(__file__).resolve().parents[2] / "docs"
    if (_clone_docs / "GETTING-STARTED.md").exists() or prefix != "sextant":
        console.print("  docs        : docs/GETTING-STARTED.md | docs/USAGE.md | docs/CONFIGURATION.md")
    else:
        console.print("  docs        : https://github.com/protechtus/sextant/blob/main/docs/GETTING-STARTED.md (USAGE.md, CONFIGURATION.md beside it)")


@app.callback()
def _main(
    ctx: typer.Context,
    version: bool = typer.Option(False, "--version", "-V", "-v", help="Print version and exit."),
) -> None:
    """Show the overview when invoked without a subcommand."""
    if version:
        _, prefix = _brand()
        typer.echo(f"{'sextant' if prefix == 'sextant' else _dist_name()} {__version__}")
        raise typer.Exit()
    if ctx.invoked_subcommand is None:
        _overview()


@app.command("commands")
def cmd_commands(
    json_output: bool = typer.Option(False, "--json", help="Emit JSON instead of a table."),
) -> None:
    """List every installed command (sextant-* and the atlas-* family) and its target."""
    import json as _json
    from importlib import metadata

    eps = {
        e.name: e.value
        for e in metadata.entry_points(group="console_scripts")
        if e.name.startswith(("atlas", "sextant"))
    }
    if json_output:
        typer.echo(_json.dumps(eps, indent=2, sort_keys=True))
        return
    # Brand-first: show the family matching how the user invoked us on top
    # (the public install's sextant-* aliases were previously HIDDEN by an
    # atlas-only filter -- the one discovery command concealed the very
    # commands the README teaches).
    _, prefix = _brand()
    first = {n: t for n, t in eps.items() if n.startswith(prefix)}
    rest = {n: t for n, t in eps.items() if not n.startswith(prefix)}
    for group, title in ((first, f"{prefix}-* commands ({len(first)})"),
                         (rest, f"also installed ({len(rest)})")):
        if not group:
            continue
        from rich.table import Table

        table = Table(title=title)
        table.add_column("command", style="bold", overflow="fold")
        table.add_column("target", style="dim", overflow="fold")  # fold, never an ellipsis (cp1252 consoles)
        for name in sorted(group):
            table.add_row(name, group[name])
        console.print(table)


# --- Guessable subcommands (2026-08-26, impatient-user review) ---------------
# Users guess `sextant init` / `sextant doctor` / `sextant search X` by
# convention long before they read that the family is hyphenated binaries.
# Every natural guess should LAND, not error: these delegate to the real
# entry points (extra args pass through untouched).


def _delegate(main_func, name: str, args: list[str]) -> None:
    import sys

    sys.argv = [name] + list(args)
    main_func()


# help_option_names=[]: `sextant import --help` must reach the real importer's help instead of
# being eaten by the umbrella (persona pass 2026-11-10, "Marco").
_PASSTHROUGH = {"context_settings": {"allow_extra_args": True, "ignore_unknown_options": True,
                                     "help_option_names": []}}


@app.command("init", **_PASSTHROUGH)
def cmd_init(ctx: typer.Context) -> None:
    """Scaffold a fresh memory corpus (same as sextant-init)."""
    from atlas_plus.init_cmd import main as init_main

    _delegate(init_main, "sextant init", ctx.args)


@app.command("doctor", **_PASSTHROUGH)
def cmd_doctor(ctx: typer.Context) -> None:
    """Health checks (same as sextant-doctor)."""
    from atlas_plus.doctor.cli import main as doctor_main

    _delegate(doctor_main, "sextant doctor", ctx.args)


@app.command("import", **_PASSTHROUGH)
def cmd_import(ctx: typer.Context) -> None:
    """Import existing knowledge (same as sextant-import)."""
    from atlas_plus.import_cmd import main as import_main

    _delegate(import_main, "sextant import", ctx.args)


@app.command("sync", **_PASSTHROUGH)
def cmd_sync(ctx: typer.Context) -> None:
    """Device sync (same as sextant-sync)."""
    from atlas_plus.sync.cli import main as sync_main

    _delegate(sync_main, "sextant sync", ctx.args)


@app.command("ingest", **_PASSTHROUGH)
def cmd_ingest(ctx: typer.Context) -> None:
    """Ingest dropped transcripts (same as sextant-transcripts ingest)."""
    # The natural guess after reading about the drop dir or Sextant Clip is
    # `sextant ingest` -- it previously errored with "did you mean init?"
    # (impatient-persona finding, quality loop run 4).
    import sys as _sys

    from atlas_plus.transcripts.cli import app as _tapp

    _sys.argv = ["sextant-transcripts", "ingest", *ctx.args]
    _tapp()


@app.command("review", **_PASSTHROUGH)
def cmd_review(ctx: typer.Context) -> None:
    """Curate the corpus (same as atlas-review-memory): `sextant review report` is the weekly check."""
    from atlas_plus.curation.cli import main as review_main

    _delegate(review_main, "sextant review", ctx.args)


@app.command("versions", **_PASSTHROUGH)
def cmd_versions(ctx: typer.Context) -> None:
    """Report the corpus history (versions/) and prune old copies (same as sextant-versions)."""
    from atlas_plus.versions_cmd import main as versions_main

    _delegate(versions_main, "sextant versions", ctx.args)


@app.command("uninstall", **_PASSTHROUGH)
def cmd_uninstall(ctx: typer.Context) -> None:
    """Report or remove Sextant's data (same as sextant-uninstall)."""
    from atlas_plus.uninstall_cmd import main as uninstall_main

    _delegate(uninstall_main, "sextant uninstall", ctx.args)


@app.command("reset", **_PASSTHROUGH)
def cmd_reset(ctx: typer.Context) -> None:
    """Wipe + re-scaffold an area (same as sextant-reset)."""
    from atlas_plus.reset_cmd import main as reset_main

    _delegate(reset_main, "sextant reset", ctx.args)


@app.command("verify", **_PASSTHROUGH)
def cmd_verify(ctx: typer.Context) -> None:
    """Prove the install: start the MCP server over stdio, write one fact, search it back."""
    from atlas_plus.roundtrip import main as roundtrip_main

    raise typer.Exit(roundtrip_main(list(ctx.args)))


@app.command("setup", **_PASSTHROUGH)
def cmd_setup(ctx: typer.Context) -> None:
    """Print the MCP registration and hook entries for THIS install (writes nothing)."""
    from atlas_plus.setup_cmd import main as setup_main

    raise typer.Exit(setup_main(list(ctx.args)))


@app.command("mcp", hidden=True, **_PASSTHROUGH)
def cmd_mcp(ctx: typer.Context) -> None:
    """Alias of `setup` (the natural guess for "how do I connect my client")."""
    from atlas_plus.setup_cmd import main as setup_main

    raise typer.Exit(setup_main(list(ctx.args)))


@app.command("connect", hidden=True, **_PASSTHROUGH)
def cmd_connect(ctx: typer.Context) -> None:
    """Alias of `setup`."""
    from atlas_plus.setup_cmd import main as setup_main

    raise typer.Exit(setup_main(list(ctx.args)))


@app.command("status")
def cmd_status() -> None:
    """Alias of `components`: what's here, what's running, what's optional."""
    cmd_components(json_output=False)


@app.command("help")
def cmd_help() -> None:
    """Where everything is (for `sextant help` guessers)."""
    _overview()
    console.print("\n[dim]Full option help: sextant --help. All commands: sextant commands.[/dim]")


@app.command("dashboard", hidden=True, **_PASSTHROUGH)
def cmd_dashboard(ctx: typer.Context) -> None:
    """Start the web dashboard (same as sextant-web; needs ATLAS_WEB_API_KEY)."""
    # Three strangers in a row typed `sextant web` and got a pointer (sweep 11): it starts now.
    console.print("Starting the dashboard (same as sextant-web; ATLAS_WEB_HOST and ATLAS_WEB_PORT pick the "
                  "bind, default 127.0.0.1:8766; the page loads htmx and Tailwind from the internet).")
    from atlas_plus.web.app import main as web_main

    _delegate(web_main, "sextant web", ctx.args)


@app.command("web", **_PASSTHROUGH)
def cmd_web(ctx: typer.Context) -> None:
    """Alias of `dashboard`."""
    cmd_dashboard(ctx)


@app.command("context", **_PASSTHROUGH)
def cmd_context(ctx: typer.Context) -> None:
    """Print the session priming text (same as sextant-session-start); with words, the task-matched
    facts (same as atlas-prime "<task words>")."""
    if ctx.args:
        from atlas_plus.hooks.prime import main as prime_main

        _delegate(prime_main, "sextant context", ctx.args)
        return
    from atlas_plus.hooks.session_start import main as session_main

    _delegate(session_main, "sextant context", [])


@app.command("prime", hidden=True, **_PASSTHROUGH)
def cmd_prime(ctx: typer.Context) -> None:
    """Alias of `context`."""
    cmd_context(ctx)


@app.command("serve", hidden=True)
def cmd_serve() -> None:
    """Which command serves what (hint only)."""
    console.print("The dashboard: [bold]sextant web[/bold] (same as sextant-web; needs ATLAS_WEB_API_KEY). "
                  "MCP server: [bold]sextant-mcp[/bold]. Brain: [bold]sextant-brain[/bold]. All: sextant commands.")


@app.command("add", hidden=True)
def cmd_add(
    fact: str = typer.Argument(..., help="One fact, a sentence or two; `-` reads it from standard input "
                                         "(for quotes a shell would eat: PowerShell 5.1)."),
    scope: str = typer.Option("reference:notes", "--scope"),
) -> None:
    """Alias of `remember`."""
    cmd_remember(fact=fact, scope=scope)


@app.command("remember")
def cmd_remember(
    fact: str = typer.Argument(..., help="One fact, a sentence or two; `-` reads it from standard input "
                                         "(for quotes a shell would eat: PowerShell 5.1)."),
    scope: str = typer.Option(
        "reference:notes", "--scope",
        help="Where it goes: identity | preference:<topic> | project:<name> | reference:<topic> "
             "(default reference:notes).",
    ),
) -> None:
    """Store a fact from the shell (same corpus your assistant reads)."""
    # A newcomer could search but not write without a client (persona pass 2026-11-10);
    # this is the human-direct counterpart of the memory_remember MCP tool.
    from atlas_plus.mcp.memory_server.tools.remember import memory_remember

    if fact == "-":
        import sys as _sys
        raw_in = _sys.stdin.buffer.read() if hasattr(_sys.stdin, "buffer") else _sys.stdin.read().encode("utf-8", "replace")
        fact = raw_in.decode("utf-8", errors="replace").lstrip("\ufeff").strip()
        if not fact:
            console.print("[red]Not stored:[/red] nothing on standard input")
            raise typer.Exit(2)
    try:
        result = memory_remember(scope, fact, source="human-direct", interactive=True)
    except RuntimeError as exc:  # the write kill switch (ATLAS_INTERACTIVE_WRITES=off)
        console.print(f"[red]Not stored:[/red] {exc}")
        raise typer.Exit(1)
    if not result.get("ok"):
        console.print(f"[red]Not stored:[/red] {result.get('error')}")
        raise typer.Exit(1)
    console.print(f"Remembered in {result['file']}")
    if result.get("possible_duplicate"):
        console.print(f"[yellow]{result.get('duplicate_hint')}[/yellow]")
    if result.get("anti_warning"):
        console.print(f"[yellow]{result.get('anti_warning')}[/yellow]")


@app.command("search")
def cmd_search(
    query: str = typer.Argument(..., help="What to look for (meaning-aware when the semantic index exists)."),
    scope: str = typer.Option("", "--scope", help="Restrict to a corpus subdirectory (reference) or a write scope (project:<name>, reference:<topic>)."),
    limit: int = typer.Option(10, "--limit", "-n"),
) -> None:
    """Search your memory from the shell (hybrid keyword + semantic)."""
    if not query.strip():
        console.print("[red]Give a query:[/red] sextant search \"<words>\" (an empty query finds nothing).")
        raise typer.Exit(2)
    from atlas_plus.mcp.memory_server.tools.search import (
        confidence_band, memory_search_hybrid, normalize_search_scope,
    )

    # The scope grammar the docs show for writes (project:<name>) works here too
    # (persona pass 2026-11-10, "Priya": it silently matched nothing).
    effective = normalize_search_scope(scope)
    if scope.strip() and not effective:
        console.print(f"[yellow]scope {scope!r} not understood (grammar: reference, project:<name>, "
                      "reference:<topic>, preference:<topic>); searching everything[/yellow]")
    results, mode = memory_search_hybrid(query=query, scope=effective, limit=limit)
    if not results:
        try:
            _root = atlas_paths.memory_root()
            corpus_files = sum(1 for p in _root.rglob("*.md")
                               if not {"versions", "_atlas", "archive", "_sync"} & set(p.relative_to(_root).parts))
        except OSError:
            corpus_files = 0
        if corpus_files == 0:
            console.print(
                f"[yellow]No hits[/yellow] for {query!r} ({mode}). "
                "An empty corpus has nothing to find yet -- store something first "
                "(sextant remember \"...\" --scope reference:<topic>, ask your assistant, or edit the markdown)."
            )
        else:
            console.print(
                f"[yellow]No hits[/yellow] for {query!r} ({mode}) in {corpus_files} file(s)"
                + (f" under scope {scope!r}" if scope else "")
                + ". Keyword search (fts-only) needs a word that appears in the note; the semantic "
                "index finds meaning but needs Ollama running and `sextant-embed-index` once."
            )
        raise typer.Exit(0)
    console.print(f"[dim]{len(results)} hit(s), retrieval: {mode}, confidence: {confidence_band(results)}[/dim]")
    from rich.markup import escape

    for r in results:
        # Escape BOTH fields: the FTS snippet marks matches as [term], which
        # Rich would swallow as unknown tags (matched words silently vanished
        # -- stranger-test batch 9), and corpus content itself may carry
        # [red]-style markup an author never meant to be interpreted.
        snippet = "".join(c for c in (r.get("snippet") or "").replace("\n", " ")[:120]
                          if c >= " " or c == "\t")   # no ESC/BEL from a stored note reaches the terminal (AG10)
        console.print(f"  [bold]{escape(r['path'])}[/bold]  [dim]{escape(snippet)}[/dim]")


def service_ports() -> dict[str, int]:
    """The ports this install is CONFIGURED for (env), not the defaults: `components` said the
    dashboard was running on :8766 while the user's ran on :47598 (persona pass 2026-11-10)."""
    return {
        "mcp": atlas_paths.env_int("ATLAS_MCP_PORT", 8765),
        "web": atlas_paths.env_int("ATLAS_WEB_PORT", 8766),
        "brain": atlas_paths.env_int("ATLAS_BRAIN_PORT", 8767),
        "sync": atlas_paths.env_int("ATLAS_SYNC_PORT", 8768),
    }


def _port_up(port: int, timeout: float = 0.4) -> bool:
    import socket

    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False


def gather_components(probe_ports: bool = True) -> list[dict]:
    """The control-panel answer: every component, its status, its off switch.

    Pure data (rendered by `components`); each entry:
    {name, what, status, disable} where status is one of
    core | running | not running | ready | disabled | not configured.
    """
    # One concurrent sweep instead of four serial connects: measured 921 ms
    # -> 404 ms for the probe phase on Windows loopback (quality loop run 5;
    # now bounded by the slowest single refused connect, not the sum).
    up: dict[int, bool] = {}
    ports = service_ports()
    if probe_ports:
        from concurrent.futures import ThreadPoolExecutor

        probe = tuple(ports.values())
        with ThreadPoolExecutor(max_workers=len(probe)) as ex:
            up = dict(zip(probe, ex.map(_port_up, probe)))

    out: list[dict] = []
    mem = atlas_paths.memory_root()
    corpus_files = sum(1 for _ in mem.rglob("*.md")) if mem.exists() else 0
    out.append({
        "name": "Memory corpus",
        "what": f"your knowledge as plain markdown ({corpus_files} file(s) at {mem})",
        "status": "core" if corpus_files else "not configured",
        "disable": "cannot be disabled -- it IS the product (init: sextant-init)",
    })
    out.append({
        "name": "MCP memory server",
        "what": f"exposes the corpus to Claude Code / any MCP client (:{ports['mcp']})",
        "status": ("running" if up.get(ports["mcp"]) else "not running"),
        "disable": "optional -- just don't run it; CLI + files keep working",
    })
    out.append({
        "name": "Keyword search (FTS)",
        "what": "built-in full-text index, rebuilt automatically",
        "status": "core",
        "disable": "always on; rebuild any time: atlas-fts-rebuild",
    })
    sem_status = "disabled"
    sem_detail = "set ATLAS_SEMANTIC_SEARCH=off"
    if os.environ.get("ATLAS_SEMANTIC_SEARCH", "").strip().lower() != "off":
        try:
            from atlas_plus.mcp.memory_server.embeddings import index_status

            st = index_status()
            sem_status = "ready" if st.get("indexed") else "not configured"
            sem_detail = (
                f"{st.get('indexed', 0)} file(s) embedded"
                if st.get("indexed")
                else "needs Ollama + `sextant-embed-index` once; falls back to keyword search"
            )
        except Exception:
            sem_status = "not configured"
    out.append({
        "name": "Semantic search",
        "what": f"meaning-aware search via local embeddings ({sem_detail})",
        "status": sem_status,
        "disable": "safe to disable: ATLAS_SEMANTIC_SEARCH=off (keyword search remains)",
    })
    out.append({
        "name": "Web dashboard (Helm)",
        "what": f"browser view of tasks/health (:{ports['web']})",
        "status": ("running" if up.get(ports["web"]) else "not running"),
        "disable": "optional -- don't run it and nothing else changes",
    })
    out.append({
        "name": "Brain + nightly jobs",
        "what": f"local task queue + memory hygiene through Ollama (:{ports['brain']})",
        "status": ("running" if up.get(ports["brain"]) else "not running"),
        "disable": "optional -- without it, no background jobs run; everything else works",
    })
    sync_paired = 0
    try:
        from atlas_plus.sync import devices as sync_devices

        sync_paired = sum(1 for d in sync_devices.load_devices() if not d.revoked)
    except Exception:
        pass
    out.append({
        "name": "Device sync",
        "what": f"hub/spoke corpus sync (:{ports['sync']}; {sync_paired} paired device(s))",
        "status": ("running" if up.get(ports["sync"]) else ("ready" if sync_paired else "not configured")),
        "disable": "optional -- single-machine use needs none of it",
    })
    out.append({
        "name": "Telegram bot",
        "what": "mobile control surface",
        "status": ("ready" if (os.environ.get("ATLAS_TELEGRAM_BOT_TOKEN") or os.environ.get("TELEGRAM_BOT_TOKEN"))
                   else "not configured"),
        "disable": "optional -- unset ATLAS_TELEGRAM_BOT_TOKEN / don't run the bot",
    })
    from atlas_plus.autonomous import autonomous_enabled
    auto = autonomous_enabled()
    out.append({
        "name": "Autonomous writes",
        "what": "master switch for background jobs WRITING to your corpus",
        "status": "ready" if auto else "disabled",
        "disable": "off by default on purpose; enable deliberately: ATLAS_AUTONOMOUS=on",
    })
    return out


_STATUS_STYLE = {
    "core": "bold green", "running": "green", "ready": "cyan",
    "not running": "yellow", "not configured": "dim", "disabled": "dim",
}


@app.command("components")
def cmd_components(
    json_output: bool = typer.Option(False, "--json", help="Emit JSON instead of a table."),
) -> None:
    """What's on this machine, what's running, and what's safe to turn off."""
    comps = gather_components()
    if json_output:
        import json as _json

        typer.echo(_json.dumps(comps, indent=2))
        return
    name, _ = _brand()
    from rich.table import Table

    table = Table(title=f"{name} components")
    table.add_column("component", style="bold")
    table.add_column("status")
    table.add_column("what it is", overflow="fold")
    table.add_column("off switch / notes", overflow="fold", style="dim")
    for c in comps:
        style = _STATUS_STYLE.get(c["status"], "")
        table.add_row(c["name"], f"[{style}]{c['status']}[/{style}]", c["what"], c["disable"])
    console.print(table)
    console.print(
        "[dim]'running' = something answers on that loopback port; another install on this "
        "machine shows here too. Ports come from ATLAS_*_PORT.[/dim]"
    )
    console.print(
        "[dim]Only the memory corpus is essential. Every service is opt-in: "
        "it is 'on' exactly while you run it, and stopping it never corrupts "
        "anything -- indexes rebuild, and your corpus is plain files.[/dim]"
    )


def main() -> None:
    """Console entry: a closed pipe (`sextant search ... | head`) ends quietly instead of a Rich
    traceback and exit 120 (MN15, sweep 11)."""
    import sys as _sys
    try:
        app()
    except OSError as exc:
        # Only a stream error with no path behind it is a closed pipe; a failed init on a bad
        # path raised EINVAL too and exited 0 in silence (AG22, sweep 12).
        if exc.filename is None and (exc.errno in (22, 32) or exc.__class__.__name__ == "BrokenPipeError"):
            try:
                _sys.stdout.close()
            except Exception:  # noqa: BLE001
                pass
            raise SystemExit(0)
        raise


if __name__ == "__main__":
    main()
