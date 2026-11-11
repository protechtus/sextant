# Install Sextant with an AI agent

This page is written for an AI coding agent (Claude Code, Codex, Cursor, Aider, or any
agent with a shell) acting on behalf of a human. If you are the agent: follow the steps in
order, run the verify command after each one, and stop to ask the human at every point
marked **ASK**. Do not skip verifies. Do not install system packages or change files outside
the install directory without asking. Do not set `ATLAS_AUTONOMOUS`, do not edit the human's
hooks or settings files without an explicit yes, and never `pip install sextant` (an unrelated
package). The whole run takes 5 to 10 minutes.

Already installed as a package (`pipx install sextant-ai`, `uv tool install sextant-ai`, or pip
from PyPI), so there is no clone? Steps 1 to 3 are done. From step 4 on, the installed commands
are `sextant-init`, `sextant-doctor`, `sextant setup` and `sextant verify`; wherever a step says
`<venv-python> -m atlas_plus.<x>`, the `sextant <x>` form is the same thing.

If you are the human: paste this into your agent:

> Read https://usesextant.com/agent-install.md and follow it to install Sextant on this
> machine. Ask me before anything that needs sudo, admin rights, or touches files outside
> the project folder.

The canonical copy of this file lives in the repository at `docs/AGENT-INSTALL.md`; the
copy at usesextant.com is republished from it on every release.

---

## 0. Detect the environment

Run whichever of these exist and record the output (one per line; do not chain them with
`||`, PowerShell rejects that):

```
python --version
python3 --version
py -3 --version
git --version
```

- Sextant needs **Python 3.11 or newer** and **git**. Check every interpreter that answered,
  not just the first: on many machines `python` is an old 3.10 while `py -3` or `python3.12`
  is current. On Windows, `py --list` shows every registered interpreter (`py -3` picks the
  newest; `py -3.11` picks one), and a `python3` that answers from the Microsoft Store is a
  stub or an old install, not the one to use. Use the newest 3.11+ you found for every later
  step. If none qualifies, **ASK**
  the human how they want it installed (python.org, `uv python install 3.12`, the OS package
  manager). Do not install it yourself.
- Note the OS and shell: Windows PowerShell, Windows Git Bash, macOS, Linux, or WSL. The
  venv paths below differ between Windows (`.venv\Scripts\`) and everything else
  (`.venv/bin/`). Git Bash on Windows uses the Windows layout with forward slashes:
  `.venv/Scripts/python.exe`.
- **ASK** where to install if the human has not said. Default: a `sextant` folder inside
  their projects or home directory. Never install inside another project's repository.

## 1. Get the code

```
git clone https://github.com/protechtus/sextant.git
cd sextant
```

Verify: `git log --oneline -1` prints a commit and `pyproject.toml` exists.

(When the package is on PyPI, `pipx install sextant-ai` or `uv tool install sextant-ai` is
the short path and skips steps 1 to 3. Until then, install from the clone. Never run
`pip install sextant`: that is an unrelated package.)

## 2. Create the virtual environment

Windows PowerShell:

```
py -3 -m venv .venv
.venv\Scripts\python.exe -m pip install -U pip setuptools
```

Windows Git Bash (same layout, forward slashes):

```
py -3 -m venv .venv
.venv/Scripts/python.exe -m pip install -U pip setuptools
```

macOS / Linux / WSL:

```
python3 -m venv .venv
.venv/bin/python -m pip install -U pip setuptools
```

Verify: `.venv\Scripts\python.exe --version` on Windows, `.venv/bin/python --version`
elsewhere, prints 3.11+.

Known failure: on Debian/Ubuntu, `venv` fails with an `ensurepip` error when the
`python3-venv` package is missing. **ASK** before running
`sudo apt-get install -y python3-venv`. If the human cannot use sudo (remove the half-built
`.venv` first, and delete `get-pip.py` afterwards so the clone stays clean):

```
rm -rf .venv
python3 -m venv --without-pip .venv
curl -sSLo get-pip.py https://bootstrap.pypa.io/get-pip.py
.venv/bin/python get-pip.py && rm get-pip.py
```
or, if `uv` is installed, `uv venv --seed .venv` (the `--seed` flag installs pip so the
`python -m pip` commands below keep working).

## 3. Install

Use the venv's Python explicitly (do not rely on `activate`; it is shell-specific):

```
<venv-python> -m pip install -e ".[dev,telegram,summarizer]"
```

Verify: `<venv-python> -m pip show sextant-ai` prints the version, and
`<venv-python> -m atlas_plus --version` prints it too.

Known failure: a dependency resolver conflict. Retry with the pinned set:
`<venv-python> -m pip install -e ".[dev,telegram,summarizer]" -c constraints.txt`.

## 4. Initialise the memory corpus

**ASK first:** by default this writes two folders outside the project, `~/.memory` (the
markdown corpus the human owns) and `~/.atlas` (state). Ask where they want them. To keep
everything inside the install folder instead, set the variables **before** running init and
keep them set for every later command (and pass them to the MCP client in step 5):

```
ATLAS_MEMORY_ROOT=<install-dir>/memory
ATLAS_STATE_DIR=<install-dir>/state
```

Rules that are easy to miss:

- `ATLAS_WORKSPACE_ROOT=<dir>` is the one-variable form: it re-roots the corpus to
  `<dir>/.memory`, state to `<dir>/.atlas` and the projects root to `<dir>/projects`. A
  specific variable (`ATLAS_MEMORY_ROOT`, `ATLAS_STATE_DIR`, `ATLAS_PROJECTS_ROOT`) always wins
  over the workspace root when both are set.
- Inside a clone, `memory/`, `state/` and `.memory/` at the top level are git-ignored, so the
  human's notes never land in a commit. Do not pick any other name inside the clone.
- If you run one shell per command (most agent harnesses do), write the variables to a file
  once (`env.sh` with `export` lines, or `env.ps1` with `$env:` lines) and source or
  dot-source it at the start of every later command. The verify in step 6 fails against the
  wrong corpus otherwise.

Then:

```
<venv-python> -m atlas_plus.init_cmd
```

Verify: `<venv-python> -m atlas_plus.doctor.cli` runs; its "Memory corpus" section names the
path you chose. The doctor is a report and creates nothing, so it is safe before init too. On
a machine with nothing else running, the Services rows (Brain, MCP, Web, Sync, Ollama) show
**down**: nothing is running yet, which is expected and not an error. On a machine that
already runs another Sextant or an Ollama, those rows show **up** and the report says so in
its first line; that is not this install.

## 5. Connect the MCP server to the human's AI client

The server runs over stdio and needs no token in this mode. Let the install print its own
registration, with the step 4 variables still set:

```
<venv-python> -m atlas_plus setup
```

It prints, filled in for this interpreter and this environment: the one-line Claude Code
command (paths quoted, every `ATLAS_*` path variable you set baked in as `-e`), the JSON
`mcpServers` entry for Claude Desktop, Cursor and other clients, and the optional hook
entries. `--json` gives the same as data; `--client claude-code` prints only the command.
The `-e` variables matter: a client launched from the Start menu or the Dock has no shell
and the server would otherwise serve `~/.memory`.

**Claude Code**: run the printed `claude mcp add -s user sextant ...` line as-is (it does
not depend on the current directory). Writing it by hand instead: the interpreter is the
venv's Python, quoted (a path with a space breaks without quotes), and each variable goes
**after the server name** as `-e "NAME=value"`, quoted the same way; `-e` before the name
fails on current Claude Code. Windows PowerShell: `"$PWD\.venv\Scripts\python.exe"`.
Git Bash: `"$(pwd -W)/.venv/Scripts/python.exe"` (the real `C:/...` path; `$PWD` there is
`/c/...` and does not work outside Git Bash). macOS / Linux: `"$PWD/.venv/bin/python"`.

**WSL install, Windows Claude Code**: run `setup` inside WSL; besides the Linux command it
prints the Windows-side form, which crosses the boundary with
`-- wsl -d <Distro> -- /home/<user>/<install>/.venv/bin/python -m atlas_plus.mcp.memory_server.server --stdio`
and carries `-e "WSLENV=ATLAS_MEMORY_ROOT:ATLAS_STATE_DIR"` (or whichever variables you set).
Without `WSLENV`, variables set on the Windows side never reach the Linux process and the
server silently serves `~/.memory` inside WSL while still answering `ok`. Run the printed
command in PowerShell on the Windows side; `claude` is usually not on the WSL PATH.

Verify: `claude mcp list` shows `sextant ... Connected` (on the side where Claude Code
runs). Then tell the human to restart Claude Code; a running session does not see a new server.

**Claude Desktop**: merge the `mcpServers` block that `setup` printed into the client's
config file (`%APPDATA%\Claude\claude_desktop_config.json` on Windows,
`~/Library/Application Support/Claude/claude_desktop_config.json` on macOS), keeping any
servers already there. The same block, with placeholders instead of real paths, is
`deploy/claude_desktop_config.snippet.json` in a clone; on Windows its interpreter path is
`<ATLAS_PLUS_DIR>/.venv/Scripts/python.exe`, not the `.venv/bin/python` shown. **ASK** before
editing that file; show the diff.

**Cursor and other MCP clients**: same command and arguments as above, in the client's MCP
settings. The server name `sextant` must be unique per machine; a second install needs a
different name.

**Session-start priming for Claude Code** (optional; only after the human says yes):
`<venv-python> -m atlas_plus setup --client hooks` prints the `hooks` block with this
interpreter already in place (in a clone the same block ships as placeholders in
`deploy/session_start_hook.json` and `deploy/stop_hook.json` for Windows, the `.macos.json`
pair for macOS **and** Linux). It goes into `~/.claude/settings.json`. **ASK** before editing
settings, show the diff, and if a `hooks` key already exists add the entries rather than
replacing the key. Hooks take effect in the next Claude Code session. The Stop hook writes
nothing until the human sets `ATLAS_AUTONOMOUS=on`.

Hooks are separate processes: the `env` block of the MCP registration applies to the MCP
server only, not to them. If you set `ATLAS_MEMORY_ROOT` / `ATLAS_STATE_DIR` /
`ATLAS_PROJECTS_ROOT` in the registration rather than in the user's environment, the hook
would read a different corpus. Either set the same variables user-wide (`setx` on Windows,
the shell profile on POSIX) or put them on the hook command itself
(`ATLAS_MEMORY_ROOT=/path python -m atlas_plus.hooks.session_start` on POSIX; a one-line
`.cmd` wrapper on Windows). The round-trip in step 6 plus a session-start run from a
project directory should show the same identity note from both.

## 6. Prove it works (the round-trip)

The shortest path, with the same environment variables as step 4 (every kind of install has
this command; `sextant verify` is the same thing):

```
<venv-python> -m atlas_plus verify --project demo
```

It starts the server over stdio, does the MCP handshake, and runs the three calls below,
printing each result as JSON and ending with `ROUND-TRIP: OK`. Paste that output into your
report. Check the `memory_root` in the `memory_health` line: it must be the corpus from
step 4, not a leftover `~/.memory`. (In a clone, `scripts/mcp_roundtrip.py` runs the same
check. From Windows against a WSL install, add
`--command "wsl -d <Distro> -- /home/<user>/<install>/.venv/bin/python"` to test the exact
launcher the registration uses.)

If the human prefers to see it through their connected client, do the same three calls there.
Exact call shapes (parameter names matter):

1. `memory_health` with no arguments. Expect `"status": "ok"`.
2. `memory_remember_tool` with `scope="project:demo"` and
   `fact="Sextant installed by an agent on <date>."`. Expect a result naming a file under
   `projects/`.
3. `memory_search_tool` with `query="Sextant installed by an agent"`. Expect the fact back
   with a `confidence` band (high/medium/low/starved).

Scope grammar for call 2: `project:<name>`, `reference:<topic>`, `preference:<topic>`. A bare
`reference` is rejected.

The CLI can confirm the write too: `<venv-python> -m atlas_plus search "installed by an agent"`.

## 7. Report to the human

Tell them, in this order: where it is installed, where the corpus lives, which client is
connected, what the round-trip returned, and anything you had to ask or work around. Tell
them to restart their client so the new server appears. Point them at
`docs/GETTING-STARTED.md` for daily use and `docs/USAGE.md` for the tool reference (in a
package install those live at https://github.com/protechtus/sextant/tree/main/docs).

## Troubleshooting (seen in real installs)

| Symptom | Cause | Fix |
| :- | :- | :- |
| `Invalid environment variable format: sextant` from `claude mcp add` | `-e` placed before the server name | put the name first, then `-e KEY=VAL`, then `--` |
| `Scope 'reference' requires a topic` | bare scope | use `reference:<topic>` |
| Session start says "not in a known project directory" | the hook primes a project note only when the working directory is a project under `ATLAS_PROJECTS_ROOT` (default `~/projects`) | set `ATLAS_PROJECTS_ROOT`, or use `atlas-prime <task>` for task context |
| HTTP server refuses to start | `sextant-mcp` over HTTP needs `ATLAS_MCP_TOKEN`; stdio mode does not | use stdio for clients; set the token only for HTTP |
| Corpus "vanished" | `ATLAS_WORKSPACE_ROOT` was set after init | set it before init, or unset it |
| `memory_health` says `ok` but its `memory_root` is `~/.memory`, not the chosen corpus | the `-e` variables never reached the server (GUI-launched client, or Windows to WSL without `WSLENV`) | re-register with the command `setup` prints |
| `=== Atlas Doctor ===` instead of `Sextant Doctor` | an old build invoked as `python -m atlas_plus.doctor.cli` | cosmetic; `sextant-doctor` is the same program |
| `venv` fails with ensurepip error (Debian/Ubuntu) | `python3-venv` not installed | ask, then `sudo apt-get install -y python3-venv`, or `--without-pip` |
| Test suite: one cookie test fails on a fresh resolve | fixed in current main; older clones pin `-c constraints.txt` | pull, or install with the constraints file |

## Uninstall

Run the report first; it lists exactly what exists and what still references the install:

```
<venv-python> -m atlas_plus.uninstall_cmd
```

Then, **ASK** before each of these, because the corpus is the human's notes:

1. `<venv-python> -m atlas_plus.uninstall_cmd --all --yes --confirm-path <corpus path> --confirm-state-path <state path>`
   removes the state dir and the corpus. Both paths must be named (the report prints them);
   the state dir is not a cache: it holds captured transcripts, the task database, the
   intake ledger, sync devices and research reports. A corpus containing a `.sextant-protect` marker refuses deletion until the
   human removes the marker by hand. A state dir that overlaps the corpus, home, or workspace
   root is refused outright.
2. `claude mcp remove -s user sextant` (or the equivalent in the client they used), and remove
   any Stop or SessionStart hook entries that point at the clone from `~/.claude/settings.json`.
3. The session log the summarizer wrote (its path is in the report when one exists) is
   never deleted by the tool; the human decides.
4. `pip uninstall sextant-ai` inside the venv, or simply delete the clone and its `.venv`.

Use the same `ATLAS_*` environment variables you used at install time, or the report will
describe a different (probably empty) location.
