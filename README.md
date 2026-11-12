<picture>
  <source media="(prefers-color-scheme: dark)" srcset="https://usesextant.com/brand/social/readme-banner-dark-2560x640.png">
  <img alt="Sextant" src="https://usesextant.com/brand/social/readme-banner-light-2560x640.png">
</picture>

# Sextant

[![CI](https://github.com/protechtus/sextant/actions/workflows/ci.yml/badge.svg)](https://github.com/protechtus/sextant/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](https://github.com/protechtus/sextant/blob/main/LICENSE)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)

> Every LLM session should start with the context of every previous session -- without manual priming.

Sextant is a self-hosted AI memory and orchestration platform (engine codename: Keel).
MCP -- the Model Context Protocol -- is the plug-in standard AI clients like Claude Code,
Claude Desktop, and Cursor use to reach external tools; Sextant speaks it natively.
It captures session transcripts, curates a structured markdown knowledge corpus, and
coordinates a local multi-agent task queue -- all on your own machine, no cloud
dependencies required.

**Who this is for:** people who use a terminal- or desktop-based MCP client --
Claude Code, Claude Desktop, Cursor, Codex CLI, opencode, or any other MCP
client -- and are comfortable cloning a repo
and running a few commands. If you only use claude.ai in the browser, Sextant
cannot connect to it yet; the closest partial path is
[Sextant Clip](https://github.com/protechtus/sextant/blob/main/extensions/sextant-clip/README.md), and it still needs the
install below first. Being honest about that now saves you ten minutes.

**Already using Claude Code's built-in memory?** Keep it if it is working:
it is free, it is editable local markdown with an index, and for one person
in one client on one machine it is often enough. Sextant is for when that
stops being true -- one corpus shared across every MCP client, across
machines (sync), and across repos; full-text plus local semantic search
over all of it; confirmations and anti-memories; a scheduled
re-verification pass that flags what needs rechecking; and importers for
the notes you already have (Obsidian, ChatGPT/Claude exports). Claude
Code's memory is per-project and, per its docs, not shared across
machines; if that has never bothered you, the built-in is the right call.

**Quick facts:**

- 30 MCP tools -- memory read/write, hybrid search (keyword full-text search fused with local semantic embeddings), temporal knowledge graph, tickets, project state
- Local Brain -- a background task agent running on Ollama (a free local-model runtime; any OpenAI-compatible server works too) with a durable task queue, worker leases, and retries
- Telegram bot for full mobile control; scheduled nightly memory-hygiene jobs
- Session hooks for Claude Code (other clients get the same priming text from `sextant-session-start` to paste into their instructions file, and the MCP server tells every client's agent to search memory first): automatic priming at session start (`deploy/session_start_hook.json` -- memory highlights and open work injected before your first message, read-only, safe to enable immediately) and summarization at session end (`deploy/stop_hook.json` -- activates once you enable background writes with `ATLAS_AUTONOMOUS=on`; inert until then, and the summary itself needs a reachable model: Ollama, or a provider key with `ATLAS_LOCAL_ONLY=off`)
- Thousands of automated tests, run in CI on Linux, macOS, and Windows on every push

## What it does

| Problem | Sextant's answer |
|---------|------------------|
| Each LLM session starts from scratch | MCP memory server exposes curated facts to any MCP-aware client |
| Transcripts accumulate but are never mined | A stop hook summarizes each session (once `ATLAS_AUTONOMOUS=on`); suggest-facts proposes durable memory updates |
| Facts silently go stale | Temporal knowledge graph + decay tracking; a scheduled re-verification pass re-checks overdue facts and reports what no longer holds |
| Manual tasks pile up across agents | Brain task queue dispatches work to local model workers, with an optional post-run test check that reopens a ticket on failure |
| System health is invisible | `sextant-doctor` self-health CLI, HTMX web dashboard, notification digests |
| Good web-chat threads are trapped in the browser | **Sextant Clip** ([extensions/sextant-clip](https://github.com/protechtus/sextant/blob/main/extensions/sextant-clip/README.md)): one click sends the claude.ai / ChatGPT / Gemini chat you are reading into your memory -- no more begging the model for a markdown export |

## Quickstart

> **Installing with an AI agent?** Tell it: *"Read https://usesextant.com/agent-install.md and
> follow it."* The same runbook ships here as [docs/AGENT-INSTALL.md](https://github.com/protechtus/sextant/blob/main/docs/AGENT-INSTALL.md): numbered
> steps, a verify command after each, and explicit points where the agent must ask you first.

Supported platforms: Windows, Linux, macOS; Python 3.11+. git required.
You also need an MCP client to connect to -- the walkthrough below uses the
Claude Code CLI. If `claude --version` prints nothing, install Claude Code
first ([anthropic.com/claude-code](https://www.anthropic.com/claude-code) has
the installer for each OS); nothing in this quickstart installs it for you.
Using Codex CLI or opencode instead? `sextant setup --client codex` and
`sextant setup --client opencode` print their registration forms, and
GETTING-STARTED section 4 explains what they do and do not get.

Prefer containers? Generate a `.env` with your keys first (at minimum
`ATLAS_MCP_TOKEN`; `ATLAS_WEB_API_KEY` for the dashboard), then
`docker compose up -d mcp web` -- both steps in [docs/HOWTO-docker.md](https://github.com/protechtus/sextant/blob/main/docs/HOWTO-docker.md).

Install from a clone as shown below (the clone also gives you the docs and the test
suite; `sextant verify` and `sextant setup` work in every kind of install). Two other
ways once you know what you want:
`pip install git+https://github.com/protechtus/sextant.git` installs the current
main without a clone (with the optional layers:
`pip install "sextant-ai[telegram,summarizer] @ git+https://github.com/protechtus/sextant.git"`;
`sextant setup` and `sextant verify` then replace every clone-only step), and
`pip install sextant-ai` installs the released
package from PyPI (v0.1.0 and later). Do NOT `pip install
sextant` -- that is an unrelated third-party package (a helm-charts CLI), not
this project.

**Git Bash / MSYS on Windows?** Follow the PowerShell block's LAYOUT, not the
Linux one: a Windows venv has `.venv/Scripts/python.exe` and no `.venv/bin/`
(the Linux paths fail, or worse, register an MCP command that only breaks
later inside Claude Code). `py -3 -m venv .venv` works in Git Bash, forward
slashes are fine, and in `claude mcp add` use
`"$(pwd -W)/.venv/Scripts/python.exe"` (the real `C:/...` path).

Windows (PowerShell):

Before the block: `py --list` must show a 3.11 or newer, and the venv line
should name it by number (`py -3.12 -m venv .venv`). A plain `python -m venv`
on an older interpreter fails before Sextant is involved (an `ensurepip ...
exit status 101` or similar, plus a half-made `.venv` to delete before
retrying), and `pip install` on an older Python stops with
`requires a different Python: 3.10.x not in '>=3.11'` and installs nothing.
Installing with `--no-build-isolation` needs setuptools 77 or newer in that
environment first (`pip install -U setuptools`).

```powershell
git clone https://github.com/protechtus/sextant.git; cd sextant
py -3 -m venv .venv   # needs Python 3.11+ (check: py --list; install: winget install Python.Python.3.12 or python.org)
.\.venv\Scripts\python.exe -m pip install -U pip setuptools
.\.venv\Scripts\python.exe -m pip install -e ".[dev,telegram,summarizer]"

# Smoke it (venv-qualified so nothing depends on PATH or activation):
.\.venv\Scripts\sextant.exe --version
.\.venv\Scripts\sextant.exe commands

# Choose where your memory lives BEFORE the next step (skip this block to accept
# ~\.memory, ~\.atlas and ~\projects; persist with setx so hooks and clients agree):
$env:ATLAS_WORKSPACE_ROOT = "C:\sextant-data"        # re-roots the three below
$env:ATLAS_MEMORY_ROOT    = "$env:ATLAS_WORKSPACE_ROOT\.memory"
$env:ATLAS_STATE_DIR      = "$env:ATLAS_WORKSPACE_ROOT\.atlas"
$env:ATLAS_PROJECTS_ROOT  = "$env:ATLAS_WORKSPACE_ROOT\projects"

# Scaffold your memory corpus there (safe to re-run):
.\.venv\Scripts\sextant-init.exe

# Optional: run the full test suite (thousands of tests, about 3-5 minutes; the telegram and summarizer tests skip unless those extras are installed):
.\.venv\Scripts\python.exe -m pytest tests -q
```

Linux / macOS:

Before the block: you need a Python 3.11+ as `python3`, plus git. Fedora and
other RPM distributions: `sudo dnf install git python3` is the whole
prerequisite (the venv module and pip come with it). Stock macOS does not
have one -- the first `git` or `python3` run pops the Xcode Command Line Tools
dialog (let it finish; its Python is too old anyway), then install from
[python.org](https://www.python.org/downloads/) or `brew install python@3.12`.
Ubuntu 24.04 ships Python 3.12, but creating a venv needs
`sudo apt install python3.12-venv` first -- if you skip it, the venv error
message names the exact package; delete the half-made `.venv` it leaves behind
before retrying with the package installed (the no-sudo fallback in
GETTING-STARTED, `venv --without-pip`, works on top of it as is).

```bash
git clone https://github.com/protechtus/sextant.git && cd sextant
python3 -m venv .venv        # needs 3.11+ (check: python3 --version)
.venv/bin/python -m pip install -U pip setuptools
.venv/bin/python -m pip install -e ".[dev,telegram,summarizer]"

# Smoke it:
.venv/bin/sextant --version
.venv/bin/sextant commands   # discover the whole CLI family

# Choose where your memory lives BEFORE the next step (skip this block to accept
# ~/.memory, ~/.atlas and ~/projects; put the exports in your shell profile to persist):
export ATLAS_WORKSPACE_ROOT="$HOME/sextant-data"     # re-roots the three below
export ATLAS_MEMORY_ROOT="$ATLAS_WORKSPACE_ROOT/.memory"
export ATLAS_STATE_DIR="$ATLAS_WORKSPACE_ROOT/.atlas"
export ATLAS_PROJECTS_ROOT="$ATLAS_WORKSPACE_ROOT/projects"

# Scaffold your memory corpus there (safe to re-run):
.venv/bin/sextant-init

# Optional: run the full test suite (thousands of tests, about 3-5 minutes; the telegram and summarizer tests skip unless those extras are installed):
.venv/bin/python -m pytest tests -q
```

(`[dev,telegram,summarizer]` pulls everything the full test suite exercises;
plain `.[dev]` works if you skip those features. Prefer activating the venv?
`.venv\Scripts\Activate.ps1` / `source .venv/bin/activate` makes the bare
`sextant` commands work everywhere below.)

Everything resolves home-relative by default (`~/.memory` for the knowledge corpus,
`~/.atlas` for derived state, `~/.atlas/updates.txt` for the chronological log). Override with
env vars: `ATLAS_MEMORY_ROOT`, `ATLAS_STATE_DIR`, `ATLAS_WORKSPACE_ROOT`,
`ATLAS_IDENTITY_PATH`, `ATLAS_USER_NAME`.

One trap if you override: an env var set only in your shell profile
(`.zshrc`, `.bashrc`, PowerShell `$PROFILE`) reaches the MCP server only
when the client was launched from that shell. A GUI-launched Claude Code
(Dock, Start menu, IDE panel) never reads your profile, and the server
silently falls back to `~/.memory`. Bake overrides into the registration
instead: `claude mcp add -s user sextant -e ATLAS_MEMORY_ROOT=/your/path -- ...`.

Connect it to Claude Code with one command. The install prints its own,
with the interpreter path quoted and any `ATLAS_*` path override you set
baked in (required if you relocated the corpus above):

```
.\.venv\Scripts\sextant.exe setup --client claude-code    # Windows
.venv/bin/sextant setup --client claude-code              # Linux / macOS
```

Or type it -- run it from your sextant clone, then restart Claude Code.
`-s user` registers it for your whole account (without it, the default
`local` scope ties the server to this one directory, and it silently will
not exist when you open Claude Code in your actual projects); `$PWD` bakes
in the absolute path, and the quotes matter when the path has a space:

Windows (PowerShell):

```powershell
# Run this IN your sextant clone -- $PWD bakes the path in. New terminal tab? cd back first.
claude mcp add -s user sextant -- "$PWD\.venv\Scripts\python.exe" -m atlas_plus.mcp.memory_server.server --stdio
claude mcp list   # expect a sextant row; "failed" means the baked path is wrong
```

Linux / macOS:

```bash
# Run this IN your sextant clone -- $PWD bakes the path in. New terminal tab? cd back first.
claude mcp add -s user sextant -- "$PWD/.venv/bin/python" -m atlas_plus.mcp.memory_server.server --stdio
claude mcp list   # expect a sextant row; "failed" means the baked path is wrong
```

Verify without leaving the terminal: `sextant verify` starts the server the
way a client would, writes one fact and searches it back (`ROUND-TRIP: OK`).
Then restart Claude Code and ask Claude to call `memory_health`. The
healthy reply is `{"status": "ok", "memory_root_exists": true, "memory_root": ...}`
(check the root is the corpus you chose). If it
answers `"status": "degraded"` instead, the reply's `hint` says why --
usually `sextant-init` was skipped, or an `ATLAS_MEMORY_ROOT` override
never reached the server (see the note below the quickstart). If the tool
is missing entirely: restart Claude Code first, then `claude mcp list`;
a failed row means re-registering from inside the clone
(`claude mcp remove -s user sextant`, then the add command again).
(Undo later with `claude mcp remove -s user sextant` -- the full removal
walkthrough is in the Uninstalling section of
[docs/GETTING-STARTED.md](https://github.com/protechtus/sextant/blob/main/docs/GETTING-STARTED.md).)

The clone path is load-bearing from here on: the MCP registration, the
session-hook commands and the editable install all hard-code it. If you
move or rename the clone later, re-run `python -m pip install -e .` with
the venv's own interpreter from the new location (the venv's `pip` and
`sextant` launchers die silently until then), then `claude mcp remove -s user sextant` and re-add, and fix any
hook paths. (Installing with pipx or `uv tool` instead of a venv? There is
no `.venv` and no `deploy/` folder then; `sextant setup` prints the
registration for that install, and `sextant verify` proves it. The short
form `claude mcp add -s user sextant -- sextant-mcp --stdio` also works when
`sextant-mcp` is on PATH.)

The full walkthrough -- config-file locations for Claude Desktop and
Cursor, the session hooks, troubleshooting -- is
[docs/GETTING-STARTED.md](https://github.com/protechtus/sextant/blob/main/docs/GETTING-STARTED.md). Prefer hand-merged
JSON? The snippets live in `deploy/` (`claude_code_config.snippet.json`
for the server; start-of-session priming and end-of-session summarization
each ship in two platform forms -- `session_start_hook.json` /
`stop_hook.json` for Windows, the same names with `.macos.json` for
macOS/Linux; each carries a `_note` saying where and how to merge it,
including what to do when your settings file already has a hooks key).

### Let Claude Code do the install

If terminals are not your thing, your assistant can run this quickstart
for you. Open Claude Code in any folder and paste:

> Install Sextant for me: clone https://github.com/protechtus/sextant.git
> into a folder of my choosing, then read docs/AGENT-INSTALL.md in the clone
> and follow it step by step, asking me at every point it marks ASK. Find a
> Python 3.11+ interpreter first and tell me if there is none; on macOS,
> pause and tell me if the Xcode Command Line Tools dialog needs my click.
> Register the MCP server with the command `sextant setup` prints, verify
> with `sextant verify` and `claude mcp list`, then stop and tell me to
> restart Claude Code. Do NOT set ATLAS_AUTONOMOUS, do NOT edit my hooks or
> settings.json unless I say yes, and do NOT `pip install sextant` (wrong
> package).

That runbook and this quickstart agree: the agent ends with the same
`memory_health` check, and the do-not list is the same one the runbook
carries. Hooks and background writes stay yours to opt into after reading
the security section below.

Running it on a server instead? [docs/HOWTO-self-hosting.md](https://github.com/protechtus/sextant/blob/main/docs/HOWTO-self-hosting.md)
walks through a VPS deployment (systemd + Caddy + HTTPS, or a private Tailscale mesh)
with every device syncing against it via [docs/HOWTO-sync.md](https://github.com/protechtus/sextant/blob/main/docs/HOWTO-sync.md).

## Architecture

```
+----------------------------------------------------------+
|                     Client layer                          |
|   Claude Code    any MCP client    Mobile (Telegram)      |
+--------+----------------+------------------+--------------+
         |  MCP :8765     |  HTTP :8767      |  Telegram API
         v                v                  v
+-----------------+ +----------------+ +------------------+
|  Memory server  | |  Brain         | |  Telegram bot    |
|  30 tools       | |  task queue +  | |  mobile control  |
|  FTS5 search    | |  local models  | |                  |
+--------+--------+ +-------+--------+ +--------+---------+
         |                  |                   |
         v                  v                   v
+----------------------------------------------------------+
|   ~/.memory  (curated markdown corpus, human-editable)    |
|   ~/.atlas   (derived state: indexes, temporal graph)     |
+----------------------------------------------------------+
```

Design principles:

- **Markdown is the source of truth.** The memory corpus is plain files you can read,
  edit, and version yourself. Everything else (FTS index, temporal graph) is derived
  and regenerable (the state dir also holds transcripts and the task
  queue, which are not).
- **Fail closed.** Every network surface requires a token, and services
  only listen on your own machine unless you deliberately expose them.
  Two separate write switches: asking your assistant to remember
  something works out of the box (your request is the consent), while
  BACKGROUND jobs that edit memory unattended stay off until you enable
  `ATLAS_AUTONOMOUS`. (`ATLAS_INTERACTIVE_WRITES=off` locks down the
  interactive path too.)
- **Version before edit.** Every memory write snapshots the previous state first.

For a fuller walkthrough of the control plane, memory model, and scheduler, see
[ARCHITECTURE-PUBLIC.md](https://github.com/protechtus/sextant/blob/main/docs/ARCHITECTURE-PUBLIC.md). The day-to-day
guide to actually using the memory tools is
[docs/USAGE.md](https://github.com/protechtus/sextant/blob/main/docs/USAGE.md), and every environment variable is
cataloged in [docs/CONFIGURATION.md](https://github.com/protechtus/sextant/blob/main/docs/CONFIGURATION.md). Deeper docs
live in `docs/` (`ARCHITECTURE.md`, the ADRs under `docs/adr/`, and
per-feature HOWTOs).

If a freshly released dependency ever breaks the install, fall back to
the known-good exact pins: append `-c constraints.txt` to the pip
command above.

## How it compares

**Claude Code built-in memory**: the honest first comparison, covered in "Who
this is for" above -- free, editable markdown, per-project and per-machine.
Sextant's case is everything cross-: client, machine, repo -- plus search,
confirmations, staleness re-checks, and importers.

Mem0, Zep, and Letta are excellent hosted/managed memory layers for agents you are
building. Sextant sits somewhere different: it is the memory and orchestration layer
for YOUR OWN daily LLM sessions -- transcript capture, a human-editable markdown
corpus you fully own, staleness detection with a scheduled re-verification pass
that flags what needs rechecking (facts that go confidently wrong over time are
a named open problem in this space), and a local
task queue -- with no cloud dependency and no per-seat pricing. If you want a
managed API for product agents, use one of the hosted platforms; if you want your
own second brain under your own roof, that is what this is.

## Status

Actively developed. The engine has run as its author's daily driver since early 2026;
this public repository is a curated cut of that system. CI runs the full test suite
on Linux, macOS, and Windows on every push. The maintainer's own service supervision
(a scheduled-task installer and PowerShell backup fleet) lives in the private deploy
tree and is NOT shipped here: on Windows you run the servers in a terminal or write
your own Task Scheduler entries for the commands in the alias table; on Linux,
[docs/HOWTO-self-hosting.md](https://github.com/protechtus/sextant/blob/main/docs/HOWTO-self-hosting.md) has systemd units.
Everything else is env-configurable and portable.

**A note on names:** Keel is the engine's internal codename; Sextant is its public
name. The PyPI distribution is `sextant-ai` -- note that `pip install sextant`
installs an unrelated third-party package (a helm-charts CLI), not this project.
The import package stays `atlas_plus`,
and you may still meet the internal project label "atlas-memory+" in docstrings and
defaults. Same engine throughout. The practical rule: anything that says
atlas-SOMETHING is this same program wearing its engine name -- never a second
thing to install or debug.

The user-facing commands ship under both names -- use whichever you prefer
(`sextant-*` recommended for new installs; the full `atlas-*` family remains
available and is what the deeper docs reference):

| `sextant-*` command | alias of | what it does |
|---|---|---|
| `sextant` | `atlas-plus` | primary CLI (`sextant commands` lists everything) |
| `sextant-init` | `atlas-init` | scaffold a fresh memory corpus |
| `sextant-doctor` | `atlas-doctor` | self-health checks |
| `sextant-mcp` | `atlas-memory-mcp` | the MCP memory server |
| `sextant-web` | `atlas-web-ui` | web dashboard |
| `sextant-brain` | `atlas-brain` | local Brain task-queue server |
| `sextant-backup` | `atlas-backup` | backup tooling |
| `sextant-transcripts` | `atlas-transcripts` | transcript capture/search |
| `sextant-dream` | `atlas-dream` | nightly synthesis ("dream") job |
| `sextant-sync` | `atlas-sync` | multi-device corpus sync (one hub, your other devices check in) |
| `sextant-import` | `atlas-import` | import existing notes (Obsidian vaults, ChatGPT/Claude exports; report-first) |
| `sextant-embed-index` | `atlas-embed-index` | (re)build the local semantic-search embedding index |
| `sextant-uninstall` | `atlas-uninstall` | report or cleanly remove Sextant's data (safety-gated) |
| `sextant-reset` | `atlas-reset` | wipe an area and re-scaffold it fresh (same safety gates) |

(The full `atlas-*` family includes further internal/service commands without
`sextant-*` twins; `sextant commands` lists everything installed.)

## Security model, honestly

What Sextant does:

- **Loopback by default.** Every service (MCP :8765, web :8766, Brain :8767,
  sync hub :8768) binds 127.0.0.1. Nothing listens on your network unless
  you deliberately expose it (see the self-hosting guide for doing that
  behind a reverse proxy or a Tailscale mesh).
- **Bearer tokens on every network surface**, rate-limited against remote
  brute force. No token, no access -- endpoints fail closed. (Each server has a
  `--no-auth` flag for local development; it only ever binds loopback, pins the
  Host header, and is never needed for the documented setup.)
- **Two write switches, deliberately separate.** Asking your assistant to
  remember something is an interactive write: your request is the consent,
  on by default (`ATLAS_INTERACTIVE_WRITES=off` makes the memory tools
  effectively read-only). Background writes -- nightly jobs editing memory
  unattended -- are gated by `ATLAS_AUTONOMOUS`, off by default and
  fail-closed on any value except an explicit on/1/true.
- **Crash-safe corpus writes**: cross-process locking plus atomic
  write-and-replace, so a crash mid-write never leaves a half-written
  memory file.
- **The dashboard's Services panel can start/stop a fixed allowlist of
  Windows Scheduled Tasks** (the maintainer's reference-deployment names,
  behind the same bearer auth). On an install without those tasks the
  panel shows `?` and the buttons are no-ops -- but know that the web
  process shells out to `Start-/Stop-ScheduledTask` for that allowlist.

Every claim above is held by automated tests -- the mapping is in
[docs/SECURITY-TEST-MATRIX.md](https://github.com/protechtus/sextant/blob/main/docs/SECURITY-TEST-MATRIX.md), so you can
check our homework.

What it does not do (know these before you rely on it):

- **Local by default, and local by force unless you say otherwise.** The
  session-summarizer's `auto` mode is local-only: an `ANTHROPIC_API_KEY` in
  your environment never sends transcript text anywhere by itself. Only
  `ATLAS_LOCAL_ONLY=off` enables the cloud fallback (Anthropic for the
  summarizer, OpenAI for other roles routed through the provider layer;
  see CONFIGURATION.md). `ATLAS_LOCAL_ONLY=on`
  refuses every cloud provider, everywhere -- including the summarizer's
  own fallback path, enforced in code on both. Separately, the web
  dashboard loads two UI libraries (htmx, Tailwind) from public CDNs:
  your browser contacts them, the data path stays local; offline, the page
  says so and its panels stay empty while the JSON endpoints keep working.
- **Two opt-in features make their own outbound calls.** The Brain's
  research executor queries a web search API (DuckDuckGo by default, or
  your own SearXNG instance) with the research topic, only when
  `ATLAS_RESEARCH_WEB_SEARCH=on`. The curation link checker sends HEAD
  requests to URLs found in your own reports when you run it. What goes
  out is the topic string and the URLs themselves, nothing more.
- **Autonomous mode snapshots your working trees.** With `ATLAS_AUTONOMOUS`
  on (off by default), the session-end hook records `git status`, the
  branch and commit, the subjects of the last five commits of the day, and
  up to 4 KB of `git diff HEAD` per repo under your projects directory into
  the state dir -- that is uncommitted work, which can include not-yet-removed
  secrets. Know that before you enable it. The same hook also launches
  the suggest-facts pass and a temporal index build in the background --
  all local work, listed so nothing surprises you in a process list.
  (`ATLAS_SESSION_AUTONOMOUS=on` enables the same hook for a single
  supervised session -- the same disclosures apply.)
- **No encryption at rest.** The corpus is plain markdown on your disk by
  design -- your data, greppable. Full-disk encryption and encrypted
  backups (e.g. an rclone crypt remote) are your job.
- **No multi-user auth.** One operator, one token per surface. It is not a
  team product; do not point five people at one instance and expect
  isolation.
- **Prompt-level risks stay yours.** Anything an LLM reads from memory can
  influence it; Sextant stores and serves your notes, it does not referee
  what your model does with them.
- **The first page load may carry the key in the URL.** Opening
  `http://127.0.0.1:8766/?key=...` once exchanges the key for a random,
  HttpOnly, SameSite=Strict session cookie; from then on every link,
  download, live-update stream and the chat WebSocket authenticates with
  that cookie, the page itself never contains the key, and `?key=` is
  refused on every other route. That one bootstrap URL still lands in
  browser history, so prefer a bookmark without the key afterwards.
  Scripted clients (curl and friends) use the `Authorization: Bearer`
  header. The web UI does not log request URLs. Restarting the web UI
  logs every browser out; open the bootstrap URL again.
- **Push notifications transit a third party unless you self-host.** The
  optional ntfy layer defaults to the public ntfy.sh service; task text
  you push goes through their servers. Point `ATLAS_NTFY_BASE` at your
  own ntfy instance if that matters to you.

## Contributing and security reports

See [CONTRIBUTING.md](https://github.com/protechtus/sextant/blob/main/CONTRIBUTING.md) and the honest [ROADMAP.md](https://github.com/protechtus/sextant/blob/main/ROADMAP.md).
Security reports: [SECURITY.md](https://github.com/protechtus/sextant/blob/main/SECURITY.md)
-- please do not open public issues for vulnerabilities.

## License

Apache-2.0. See `LICENSE` and `NOTICE` (Copyright 2026 Protechtus LLC).
