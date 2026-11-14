# Configuration reference

Every knob in Sextant is an environment variable. There is no config file
to hunt for: set variables in your shell profile, your service manager,
or the `env` block of your MCP client snippet. Everything has a working
default -- a fresh install needs NOTHING here except tokens for the
optional layers you turn on.

Conventions: `on/off` variables are fail-closed allowlists -- only
`on`, `1`, or `true` enables; any other value (including typos) is off.
Paths accept absolute paths on any OS. Every variable uses the `ATLAS_`
prefix -- the engine's namespace -- even though the commands are
`sextant-*`. A `SEXTANT_`-prefixed twin (the natural guess) is never
read; the path variables warn on stderr if you set one.

**Setting a variable so it sticks** (a `$env:X=...` line dies with the
terminal window):

```powershell
# Windows -- takes effect in NEW terminals from then on:
setx ATLAS_WEB_PORT 8899
```

```bash
# Linux / macOS (and Git Bash on Windows) -- append to your shell profile, then reopen the shell:
echo 'export ATLAS_WEB_PORT=8899' >> ~/.bashrc   # or ~/.zshrc
```

Git Bash also sees variables set with `setx`, so one `setx` covers PowerShell, Git Bash and
GUI-launched clients alike; venv tools are `.venv/Scripts/<tool>.exe` there.

Services started by a service manager (systemd, Task Scheduler) read
their environment from the service definition instead -- set it there.

On Windows, put the path overrides (`ATLAS_WORKSPACE_ROOT` or the three
`ATLAS_*_ROOT`/`_DIR` variables) and `ATLAS_WEB_API_KEY` in the USER
environment with `setx`: a GUI-launched Claude Code, the hooks it runs and
your terminals then all see the same values. The `env` block of the MCP
registration reaches only the MCP server process, not the hooks.

## Core paths and identity

| Variable | Default | What it does |
| --- | --- | --- |
| `ATLAS_MEMORY_ROOT` | `~/.memory` | The corpus: your markdown memory. Back this up; everything else is rebuildable. |
| `ATLAS_STATE_DIR` | `~/.atlas` | Derived state (indexes, task DB, logs). Safe to wipe; rebuilt from the corpus. |
| `ATLAS_WORKSPACE_ROOT` | home dir | Base directory the two paths above (and `ATLAS_PROJECTS_ROOT`) default under; a specific variable set alongside it wins. |
| `ATLAS_HOME_TZ` | machine local zone | IANA zone (`Europe/Berlin`) that leads the session-start clock line. |
| `ATLAS_VERIFY_TIMEOUT` | `240` | Seconds `sextant verify` waits for each reply from the stdio server it starts (a non-number falls back to the default). |
| `ATLAS_IDENTITY_PATH` | `<corpus>/identity/user.md` | Who you are, loaded at session priming. |
| `ATLAS_USER_NAME` | -- | Display name used in identity and tagging. |
| `ATLAS_PROJECTS_ROOT` | -- | Optional root of your code projects (ticket/project tooling). |
| `ATLAS_UPDATES_FILE` | `<state>/updates.txt` | Legacy session-log file the Stop hook and nightly job prepend to. Defaults inside the state dir so a file you made yourself (such as `~/updates.txt`) is never touched; point this at your own file to opt in. Version copies land in a `versions/` folder beside it. |

## Ports, hosts, and service URLs

| Variable | Default | What it does |
| --- | --- | --- |
| `ATLAS_MCP_HOST` / `ATLAS_MCP_PORT` | `127.0.0.1` / `8765` | MCP memory server (HTTP mode). In a container set `ATLAS_MCP_HOST=0.0.0.0` and publish the port on the host's loopback only (`-p 127.0.0.1:8765:8765`); the default bind is unreachable through a port publish. |
| `ATLAS_MCP_ALLOWED_HOSTS` | unset | Comma-separated Host names the HTTP MCP server accepts besides loopback (`sextant.example.com`, `192.168.1.5`, `[2001:db8::5]`). Each entry matches the bare name (what a client on 80/443 sends) and `name:<any port>`; an entry with its own port (`host:8765`) matches that port only; names are case-insensitive. Needed behind a reverse proxy, a tunnel or on a LAN: the server otherwise answers 421 Invalid Host header (DNS-rebinding guard; the body and the log name this setting). With `ATLAS_MCP_HOST` set beyond loopback and no list, the Host check is off and the bearer token is the guard. Cannot be combined with `--no-auth`. |
| `ATLAS_VERSION_MIN_INTERVAL_SECONDS` | `0` | Coalescing window for history copies: when set, a write within this many seconds of the note's newest copy makes no new copy (the previous one already holds the pre-burst state). `0` keeps a copy on every write. 1,000 appends to one 1.2 MB note made 1.1 GB of copies; `sextant versions --prune` reclaims them. |
| `ATLAS_WEB_PANEL_CACHE` | `on` | The dashboard caches its slow panels (topics, next actions, the curation report) per corpus signature (note count + newest mtime) for up to five minutes; `off` recomputes on every load. |
| `ATLAS_MCP_LOG_LEVEL` | `WARNING` | Log level of the MCP server (`DEBUG`, `INFO`, ...). `INFO` shows one line per request on stderr, which some clients display as noise. |
| `ATLAS_CLAUDE_PROJECT_DIR` | the slug of the workspace root (`D:\Work\notes` -> `D--Work-notes`), matched case-insensitively under the Claude Code projects root | The Claude Code project folder whose transcripts the Stop hook's mining stage indexes. Set it when your corpus lives elsewhere than the project you run Claude Code in. |
| `ATLAS_WEB_HOST` / `ATLAS_WEB_PORT` | `127.0.0.1` / `8766` | Web dashboard. |
| `ATLAS_BRAIN_HOST` / `ATLAS_BRAIN_PORT` | `127.0.0.1` / `8767` | Brain task-queue API. |
| `ATLAS_SYNC_HOST` / `ATLAS_SYNC_PORT` | `127.0.0.1` / `8768` | Device-sync hub. |
| `ATLAS_BRAIN_URL` | `http://127.0.0.1:8767` | Where clients (web UI, Telegram, doctor) find the Brain. Change together with `ATLAS_BRAIN_PORT`. |
| `ATLAS_MCP_URL` | derived | Where doctor and the web UI's Map tab probe the MCP server. |
| `ATLAS_WEB_URL` | `http://127.0.0.1:8766` | Where doctor probes the dashboard's health. Change together with `ATLAS_WEB_PORT`. |
| `ATLAS_SYNC_URL` | `http://127.0.0.1:8768` | Where the Map tab probes the sync hub's health. Change together with `ATLAS_SYNC_PORT`. |
| `OLLAMA_BASE_URL` | `http://127.0.0.1:11434` | Where the Map tab finds the local model backend; the Brain reads it only when `ATLAS_BRAIN_OLLAMA_URL` is unset (prefer that one for the Brain). |

Everything binds loopback by default; see "Security model, honestly" in
the README before exposing anything.

## Auth and abuse protection

| Variable | Default | What it does |
| --- | --- | --- |
| `ATLAS_MCP_TOKEN` | -- | Bearer token for the MCP HTTP surface. To rotate it: restart the server with the new value; a client that still holds a session gets 401 with the old token and `Session not found` with the new one until it initializes again (every HTTP client re-initializes after a rotation). |
| `ATLAS_WEB_API_KEY` | -- | Bearer token for the dashboard. `sextant-web` refuses to start without it (generate: `python -c "import secrets; print(secrets.token_urlsafe(32))"`; `--no-auth` allows loopback-only dev). Scripts send it as `Authorization: Bearer <key>` (an `X-Atlas-Key: <key>` header is also accepted). |
| `ATLAS_BRAIN_API_KEY` | -- | Bearer token for the Brain API. |
| `ATLAS_SYNC_DEVICE_TOKEN` | -- | This device's token when talking to a sync hub. |
| `ATLAS_BRAIN_RATE_LIMIT_RPM` | code default | Brain request rate limit. |
| `ATLAS_FORWARDED_IP_HEADER` | `Cf-Connecting-Ip` | Header a loopback proxy uses to report the real client address for failed-auth throttling (then the last non-loopback hop of `X-Forwarded-For`); `none` ignores every forwarded header. See SECURITY.md, "Behind a reverse proxy". |
| `ATLAS_MCP_LOCKOUT_THRESHOLD` / `ATLAS_MCP_LOCKOUT_SECONDS` / `ATLAS_MCP_ALERT_THRESHOLD` | `10` / `300` / code default | Failed-auth lockout and alerting on the MCP surface: after the threshold of failures in the window a source gets 429 for the lockout period, correct token included. A correct token clears the source's failure count. |
| `ATLAS_MCP_TRUSTED_PEERS` | unset | Comma-separated IPs or CIDRs treated like loopback by the lockout (never locked out, failures not counted; a listed source is exempt whether it connects directly or through a proxy). A reverse proxy listed here is also trusted to name the real client in `Cf-Connecting-Ip` (or `X-Forwarded-For`), so the lockout then counts that client instead of the proxy. Every client behind one NAT shares one counter, and a container published on the host's loopback sees all host clients as the Docker bridge gateway (`172.17.0.1`): list that gateway when the port is published on `127.0.0.1` only. Never list a public range. |
| `CODEX_HOME`, `XDG_CONFIG_HOME` | unset | Not Sextant's own: `sextant uninstall` reads them to find a Codex CLI (`config.toml`) or opencode (`opencode.json`) registration that still names this install, beside `~/.codex`, `~/.config/opencode` and the current directory. |
| `CLAUDE_CONFIG_DIR` | unset | Read only by the uninstall report, which lists the Claude Code config files that still reference the install; Claude Code itself honours it. |
| `NO_COLOR` | unset | Any value makes `sextant doctor` print plain text; piped output is plain regardless. |
| `APPDATA` | Windows default | Read only by the uninstall report to find Claude Desktop's `claude_desktop_config.json`. |
| `LOCALAPPDATA` | Windows default | Read only by the uninstall report to find the Microsoft Store build of Claude Desktop (`Packages\Claude_*\LocalCache\Roaming\Claude\claude_desktop_config.json`). |

## Write consent (the two switches)

| Variable | Default | What it does |
| --- | --- | --- |
| `ATLAS_INTERACTIVE_WRITES` | `on` | Interactive writes: you asking your assistant to remember something. `off` makes memory tools effectively read-only. |
| `ATLAS_AUTONOMOUS` | `off` | Background writes: nightly jobs editing memory unattended. Fail-closed allowlist; leave off until you trust the loop. |
| `ATLAS_SESSION_AUTONOMOUS` | `off` | Session-scoped autonomous override: `on` lets the Stop hook's summarizer AND its suggest-facts stage write, without `ATLAS_AUTONOMOUS`. Put it on the hook command as `--env ATLAS_SESSION_AUTONOMOUS=on` (hooks run outside your shell). |
| `ATLAS_MEMORY_AUTOCOMMIT` | `off` | Git-commit the corpus after each interactive write (corpus must be a git repo). |

## Search and embeddings

| Variable | Default | What it does |
| --- | --- | --- |
| `ATLAS_SEMANTIC_SEARCH` | `on` | Hybrid semantic search kill switch; `off` = keyword FTS only. |
| `ATLAS_FTS_FRESHNESS_WINDOW` | `10` | Seconds between index-freshness sweeps on search. Within the window, searches reuse the index (a burst pays the sweep once); this process's own memory writes always void the window, so remember-then-search stays exact. `0` = sweep on every search. |
| `ATLAS_EMBED_MODEL` | `nomic-embed-text` | Embedding model (Ollama). |
| `ATLAS_EMBED_URL` | Ollama default | Embedding endpoint override. Proxy variables apply to a remote host only; loopback goes direct. |
| `ATLAS_EMBED_TIMEOUT` | `15` | Seconds to wait for one embedding call (a hung endpoint costs this per search). |

## Models and providers

Model roles resolve independently, so the Brain can run a local model
while the summarizer uses a cloud one:

| Variable | Default | What it does |
| --- | --- | --- |
| `ATLAS_MODEL_<ROLE>_PROVIDER` | role default | Provider per role -- roles are `BRAIN`, `INSPIRE`, `FAST`, `SUMMARIZER`, `EMBED`; providers are `ollama`, `anthropic`, `openai`, or `auto` (local first, cloud fallback). |
| `ATLAS_BRAIN_MODEL` | `qwen3:8b` | Brain + inspiration model. |
| `ATLAS_FAST_MODEL` | `qwen3:1.7b` | Small fast-path model. |
| `ATLAS_BRAIN_OLLAMA_URL` | `http://localhost:11434` | Ollama endpoint for the Brain. |
| `OLLAMA_HOST` | -- | Read only when `ATLAS_BRAIN_OLLAMA_URL` is unset: the standard Ollama variable (`host:port` or a URL), as the Ollama CLI reads it. |
| `ATLAS_SUMMARIZER_PROVIDER` | `auto` | Legacy alias for `ATLAS_MODEL_SUMMARIZER_PROVIDER`. |
| `ATLAS_SUMMARIZER_MODEL` | `ATLAS_BRAIN_MODEL`, else `qwen3:8b` | Session-summarizer and suggest-facts model; the Stop hook's probe says so when it is not pulled. |
| `ATLAS_SUMMARIZER_CLAUDE_MODEL` | code default | Model used when the summarizer runs on Anthropic. |
| `ATLAS_SUMMARIZER_TIMEOUT` | code default | Summarizer call timeout (seconds). |
| `ATLAS_OLLAMA_URL` | Ollama default | Summarizer-side Ollama endpoint. |
| `ANTHROPIC_API_KEY` | -- | Enables the Anthropic provider. |
| `OPENAI_API_KEY` / `OPENAI_BASE_URL` | -- | OpenAI-compatible provider; point `OPENAI_BASE_URL` at LM Studio, vLLM, llama.cpp, or LiteLLM. |
| `ATLAS_OPENAI_MODEL` / `ATLAS_OPENAI_EMBED_MODEL` | code defaults | Model names for the OpenAI-compatible provider. |

## Device sync

| Variable | Default | What it does |
| --- | --- | --- |
| `ATLAS_SYNC_HUB` | -- | Hub URL this device syncs against. |
| `ATLAS_SYNC_SHARE` | -- | Comma-separated corpus prefixes this system agrees to exchange with the hub (`reference/sip/,projects/x/`); same as repeatable `--share`. Required when the hub pairs you as a federation peer; nothing outside it is read, sent or pulled. |
| `ATLAS_SYNC_MANIFEST_TTL` | code default | Seconds the hub caches its corpus manifest. |

## Intake (watched drop folder)

| Variable | Default | What it does |
| --- | --- | --- |
| `ATLAS_INTAKE_DIR` | -- | The drop folder `atlas-intake run` scans (for example a cloud-synced `Intake` folder). Required unless `--source` is passed. |
| `ATLAS_INTAKE_LANDING` | `<workspace>/intake` | Where files without a frontmatter `project` or `destination` land, grouped by type and date. |
| `ATLAS_INTAKE_RULES` | `<state>/intake/rules.json` | Optional JSON file of routing rules; prepended to the built-in rules (or replaces them with `"replace_defaults": true`). |

Intake never deletes: imported originals move to `<ATLAS_INTAKE_DIR>/_imported/<date>/` with a `.meta.json` sidecar, and `_imported/MANIFEST.md` is regenerated from the ledger at `<state>/intake/ledger.jsonl`. See docs/INTAKE-DESIGN-2026-11.md.

## Telegram and push notifications (optional layers)

| Variable | Default | What it does |
| --- | --- | --- |
| `TELEGRAM_BOT_TOKEN` / `ATLAS_TELEGRAM_BOT_TOKEN` | -- | Bot token from @BotFather. |
| `ATLAS_TELEGRAM_CHAT_ID` | -- | Chat that receives alerts. |
| `ATLAS_TELEGRAM_ALLOWED_USERS` | -- | Comma-separated user IDs allowed to command the bot (empty = nobody). |
| `ATLAS_JOB_POLL_INTERVAL` / `ATLAS_JOB_TIMEOUT` | code defaults | Telegram job-poller cadence and per-job timeout. |
| `ATLAS_NTFY_URL` / `ATLAS_NTFY_BASE` / `ATLAS_NTFY_TOPIC` / `ATLAS_NTFY_TOKEN` | -- | ntfy push notifications (server, topic, auth). |
| `ATLAS_NTFY_CMD_TOPIC` / `ATLAS_NTFY_CMD_SECRET` | -- | Inbound phone-to-Brain command channel. |
| `ATLAS_NTFY_COMPLETIONS` | `off` | Push a note when background tasks complete. |

## Transcripts, hooks, and proxies

| Variable | Default | What it does |
| --- | --- | --- |
| `ATLAS_TRANSCRIPT_ROOT` / `ATLAS_TRANSCRIPT_INDEX` | state dir | Where captured transcripts and their index live. |
| `ATLAS_TRANSCRIPT_DROP` | `<state>/transcripts/drop` | Drop directory for `sextant-transcripts ingest` (bring transcripts from any tool). |
| `ATLAS_CLAUDE_CODE_ROOT` | `~/.claude/projects` | Claude Code projects dir to mine transcripts from (the Stop hook derives it from its payload when unset). |
| `ATLAS_DISABLE_AUTOSUMMARIZE` / `ATLAS_DISABLE_SUGGEST_FACTS` / `ATLAS_DISABLE_TRANSCRIPT_MINING` | unset | Opt out of individual stop-hook stages. |
| `OLLAMA_UPSTREAM` / `ATLAS_OLLAMA_PROXY_PORT` | -- | Transcript-capturing Ollama proxy. |
| `OPENAI_PROXY_TARGET` / `ATLAS_OPENAI_PROXY_PORT` | -- | Transcript-capturing OpenAI-compatible proxy. |
| `ATLAS_LOG_LEVEL` | `INFO` | Log level for capture sources. |

## Backup

| Variable | Default | What it does |
| --- | --- | --- |
| `ATLAS_RCLONE_REMOTE` / `ATLAS_RCLONE_ROOT` | -- | rclone remote and path for `sextant-backup` (use a crypt remote for cloud targets). |

## Brain internals (tune only when you know why)

Defaults live in code and are deliberate; unset means a sensible value.

| Variable | What it tunes |
| --- | --- |
| `ATLAS_BRAIN_INLINE_TASKS` | Run tasks inline in the API process (dev mode). |
| `ATLAS_WORKER_HEARTBEAT_INTERVAL` | Worker heartbeat cadence. |
| `ATLAS_SLA_MINUTES` | Task SLA before it counts as late. |
| `ATLAS_SCHEDULER_CATCHUP` | Run missed nightly jobs on next start. |
| `ATLAS_AUTO_SESSION_LOG` | Nightly auto session-log job. |
| `ATLAS_CONFIRM_DECAY_DAYS` | Staleness threshold for confidence decay. |
| `ATLAS_DB_BACKUP_KEEP` | Rotating task-DB backup count. |
| `ATLAS_DIGEST_RESULTS` / `ATLAS_VERIFY_TICKETS` | Worker digest and ticket-verify passes. |
| `ATLAS_BRAIN_ARTIFACT_DIR` | Where task artifacts land. |
| `ATLAS_LOCAL_ONLY` | Three levels, one predicate for the whole program. `on`: refuse cloud providers everywhere (Brain agent routing AND every LLM role). Unset: local by default; a cloud model runs only where a task's routing or a role's provider names one explicitly, never as a silent fallback. `off`: also allow the `auto` chain to fall back to a cloud provider when Ollama is down. When unset, the `auto` provider chain (summarizer, dream mode, weekly synthesis) is local-only: an API key in the environment never sends your transcripts to Anthropic/OpenAI when Ollama is down. Set `ATLAS_LOCAL_ONLY=off` to opt in to that cloud fallback; set it to `on` to also refuse explicitly-configured cloud providers. |
| `ATLAS_SMART_ROUTING` | Cheapest-capable model routing for agents. |
| `ATLAS_CLAUDE_CLI` / `ATLAS_CODEX_CLI` / `ATLAS_OPENCODE_CLI` / `ATLAS_OPENCODE_MODEL` | External agent CLIs the Brain may dispatch to. |
| `ATLAS_CLAUDE_ESCALATION` / `ATLAS_CLAUDE_QUALITY_ESCALATION` / `ATLAS_CLAUDE_QUALITY_THRESHOLD` / `ATLAS_CLAUDE_AUTO_PROMOTE` / `ATLAS_CLAUDE_TASK_ESTIMATE` | Escalation ladder from local model to CLI agents. |
| `ATLAS_BUDGET_PREFLIGHT` | Token-budget preflight before big dispatches. |
| `ATLAS_TRANSCRIPTS_GLOB` | Which transcripts the budget meter counts. |
| `ATLAS_INSPIRE_SAMPLES` / `ATLAS_INSPIRE_TEMPERATURE` | Inspiration-engine sampling. |
| `ATLAS_REVERIFY_LIMIT` / `ATLAS_REVERIFY_RESET` | Stale-fact reverification batch size. |
| `ATLAS_RESEARCH_API_URL` / `RESEARCH_API_KEY` / `ATLAS_RESEARCH_SEARXNG_URL` / `ATLAS_RESEARCH_WEB_SEARCH` / `ATLAS_RESEARCH_CACHE_TTL` / `ATLAS_RESEARCH_DEDUP_DAYS` / `ATLAS_RESEARCH_MIN_WORDS` / `ATLAS_RESEARCH_RETRY` / `ATLAS_RESEARCH_RETRY_THRESHOLD` / `ATLAS_RESEARCH_ROOT` | Research pipeline (search backends, caching, quality floors). |
| `ATLAS_TICKET_DISPATCH` / `ATLAS_TICKET_QUALITY_THRESHOLD` / `ATLAS_TICKET_ORPHAN_DAYS` / `ATLAS_TICKET_STALE_BL_DAYS` / `ATLAS_TICKET_STALE_IP_DAYS` / `ATLAS_TICKET_SYNC_DAYS` / `ATLAS_TICKET_SYNC_REPOS` / `ATLAS_ALLOW_REMOTE_DISPATCH` | Ticket store hygiene and dispatch. |
| `ATLAS_SUGGEST_AUTO_CAP` / `ATLAS_SUGGEST_AUTO_THRESHOLD` / `ATLAS_SUGGEST_CONFIDENCE_THRESHOLD` | Suggested-facts curation thresholds. |
| `ATLAS_TEMPORAL_ROOT` | Temporal-graph storage override. |
| `ATLAS_TUI_APPS` | Comma-separated companion-app names whose session-log events `sextant-doctor` reports (default: none). |
| `ATLAS_KNOWN_PROJECTS` | Extra project names for task-fact scope inference, `name:synonym\|name2` format (default: none). |
| `ATLAS_MODEL_PRICES` | JSON `{"model-prefix": [in_per_1M, out_per_1M]}` extending the built-in price table for spend estimates (unknown models cost 0). |
| `ATLAS_TASK_HINT` / `CLAUDE_HOOK_SESSION_SOURCE` | Session-start hook plumbing. |
| `EDITOR` | Editor opened when a command offers to edit a note or report. |
| `PYTHONIOENCODING` | Standard Python: when set, Sextant leaves the output encoding alone (otherwise a pipe on Windows gets the console's code page). |
| `ATLAS_WEB_SECURE_COOKIE` | `on` sets the Secure flag on the dashboard's auth cookie; use when the web UI sits behind TLS (default off: plain-HTTP loopback). |
| `ATLAS_TUNNEL_CHECK` | `on` makes `sextant-doctor` probe for a Cloudflare tunnel even when `cloudflared` is not on PATH (default: probe only when it is). |

Optional-integration variables you may see in source (`ATLAS_BRAIN_GATEKEEPER`,
`ATLAS_GATEKEEPER_PROJECT`, `ATLAS_BRAIN_BREAKGLASS_FILE`, `ATLAS_LOOP_STATE_FILE`):
hooks for extensions that are not part of the public distribution; unset means
disabled.
