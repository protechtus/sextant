# ADR-008: Bidirectional Markdown Sync With Rules As Code

Date: 2026-04-27
Status: accepted

## Context

`<workspace>/.memory\` is a curated markdown corpus and the human-readable face of the memory system. The kernel's event log is the machine-readable source of truth. Both must stay coherent without manual reconciliation, and the six load-bearing rules (version-before-edit, archive-not-delete, llm-agnostic-output, session-log, update-memory-after-fail, code-delivery) must be enforced uniformly whether a human or an LLM wrote the change.

## Decision

A single Markdown Sync component owns both directions:

- **Read** — reuses [migration/markdown_bootstrap.py](../../../atlas-memory/src/atlas_memory/migration/markdown_bootstrap.py) to import `.md` files as `memory.proposed` + `memory.committed` events.
- **Write** — projects events to `.md` files via templated renderers, with the rules implemented as preconditions:
  - `version_before_edit(path)` runs before any overwrite.
  - `archive_on_remove(path, reason)` is the only way to "delete".
  - `enforce_llm_agnostic(text)` strips XML tags, emoji, em-dashes, slash-commands.
  - `prepend_session_log(date, entry)` writes both `updates.txt` and `.memory/sessions/<date>.md`.
  - `lesson_stub_on_fail(task)` appends to `feedback/` after `task.state_changed{status=failed}`.

Conflict between a human-edited file and a kernel projection raises a normal kernel `conflicts` row. The human resolves via `/v1/conflicts/resolve` like any other write.

## Consequences

- Rules are auditable as code, not as folklore.
- The mirror at `~\.claude\projects\c--Tools\memory\` continues to be one-way (canonical → mirror).
- A human still owns the lesson text after `update-memory-after-fail` triggers; the system writes a stub, not the analysis.
