---
id: adr-012
title: ADR-012 — Persona Schema
type: adr
status: accepted
updated: 2026-05-03
---

# ADR-012 — Persona Schema

## Context

Atlas needs a way to give any LLM a specific identity, memory access scope, provider preference, and cost guard before it begins working. Without this, every agent starts generic and has to be primed by hand.

We also have `.memory/runbooks/` — repeatable procedures any agent can follow. These overlap with the emerging SKILL.md standard (adopted by Cursor, Cline, Windsurf, and others). They need a standard shape so clients can discover and invoke them.

## Decision

### Personas

A persona is a markdown file at `.memory/personas/<name>.md` with:
- YAML frontmatter defining the schema
- A freeform `# Identity` body that becomes the system prompt

Schema fields:

| Field | Type | Required | Purpose |
|-------|------|----------|---------|
| `id` | string | yes | Unique machine-readable name |
| `title` | string | yes | Human-readable label |
| `type` | `persona` | yes | Fixed value |
| `status` | `active`/`retired` | yes | Lifecycle state |
| `updated` | ISO date | yes | Last edit date |
| `allowed_namespaces` | list[str] | yes | Which `.memory/` subtrees this persona may read/write. `global` means all non-persona dirs. |
| `default_provider` | string | yes | `ollama:<model>` or `claude:<model>` |
| `cost_cap_usd_per_session` | float | yes | Hard ceiling. 0 = local-only. |
| `skill_refs` | list[str] | no | Runbook names this persona is expected to follow |

### Skills (runbook projection)

A skill is a read-only projection of `.memory/runbooks/<name>.md` into a SKILL.md-shaped descriptor. No new file format is introduced — runbooks already exist; skills are just a discovery + invocation layer over them.

SKILL.md-aligned shape:
```json
{
  "name": "version-before-edit",
  "title": "Version Before Edit",
  "type": "runbook",
  "description": "First sentence of the runbook body",
  "body": "<full runbook markdown>"
}
```

The `skills_invoke` MCP tool returns the full body for the agent to follow. There is no execution — skills are information, not code.

## Relationship: personas vs skills

| Concept | Where defined | Purpose |
|---------|--------------|---------|
| Persona | `.memory/personas/<name>.md` | Role identity + access scope + cost guard |
| Skill | `.memory/runbooks/<name>.md` (projected) | Repeatable procedure to follow |

A persona may reference skills via `skill_refs`. An agent adopts a persona, then invokes the skills it needs. Skills are reusable across personas.

## MCP tools added

| Tool | What it does |
|------|-------------|
| `personas_list_tool()` | Lists all active personas with metadata |
| `personas_adopt_tool(name)` | Returns identity prompt + access scope for a named persona |
| `skills_list_tool()` | Lists all runbooks as SKILL.md-shaped descriptors |
| `skills_invoke_tool(name)` | Returns full runbook body for the agent to follow |

## Consequences

- Adding a persona is a markdown edit — no code change needed.
- Adding a runbook automatically exposes a new skill.
- The persona system is optional — agents that don't call `personas_adopt_tool` are unaffected.
- `allowed_namespaces` is advisory at the MCP layer for now; enforcement is left to Phase 8+.
