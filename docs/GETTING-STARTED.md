# Getting started with Sextant

About ten minutes from clone to a memory your sessions actually use on a
machine that already has Python 3.11+, git and Claude Code (the README
quickstart's prerequisite notes cover getting those); budget half an hour the
first time if any of them is missing. Commands below say `python`; on Debian and Ubuntu the system interpreter is
`python3`, and inside an activated venv both names work. This page assumes
you finished that quickstart (clone, venv, install; the test suite is
optional).

Command names: every user-facing command installs under both a `sextant-*`
name and an `atlas-*` name (the engine's internal names -- see the README's
alias table). They are identical; this page uses `sextant-*`.

Bare commands below assume an ACTIVATED venv, on any platform. (The venv
is the `.venv` folder the quickstart created -- a self-contained Python
environment inside your clone; "activating" it points your current
terminal at it.) Activate with `source .venv/bin/activate` (Linux/macOS)
or `.venv\Scripts\Activate.ps1` (Windows PowerShell -- if that errors with
"running scripts is disabled", run
`Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` once). Or skip
activation entirely and prefix each command the way the README quickstart
does: `.venv/bin/sextant-init` on Linux/macOS,
`.\.venv\Scripts\sextant-init.exe` on Windows, and so on.

## 1. Create your memory

Decide where it lives first. The default is `~/.memory` (corpus), `~/.atlas`
(derived state) and `~/projects` (where the session hook looks for your
repos). To put everything somewhere else, set the variables BEFORE
`sextant-init`, and persist them (`setx` on Windows, your shell profile on
POSIX) so terminals, hooks and GUI-launched clients all agree:

```bash
export ATLAS_WORKSPACE_ROOT="$HOME/sextant-data"   # PowerShell: $env:ATLAS_WORKSPACE_ROOT = "C:\sextant-data"
export ATLAS_MEMORY_ROOT="$ATLAS_WORKSPACE_ROOT/.memory"
export ATLAS_STATE_DIR="$ATLAS_WORKSPACE_ROOT/.atlas"
export ATLAS_PROJECTS_ROOT="$ATLAS_WORKSPACE_ROOT/projects"
```

```bash
sextant-init
```

This scaffolds the corpus (default `~/.memory`, override with
`ATLAS_MEMORY_ROOT`) and the state directory (`~/.atlas`: transcripts, task queue,
indexes), and never
touches anything that already exists:

```
.memory/
  identity/user.md    <- start here
  feedback/           <- durable corrections and preferences
  projects/           <- one file per ongoing project
  reference/          <- external facts worth keeping
  sessions/           <- session logs (hooks write these)
  decisions/, anti/   <- appear on first use (decisions; "never suggest again")
  versions/, _atlas/  <- history copies and sync/intake internals (never edit)
```

## 2. Introduce yourself

Open `identity/user.md` and replace the template: what you go by, how you
like to work, what you are focused on. Sessions are primed from this file --
write it like a briefing for a sharp new collaborator.

## 3. Check the instruments

```bash
sextant-doctor        # health of each subsystem
sextant components    # what's here, what's running, what's safe to turn off
```

Doctor reports each subsystem's state. On a fresh machine expect the memory
corpus green, and services (Brain, web) not running -- that is fine; they are
optional layers you can start later.

## 4. Wire your MCP client

The install prints its own registration, with the interpreter path quoted and
any `ATLAS_*` path override from step 1 baked in; run it and copy what it shows:

```
sextant setup                  # Claude Code command, client JSON, optional hooks
sextant setup --client claude-code   # just the one line
```

Typed by hand, the Claude Code line is (no config file to find):

```powershell
# Windows (run from your Sextant directory; $PWD bakes in the absolute path)
claude mcp add -s user sextant -- "$PWD\.venv\Scripts\python.exe" -m atlas_plus.mcp.memory_server.server --stdio
```

```bash
# Linux / macOS
claude mcp add -s user sextant -- "$PWD/.venv/bin/python" -m atlas_plus.mcp.memory_server.server --stdio
```

Two traps, both silent:

- Use `-s user` scope. The default (`local`) ties the registration to the
  directory you ran the command in -- open Claude Code in any other project
  and the server simply is not there.
- Use the absolute path (that is what `$PWD` gives you): a relative
  `./.venv/...` only works when Claude Code happens to be launched from
  this directory, and fails confusingly from anywhere else.

Prefer editing config by hand, or using Claude Desktop? The snippets in
`deploy/` show the exact JSON to merge (placeholders marked
`<LIKE_THIS>`). Where those config files live:

- **Claude Code**: `.mcp.json` in your project directory (shared with the
  project) or `~/.claude.json` (personal, all projects).
- **Claude Desktop**: `%APPDATA%\Claude\claude_desktop_config.json` on
  Windows (the Microsoft Store build keeps it under
  `%LOCALAPPDATA%\Packages\Claude_*\LocalCache\Roaming\Claude\` instead; if
  `%APPDATA%\Claude` does not exist, look there); `~/Library/Application
  Support/Claude/claude_desktop_config.json` on macOS.
- **Cursor**: `~/.cursor/mcp.json` (every project) or `.cursor/mcp.json`
  inside a project. `sextant setup --client cursor` prints the block;
  `sextant uninstall` reports both locations.

Restart the client and you have 30 memory tools available in-session.
Ask your assistant to call `memory_health` -- the healthy reply is
`{"status": "ok", "memory_root_exists": true}`. A `"degraded"` status
comes with a `hint` field naming the usual causes (step 1 skipped, or an
`ATLAS_MEMORY_ROOT` override that never reached the server process).

Any other MCP-aware client works the same way: it runs the stdio server
as a subprocess (`sextant-mcp --stdio`, which `sextant setup` spells out as
`<venv python> -m atlas_plus.mcp.memory_server.server --stdio` so no PATH
is assumed) WITH the `ATLAS_*` variables from `sextant setup` in the server's
environment, or connects to the HTTP server (`sextant-mcp`, which needs
`ATLAS_MCP_TOKEN` set). The variables are not optional: a registration that
carries only the command shows "connected" in every client while the server
serves an empty default corpus, and the first `memory_health` call says
`degraded`. Two clients have their own shapes, and `sextant setup` prints
both filled in:

- **Codex CLI** (OpenAI): `sextant setup --client codex` prints the
  `codex mcp add` line (PowerShell needs the quoted `"--"`) and the
  `[mcp_servers.sextant]` table for `~/.codex/config.toml`
  (`$CODEX_HOME/config.toml` when set). Paths in TOML are single-quoted
  literal strings; a double-quoted `"C:\sextant\..."` makes Codex refuse the
  whole file. Check with `codex mcp list`.
- **opencode**: `sextant setup --client opencode` prints the `mcp` block
  for `opencode.json` in the project (or
  `~/.config/opencode/opencode.json`): `"type": "local"`, `"enabled": true`,
  `command` as one array, and `environment` for the variables. Check with
  `opencode mcp list` from the project directory.

Neither has Claude Code's session hooks. The server's MCP `instructions`
tell their agent to search memory first, and `sextant-session-start` prints
the same priming text the Claude Code hook injects (identity, clock, house
rules from `identity/house-rules.md` when that file exists, the current
project) for pasting into `AGENTS.md`, opencode's `instructions`, a Claude
Desktop project's instructions, a Cursor rule, or any system prompt;
`atlas-prime "<task words>"` prints task-matched facts the same way. Both
are snapshots: the clock line is the moment you ran them, so re-run and
re-paste when the identity or rules change rather than treating the text
as live. End-of-session summaries exist only through
the Claude Code Stop hook. Before any client is involved, `sextant verify`
starts the stdio server, writes one fact and searches it back
(`ROUND-TRIP: OK`); `sextant verify --command "<the command in your
config>" --env ATLAS_WORKSPACE_ROOT=...` proves the exact registration.
Once it answers, the day-to-day patterns (remembering, decisions,
anti-memory, temporal queries) live in `docs/USAGE.md`.

## 5. First facts, first search

In a wired session, ask your assistant to remember something real ("remember
that my staging server is at 10.0.0.5, scope: reference:infrastructure"), then start a NEW
session and ask about it. That round trip -- write, restart, recall -- is
Sextant doing its one job.

From the shell, the same corpus is yours directly:

```bash
sextant remember "I take my coffee black" --scope preference:coffee   # store a fact
sextant search "coffee"                                               # find it (shows the confidence band)
sextant --help                 # the CLI family
atlas-review-memory summary    # corpus health at a glance
```

## 5b. Bring your existing knowledge

```bash
sextant-import --source obsidian --path ~/MyVault        # report first ($HOME\MyVault in PowerShell; it does not expand ~)
sextant-import --source obsidian --path ~/MyVault --yes --archive-stale   # daily re-import; stale copies go to archive/
sextant-import --source chatgpt --path conversations.json --yes   # or the export .zip as delivered
sextant-import --source claude --path data-export.zip --yes       # claude.ai export (zip or its conversations.json)
```

Imports are structural and provenance-stamped (never overwrite anything).
The report, with or without `--yes`, classifies every note the same way: new,
unchanged, edited (a new `_2` copy lands beside the old one) or renamed (the
note is imported under its new name). Every stale corpus copy is named: older
copies of edited notes and notes that are no longer in the vault. Stale copies
still show up in search until you move them: `--yes --archive-stale` moves
them into `archive/` inside the corpus (never deleted; search ignores that
folder), or move them there by hand. Links are never followed, binary and
undecodable files are skipped, a top-level `Templates/` folder is skipped,
non-markdown files (canvas, images, PDFs) are counted but not imported, and
the first twenty files `--yes` would write are listed. A ChatGPT or claude.ai
conversation an older build already imported under another name is recognised
and not filed again. A ChatGPT export keeps only the live branch
of each conversation (regenerated or edited answers are left out). Keyword
search picks imported notes up on its own (Chinese, Japanese and Korean text is
matched character by character, so `東京` finds `東京都`; full-width letters and
half-width katakana match their ordinary forms); if you run Ollama, refresh the
semantic index with `sextant-embed-index` to enable semantic search
(`ollama pull nomic-embed-text` once, then searches understand meaning,
not just keywords; without it, keyword search works exactly as before).

Facts from the shell: a line that starts with `---` is refused (it would
close the note's frontmatter); dashes mid-sentence are fine. In PowerShell
5.1 a fact that contains double quotes is easier to pass on standard input:
`"Ilse said: \"The sea waits.\"" | sextant remember - --scope project:novel`
(`-` reads the fact from stdin). A topic may name a subfolder note from an
imported vault: `--scope reference:obsidian/README`.

## 6. Optional layers, when you want them

- **Session hooks** for Claude Code -- `sextant setup --client hooks` prints
  the block with your interpreter already in place (the same block ships as
  placeholders in two platform forms, `deploy/*.json` for Windows and
  `deploy/*.macos.json` for macOS/Linux -- pick YOUR platform's file; the
  other one's command will not run on your machine). Merging, step by step: back up your settings
  file first (`~/.claude/settings.json` user-wide, or
  `.claude/settings.json` per-project; create it if missing); if it
  already has a `hooks` key, add the snippet's entry to the existing
  lists instead of replacing them; replace `<ATLAS_PLUS_DIR>` with your
  clone's absolute path (`pwd` inside the clone prints it on POSIX,
  `$PWD.Path` in PowerShell); do not copy
  the `_note` key; sanity-check the result with
  `python -m json.tool ~/.claude/settings.json` (on Windows save the file as
  UTF-8 without a BOM; PowerShell 5.1's `Set-Content -Encoding utf8` adds one and so does its `>` redirect, so use `sextant setup --client hooks --out hooks.json` instead
  and the check then fails). Or simply ask your
  assistant to do the merge -- paste the snippet file and say which
  settings file to put it in.
  - `session_start_hook*` (priming): injects memory highlights,
    the active project, and open work as context before your first message.
    Read-only and fail-fast; safe to enable immediately.
  - `stop_hook*` (summarize): deliberately inert until
    background writes are enabled (`ATLAS_AUTONOMOUS=on`); once on, it
    summarizes the session and extracts high-confidence facts when a model is
    reachable (Ollama, or a provider key with `ATLAS_LOCAL_ONLY=off`; when none
    is, it says so on stderr and in the activity log and skips that part), and
    snapshots `git status` + a bounded `git diff HEAD` from repos under your
    projects directory (see the README's security section before enabling).
- **Brain** (local task queue + tool-calling agent): `sextant-brain` +
  `atlas-brain-worker`. Needs its own key first (`ATLAS_BRAIN_API_KEY`, generated
  the same way as the dashboard key below; it refuses to start without one).
  Runs on Ollama by default -- that is a separate,
  free program that runs models on your own machine; install it from
  ollama.com, then `ollama pull qwen3:8b` (the default Brain model).
  Semantic search uses it too (`ollama pull nomic-embed-text`). Without
  Ollama these layers simply stay off; everything else keeps working.
  Already running something else? Set `ATLAS_MODEL_BRAIN_PROVIDER=openai`
  with `OPENAI_BASE_URL` pointing at any OpenAI-compatible server
  (LM Studio, vLLM, llama.cpp), or `anthropic` with an API key. Your
  models, your choice.
- **Web dashboard**: needs a key first --
  `python -c "import secrets; print(secrets.token_urlsafe(32))"`,
  set it as `ATLAS_WEB_API_KEY` (persistently: see docs/CONFIGURATION.md's
  note on setting variables), then `sextant-web` and open. The page itself
  loads two UI libraries (htmx, Tailwind) from public CDNs in your browser:
  offline or with those hosts blocked, every panel stays at "loading..." and
  the page says so; the server's JSON endpoints still answer. Open
  `http://127.0.0.1:8766/?key=<your key>` once; that exchanges the key for a
  session cookie and the bare URL works from then on (without the cookie it
  answers 401, on purpose). Without the key the server refuses to start.
- **Telegram control**: optional; `python -m pip install -e ".[telegram]"` from a clone,
  `pip install "sextant-ai[telegram]"` for a package install
  (in a uv-made venv there is no pip -- use `uv pip install -e ".[telegram]"`), set
  `ATLAS_TELEGRAM_BOT_TOKEN` (from @BotFather) and `ATLAS_TELEGRAM_ALLOWED_USERS`
  (your numeric Telegram user id, from @userinfobot; the bot refuses to run
  without an allowlist), then run `atlas-telegram-bot`.

Everything binds loopback and fails closed by default. Two write switches,
deliberately separate: asking your assistant to remember something is an
INTERACTIVE write -- your consent is the request -- and works out of the box
(disable with `ATLAS_INTERACTIVE_WRITES=off`). BACKGROUND writes (nightly
jobs editing memory unattended) are gated by `ATLAS_AUTONOMOUS`, off by
default; leave it off until you trust the loop.

## Backing up

Your corpus is a directory of plain markdown -- backing it up is copying a
folder. Zip it, sync it with any tool you already trust (rclone, Syncthing,
a cloud-drive client), or make it a git repo and push it somewhere private.
Everything else (`~/.atlas`, the FTS and embedding indexes) is derived state
that `sextant-fts-rebuild` (alias `atlas-fts-rebuild`) and `sextant-embed-index` rebuild from the markdown,
so the corpus is the only thing you need to protect. If you back up to a
shared or cloud location, encrypt first (e.g. an rclone crypt remote) --
your memory is the most personal data on the machine.

`sextant-backup` is the optional rclone-based way to do that: it needs
rclone on your PATH and a configured remote named by `ATLAS_RCLONE_REMOTE`
(`ATLAS_RCLONE_ROOT` picks the folder; `sextant-backup verify` checks the
setup, `sextant-backup now --dry-run` shows what would be copied). It
refuses to run until the remote exists, and no scheduled job is installed
for you: run it by hand or schedule `sextant-backup now` yourself.

## Troubleshooting: port already in use

Sextant's services default to four loopback ports: MCP `8765`, web
dashboard `8766`, Brain `8767`, sync hub `8768`. If one refuses to start
or `sextant-doctor` shows a service you never launched, something else
(or a second Sextant install) is squatting the port.

Find the squatter:

```powershell
# Windows
netstat -ano | findstr :8766
tasklist /fi "PID eq <pid-from-netstat>"
```

```bash
# Linux / macOS
lsof -i :8766
```

Move Sextant off the contested port with the matching environment
variable: `ATLAS_MCP_PORT`, `ATLAS_WEB_PORT`, `ATLAS_BRAIN_PORT`, or
`sextant-sync serve --port <n>` for the hub. (Every variable Sextant
reads is cataloged in `docs/CONFIGURATION.md`.) If you move the Brain, also
point the dashboard at it (`ATLAS_BRAIN_URL=http://127.0.0.1:<n>`); if you
move the hub, tell doctor with `ATLAS_SYNC_PORT=<n>` (or `ATLAS_SYNC_URL`).
Remember `sextant-doctor`'s service rows are local port probes -- they
report whatever answers on those ports, including a different install.

## Uninstalling (cleanly)

Work top to bottom: unhook Sextant from your MCP client first, then remove
data, then remove the code. If you delete the clone first, Claude Code keeps
trying to launch a server and a Stop hook that no longer exist, and errors
on every session.

**Know what is whose before deleting.** The corpus (`~/.memory`) mixes notes
YOU wrote (`identity/`, `feedback/`, `projects/`, `reference/`) with files
Sextant wrote (`sessions/`, `versions/`, `tickets/`, `inbox/`). Copy anything
you want to keep first. If you ever set `ATLAS_MEMORY_ROOT` or
`ATLAS_STATE_DIR` (shell profile, or the MCP snippet's `env` block), export
the same values in the shell where you run the commands below, or they will
inspect the default locations instead of your real ones.

1. **Unhook the client.** `claude mcp remove -s user sextant` (drop `-s user`
   if you registered with the default scope), then `claude mcp list` to
   confirm. If you merged the session hooks, delete BOTH entries from
   `~/.claude/settings.json` (or the project-level `.claude/settings.json`):
   the `SessionStart` entry whose command ends in
   `atlas_plus.hooks.session_start`, and the `Stop` entry whose command ends
   in `atlas_plus.summarizer.hook_entry`.
   Hand-merged configs: remove the `sextant` block from `~/.claude.json`,
   `.mcp.json`, or Claude Desktop's `claude_desktop_config.json`.
   If you created your own Scheduled Tasks or systemd units for the servers,
   stop and remove those now too, before the code they point at goes away.
2. **Remove Sextant's data.** Report first; nothing is deleted without
   explicit flags, `--yes`, and (for the corpus) typing its full path back:

   ```bash
   sextant-uninstall                  # report only: what exists, where, how big
   sextant-uninstall --state --yes    # remove the state dir: transcripts, task DB, ledger,
                                      # sync devices, research reports; asks you to type its path
   sextant-uninstall --all --yes      # remove everything (asks for the corpus AND state paths;
                                      # scripts pass --confirm-path and --confirm-state-path)
   ```

   If you enabled the Stop-hook summarizer (`ATLAS_AUTONOMOUS=on`), it also
   wrote a session log (`<state>/updates.txt` by default, or `ATLAS_UPDATES_FILE`)
   and version copies in a `versions/` directory beside it. The uninstaller
   does not manage those two -- check whether they predate Sextant before
   removing them; if they existed before, they are yours.
3. **Remove env vars and secrets you added.** Grep your shell profile
   (`~/.bashrc`, `~/.zshrc`, or `setx`-persisted vars on Windows) for
   `ATLAS_` (the Telegram token is `ATLAS_TELEGRAM_BOT_TOKEN`) and delete the lines. Treat any tokens
   there as exposed: revoke a Telegram bot via @BotFather if you made one.
4. **Optional layers you set up.** Ollama models pulled for Sextant
   (`ollama rm qwen3:8b nomic-embed-text ...`); Docker:
   `docker compose down -v` deletes the `sextant-data` and `ollama-models`
   volumes, then `docker image rm sextant-mcp sextant-web` (the images are named
   after the Compose project; `docker images | grep sextant`); any systemd units you
   wrote from the self-hosting guide (disable, remove, `daemon-reload`).
5. **Remove the code.** `pip uninstall sextant-ai` only matters if you
   installed outside the clone's venv; otherwise deleting the clone
   (including its `.venv`) is the whole step.

Sextant installs no services, cron jobs, or files outside your home
directory on its own -- those exist only if you created them from the
HOWTOs, and only you can remove them.

Protect a corpus you never want a script to touch (your daily machine) by
creating an empty `.sextant-protect` file in its root -- the uninstaller
refuses it unconditionally, no flag overrides.

Starting over rather than leaving? `sextant-reset` wipes an area and
immediately re-scaffolds it (same safety gates as the uninstaller):

```bash
sextant-reset --state --yes    # clear derived caches/indexes only; transcripts and the
                               # task DB are kept
sextant-reset --all --yes      # factory reset: fresh state + fresh corpus skeleton
```

## Updating Sextant

First, the answer to the question that matters: **your memory data is safe
across updates.** The corpus is never migrated (it is plain markdown and
updates never touch it), and derived state either upgrades itself or can
simply be rebuilt (`sextant-reset --state --yes` is always a clean way
out). After an update, run `atlas-temporal build` once: it notices when the
fact rules changed and re-reads every file, retiring facts the new rules no
longer derive (`--force` wipes and rebuilds instead). Dates inside the
corpus are what the writing build stamped; the `made` date on decisions is
local time since 0.1.0 (earlier builds used UTC, so a decision written late
in the evening may carry the next day's date). Your `claude mcp add` registration survives too -- it points at the
venv's python by absolute path, and the update below reuses that venv
(if you ever recreate the venv instead, `claude mcp remove -s user sextant`
and then re-run the registration: `claude mcp add` refuses a name that
already exists). The pip
reinstall is what makes any NEW `sextant-*` commands appear, so do not
skip it. Local edits in the clone? `git stash` before pulling, `git stash pop`
after. If the pop reports a conflict (`UU <file>`), the upstream change and
yours touched the same lines: `git checkout --ours <file>` keeps the new
release's version (in a stash pop, "ours" is the release and "theirs" is your
stash; the other way round silently keeps the stale file), `git stash drop`
discards your copy, and you re-apply your edit by hand. On a detached HEAD
(you checked out a specific commit) `git pull` says "not currently on a
branch": `git checkout main` first.

After the reinstall, three short commands: `sextant-init` once more (it is
idempotent and adds anything a newer build expects, for example the state-dir
ownership marker that builds before 2026-11-10 did not write and the
uninstaller now requires); `sextant setup` to compare with your registration and your hooks
(`claude mcp get sextant` shows the registration; the hook commands are in
`~/.claude/settings.json`; both stay valid unless you moved the venv, but a
release can change the hook command, so re-merge section 3 when it differs);
and
`sextant verify --project upgrade-check` to prove the round-trip (it writes
one dated fact under that project; `--skip-remember` only reads).

Windows (PowerShell -- note `&&` does not work in PowerShell 5.1, hence
separate lines):

```powershell
cd sextant
git pull
.\.venv\Scripts\python.exe -m pip install -e ".[dev,telegram,summarizer]"
.\.venv\Scripts\python.exe -m pytest tests -q   # optional but recommended
.\.venv\Scripts\sextant-init
.\.venv\Scripts\atlas-temporal build
.\.venv\Scripts\sextant setup
.\.venv\Scripts\sextant verify --project upgrade-check
```

(The three `sextant` commands are on PATH only inside an activated venv; the
`.venv\Scripts\` prefix works from any shell.)

Linux / macOS:

```bash
cd sextant && git pull
.venv/bin/python -m pip install -e ".[dev,telegram,summarizer]"
.venv/bin/python -m pytest tests -q   # optional but recommended
.venv/bin/sextant-init
.venv/bin/atlas-temporal build
.venv/bin/sextant setup
.venv/bin/sextant verify --project upgrade-check
```

Then restart whatever services you run. Docker users:
`git pull && docker compose build && docker compose up -d` (on a clone pinned
to a commit, `git checkout main` first: a detached-HEAD `git pull` prints a
help page, exits 0, and the build then rebuilds the OLD source). The first
search after the update rebuilds the keyword index once when the tokenizer
changed; the log says so, and last week's image still reads the new index
if you roll back.

## Good to know (from fresh-install testing)

- **Scopes have a grammar.** `memory_remember_tool` wants `project:<name>`,
  `reference:<topic>`, or `preference:<topic>`; a bare `reference` is rejected. On disk,
  `project:<name>` lands in `projects/<name>.md`, `reference:<topic>` in
  `reference/<topic>.md`, and `preference:<topic>` in `feedback/<topic>.md`. The same
  grammar works as the `scope` of a search.
- **Two folders appear inside the corpus later:** `versions/` (a copy of every file
  before it is rewritten; it appears the first time an existing note is changed, not at init)
  and `_atlas/.locks/` (cross-process write locks, created on the first locked write; a lock a
  crashed writer left on this machine is cleared by the next write, one from another machine is
  broken after 30 s of waiting, and doctor names locks older than a minute). Both are
  Sextant's. `versions/` is your undo history and it grows by one full copy of a note per
  write: 500 short facts across ten notes made about 2 MB of history for an 89 KB corpus, and a
  200 KB note rewritten fifty times adds 10 MB. `sextant versions` reports the size and the
  biggest notes; `sextant versions --prune --older-than 90 --keep-last 3` plans a prune (copies
  older than 90 days, always keeping the newest three of every note) and `--yes` deletes them.
  The live notes are never touched by it.
- **What gets primed at session start.** The session-start hook injects your identity, the
  clock, your own house rules (from `identity/house-rules.md`, if you wrote any: one rule per
  list line, `- `, `* ` or `1.`; prose outside list lines is not a rule, and the hook says so
  when the file has text but no list lines), and the project note whose name matches the
  current directory under `ATLAS_PROJECTS_ROOT` (default `~/projects`): the git root when
  there is one, otherwise the first folder below the projects root that has a note. Save
  `identity/*.md` as UTF-8 without a BOM (PowerShell 5.1's `Set-Content -Encoding utf8` adds
  one; it is tolerated now, but `-Encoding ascii` or an editor is safer). Reference facts are not injected automatically; ask for them
  with the search tool or run `atlas-prime <task>`, which adds the facts that match the
  task words (any scope) and the project note when a word is a project name.
- **`ATLAS_WORKSPACE_ROOT` re-roots everything.** It moves `.memory/` and `.atlas/` too, so
  set it once, before `sextant-init`, or your corpus will appear to vanish. A specific
  variable (`ATLAS_MEMORY_ROOT`, `ATLAS_STATE_DIR`, `ATLAS_PROJECTS_ROOT`) wins over it when
  both are set. Inside a clone, top-level `memory/`, `state/` and `.memory/` are git-ignored.
- **Keyword search needs SQLite's FTS5 module** in the Python you installed into. Every
  python.org, uv, Debian, Ubuntu, Fedora and Alpine build has it; a bespoke build without it
  makes every search return nothing, and `sextant doctor` says so in the corpus row.
- **A user-wide `ATLAS_MEMORY_ROOT` set AFTER setup wins** over the `ATLAS_WORKSPACE_ROOT`
  the hook commands carry (the more specific variable always wins). If you add one later,
  run `sextant setup` again and re-merge the hooks.
- **The doctor is a report.** It creates no files and no databases (`sextant status` and `sextant components` may create the search index, which is harmless), so running it before
  `sextant-init` is harmless; its Worker row says "no task database yet" until the Brain
  writes one, which is not an error.
- **The clock line** at session start leads with your machine's local zone; set
  `ATLAS_HOME_TZ` (an IANA name such as `Europe/Berlin`) to pin a different one.
- **HTTP mode needs a token.** `sextant-mcp` over HTTP refuses to start without
  `ATLAS_MCP_TOKEN`; stdio mode (what `claude mcp add` uses) does not need one. Both servers
  expose an unauthenticated `/health`; everything else requires the bearer.
- **No sudo?** If `python3 -m venv` fails on a missing `ensurepip` and you cannot install
  `python3-venv`, use `python3 -m venv --without-pip .venv` (it works on top of the half-made
  `.venv` the failure left), then bootstrap pip without curl, which a stock Ubuntu also lacks:
  `.venv/bin/python -c "import urllib.request as u; exec(u.urlopen('https://bootstrap.pypa.io/get-pip.py').read())"`
  (with curl: `curl -sSL https://bootstrap.pypa.io/get-pip.py | .venv/bin/python`), or use
  `uv venv` (uv brings its own installer: `uv pip install -e .`).
- **Scripting a health check?** `sextant-doctor --strict` exits 1 when a service you have
  configured (a key or token set for it) is down, or any check reports an error, and prints
  `strict: FAIL (...)` naming why as its last line
  (plain `sextant-doctor` is a report and always exits 0). On a machine that also runs
  another install, the probes see that install's services; the report says so.
- **Two installs, one client.** `claude mcp add` registers the server under the name
  `sextant`; a second install on the same machine must use a different name:
  `sextant setup --name sextant-work` prints every form (Claude Code, JSON,
  Codex, opencode) under that name, and `claude mcp get` / `remove` take the
  same name. Two installs also mean two sets of hook commands: Claude Code's
  settings.json holds one, so pick the install that primes your sessions.
