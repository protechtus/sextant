# ADR-006: Transcript Manager — Crash-Resilient Append-Only Capture

Date: 2026-04-27
Status: accepted

## Context

Every LLM conversation across every client (Claude Code, ChatGPT, Cursor, Cline, Continue, Ollama, LM Studio, llama.cpp, Jan, GPT4All, OpenLLaMA) is a primary input to memory. The system must capture them without trusting the client to flush cleanly: the host or the client can die mid-turn at any time.

## Decision

Per-conversation append-only NDJSON files with fsync per turn, atomic rename on clean close, per-conversation hash chain, and a side journal recording the last committed offset and hash.

- File path: `<workspace>/.atlas\transcripts\<source>\<YYYY-MM-DD>\<conversation-id>.ndjson` (canonical) or `<conversation-id>.ndjson.partial` (in-flight).
- Each NDJSON record carries `hash_prev` and `hash` so a corrupt or truncated tail is detectable.
- A SQLite + FTS5 sidecar at `<workspace>/.atlas\transcripts\index.sqlite` indexes conversations and turns for fast list/search.
- Capture sources are independent processes that share the same store API; no source can corrupt another.
- Recovery scans `.partial` files at startup, validates hash chains, replays unflushed turns onto the bus so downstream consumers (Memory Writer) catch up.

## Consequences

- Power-loss or kill -9 mid-turn loses at most the in-flight record, never prior turns.
- Hash chain is per-conversation, not global, so transcripts grow independently and don't bottleneck the kernel event log.
- Sources stay decoupled — a Claude Code tailer crashing does not affect the OpenAI proxy.
- Storage cost is small (NDJSON, no embeddings) but must be retention-managed eventually; archival policy is deferred to v0.2.
