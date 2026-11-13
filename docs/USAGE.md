# Using Sextant day to day

This is the guide for after the wiring works: your MCP client is
connected (GETTING-STARTED section 4) and `memory_health` answers. You
do not call these tools yourself -- you ask your assistant in plain
language, and it calls them. Tool names below are what shows up in your
client's tool log, so you can see what actually happened.

## The mental model

Your corpus is a directory of markdown files, split into scopes that
double as routing hints:

- `identity/` -- who you are, how you like to work
- `feedback/` -- corrections you have given your assistant; the rules it
  must not relearn the hard way
- `reference/` -- durable facts: people, machines, systems, glossaries
- `projects/` -- per-project state that outlives a session
- `decisions/` -- decisions with the why, conditions, and revisit triggers
- `sessions/` -- the chronological log
- `personas/`, `runbooks/` -- reusable agent identities and procedures
- `tickets/` -- lightweight per-project task tracking

Every file carries YAML frontmatter (`updated`, `confidence`,
`last_confirmed`, `tags`) that the staleness machinery reads. The files
are yours: edit them in any editor, keep them in git, grep them.

## Remembering

Say "remember that ..." and the fact is routed to the right scope file
(`memory_remember_tool`; agents with provenance use
`memory_remember_tool_v2`, which records source, confidence, and the
session that produced the fact). This is an interactive write -- your
request is the consent. Nothing writes in the background unless you
enable `ATLAS_AUTONOMOUS` (see docs/CONFIGURATION.md).

Three specialized flavors matter more than they look:

- **Decisions**: "record the decision to use X because Y, revisit if Z"
  (`memory_remember_decision_tool`). Future sessions get the why, not
  just the what.
- **Anti-memory**: "we tried that and rejected it -- don't suggest it
  again" (`memory_anti_remember_tool` with a category `scope` such as
  `libraries` or `project:<name>`, a `reject_pattern`, and `applies_to`
  `workspace` or `project`; listed via `memory_anti_list_tool`). A later
  `memory_remember` that repeats a rejected idea is stored with an
  `anti_warning` naming the rejection, and `atlas-prime` lists the rejected
  ideas that match the task words under "Do NOT suggest".
- **Autocommit**: set `ATLAS_MEMORY_AUTOCOMMIT=on` with a git-repo
  corpus and every interactive write becomes a commit.

## Finding things

"What do we know about X?" runs `memory_search_tool` -- hybrid search
(keyword FTS fused with semantic embeddings when Ollama is available;
plain keyword otherwise, automatically). For targeted reads there are
`memory_read_tool`, `memory_list_tool`, `memory_frontmatter_tool`, and
`memory_topics_tool` (the tag index). If you maintain a local research
library, `research_search_tool` searches it with citations.

## Keeping memory honest

Facts go stale; a memory system that cannot say "this might be outdated"
becomes confidently wrong. When your assistant re-verifies something,
have it "confirm that fact" (`memory_confirm_tool`) -- that refreshes
`last_confirmed`. `atlas-review-memory unconfirmed-decay` lists notes not
confirmed within the threshold (a note never confirmed counts from its last
edit and shows `never`); `--write` downgrades their confidence, by you at
the terminal or by the nightly job when `ATLAS_AUTONOMOUS=on`.
`memory_list_confirmed_tool` shows what has been checked recently.
`atlas-review-memory contradictions` needs a model (Ollama, or a provider
key with `ATLAS_LOCAL_ONLY=off`); without one it says so and exits 2 rather
than reporting "none". It reports pairs; it does not edit the files.

## Time as a first-class fact

`now_state_tool` gives sessions a real clock (LLMs guess otherwise). The
temporal knowledge graph tracks how facts about an entity changed:
"what did we believe about the deploy pipeline in June?" is
`temporal_search_tool` / `temporal_history_tool`, with
`temporal_entities_tool` listing what the graph knows and
`temporal_build_tool` rebuilding the index.

## Sessions, personas, runbooks

Start sessions by asking "prime from memory" -- identity, feedback rules,
and the latest session log come back in one pass. End them with "log
this session." Repeatable procedures live in `runbooks/`
(`skills_list_tool` / `skills_invoke_tool` return them SKILL.md-shaped);
agent identities with scoped access live in `personas/`
(`personas_list_tool` / `personas_adopt_tool`).

## Bringing transcripts from anywhere

Claude Code sessions are summarised by the Stop hook into `sessions/<date>.md` (when
`ATLAS_AUTONOMOUS=on` reaches the hook and a local model answers); to index the
transcripts themselves, run the tailer `python -m atlas_plus.transcripts.sources.claude_code`
(it watches `~/.claude/projects`), or drop the `.jsonl` files into the drop directory as
below (Claude Code's own line shape is accepted). Any tool that can
write a file can feed the transcript index: drop `.jsonl` / `.json`
chat exports (or plain `.md`/`.txt` notes) into the drop directory
(`<state-dir>/transcripts/drop/` by default) and run
`sextant-transcripts ingest`. Originals are archived beside the drop
dir, never deleted, and re-running never double-ingests.

For web chats (claude.ai, ChatGPT, Gemini) there is a one-click path:
the **Sextant Clip** browser extension (`extensions/sextant-clip/`)
scrapes the conversation you are looking at and POSTs it to the
dashboard's inbox endpoint (`POST /api/inbox/conversation`, bearer header,
`{"title", "source", "messages": [{"role", "content"}]}`), which feeds the
same pipeline. Install instructions and the exact body live in the
extension's README.

## Tickets

"Open a ticket on project X to fix Y" (`tickets_create_tool`), "what's
open on X?" (`tickets_open_tool` / `tickets_list_tool`), "mark T-12
done" (`tickets_status_tool`). Markdown files under `tickets/<project>/`
-- greppable, git-tracked, no external tracker.

Steps only a human can take (paste a credential, approve a plan, flip a
switch) are tickets too: assign them to `human` and they appear in the
web dashboard's **Operator** tab -- one priority-sorted queue across
every project, instructions inline. A `verified:` date in the ticket
marks the instructions as researched and ready; without one the step is
flagged as still needing research. Marking a step done or skipped
records it in `operator/action-log.md` inside the corpus, so what the
operator actually did is searchable memory like everything else, and
session start mentions how many steps are waiting.

## Watching the system work: the Map tab

Most of what Sextant does happens where you cannot see it -- an agent
reads memory over MCP, the Brain finishes a task, a ticket closes, the
temporal graph rebuilds. The web dashboard's **Map** tab makes that
visible: a live diagram of your services (probed over their health
endpoints), your data stores (from what actually exists on disk), and
the connections between them. When a component acts, its edge pulses
and the event appears in the ticker beside the map.

The feed behind it is `logs/activity.ndjson` in the state directory:
each component appends one metadata line per event (component, event
name, durations, small identifiers). Events carry a *reference* to
where the data involved lives -- the memory path an agent read, the
ticket's markdown file, a task's artifact path, a truncated search
query -- but never the contents themselves: bodies, arguments, and
results are not recorded, so the feed is safe to display and cheap to
keep. It self-trims to the newest 5,000 lines.

## Ops from inside a session

- `memory_health` -- is the server up, which corpus, which features.
- `project_state_tool` -- git status, dependencies, and compose state of
  a project directory in one snapshot.
- `budget_preflight_tool` -- for Claude subscribers: check 5-hour
  rate-limit headroom before committing to a large task.

## Phrases that work

- "Remember that the staging DB password rotates monthly."
- "Record the decision to ship X as a monolith; revisit if the team doubles."
- "We rejected Redis for this -- anti-remember it with the reason."
- "What do we know about the Henderson contract?"
- "Confirm the fact about the backup schedule -- I just verified it."
- "What changed about the auth service since July?"
- "Prime from memory." / "Log this session."

The full tool inventory with schemas is what your MCP client shows on
connect; `sextant components` lists which optional layers are active.
