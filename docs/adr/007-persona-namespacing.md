# ADR-007: Persona Namespacing on Top of Kernel ACL

Date: 2026-04-27
Status: accepted

## Context

Personas (planner, frontend, ux, backend, security, finance, life-ops, devops, doc-writer, transcript-historian) need their own private memory while still sharing global facts about the operator and the workspace.

## Decision

Add `PERSONA` to `atlas_memory.models.NamespaceKind`. A persona reads from the global namespace plus its own `persona/<name>/...` namespace, and writes only to its own. Cross-persona writes are routed through the conflict queue so no persona can silently overwrite another.

The kernel's existing `_assert_namespace_access` is extended to enforce this; no parallel ACL system is built.

## Consequences

- One source of truth for access control (the kernel).
- Persona scope is a string convention enforced at the boundary, easy to grep.
- Adding a new persona is a YAML edit plus a namespace claim, not a code change.
