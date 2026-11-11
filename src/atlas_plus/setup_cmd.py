"""`sextant setup`: print the MCP registration and hook entries for THIS install.

Every earlier walkthrough had the human (or their agent) assemble the command by hand from
placeholders: the venv path, the platform's interpreter layout, the ATLAS_* variables that
must be baked into the registration, and JSON escaping for the hook files. A package install
(pipx, uv tool, pip from PyPI or a git URL) has no clone and therefore none of the deploy/
snippets at all (sweep 2026-11-11, package-user persona). This command knows all of that about
the running interpreter and prints it filled in. It writes nothing: the human merges or runs
what it shows.

    sextant setup                         everything, as text with instructions
    sextant setup --client claude-code    just the one-line registration
    sextant setup --client claude-desktop just the JSON block (also: cursor, json)
    sextant setup --client hooks          just the hooks block for settings.json
    sextant setup --json                  machine-readable (an agent pastes the pieces)

Quoting is the whole point, so it is done per platform: `shlex.quote` on POSIX (a path with
`$`, backticks or a quote arrives verbatim and never executes), double quotes on Windows with
the two cases cmd cannot carry refused (a quote or a newline inside a value) and a trailing
backslash doubled so it does not escape the closing quote (adversarial review, 2026-11-11).
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import sys
from pathlib import Path

SERVER_MODULE = "atlas_plus.mcp.memory_server.server"
SERVER_NAME = "sextant"
#: The variables the MCP SERVER reads to decide where the corpus and its state live. When any
#: is set in the environment the registration must carry it, or the server (launched by a GUI
#: client with no shell) falls back to the home-directory defaults and silently serves the
#: wrong corpus.
PATH_VARS = ("ATLAS_WORKSPACE_ROOT", "ATLAS_MEMORY_ROOT", "ATLAS_STATE_DIR", "ATLAS_PROJECTS_ROOT",
             "ATLAS_UPDATES_FILE")
#: Variables only the HOOK processes read (identity file, display name, clock anchor). They go
#: on the POSIX hook command line, not into the server registration.
HOOK_VARS = PATH_VARS + ("ATLAS_IDENTITY_PATH", "ATLAS_USER_NAME", "ATLAS_HOME_TZ")


class UnquotableValue(ValueError):
    """A value that no quoting rule on this platform can carry verbatim."""


def interpreter() -> str:
    """The interpreter that has the package: a clone venv, a pipx or uv tool venv, anything."""
    return str(Path(sys.executable))


def _collect(names: tuple[str, ...], environ: dict | None = None) -> dict[str, str]:
    env = os.environ if environ is None else environ
    return {k: env[k] for k in names if env.get(k, "").strip()}


def path_env(environ: dict | None = None) -> dict[str, str]:
    return _collect(PATH_VARS, environ)


def hook_env(environ: dict | None = None) -> dict[str, str]:
    return _collect(HOOK_VARS, environ)


def _q(value: str, posix: bool | None = None) -> str:
    """Quote one argument for the shell the printed command will run in.

    POSIX: shlex.quote (single quotes; metacharacters never expand). Windows: double quotes;
    a value containing a double quote or a newline cannot be carried by cmd or by the argv
    rules node uses, so it is refused with a reason instead of printed broken; a trailing
    backslash is doubled so it does not escape the closing quote (`D:\\` -> `"D:\\\\"`).
    """
    posix = (os.name != "nt") if posix is None else posix
    if posix:
        return shlex.quote(value)
    if '"' in value or "\n" in value or "\r" in value:
        raise UnquotableValue(f"a value with a quote or a line break cannot be passed on Windows: {value!r}")
    if value.endswith("\\"):
        value = value + "\\"
    return f'"{value}"'


def wsl_distro(environ: dict | None = None) -> str | None:
    env = os.environ if environ is None else environ
    return env.get("WSL_DISTRO_NAME") or None


def claude_code_command(python: str, env: dict[str, str], name: str = SERVER_NAME,
                        posix: bool | None = None) -> str:
    parts = ["claude", "mcp", "add", "-s", "user", name]
    for k, v in env.items():
        parts += ["-e", _q(f"{k}={v}", posix)]
    parts += ["--", _q(python, posix), "-m", SERVER_MODULE, "--stdio"]
    return " ".join(parts)


def claude_code_command_from_windows(python: str, env: dict[str, str], distro: str,
                                     name: str = SERVER_NAME, existing_wslenv: str = "") -> str:
    """Windows Claude Code reaching a WSL install: variables cross only when WSLENV lists them.

    Linux-path values need no `/p` flag (they are used verbatim inside WSL). An existing WSLENV
    is kept, so the user's own entries survive the registration.
    """
    parts = ["claude", "mcp", "add", "-s", "user", name]
    for k, v in env.items():
        parts += ["-e", _q(f"{k}={v}", posix=False)]
    if env:
        names = list(env)
        wslenv = ":".join(([existing_wslenv] if existing_wslenv else []) + names)
        parts += ["-e", _q(f"WSLENV={wslenv}", posix=False)]
    parts += ["--", "wsl", "-d", _q(distro, posix=False), "--", _q(python, posix=True),
              "-m", SERVER_MODULE, "--stdio"]
    return " ".join(parts)


def mcp_servers_json(python: str, env: dict[str, str], name: str = SERVER_NAME) -> dict:
    entry: dict = {"type": "stdio", "command": python, "args": ["-m", SERVER_MODULE, "--stdio"]}
    if env:
        entry["env"] = dict(env)
    return {"mcpServers": {name: entry}}


def hooks_json(python: str, env: dict[str, str], posix: bool | None = None) -> dict:
    """SessionStart priming and the Stop summarizer, with this interpreter.

    Hooks are separate processes: the env block of an MCP registration never reaches them.
    On POSIX the variables ride on the command line (quoted); on Windows the command runs
    through cmd, so set them user-wide (setx) instead, as the text output says.
    """
    posix = (os.name != "nt") if posix is None else posix
    prefix = ""
    if env and posix:
        prefix = " ".join(f"{k}={_q(v, True)}" for k, v in env.items()) + " "
    start = f"{prefix}{_q(python, posix)} -m atlas_plus.hooks.session_start"
    stop = f"{prefix}{_q(python, posix)} -m atlas_plus.summarizer.hook_entry"
    return {"hooks": {
        "SessionStart": [{"matcher": "", "hooks": [{"type": "command", "command": start}]}],
        "Stop": [{"matcher": "", "hooks": [{"type": "command", "command": stop}]}],
    }}


def describe(environ: dict | None = None) -> dict:
    """Everything `setup` knows, as data (the --json output). Never raises on a bad value: the
    piece that cannot be quoted carries an `error` and the others are still printed."""
    from atlas_plus import paths as atlas_paths

    env = path_env(environ)
    henv = hook_env(environ)
    py = interpreter()
    distro = wsl_distro(environ)
    verify = f"{py} -m atlas_plus verify" if " " not in py else f'"{py}" -m atlas_plus verify'
    out: dict = {
        "interpreter": py,
        "platform": "windows" if os.name == "nt" else ("wsl" if distro else "posix"),
        "memory_root": str(atlas_paths.memory_root()),
        "state_dir": str(atlas_paths.state_dir()),
        "env": env,
        "hook_env": henv,
        "client_config_json": mcp_servers_json(py, env),
        "verify": verify,
    }
    try:
        out["claude_code"] = claude_code_command(py, env)
    except UnquotableValue as exc:
        out["claude_code"] = None
        out["error"] = str(exc)
    try:
        out["settings_json"] = hooks_json(py, henv)
    except UnquotableValue as exc:
        out["settings_json"] = None
        out["error"] = str(exc)
    # A server has no Claude Code: remote clients reach HTTP mode behind an ingress.
    out["http"] = {
        "serve": f"ATLAS_MCP_TOKEN=<long random> {verify.split(' -m ')[0]} -m {SERVER_MODULE}",
        "remote_claude_code": 'claude mcp add --transport http sextant https://<your-host>/mcp '
                              '--header "Authorization: Bearer <that token>"',
        "docs": "docs/HOWTO-self-hosting.md",
    }
    import shutil as _shutil
    out["claude_on_path"] = _shutil.which("claude") is not None
    if distro:
        out["wsl_distro"] = distro
        try:
            out["claude_code_from_windows"] = claude_code_command_from_windows(
                py, env, distro, existing_wslenv=(os.environ if environ is None else environ).get("WSLENV", ""))
        except UnquotableValue as exc:
            out["claude_code_from_windows"] = None
            out["error"] = str(exc)
    return out


def render(info: dict, client: str = "all") -> str:
    env = info["env"]
    lines: list[str] = []
    if info.get("error"):
        lines += [f"CANNOT PRINT ONE PIECE: {info['error']}", "   (move the corpus to a plainer path, "
                  "or register by hand with the JSON block, which needs no shell quoting)", ""]
    if client == "all":
        lines += [
            "Sextant setup for this install (nothing is written; run or merge what follows)",
            f"  interpreter : {info['interpreter']}",
            f"  memory root : {info['memory_root']}",
            f"  state dir   : {info['state_dir']}",
        ]
        if env:
            lines.append("  baked in    : " + ", ".join(f"{k}={v}" for k, v in env.items()))
        else:
            lines.append("  baked in    : nothing (home-directory defaults; set ATLAS_MEMORY_ROOT "
                         "or ATLAS_WORKSPACE_ROOT first to relocate, then run setup again)")
        lines.append("")
    if client in ("all", "claude-code"):
        if client == "all":
            lines.append("1. Claude Code (one command, any directory; then `claude mcp list`, "
                         "then restart Claude Code):")
        if info.get("claude_code"):
            lines += ["   " + info["claude_code"], ""]
        if info.get("claude_code_from_windows"):
            lines += [f"   Windows Claude Code reaching this WSL install ({info['wsl_distro']}); "
                      "run it in PowerShell on the Windows side:",
                      "   " + info["claude_code_from_windows"], ""]
    if client in ("all", "json"):
        if client == "all":
            lines.append("2. Claude Desktop, Cursor, or any MCP client that takes JSON: merge "
                         "this `mcpServers` entry into the client's config")
            lines.append("   (Claude Desktop: %APPDATA%\\Claude\\claude_desktop_config.json on "
                         "Windows, ~/Library/Application Support/Claude/claude_desktop_config.json "
                         "on macOS; keep any servers already there):")
        lines += [json.dumps(info["client_config_json"], indent=2), ""]
    if client in ("all", "hooks"):
        if client == "all":
            lines.append("3. Claude Code hooks, optional (session-start priming, end-of-session "
                         "summary): merge into ~/.claude/settings.json, ADDING to any existing "
                         "hooks list. Ask before editing someone else's settings.")
            if info.get("hook_env") and os.name == "nt":
                lines.append("   Hooks run outside your shell: set the baked-in variables above "
                             "user-wide first (setx NAME value) or the hooks read the home defaults.")
            lines.append("   The session-start hook prints the corpus it read; the Stop hook writes "
                         "nothing until ATLAS_AUTONOMOUS=on.")
        if info.get("settings_json"):
            lines += [json.dumps(info["settings_json"], indent=2), ""]
    if client == "all":
        h = info.get("http") or {}
        if h:
            lines.append("4. No Claude Code on this machine, or clients elsewhere (a server): run HTTP "
                         "mode behind your ingress and register the remote client against it:")
            if not info.get("claude_on_path"):
                lines.append("   (`claude` is not on PATH here, so section 1 applies on the client machine)")
            lines += ["   " + h["serve"] + "   (binds 127.0.0.1:8765; the token is required)",
                      "   " + h["remote_claude_code"],
                      f"   Full server setup (systemd, Caddy, Tailscale): {h['docs']}", ""]
        lines.append(f"Verify: {info['verify']}   (starts the server over stdio, writes one fact under "
                     "project:demo, searches it back)")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="sextant setup",
        description="Print the MCP registration and hook entries for this install, filled in.",
    )
    ap.add_argument("--client", choices=("all", "claude-code", "json", "claude-desktop", "cursor", "hooks"),
                    default="all",
                    help="which piece to print (default: all, with instructions; claude-desktop and "
                         "cursor are the json block)")
    ap.add_argument("--json", action="store_true", help="machine-readable output of everything")
    args = ap.parse_args(argv)
    if args.client in ("claude-desktop", "cursor"):
        args.client = "json"
    info = describe()
    if args.json:
        print(json.dumps(info, indent=2))
    else:
        print(render(info, args.client))
    return 1 if info.get("error") else 0


if __name__ == "__main__":
    sys.exit(main())
