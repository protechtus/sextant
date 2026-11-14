"""`sextant setup`: print the MCP registration and hook entries for THIS install.

Every earlier walkthrough had the human (or their agent) assemble the command by hand from
placeholders: the venv path, the platform's interpreter layout, the ATLAS_* variables that
must be baked into the registration, and JSON escaping for the hook files. A package install
(pipx, uv tool, pip from PyPI or a git URL) has no clone and therefore none of the deploy/
snippets at all (sweep 2026-11-11, package-user persona). This command knows all of that about
the running interpreter and prints it filled in. Without `--out` it writes nothing: the human merges or runs
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
import re
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
    """Quote one argument for the shell the printed command line will be pasted into.

    POSIX: shlex.quote (single quotes; metacharacters never expand). Windows: the line is for
    PowerShell (or Git Bash), so single quotes too, with an embedded quote doubled: a double-
    quoted `$100%` lost its `$100` to PowerShell's variable expansion and the server then
    served a corpus that did not exist while `claude mcp list` said Connected (AG6, pipeline
    sweep 2026-11-11). A double quote or a newline cannot be carried by the argv rules node
    applies, so it is refused with a reason instead of printed broken; a trailing backslash is
    doubled because PowerShell 5.1 re-quotes an argument with spaces for the child and `\\"`
    would otherwise escape its closing quote. cmd.exe lines use `_q_cmd`.
    """
    posix = (os.name != "nt") if posix is None else posix
    if posix:
        return shlex.quote(value)
    if '"' in value or "'" in value or "\n" in value or "\r" in value:
        # A single quote has no form that both PowerShell ('it''s') and Git Bash ('it'\''s')
        # read the same way, and the printed line is pasted into either: bash dropped the quote
        # and registered a path that does not exist (MN12, sweep 10). Refused, like a double
        # quote; the JSON forms carry any value.
        raise UnquotableValue(f"a value with a quote or a line break cannot be passed on a Windows "
                              f"command line: {value!r}")
    if value.endswith("\\"):
        value = value + "\\"
    return "'" + value + "'"


def _q_cmd(value: str) -> str:
    """Quote for cmd.exe (Claude Code runs Windows hooks through it): double quotes; `$` and
    backticks are literal there, a `%` pair expands only when it names an existing variable."""
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


def wslenv_with(existing: str, names: list[str]) -> str:
    """WSLENV with ``names`` appended: empty entries dropped (a trailing colon is common, and
    verbatim it became ``::``), nothing listed twice (entries may carry ``/u``-style flags)."""
    parts = [p for p in existing.split(":") if p]
    present = {p.split("/", 1)[0] for p in parts}
    for n in names:
        if n not in present:
            parts.append(n)
            present.add(n)
    return ":".join(parts)


def wsl_command_from_windows(python: str, env: dict[str, str], distro: str, tail: list[str]) -> str:
    """A Windows-side command that runs ``python <tail>`` inside the distro with the registration's
    variables: ``wsl -d D -- env K=V python ...``. Needs no Windows Python and no WSLENV."""
    parts = ["wsl", "-d", _q(distro, posix=False), "--"]
    if env:
        parts += ["env"] + [_q(f"{k}={v}", posix=True) for k, v in env.items()]
    parts += [_q(python, posix=True), *tail]
    return " ".join(parts)


def wsl_hooks_json(python: str, env: dict[str, str], distro: str) -> dict:
    """Hook entries for a WINDOWS settings.json whose Sextant lives in WSL (the Linux-only block
    cannot run there; AG3, pipeline sweep 2026-11-11)."""
    # Claude Code runs hook commands through Git Bash, whose MSYS layer rewrites every /home/...
    # argument into C:/Program Files/Git/home/... (MN24): the prefix switches that off.
    start = "MSYS_NO_PATHCONV=1 " + wsl_command_from_windows(python, env, distro, ["-m", "atlas_plus.hooks.session_start"])
    stop = "MSYS_NO_PATHCONV=1 " + wsl_command_from_windows(python, env, distro, ["-m", "atlas_plus.summarizer.hook_entry"])
    return {"hooks": {
        "SessionStart": [{"matcher": "", "hooks": [{"type": "command", "command": start}]}],
        "Stop": [{"matcher": "", "hooks": [{"type": "command", "command": stop}]}],
    }}


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
        wslenv = wslenv_with(existing_wslenv, names)
        parts += ["-e", _q(f"WSLENV={wslenv}", posix=False)]
    parts += ["--", "wsl", "-d", _q(distro, posix=False), "--", _q(python, posix=True),
              "-m", SERVER_MODULE, "--stdio"]
    return " ".join(parts)


def mcp_servers_json(python: str, env: dict[str, str], name: str = SERVER_NAME) -> dict:
    entry: dict = {"type": "stdio", "command": python, "args": ["-m", SERVER_MODULE, "--stdio"]}
    if env:
        entry["env"] = dict(env)
    return {"mcpServers": {name: entry}}


def _toml_str(value: str) -> str:
    """A TOML string for a path: literal (single-quoted, no escapes) unless it holds a quote,
    then basic with the backslashes doubled. A hand-typed basic string `"C:\\sextant\\..."` made
    Codex refuse its whole config (AG12, sweep 10)."""
    if "\n" in value or "\r" in value:
        # Neither TOML string form carries a line break on one line; a broken table took every
        # other Codex server down with it (MN12, sweep 10).
        raise UnquotableValue(f"a value with a line break cannot be written into config.toml: {value!r}")
    if "'" not in value:
        return "'" + value + "'"
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def codex_toml(python: str, env: dict[str, str], name: str = SERVER_NAME) -> str:
    """The `[mcp_servers.<name>]` table for ~/.codex/config.toml (or $CODEX_HOME/config.toml)."""
    lines = [f"[mcp_servers.{name}]", f"command = {_toml_str(python)}",
             f'args = ["-m", "{SERVER_MODULE}", "--stdio"]']
    if env:
        lines += ["", f"[mcp_servers.{name}.env]"] + [f"{k} = {_toml_str(v)}" for k, v in env.items()]
    return "\n".join(lines)


def codex_command(python: str, env: dict[str, str], name: str = SERVER_NAME) -> str:
    """`codex mcp add`, which writes the TOML itself. PowerShell 5.1 swallows a bare `--`, so
    the separator is quoted on Windows (AG12)."""
    parts = ["codex", "mcp", "add", name]
    for k, v in env.items():
        parts += ["--env", _q(f"{k}={v}")]
    parts += ['"--"' if os.name == "nt" else "--", _q(python), "-m", SERVER_MODULE, "--stdio"]
    return " ".join(parts)


def opencode_json(python: str, env: dict[str, str], name: str = SERVER_NAME) -> dict:
    """opencode's `mcp` block: `type: local`, `command` as one array, `environment`, and
    `enabled`, none of which match the generic mcpServers shape (AG12)."""
    entry: dict = {"type": "local", "enabled": True,
                   "command": [python, "-m", SERVER_MODULE, "--stdio"]}
    if env:
        entry["environment"] = dict(env)
    return {"$schema": "https://opencode.ai/config.json", "mcp": {name: entry}}


def _self_hosting_doc() -> str:
    """The server guide: the file in a clone, the public URL for a package install (no docs/)."""
    local = Path(__file__).resolve().parents[2] / "docs" / "HOWTO-self-hosting.md"
    if local.exists():
        return "docs/HOWTO-self-hosting.md"
    return "https://github.com/protechtus/sextant/blob/main/docs/HOWTO-self-hosting.md"


def hooks_json(python: str, env: dict[str, str], posix: bool | None = None) -> dict:
    """SessionStart priming and the Stop summarizer, with this interpreter.

    Hooks are separate processes: the env block of an MCP registration never reaches them.
    Claude Code runs a hook command through sh on POSIX and Git Bash on Windows (PowerShell only
    when Git Bash is absent; never cmd.exe), so the command is written for bash on every platform
    and carries the variables as `--env NAME=value` flags that `atlas_plus.hooks.run` applies
    before any module loads. (A `set "NAME=value" &&` prefix did nothing under Git Bash and the
    hook primed from the wrong corpus while reporting success: MN1, pipeline sweep 2026-11-11.
    Single quotes are literal in bash and in PowerShell alike.)
    """
    del posix  # one form for every platform
    flags = "".join(f" --env {shlex.quote(f'{k}={v}')}" for k, v in env.items())
    start = f"{shlex.quote(python)} -m atlas_plus.hooks.run{flags} session-start"
    stop = f"{shlex.quote(python)} -m atlas_plus.hooks.run{flags} stop"
    return {"hooks": {
        "SessionStart": [{"matcher": "", "hooks": [{"type": "command", "command": start}]}],
        "Stop": [{"matcher": "", "hooks": [{"type": "command", "command": stop}]}],
    }}


def describe(environ: dict | None = None, name: str = SERVER_NAME) -> dict:
    """Everything `setup` knows, as data (the --json output). Never raises on a bad value: the
    piece that cannot be quoted carries an `error` and the others are still printed. ``name``
    is the server name every form registers (a second install on one machine needs its own;
    MN9, sweep 10)."""
    from atlas_plus import paths as atlas_paths

    env = path_env(environ)
    henv = hook_env(environ)
    py = interpreter()
    distro = wsl_distro(environ)
    verify = f"{py} -m atlas_plus verify" if " " not in py else f"{_q(py)} -m atlas_plus verify"
    out: dict = {
        "interpreter": py,
        "platform": "windows" if os.name == "nt" else ("wsl" if distro else "posix"),
        "memory_root": str(atlas_paths.memory_root()),
        "state_dir": str(atlas_paths.state_dir()),
        "env": env,
        "hook_env": henv,
        "client_config_json": mcp_servers_json(py, env, name),
        "verify": verify,
        "server_name": name,
    }
    try:
        out["claude_code"] = claude_code_command(py, env, name)
    except UnquotableValue as exc:
        out["claude_code"] = None
        out["error"] = str(exc)
    try:
        out["settings_json"] = hooks_json(py, henv)
    except UnquotableValue as exc:
        out["settings_json"] = None
        out["error"] = str(exc)
    try:
        out["codex_toml"] = codex_toml(py, env, name)
    except UnquotableValue as exc:
        out["codex_toml"] = None
        out["error"] = str(exc)
    try:
        # No half section: when the TOML cannot carry the value, the `codex mcp add` line (which
        # would write that same TOML) is withheld too, and CANNOT PRINT names the reason.
        out["codex"] = codex_command(py, env, name) if out["codex_toml"] else None
    except UnquotableValue:
        out["codex"] = None   # the TOML form above needs no shell quoting
    out["opencode_json"] = opencode_json(py, env, name)
    # A server has no Claude Code: remote clients reach HTTP mode behind an ingress.
    out["http"] = {
        "serve": f"ATLAS_MCP_TOKEN=<long random> {verify.split(' -m ')[0]} -m {SERVER_MODULE}",
        "serve_container": f"ATLAS_MCP_HOST=0.0.0.0 ATLAS_MCP_TOKEN=<long random> {verify.split(' -m ')[0]} "
                           f"-m {SERVER_MODULE}",
        "remote_claude_code": 'claude mcp add -s user --transport http sextant https://<your-host>/mcp '
                              '--header "Authorization: Bearer $ATLAS_MCP_TOKEN"',
        "lan_url": "http://<host>:8765/mcp",
        "verify_http": "python -m atlas_plus verify --url http://<host>:8765/mcp --token $ATLAS_MCP_TOKEN",
        "docs": _self_hosting_doc(),
    }
    import shutil as _shutil
    out["claude_on_path"] = _shutil.which("claude") is not None
    if distro:
        out["wsl_distro"] = distro
        try:
            out["claude_code_from_windows"] = claude_code_command_from_windows(
                py, env, distro, name=name,
                existing_wslenv=(os.environ if environ is None else environ).get("WSLENV", ""))
        except UnquotableValue as exc:
            out["claude_code_from_windows"] = None
            out["error"] = str(exc)
        out["verify_from_windows"] = wsl_command_from_windows(py, env, distro, ["-m", "atlas_plus", "verify"])
        out["hooks_from_windows"] = wsl_hooks_json(py, henv, distro)
    return out


def render(info: dict, client: str = "all") -> str:
    env = info["env"]
    lines: list[str] = []
    if info.get("error") and client in ("all", "claude-code", "hooks", "codex"):
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
        if any(not v.isascii() for v in [info["interpreter"], *env.values()]):
            lines.append("  NOTE        : a path above has non-ASCII characters. Capture this text only "
                         "from a UTF-8 console (PowerShell: [Console]::OutputEncoding = "
                         "[Text.Encoding]::UTF8; cmd: chcp 65001), or use --json or --client "
                         "claude-desktop, whose JSON escapes them.")
        lines.append("")
    if client in ("all", "claude-code"):
        if client == "all":
            lines.append("1. Claude Code (one command, any directory; then `claude mcp list`, "
                         "then restart Claude Code):")
        if info.get("claude_code"):
            lines += ["   " + info["claude_code"]]
            if Path("/.dockerenv").exists():
                lines.append("   (this is a container: a client on the host cannot run that command; "
                             "clients connect over HTTP, section 4)")
            if os.name == "nt":
                lines.append("   (PowerShell or Git Bash: the single quotes are literal in both; "
                             "from cmd.exe use the JSON block in section 2 instead)")
            if info.get("server_name", SERVER_NAME) == SERVER_NAME:
                lines.append("   (a second install on this machine needs its own server name: "
                             "`sextant setup --name sextant-work` prints every form under that name)")
            lines.append("")
        if info.get("claude_code_from_windows"):
            lines += [f"   Windows Claude Code reaching this WSL install ({info['wsl_distro']}); "
                      "run it in PowerShell on the Windows side:",
                      "   " + info["claude_code_from_windows"],
                      "   Verify from Windows (no Windows Python needed; the same variables as the "
                      "registration):",
                      "   " + info["verify_from_windows"], ""]
    if client in ("all", "json"):
        if client == "all":
            lines.append("2. Claude Desktop, Cursor, or any MCP client that takes JSON: merge "
                         "this `mcpServers` entry into the client's config")
            lines.append("   (Claude Desktop: %APPDATA%\\Claude\\claude_desktop_config.json on "
                         "Windows, or for the Microsoft Store build "
                         "%LOCALAPPDATA%\\Packages\\Claude_*\\LocalCache\\Roaming\\Claude\\; "
                         "~/Library/Application Support/Claude/claude_desktop_config.json "
                         "on macOS; Cursor: ~/.cursor/mcp.json, or .cursor/mcp.json in a project; "
                         "keep any servers already there, then restart the client):")
        lines += [json.dumps(info["client_config_json"], indent=2), ""]
    if client in ("all", "codex"):
        if client == "all":
            lines.append("2b. Codex CLI (OpenAI): one command (it writes the TOML; in PowerShell the "
                         "quoted \"--\" matters), or merge the table into ~/.codex/config.toml "
                         "($CODEX_HOME/config.toml if set); then `codex mcp list`:")
        if info.get("codex"):
            lines += ["   " + info["codex"]]
        if info.get("codex_toml"):
            lines += [info["codex_toml"], ""]
    if client in ("all", "opencode"):
        if client == "all":
            lines.append("2c. opencode: merge into opencode.json in the project (or "
                         "~/.config/opencode/opencode.json); then `opencode mcp list`:")
        lines += [json.dumps(info["opencode_json"], indent=2), ""]
    if client in ("all", "hooks"):
        if client == "all":
            lines.append("3. Claude Code hooks, optional (session-start priming, end-of-session "
                         "summary): merge into ~/.claude/settings.json, ADDING to any existing "
                         "hooks list. Ask before editing someone else's settings.")
            if info.get("hook_env"):
                lines.append("   Hooks run outside your shell, so each command below carries the "
                             "baked-in variables as --env flags; nothing to set user-wide.")
            lines.append("   Claude Code runs hook commands through bash (Git Bash on Windows), "
                         "which these are written for.")
        _hint = ("   The session-start hook prints the corpus it read; the Stop hook writes "
                 "nothing until ATLAS_SESSION_AUTONOMOUS=on reaches it: add `--env ATLAS_SESSION_AUTONOMOUS=on` "
                 "(and `--env ATLAS_OLLAMA_URL=...` when Ollama is not on localhost) to the stop command; "
                 "in the Windows-to-WSL form below, put `ATLAS_SESSION_AUTONOMOUS=on` right after `env` instead.")
        if client == "all":
            lines.append(_hint)
        else:
            print(_hint.strip(), file=sys.stderr)  # stdout stays the JSON block (MN32/MN33: `> hooks.json` was not JSON)
        if info.get("settings_json"):
            lines += [json.dumps(info["settings_json"], indent=2), ""]
        if info.get("hooks_from_windows"):
            lines += ["   Windows Claude Code with this WSL install: use these hook commands instead "
                      "(the block above is Linux-only; these carry the registration's variables):",
                      json.dumps(info["hooks_from_windows"], indent=2), ""]
        if client == "all":
            lines += ["   Other clients (Codex CLI, opencode, Cursor, Claude Desktop) have no session "
                      "hooks: `sextant-session-start` prints the same priming text to paste into their "
                      "instructions file (AGENTS.md, opencode's instructions), the server tells the "
                      "agent to search memory first, and end-of-session summaries are Claude Code only.",
                      ""]
    if client == "all":
        h = info.get("http") or {}
        if h:
            lines.append("4. No Claude Code on this machine, or clients elsewhere (a server): run HTTP "
                         "mode behind your ingress and register the remote client against it:")
            if not info.get("claude_on_path"):
                lines.append("   (`claude` is not on PATH here, so section 1 applies on the client machine)")
            lines += ["   " + h["serve"],
                      "   (binds 127.0.0.1:8765; the token is required; ready when the log says "
                      "`Uvicorn running on`, a few seconds)",
                      "   In a container: " + h["serve_container"],
                      "   (0.0.0.0 inside the container, and publish the port on the host's loopback "
                      "only: -p 127.0.0.1:8765:8765)",
                      "   " + h["remote_claude_code"],
                      f"   (behind an ingress; on a LAN or the same host the URL is {h['lan_url']}, and for "
                      "another machine on the LAN the server must bind beyond loopback: ATLAS_MCP_HOST=<this "
                      "machine's LAN address>; make the token with python -c \"import secrets; "
                      "print(secrets.token_urlsafe(32))\"; pass it from a variable, since `claude mcp get` "
                      "and shell history show it)",
                      "   Prove a write over HTTP from the client machine, with that machine's own Python that "
                      "has the package (or scripts/mcp_roundtrip.py from a clone): " + h["verify_http"],
                      "   (curl -s http://<host>:8765/health answers without a token; 10 failed "
                      "requests from one source lock it out for 5 min (429), and every client behind "
                      "one NAT, Docker's bridge included, shares that counter: ATLAS_MCP_TRUSTED_PEERS=<ip> "
                      "exempts a peer you own)",
                      f"   Full server setup (systemd, Caddy, Tailscale): {h['docs']}", ""]
        lines.append(f"Verify: {info['verify']}   (starts the server over stdio, writes one fact under "
                     "project:demo, searches it back)")
    return "\n".join(lines)


_REGISTRY_KEYS = ("mcpServers", "mcp")


def _is_ours(item) -> bool:
    """A hook entry Sextant itself wrote (its command names the package)."""
    return "atlas_plus" in json.dumps(item, sort_keys=True)


def _merge(existing, piece):
    """Deep-merge a JSON piece into an existing object: mappings merge key by key, a NAMED
    registration (`mcpServers.<name>`, `mcp.<name>`) is replaced whole, hook arrays drop the
    entries Sextant wrote before and gain the new ones, other arrays keep what is there and
    gain what is new, scalars from the piece win. Merging a registration key by key appended
    the new interpreter as a stray argument and kept the old corpus variable (FN6, final pass)."""
    if isinstance(existing, dict) and isinstance(piece, dict):
        out = dict(existing)
        for k, v in piece.items():
            if k in _REGISTRY_KEYS and isinstance(existing.get(k), dict) and isinstance(v, dict):
                out[k] = {**existing[k], **v}
            elif k in existing:
                out[k] = _merge(existing[k], v)
            else:
                out[k] = v
        return out
    if isinstance(existing, list) and isinstance(piece, list):
        if any(_is_ours(i) for i in piece):
            out = [i for i in existing if not _is_ours(i)]
        else:
            out = list(existing)
        for item in piece:
            if item not in out:
                out.append(item)
        return out
    return piece


def _write_out(path: Path, file_text: str, piece) -> None:
    """Write a JSON piece to --out. An existing JSON file is MERGED into, not replaced, and
    backed up first: `--out opencode.json` dropped the user's other servers and `--out
    settings.json` wiped their permissions (AG24, sweep 12)."""
    if path.exists() and piece is not None:
        raw = path.read_text(encoding="utf-8-sig")
        if raw.strip():
            try:
                existing = json.loads(raw)
            except ValueError as exc:
                raise ValueError(f"{path} exists and is not valid JSON ({exc}); merge the block by hand or "
                                 "move the file aside") from None
            if not isinstance(existing, dict):
                raise ValueError(f"{path} exists and is not a JSON object; merge the block by hand")
            merged = _merge(existing, piece)
            from datetime import datetime as _dt
            if merged == existing:
                # Nothing to merge: no backup, no rewrite (FN1, final readiness pass).
                print(f"sextant setup: {path} already holds this block; nothing written", file=sys.stderr)
                return
            stamp = _dt.now().strftime("%Y%m%d%H%M%S")
            backup = path.with_name(path.name + ".bak-" + stamp)
            n = 2
            while backup.exists():   # two runs in one second overwrote the original's backup (FN1)
                backup = path.with_name(f"{path.name}.bak-{stamp}-{n}")
                n += 1
            backup.write_bytes(path.read_bytes())   # byte-exact, BOM and all
            print(f"sextant setup: merged into {path} (backup: {backup.name})", file=sys.stderr)
            file_text = json.dumps(merged, indent=2)
    path.write_text(file_text + "\n", encoding="utf-8", newline="\n")


def main(argv: list[str] | None = None) -> int:
    from atlas_plus._console import utf8_stdout_when_piped
    utf8_stdout_when_piped()
    ap = argparse.ArgumentParser(
        prog="sextant setup",
        description="Print the MCP registration and hook entries for this install, filled in.",
    )
    ap.add_argument("--client", choices=("all", "claude-code", "json", "claude-desktop", "cursor",
                                         "codex", "opencode", "hooks"),
                    default="all",
                    help="which piece to print (default: all, with instructions; claude-desktop and "
                         "cursor are the json block; codex is the TOML, opencode its JSON)")
    ap.add_argument("--name", default=SERVER_NAME, metavar="NAME",
                    help=f"the server name every form registers (default {SERVER_NAME}); a second "
                         "install on one machine needs a different one, and `claude mcp remove`/`get` "
                         "take the same name")
    ap.add_argument("--json", action="store_true", help="machine-readable output of everything")
    ap.add_argument("--out", default=None, metavar="FILE",
                    help="also write the output to FILE as UTF-8 without a byte-order mark (PowerShell's "
                         "> redirect adds one, which JSON parsers reject)")
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    if "--client" in raw_argv:
        guess = raw_argv[raw_argv.index("--client") + 1] if raw_argv.index("--client") + 1 < len(raw_argv) else ""
        if guess in ("claude", "claude_code", "claudecode"):
            print(f"sextant setup: --client {guess!r} is not a choice; did you mean --client claude-code? "
                  "(claude-desktop and cursor print the JSON block)", file=sys.stderr)
            return 2
    args = ap.parse_args(argv)
    if args.client in ("claude-desktop", "cursor", "json"):
        # The bare JSON forms said nothing about where it goes; a whole-file paste would drop
        # the user's other servers (AG13, sweep 11). stdout stays pure JSON; this is stderr.
        where = {
            "claude-desktop": "%APPDATA%\\Claude\\claude_desktop_config.json (Microsoft Store build: "
                              "%LOCALAPPDATA%\\Packages\\Claude_*\\LocalCache\\Roaming\\Claude\\) on Windows, "
                              "~/Library/Application Support/Claude/claude_desktop_config.json on macOS",
            "cursor": "~/.cursor/mcp.json, or .cursor/mcp.json inside the project",
            "json": "your MCP client's config file",
        }[args.client]
        print(f"sextant setup: merge the `mcpServers` entry below into {where}; keep any servers already "
              "there, then restart the client.", file=sys.stderr)
        args.client = "json"
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", args.name):
        print(f"sextant setup: --name {args.name!r} must be letters, digits, '.', '_' or '-' (64 max)",
              file=sys.stderr)
        return 2
    info = describe(name=args.name)
    if args.json and args.client not in ("all",):
        # `--client X --json` used to ignore --client and print everything (AG24, sweep 12).
        pieces = {"json": info.get("client_config_json"), "opencode": info.get("opencode_json"),
                  "hooks": info.get("settings_json"),
                  "codex": {"command": info.get("codex"), "toml": info.get("codex_toml")},
                  "claude-code": {"command": info.get("claude_code")}}
        output = json.dumps(pieces.get(args.client, info), indent=2)
    else:
        output = json.dumps(info, indent=2) if args.json else render(info, args.client)
    print(output)
    if args.out:
        # The file form of a JSON piece is the JSON alone: on WSL the hooks output carries a
        # prose line and a second object for the Windows side, which made the file unparseable
        # (AG9, sweep 10).
        file_text = output
        piece = None
        if not args.json and args.client == "hooks" and info.get("settings_json"):
            piece = info["settings_json"]
        elif not args.json and args.client == "json":
            piece = info["client_config_json"]
        elif not args.json and args.client == "opencode":
            piece = info["opencode_json"]
        if piece is not None:
            file_text = json.dumps(piece, indent=2)
        try:
            Path(args.out).parent.mkdir(parents=True, exist_ok=True)  # ~/.config/opencode may not exist yet (AG39)
            _write_out(Path(args.out), file_text, piece)
        except OSError as exc:
            print(f"sextant setup: cannot write {args.out}: {exc}", file=sys.stderr)
            return 1
        except ValueError as exc:
            print(f"sextant setup: {exc}", file=sys.stderr)
            return 1
    # Only a requested SHELL line that could not be printed is a failure: the JSON blocks need
    # no shell quoting and are complete (AG6, pipeline sweep 2026-11-11).
    shell_forms = args.client in ("all", "claude-code", "hooks", "codex") and not args.json
    return 1 if (info.get("error") and shell_forms) else 0


if __name__ == "__main__":
    sys.exit(main())
