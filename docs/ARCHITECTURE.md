---
id: atlas-architecture
title: Sextant System Architecture
type: reference
status: active
priority: high
updated: 2026-10-16
applies_to: [sextant]
---

# Sextant System Architecture

This document gives a new contributor or AI agent enough context to understand
the system end-to-end, locate any component, and make informed changes without
reading every source file.

**A note on names.** The public product is **Sextant** (PyPI name
`sextant-ai`). Its engine's codename is **Keel** (it grew up as "Atlas", which
survives in three places you will see constantly: the Python package is
`atlas_plus`, every `sextant-*` command has an `atlas-*` alias, and the
environment variables are `ATLAS_*`. Same thing, one codebase; the deeper
internals simply predate the rename.

---

## What Sextant Does

Sextant solves one problem: every LLM session should start with the context of
every previous session, without manual priming. It layers:

1. **Memory** -- a curated Markdown corpus (default `~/.memory`, overridable
   with `ATLAS_MEMORY_ROOT`) with YAML frontmatter, session logs, and
   structured fact files.
2. **MCP** -- a Model Context Protocol server that exposes the corpus as tools
   any MCP-aware client can call (Claude Code, Cursor, Cline, etc.).
3. **Brain** -- a local Qwen3 orchestration layer that runs tasks, writes memory,
   and executes nightly maintenance jobs without needing external API credits.
4. **Web UI** -- a single-file FastAPI + HTMX dashboard for task management,
   memory browsing, and system health (`web/app.py`, ~6,100 lines -- the
   biggest single file in the tree; everything it serves lives there).
5. **Telegram bot** -- a mobile interface for submitting tasks and querying state
   from anywhere.

---

## Ports and Processes

| Process | Port | Entry point | Purpose |
|---------|------|------------|---------|
| MCP server | 8765 | `atlas_plus.mcp.memory_server.server` | Memory tools for all MCP clients |
| Brain (FastAPI) | 8767 | `atlas_plus.brain.server` | REST API for chat, tasks, scheduling |
| Web UI (FastAPI) | 8766 | `atlas_plus.web.app` | HTMX dashboard |
| Brain worker | -- | `atlas_plus.brain.worker` | Polls brain_tasks queue, runs executors |
| Telegram bot | -- | `atlas_plus.telegram.bot` | Long-poll, routes messages to Brain |

On the reference Windows deployment these services run as Scheduled Tasks
installed by `deploy/install-atlas-stack.ps1` (a Windows-only helper that is not
shipped in the public tree). On macOS/Linux there is no such installer: start each
service manually (see the "Ports and Processes" entry points above) or run them
under your own supervisor (systemd, launchd, supervisord, tmux, etc.). Every path
and port is env-configurable, so nothing here is Windows-specific beyond the
convenience installer.

---

## Data Stores

| Path | Format | Contents |
|------|--------|---------|
| `<workspace>/.memory\` | Markdown + YAML frontmatter | Curated facts, session logs, decisions, tickets |
| `<workspace>/.atlas\state.sqlite` | SQLite (WAL mode) | Brain tasks, conversations, observations, pending facts |
| `<workspace>/.atlas\brain\artifacts\` | Markdown | Task result artifacts (reports, diffs, etc.) |
| `<workspace>/.atlas\logs\mcp-search.ndjson` | NDJSON | MCP search call log (hit/miss, query, score) |
| `<workspace>/.atlas\logs\activity.ndjson` | NDJSON | Cross-component activity feed (Brain task lifecycle, MCP tool calls by name + duration, ticket writes, temporal builds, sync events); each event carries a reference to where the data lives (memory path, ticket file, task artifact, truncated query) but never contents, bodies, or results; self-trims to the newest 5,000 lines; feeds Helm's Map tab via `/api/activity`, with `/api/topology` probing service health live |
| `<workspace>/.atlas\fsdiffs\` | JSON | Git diff snapshots captured by the Stop hook |

Key SQLite tables in `state.sqlite`:

- **brain_tasks** -- task queue with lease-based worker claiming (WAL mode)
- **task_attempts** -- per-attempt execution telemetry (attempt_num, outcome, agent, result_summary 300 chars); survives task result overwrites on retry
- **brain_conversations** -- per-session chat history (session_id, role, content, caller_identity)
- **brain_observations** -- quality scores per task (agent_name, quality 0-10, signals JSON)
- **pending_facts** -- candidate facts extracted from task results, awaiting human approval

---

## Memory File Structure

```
.memory/
  identity/       -- who the operator is (role, working style, communication style)
  feedback/       -- rules Claude should follow (version-before-edit, archive-not-delete, etc.)
  projects/       -- one file per active project with git state and tickets
  reference/      -- external systems, people, glossary, machines
  sessions/       -- session logs: _history.md (compact), YYYY-MM-DD.md (daily detail)
  decisions/      -- decision records with revisit_if and alternatives_rejected
  constraints/    -- invariants that should never change
  observations/   -- factual observations with low half-life
  tickets/        -- ticket files grouped by project (sextant/, gatekeeper/, etc.)
  anti/           -- rejection records: things agents should not re-propose
  gaps/           -- stub files for recurring memory misses (auto-created by gaps --autofile)
  personas/       -- persona definition files
  _atlas/         -- routing.yaml (scope -> file mapping rules)
```

Epistemic provenance fields on every fact file (Angle 5, v0.3-C):
- `first_observed: YYYY-MM-DD` -- set once at creation
- `last_confirmed: YYYY-MM-DD` -- updated on every write or explicit confirm call
- `source: agent|suggest-facts|human-direct|telegram` (the writers Sextant ships; an MCP caller's own label is stored as given, except the human ones, which become `agent`; it is a claim, not an identity)
- `confidence: high|medium|low`
- `contradictions: [path1, path2]` -- detected by nightly contradiction check

---

## MCP Tool Inventory

The MCP server exposes these tools to all connected clients:

| Tool | What it does |
|------|-------------|
| `memory_list_tool` | List .memory/ files with frontmatter metadata |
| `memory_read_tool` | Read a file's full content + frontmatter |
| `memory_frontmatter_tool` | Read only the YAML frontmatter |
| `memory_search_tool` | FTS5 full-text search; returns `{confidence, results}` |
| `memory_remember_tool` | Write a fact to the appropriate .memory/ file |
| `memory_remember_tool_v2` | Write with explicit confidence + source provenance |
| `memory_confirm_tool` | Update last_confirmed without adding content |
| `memory_list_confirmed_tool` | List files confirmed within N days |
| `memory_anti_remember_tool` | Record a rejection pattern |
| `memory_anti_list_tool` | List all rejection entries |
| `memory_remember_decision_tool` | Write a decision record to decisions/<slug>.md |
| `memory_list_decisions_tool` | List all decision records |
| `memory_health` | Server status + stats |
| `memory_topics_tool` | Topic index built from `tags:` frontmatter across the corpus |
| `project_state_tool` | Git state + dep manifest for a named project |
| `now_state_tool` | Current wall-clock time across timezones (the temporal sensor) |
| `budget_preflight_tool` | Token-headroom check for the current 5h rate-limit window |
| `research_search_tool` | Search the local research library (RAG) for cited, quality-ranked passages |
| `personas_list_tool` | List available personas |
| `personas_adopt_tool` | Adopt a persona (load identity + scope) |
| `skills_list_tool` | List skills projected from runbooks |
| `skills_invoke_tool` | Invoke a skill by name |
| `tickets_create_tool` | Create a ticket |
| `tickets_list_tool` | List tickets with filters |
| `tickets_open_tool` | Open a ticket file for reading |
| `tickets_status_tool` | Update a ticket status |
| `temporal_build_tool` | Ingest sessions into the temporal graph |
| `temporal_search_tool` | Search the temporal knowledge graph |
| `temporal_history_tool` | Retrieve entity history |
| `temporal_entities_tool` | List all named entities in the temporal graph |

(30 tools total -- `tests/test_docs_claims.py` holds this table to the
`@mcp.tool` registrations in `server.py`, so it cannot silently drift again.)

Temporal graph source options (atlas-temporal build --source X):
- `fixtures` -- built-in Atlas Phase 4 lifecycle data
- `memory` -- .memory/ Markdown files via MemoryMarkdownSource
- `sessions` -- .memory/sessions/YYYY-MM-DD.md via SessionLogSource
- `transcripts` -- Claude Code JSONL metadata via ClaudeCodeTranscriptSource
- `mining` -- Claude Code JSONL tool patterns via TranscriptMiningSource
- `atlas` -- memory + sessions
- `all` -- all four real sources

---

## Automation Loop (v0.10+)

The full hands-free automation cycle triggered by the ticket dispatcher:

```
1. Create tickets (Web UI / Telegram / MCP tickets_create_tool)
   assignee = claude-code | local | any | codex
   status   = todo

2. Ticket dispatcher (every 10 min, ATLAS_TICKET_DISPATCH=on)
   dispatch_all_eligible() scans todo tickets
     -> dispatch_ticket(t):  create_task(session_id="ticket-{ID}", agent=..., priority=...)
     -> update_ticket_status(id, "in-progress")
     -> ntfy push: "Atlas: N ticket(s) dispatched"

3. Brain worker picks up task via claim_next_task()
   run_executor(task) -> _agent executor (or routed to claude-code/codex)

4. _finish_task() after execution
   status=done:
     -> mark_done()
     -> _try_close_ticket()  -- ticket-{ID} -> ticket done
     -> quality gate: classify_result_quality() < ATLAS_TICKET_QUALITY_THRESHOLD
          -> reopen_ticket_if_low_quality() -> ticket todo  (re-dispatch)
     -> [ATLAS_VERIFY_TICKETS=on] create_verify_task() -> queue kind=verify task

5. Verify task (kind=verify, session_id="verify-ticket-{ID}")
   _verify executor runs full pytest suite
   pass: ticket stays done
   fail: _reopen_ticket_from_verify_failure() -> ticket todo  (re-dispatch)

6. status=failed/blocked:
   -> _try_close_ticket()  -- ticket-{ID} -> todo (retry) or blocked
   -> [ATLAS_CLAUDE_ESCALATION=on] create_escalation_task() -> claude-code task
```

Key env vars for the loop:
- `ATLAS_TICKET_DISPATCH` -- on/off, toggleable from Web UI Config panel
- `ATLAS_TICKET_QUALITY_THRESHOLD` -- float, default 4.0 (0=empty, 1=refusal, 2=short, 7=ok)
- `ATLAS_VERIFY_TICKETS` -- on/off, queues pytest verify after each agent task
- `ATLAS_CLAUDE_ESCALATION` -- on/off, auto-escalates failures to Claude Code

---

## Brain Task Lifecycle

```
Client (Web UI / Telegram / Scheduler)
  |
  v  POST /task  (or create_task() directly)
Brain FastAPI (port 8767)
  |-- Creates row in brain_tasks with status='pending'
  |-- If kind='agent' and ATLAS_BRAIN_INLINE_TASKS=on:
  |     asyncio.create_task(_execute_task(...))  # inline
  |-- Otherwise: leaves pending for worker
  |
  v  atlas-brain-worker (polls every 2-5s)
claim_next_task()    -- atomic UPDATE WHERE status='pending', sets lease
run_executor(task)   -- dispatches to the right executor by task.kind
_finish_task(...)    -- mark_done/blocked/failed
                     -- record_task_attempt() -> task_attempts table (always)
                     -- _try_close_ticket() if session_id matches ticket-{ID}
                     -- quality gate (done tasks)
                     -- verify task auto-queue (ATLAS_VERIFY_TICKETS=on, kind=agent)
                     -- verify failure -> ticket reopen (kind=verify, failed)
                     -- escalation (failed/blocked, ATLAS_CLAUDE_ESCALATION=on)

Worker heartbeat: _HeartbeatKeeper daemon thread renews heartbeat_at + lease
every ATLAS_WORKER_HEARTBEAT_INTERVAL=60s. Prevents zombie cleanup from
reaping live long-running tasks.
```

### Executor Kinds

| kind | Executor | What it does |
|------|----------|-------------|
| `agent` | `_agent` | Routes to Qwen3 or CLI agent via agents.route() |
| `verify` | `_verify` | Runs full pytest suite to verify a ticket task; failure reopens ticket |
| `temporal_build` | `_temporal_build` | Ingests session logs into temporal graph |
| `suggest_facts_review` | `_suggest_facts_review` | Dry-run fact candidate review |
| `curation_report` | `_curation_report` | Duplicate/stale cleanup report |
| `gaps_report` | `_gaps_report` | Weekly memory miss report |
| `convergence_report` | `_convergence_report` | Weekly contradiction divergence scan |
| `context_pack` | `_context_pack` | Writes project context artifact (includes temporal context) |
| `research_runner` | `_research_runner` | Runs local Ollama research pipeline |
| `research_runner_api` | `_research_runner_api` | Runs Anthropic API research pipeline |
| `research_gap` | `_research_gap` | Runs run_research() for a gap ticket ('Research gap: <topic>') |
| `pytest` | `_pytest` | Runs a focused Brain test subset (fast CI-like check) |
| `narrative_weekly` | `_narrative_weekly` | Cross-session open loops + direction analysis |
| `memory_agent` | `_memory_agent` | Stale project + decay scan + ticket creation |
| `patterns_rebuild` | `_patterns_rebuild` | Session co-occurrence model rebuild |
| `decay_report` | `_decay_report` | Memory files past their half_life: threshold |
| `claude_supervisor` | `_claude_supervisor` | Weekly Claude Code review of failed/blocked tasks |
| `suggest_facts_auto` | `_suggest_facts_auto` | Nightly auto-mode fact extraction (writes only >= 0.95 confidence) |
| `dream` | `_dream` | Nightly meta-cognitive synthesis over the corpus |
| `reflexion_loop` | `_reflexion_loop` | Weekly self-review loop (ticket writes gated by ATLAS_AUTONOMOUS) |
| `reverify` | `_reverify_scan` | Re-verify stale memories and act on the result |
| `ticket_sync` | `_ticket_sync` | Auto-close tickets from git commits |
| `ticket_health_check` | `_ticket_health_check` | Ticket staleness audit + orphan reset |
| `claude_task` | `_claude_task` | Headless `claude --print` session (zero loop context) |
| `triage` | `_triage` | Classify routine/hard, enqueue agent or claude_task |
| `research_digest` | `_research_digest` | Digest of recent research output |
| `inbox_summarize` | `_inbox_summarize` | Summarize a captured link/blurb locally |
| `inspire` | `_inspire` | Inspiration-engine prompt run |
| `noop` / `fail` | `_noop` / `_fail` | Test/diagnostic executors |

(30 registered kinds -- the registry is `_EXECUTORS` in `brain/executors.py`,
pinned by `tests/test_docs_claims.py`. On the operator's machine a private
`executors_local` module may register extras; the public build ships exactly
the registry above.)

---

## Agent Routing

`agents.route(instruction)` selects the best agent for a task:

1. If `ATLAS_LOCAL_ONLY=1`: only ollama-type agents are considered
2. If smart routing is on (default): `qwen3:1.7b` classifies the instruction
   (cached in `_ROUTING_CACHE`, 500-entry FIFO)
3. Falls back to first available online agent

Agent types in registry:
- `fast` -- qwen3:1.7b (ollama): quick Q&A, routing classifier
- `brain` -- qwen3:8b (ollama): reasoning, planning
- `coder` -- qwen2.5-coder:7b (ollama): code tasks
- `mistral` -- mistral:latest (ollama): general
- `research` -- custom pipeline (ollama): web research
- `claude-code` -- claude-cli: complex coding
- `codex` -- codex-cli: OpenAI Codex
- `opencode` -- opencode-cli: free tier

---

## Nightly Scheduler (APScheduler, UTC)

Most jobs queue a `brain_tasks` row (`_queue_worker_task`) and let the worker
execute them. A few run in-process (no task queued): see In-process column.

| Time | Job ID | Kind | In-process? |
|------|--------|------|-------------|
| 02:00 | db-backup | (direct) | yes -- WAL-safe VACUUM INTO |
| 03:05 | temporal-build | temporal_build | no |
| 03:10 | suggest-facts | suggest_facts_auto | no |
| 03:15 | curation | curation_report | no |
| 03:20 | research-batch | research_runner | no |
| 03:25 | patterns-rebuild | patterns_rebuild | no |
| Sun 03:30 | gaps-report | gaps_report | no |
| Sun 03:35 | convergence-report | convergence_report | no |
| Sun 03:40 | unconfirmed-decay | (direct) | yes -- downgrade_stale_confirmations |
| Sun 03:45 | decay-report | decay_report | no |
| Sun 03:50 | memory-agent | memory_agent | no |
| Sun 03:55 | narrative-weekly | narrative_weekly | no |
| Mon 04:00 | claude-supervisor | claude_supervisor | no |
| Sun 04:20 | reverify | reverify | no |
| 04:30 | dream | dream | no |
| Sun 04:35 | reflexion-loop | reflexion_loop | no |
| 04:40 | embed-index | (direct) | yes -- refresh the semantic embedding index |
| 04:05 | memory-trends | (direct) | yes -- append JSON to .atlas/memory-trends.jsonl |
| 04:10 | research-quality | (direct) | yes -- inject quality_score into unscored reports |
| 04:15 | suggest-tags | (direct) | yes -- inject tags into untagged .memory/ files |
| Mon 09:00 | weekly-digest | (direct) | yes -- ntfy weekly stats summary |
| 23:30 | auto-session-log | (direct) | yes -- write today's session log from git commits |
| */10 | sla-check | (direct) | yes -- ntfy push for overdue tasks |
| */10 | ticket-dispatch | (direct) | yes -- dispatch todo tickets (ATLAS_TICKET_DISPATCH=on) |
| *:45 | zombie-cleanup | (direct) | yes -- mark stale running tasks failed |

Job failures are re-emitted as WARNING via `_job_event_listener` -- APScheduler's
default is silent swallowing. Check `atlas.brain.scheduler` log level for failures.

---

## Brain REST API (port 8767)

All endpoints require `Authorization: Bearer <ATLAS_BRAIN_API_KEY>` except `/health` and `/status`.

| Method | Path | Description |
|--------|------|-------------|
| `GET`  | `/health` | Liveness + model-backend reachability: `{"status": "ok"}` or `{"status": "degraded", "backend": "unreachable"}` (HTTP 200 either way while the process is alive) |
| `GET`  | `/status` | Public health snapshot (no auth) |
| `POST` | `/chat` | Buffered Brain chat -- returns full response JSON |
| `POST` | `/chat/stream` | **SSE streaming chat** -- emits `{type:tool_call}` events per tool dispatch, then `{type:done}` or `{type:error}`. Used by `ws_chat` in the Web UI to show live tool call progress. |
| `POST` | `/task` | Submit a task to the queue (202 Accepted, returns task_id) |
| `GET`  | `/task/{id}` | Poll task status |
| `GET`  | `/tasks` | List tasks with optional status/agent filter |
| `GET`  | `/tasks/stats` | Aggregate task counts by status/agent (since_days param) |
| `GET`  | `/tasks/export` | Download task list as CSV or JSON (fmt, limit, status params) |
| `GET`  | `/workers` | Per-worker active task and heartbeat info |
| `GET`  | `/diagnostic` | Internal diagnostics (model, credit state, queue stats) |
| `GET`  | `/dispatch-stats` | Ticket dispatcher run stats |
| `GET`  | `/routing/stats` | Smart routing config and category distribution |
| `WS`   | `/ws/brain` | WebSocket for real-time task completion notifications |

**WebSocket streaming** (`ws_chat` in web/app.py): the Web UI Chat tab connects
to `/ws/chat/{session_id}`. Each user message POSTs to `/chat/stream` and relays
SSE events to the browser:
- `{type: "thinking"}` -- immediately when message received
- `{type: "tool_call", tool: <name>, round: <int>}` -- before each tool dispatch
- `{type: "response", text: ..., tool_log: [...]}` -- final response

---

## Brain CLI (REPL)

Start with `.venv\Scripts\python.exe -m atlas_plus.brain.cli`. All commands listed in `/help`.

Key command categories:

| Category | Commands |
|----------|---------|
| Tasks | `/tasks [N]`, `/task <id>`, `/recent [N]`, `/submit`, `/watch <id>`, `/kill <id>`, `/retry <id>` |
| Task history | `/attempts <id>` (per-attempt history), `/exhausted` (no retries left) |
| Tickets | `/ticket-status <id>`, `/dispatch-tickets`, `/verify <id>` |
| Memory | `/search <query>`, `/browse [scope]`, `/save <scope> <fact>`, `/pending`, `/approve`, `/reject` |
| Research | `/research-index [N]`, `/quality [N]`, `/health`, `/research-gaps [N]`, `/gap-report [--watch] <topic>`, `/queue-research <topics>` |
| Memory hygiene | `/next-actions [N]`, `/suggest-facts`, `/suggest-tags`, `/curation-report [--write]`, `/decay-report [--watch]`, `/session-log [--dry-run]` |
| Planning | `/plan [N]` (dispatch-eligible tickets), `/loop`, `/stats` |
| Temporal graph | `/temporal-search <q>`, `/temporal-history <entity>`, `/temporal-entities [N]`, `/temporal-rebuild [--force]` |
| System | `/check`, `/escalation`, `/sessions`, `/agents`, `/clear`, `/export` |

---

## Telegram Bot Commands

| Command | Purpose |
|---------|---------|
| `/ticket <proj> <title>` | Create ticket (human assignee) |
| `/queue <proj> <title>` | Queue ticket for claude-code (high priority) |
| `/tickets [proj]` | List open tickets |
| `/setstatus <id> <status>` | Update ticket status |
| `/dispatch_tickets` | Run dispatch_all_eligible() immediately |
| `/task <instr>` | Submit async Brain task |
| `/tasks` | List recent tasks |
| `/tasks_batch` | Submit up to 10 tasks (newline-separated) |
| `/taskstatus <id>` | Poll one task |
| `/watch <id>` | Poll task until done, send completion notification |
| `/dispatch <agent> <instr>` | Route to specific agent |
| `/run <instr>` | Quick task (brain agent) |
| `/think <instr>` | Force qwen3:8b brain agent |
| `/agents` | Agent registry status |
| `/status` | System snapshot (services, queue, memory) |
| `/doctor` | Full atlas-doctor health check |
| `/search <query>` | FTS5 memory search |
| `/remember <text>` | Write fact to memory |
| `/memory <query>` | Alias for /search |
| `/confirm <path>` | Mark memory file confirmed today |
| `/temporal <query>` | Temporal graph query |
| `/decisions` | Decision records needing review |
| `/convergence` | Files with contradiction flags |
| `/gaps` | Memory gap report |
| `/rederive` | Re-derivable memory candidates |
| `/decay [days]` | Dry-run unconfirmed decay scan |
| `/research <topic>` | Research agent: investigate and write report |
| `/research_force <topic>` | Research bypassing deduplication check |
| `/research_index [N]` | List last N research reports |
| `/research_quality` | Research quality summary |
| `/narrative` | Weekly cross-session narrative |
| `/convergence` | Files with contradiction flags |
| `/supervisor` | Trigger weekly Claude Code supervisor review |
| `/claude <instr>` | Priority-1 task routed to Claude Code |
| `/credit` | Reset credit-exhausted agent state |
| `/brain_sessions` | List recent Brain chat sessions |
| `/stats` | Summary stats (tasks, memory, research) |
| `/digest <id>` | AI-generated result digest for completed task |
| `/export` | Task CSV/JSON export |
| `/report` | Request curation report |
| `/orphans [N]` | Top N memory orphan candidates (unreferenced + low health + old) |
| `/loop_status` | Automation loop health (dispatch stats + ticket counts + exhausted count) |
| `/exhausted` | Tasks that burned through all retry attempts; /retry hint |
| `/dispatch_plan [N]` | Next N dispatch-eligible todo tickets ranked by priority |
| `/recent_tasks [N]` | Last N completed Brain tasks (done/failed/blocked) |
| `/sys_health` | Compact system health: services, worker, memory quality, pending facts |
| `/health` | Alias for /sys_health |
| `/research_gaps [N]` | Top N memory miss queries with ticket+report coverage status |
| `/research_quality [topic]` | Research quality summary; optional topic filter |
| `/gap_report <topic>` | Queue a research_gap Brain task for a topic from mobile |
| `/curation_report` | Weekly curation scan (duplicates/stale/convergence/decisions) |
| `/temporal_search <query>` | Current-fact search with confidence scores |
| `/temporal_history <entity>` | Full fact timeline for a named entity |
| `/temporal_stats` | Temporal graph episode/entity/fact counts |
| `/temporal_rebuild [--force]` | Rebuild temporal graph (skips if fresh; --force overrides) |
| `/suggest_facts` | Dry-run fact extraction from today's session transcript |
| `/suggest_tags` | Scan for memory files missing frontmatter tags |
| `/session_log [--dry-run]` | Write today's session log from git commits |
| `/next_actions [N]` | Prioritized memory hygiene list (decayed/orphans/gaps) |
| `/decay_report` | Queue decay_report Brain task (memory half-life overdue scan + artifact) |

---

## Key Environment Variables

| Variable | Default | Effect |
|----------|---------|--------|
| `ATLAS_AUTONOMOUS` | `on` | Master gate for autonomous memory writes |
| `ATLAS_SESSION_AUTONOMOUS` | unset | Per-session gate (session logs write when on) |
| `ATLAS_LOCAL_ONLY` | `off` | When `1`, API agents treated as offline |
| `ATLAS_BRAIN_INLINE_TASKS` | `on` | Execute agent tasks inline (not via worker) |
| `ATLAS_MEMORY_ROOT` | `<workspace>/.memory` | Memory corpus root |
| `ATLAS_STATE_DIR` | `<workspace>/.atlas` | SQLite DB + artifacts + logs |
| `ATLAS_BRAIN_URL` | `http://127.0.0.1:8767` | Brain API URL for clients |
| `ATLAS_MCP_TOKEN` | -- | Bearer token for MCP HTTP mode |
| `ATLAS_BRAIN_API_KEY` | -- | Bearer token for Brain HTTP mode |
| `ATLAS_RESEARCH_WEB_SEARCH` | `off` | Enable DDG/SearXNG web search in research agent |
| `ATLAS_RESEARCH_SEARXNG_URL` | unset | Local SearXNG base URL (e.g. http://localhost:8888) |
| `ATLAS_RESEARCH_CACHE_TTL` | `3600` | Web search result cache TTL in seconds |
| `ATLAS_SMART_ROUTING` | `on` | Use qwen3:1.7b instruction classifier for routing |
| `ATLAS_TICKET_DISPATCH` | `off` | Enable auto-dispatch of todo tickets to Brain (every 10 min) |
| `ATLAS_TICKET_QUALITY_THRESHOLD` | `4.0` | Quality score below which done results reopen the ticket |
| `ATLAS_VERIFY_TICKETS` | `off` | Auto-queue pytest verify task after each agent ticket task |
| `ATLAS_CLAUDE_ESCALATION` | `off` | Auto-escalate failed/blocked tasks to Claude Code |
| `ATLAS_CLAUDE_QUALITY_ESCALATION` | `off` | Auto-escalate low-quality results to Claude Code |
| `ATLAS_CLAUDE_QUALITY_THRESHOLD` | `3.5` | Self-improvement quality score floor for escalation |
| `ATLAS_NTFY_URL` | unset | ntfy.sh topic URL for push notifications |
| `ATLAS_NTFY_COMPLETIONS` | `off` | Push ntfy notification when each Brain task completes (done/failed) |
| `ATLAS_SLA_MINUTES` | `15` | Minutes before ntfy SLA breach alert fires |
| `ATLAS_DIGEST_RESULTS` | `off` | Generate 2-3 sentence AI digest for long task results |
| `ATLAS_MEMORY_AUTOCOMMIT` | `off` | `git add + commit` after every memory_remember write |
| `ATLAS_CONFIRM_DECAY_DAYS` | `90` | Days before unconfirmed facts get `confidence: low` |
| `ATLAS_DB_BACKUP_KEEP` | `14` | Number of WAL-safe DB snapshots to retain |
| `ATLAS_WORKER_HEARTBEAT_INTERVAL` | `60` | Seconds between heartbeat renewals in _HeartbeatKeeper |
| `ATLAS_RESEARCH_RETRY` | `off` | Re-synthesize when initial quality_score < threshold |
| `ATLAS_RESEARCH_RETRY_THRESHOLD` | `0.35` | Score below which research retry fires |
| `ATLAS_RESEARCH_MIN_WORDS` | `200` | Word count floor for quality gate (flags word_count_low) |
| `ATLAS_RESEARCH_DEDUP_DAYS` | `30` | Max age of existing report checked for deduplication |

---

## Testing

Python 3.11+ is required (the stdlib tomllib dependency sets the floor); 3.11 is the recommended version for running the suite.

```bash
# From an activated venv (see the Quickstart in the README):
python -m pytest tests -q -p no:cacheprovider
# The suite runs green on Linux, macOS, and Windows in CI on every push;
# your local run is the same suite CI executes.
```

Test files follow the pattern `test_<module>.py`. Integration tests requiring
Ollama are marked `@pytest.mark.ollama` and skipped in CI.

Contribution workflow, suite expectations, and test-isolation notes live in
[CONTRIBUTING.md](../CONTRIBUTING.md); the suite itself (under `tests/`) is
the authoritative behavior spec.

---

## Common Workflows

**Submit a task from Web UI:**
Dashboard -> type instruction -> Run (or Quick Run buttons for scheduled jobs)

**Force-run a scheduler job immediately:**
Web UI Config tab -> Scheduler Trigger, or: `POST /scheduler/trigger/{job-id}`

**Check system health:**
```powershell
atlas-doctor          # color dashboard
atlas-doctor --json   # machine-readable
atlas-doctor --watch  # refresh every 30s
```

**Approve a pending memory fact:**
Web UI Memory tab -> Pending Facts panel -> Approve/Reject

**Find why a task failed:**
Web UI Tasks tab -> click task row -> Detail panel shows result + error

**Reset credit-exhausted agents:**
Web UI Config tab -> Reset credit exhaustion button

**Activate the automation loop:**
```powershell
# In the environment the Brain process runs in (shell, service
# definition, or a local env file your supervisor sources):
$env:ATLAS_TICKET_DISPATCH = "on"   # auto-dispatch todo tickets every 10 min
$env:ATLAS_VERIFY_TICKETS  = "on"   # auto-queue pytest verify after agent tasks
```
Or toggle ATLAS_TICKET_DISPATCH from Web UI Config panel without restarting Brain.

**Monitor dispatch health:**
```powershell
atlas-doctor            # Ticket dispatcher panel: state / last run / in-flight
/ticket-status ATL-001  # Brain CLI: ticket state + linked task
/dispatch-tickets       # Brain CLI: trigger dispatch now
```

**Manually trigger verification:**
```powershell
# Queue a verify task for a ticket from Brain CLI:
# /submit --agent brain Verify: <describe what to test>
# session_id must follow "verify-ticket-{ID}" pattern to trigger reopen on fail
```

**Memory hygiene (curation, decay, next actions):**
```powershell
# Web UI Memory tab panels (HTMX -- auto-loads on tab open):
#   Curation Health  -- GET /api/memory/curation-report-panel (duplicates/stale/convergence counts)
#   Next Actions     -- GET /api/memory/next-actions-panel (decayed/orphans/gaps)
#   Memory Gaps      -- GET /api/memory/gaps (recurring miss queries)

# JSON API endpoints:
#   GET  /api/memory/curation-report          -- {duplicates, stale, convergence, decisions, rederivable}
#   GET  /api/memory/curation-report?dry_run=false  -- same but writes report to .memory/_atlas/
#   GET  /api/memory/next-actions?limit=5     -- {decayed, orphans, untagged, gaps}

# Brain CLI commands:
#   /curation-report          -- run full weekly scan inline, show counts
#   /curation-report --write  -- same, write report to .memory/_atlas/
#   /next-actions [N]         -- prioritized to-do list (decayed, orphans, gaps)
#   /decay-report [--watch]   -- queue decay_report task (half-life overdue files)
#   /decay-report --watch     -- queue and poll until done

# Telegram commands:
#   /curation_report   -- curation scan summary (duplicates/stale/convergence/decisions)
#   /next_actions [N]  -- same as Brain CLI /next-actions
#   /decay [days]      -- dry-run unconfirmed decay scan (fast, no task queued)
```

**Temporal knowledge graph:**
```powershell
# Web UI Memory tab / Temporal Graph panel:
#   Query input -> GET /api/temporal/query?q=<query>&mode=current|history (HTMX)
#   Stats panel -> GET /api/temporal/stats (HTMX, shows last_built date + staleness)

# JSON API endpoints:
#   GET /api/temporal/entities?limit=50       -- sorted entity list
#   GET /api/temporal/history?entity=<name>   -- full fact timeline for an entity
#   GET /api/temporal/stats                   -- episode/entity/fact counts + staleness

# Brain CLI:
#   /temporal-search <query>   -- current facts matching query
#   /temporal-history <entity> -- full fact timeline
#   /temporal-entities [N]     -- list all entities
#   /temporal-rebuild [--force]-- rebuild from .memory/ files (skips if fresh)
```
