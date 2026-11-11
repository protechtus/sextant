"""`sextant setup`: print the MCP registration and hook entries for THIS install.

Every earlier walkthrough had the human (or their agent) assemble the command by hand from
placeholders: the venv path, the platform's interpreter layout, the ATLAS_* variables that
must be baked into the registration, and JSON escaping for the hook files. A package install
(pipx, uv tool, pip from PyPI) has no clone and therefore none of the deploy/ snippets at all
(sweep 2026-11-11, package-user persona). This command knows all of that about the running
interpreter and prints it filled in. It writes nothing: the human merges or runs what it shows.

    sextant setup                     everything, as text with instructions
    sextant setup --client claude-code   just the one-line registration
    sextant setup --json              machine-readable (an agent pastes the pieces)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

SERVER_MODULE = "atlas_plus.mcp.memory_server.server"
SERVER_NAME = "sextant"
#: The variables that decide where the corpus lives. When any is set in the environment the
#: registration must carry it, or the server (launched by a GUI client with no shell) falls
#: back to the home-directory defaults and silently serves the wrong corpus.
PATH_VARS = ("ATLAS_WORKSPACE_ROOT", "ATLAS_MEMORY_ROOT", "ATLAS_STATE_DIR", "ATLAS_PROJECTS_ROOT")


def interpreter() -> str:
    """The interpreter that has the package: a clone venv, a pipx or uv tool venv, anything."""
    return str(Path(sys.executable))


def path_env(environ: dict | None = None) -> dict[str, str]:
    env = os.environ if environ is None else environ
    return {k: env[k] for k in PATH_VARS if env.get(k, "").strip()}


def _q(value: str) -> str:
    """Double quotes survive PowerShell, cmd, bash and zsh alike; a path with a space needs them."""
    return f'"{value}"'


def wsl_distro(environ: dict | None = None) -> str | None:
    env = os.environ if environ is None else environ
    return env.get("WSL_DISTRO_NAME") or None


def claude_code_command(python: str, env: dict[str, str], name: str = SERVER_NAME) -> str:
    parts = ["claude", "mcp", "add", "-s", "user", name]
    for k, v in env.items():
        parts += ["-e", _q(f"{k}={v}")]
    parts += ["--", _q(python), "-m", SERVER_MODULE, "--stdio"]
    return " ".join(parts)


def claude_code_command_from_windows(python: str, env: dict[str, str], distro: str,
                                     name: str = SERVER_NAME) -> str:
    """Windows Claude Code reaching a WSL install: variables cross only when WSLENV lists them."""
    parts = ["claude", "mcp", "add", "-s", "user", name]
    for k, v in env.items():
        parts += ["-e", _q(f"{k}={v}")]
    if env:
        parts += ["-e", _q("WSLENV=" + ":".join(env))]
    parts += ["--", "wsl", "-d", distro, "--", python, "-m", SERVER_MODULE, "--stdio"]
    return " ".join(parts)


def mcp_servers_json(python: str, env: dict[str, str], name: str = SERVER_NAME) -> dict:
    entry: dict = {"type": "stdio", "command": python, "args": ["-m", SERVER_MODULE, "--stdio"]}
    if env:
        entry["env"] = dict(env)
    return {"mcpServers": {name: entry}}


def hooks_json(python: str, env: dict[str, str]) -> dict:
    """SessionStart priming and the Stop summarizer, with this interpreter.

    Hooks are separate processes: the env block of an MCP registration never reaches them.
    On POSIX the variables ride on the command line; on Windows the command runs through cmd,
    so set them user-wide (setx) instead, as the text output says.
    """
    prefix = ""
    if env and os.name != "nt":
        prefix = " ".join(f"{k}={_q(v)}" for k, v in env.items()) + " "
    start = f'{prefix}{_q(python)} -m atlas_plus.hooks.session_start'
    stop = f'{prefix}{_q(python)} -m atlas_plus.summarizer.hook_entry'
    return {"hooks": {
        "SessionStart": [{"matcher": "", "hooks": [{"type": "command", "command": start}]}],
        "Stop": [{"matcher": "", "hooks": [{"type": "command", "command": stop}]}],
    }}


def describe(environ: dict | None = None) -> dict:
    """Everything `setup` knows, as data (the --json output)."""
    from atlas_plus import paths as atlas_paths

    env = path_env(environ)
    py = interpreter()
    distro = wsl_distro(environ)
    out: dict = {
        "interpreter": py,
        "platform": "windows" if os.name == "nt" else ("wsl" if distro else "posix"),
        "memory_root": str(atlas_paths.memory_root()),
        "state_dir": str(atlas_paths.state_dir()),
        "env": env,
        "claude_code": claude_code_command(py, env),
        "mcp_servers": mcp_servers_json(py, env),
        "hooks": hooks_json(py, env),
        "verify": "sextant verify",
    }
    if distro:
        out["wsl_distro"] = distro
        out["claude_code_from_windows"] = claude_code_command_from_windows(py, env, distro)
    return out


def render(info: dict, client: str = "all") -> str:
    env = info["env"]
    lines: list[str] = []
    if client in ("all",):
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
        lines += [json.dumps(info["mcp_servers"], indent=2), ""]
    if client in ("all", "hooks"):
        if client == "all":
            lines.append("3. Claude Code hooks, optional (session-start priming, end-of-session "
                         "summary): merge into ~/.claude/settings.json, ADDING to any existing "
                         "hooks list. Ask before editing someone else's settings.")
            if env and os.name == "nt":
                lines.append("   Hooks run outside your shell: set the baked-in variables above "
                             "user-wide first (setx NAME value) or the hooks read the home defaults.")
            lines.append("   The Stop hook writes nothing until ATLAS_AUTONOMOUS=on.")
        lines += [json.dumps(info["hooks"], indent=2), ""]
    if client == "all":
        lines.append("Verify: sextant verify   (starts the server over stdio, writes one fact, "
                     "searches it back)")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="sextant setup",
        description="Print the MCP registration and hook entries for this install, filled in.",
    )
    ap.add_argument("--client", choices=("all", "claude-code", "json", "hooks"), default="all",
                    help="which piece to print (default: all, with instructions)")
    ap.add_argument("--json", action="store_true", help="machine-readable output of everything")
    args = ap.parse_args(argv)
    info = describe()
    if args.json:
        print(json.dumps(info, indent=2))
    else:
        print(render(info, args.client))
    return 0


if __name__ == "__main__":
    sys.exit(main())
