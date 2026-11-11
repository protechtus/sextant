# Sextant Roadmap

Honest state of what exists, what is planned, and what is deliberately out
of scope. Gaps stated here are invitations, not apologies -- issues and
discussion on any of them are welcome.

## Recently landed (the launch-readiness arc)

- Hybrid search: FTS + local semantic embeddings (Ollama), graceful
  keyword-only degradation
- Multi-device sync: hub/spoke, conflict quarantine, policy-fenced
- Docker compose path; VPS (Caddy) and Tailscale self-hosting guides
- Importers: Obsidian vaults, ChatGPT exports, claude.ai exports
- Safety-gated uninstall/reset; `.sextant-protect` corpus marker
- Interactive-vs-background write consent separation
- Cross-process write locking + atomic corpus writes

## Near-term (roughly in order)

1. ~~**Web UI corpus browser**~~ -- shipped earlier than this entry
   admitted (stale text caught 2026-09-24): the dashboard's Memory tab
   already browses the corpus tree, searches with confidence banding,
   views files, and edits them in place. Verified against the live
   endpoints; nothing left to build here.
2. ~~**Environment variable reference**~~ -- DONE 2026-09-23:
   [docs/CONFIGURATION.md](docs/CONFIGURATION.md) catalogs every variable,
   with a test that fails when a new env read ships undocumented.
3. ~~**Dashboard download links carry the API key in the query string**~~
   -- DONE 2026-09-24: the authed page load sets an HttpOnly
   SameSite=Strict cookie accepted for GET/HEAD auth only; download and
   export links carry no `?key=`. 2026-09-24 follow-through: the
   EventSource/WebSocket streams ride the cookie too -- no dashboard
   URL carries the key after the initial page load.
4. ~~**FTS incremental indexing**~~ -- DONE 2026-09-24: the search-path
   freshness check now diffs mtimes and re-indexes only changed files
   (deletes included); the full rebuild remains as `atlas-fts-rebuild
   --force` for recovery. The former any-change-full-rebuild cliff is
   gone.
5. ~~**Spend/usage visibility**~~ -- DONE 2026-09-24: usage is captured
   at the provider seam (Ollama/Anthropic/OpenAI token counts), recorded
   to a ledger on every Brain turn (tasks AND interactive chat), and
   surfaced as Brain `GET /spend`, dashboard `/api/spend`, and a doctor
   "Token Spend (7d)" line. Cost is an estimate from a small
   `ATLAS_MODEL_PRICES`-extendable table; local models cost zero. A
   LiteLLM proxy remains a fine companion (docs/HOWTO-litellm.md).
6. ~~**Auth consolidation**~~ -- DONE 2026-09-24: `atlas_plus.auth` is
   the single home for the primitives (timing-safe compare, bearer
   parsing, sliding-window rate limiter, failure log); web, Brain, and
   MCP compose from it with identical behavior. The sync hub's hashed
   device-token store and MCP lockout thresholds stay policy-local by
   design.
7. ~~**Capture beyond Claude Code**~~ -- DONE 2026-09-24 via the generic
   drop path: put `.jsonl`/`.json` exports (or `.md`/`.txt` notes) in the
   drop directory and run `sextant-transcripts ingest`; originals are
   archived, re-runs never double-ingest. Per-client live hooks remain
   possible later, but any tool that writes a file can feed the index
   today.
8. ~~**Dependency upper bounds / lockfile**~~ -- DONE 2026-09-24 as a
   constraints file, not upper bounds: `constraints.txt` carries
   known-good exact pins (`pip install ... -c constraints.txt`) with a
   test that every direct dependency stays pinned. Blanket upper bounds
   were deliberately rejected -- they fight other packages in shared
   environments; CI keeps validating the floating set daily.

## New near-term (added 2026-09-24)

9. **Sextant Clip browser extension** -- shipped as an unpacked MV3
   extension (`extensions/sextant-clip/`): one click sends the current
   claude.ai / ChatGPT / Gemini chat (or any page) to the local inbox
   endpoint and into the transcript pipeline. Next: Firefox shims,
   selector upkeep as chat UIs drift, store publishing (developer
   accounts + privacy disclosures + review -- deliberate, post-traction).
10. **Remote MCP connectors (claude.ai / ChatGPT -> live memory tools)**
   -- both vendors now support user-added remote MCP servers, so the web
   chat can call `memory_remember`/`memory_search` directly. Spike
   specced (auth shim is the open question); operator-assisted since it
   needs the tunnel and vendor sessions. Directory publishing is
   explicitly deferred.

## Later / with demonstrated demand

- Sync: append-union merge (fewer conflict quarantines), scheduled
  background sync, tombstone protocol for deliberate deletes
- Hosted sync relay for users who do not want to run a server
- Mobile read-only corpus client (the sync protocol already suffices)
- E2E encryption at rest for VPS-hosted hubs
- Import distillation: LLM summarization of imported history into curated
  memories (opt-in, local models)

## Non-goals

- **LLM gateway/routing/keys management** -- LiteLLM and friends do this
  well; Sextant integrates with them rather than competing.
- **A chat UI** -- Sextant serves AI clients; it is not one.
- **Multi-user/teams/SSO** -- the engine is personal infrastructure; a
  team layer, if it ever exists, will be a separate concern.
- **Cloud dependencies on the core path** -- non-negotiable.
