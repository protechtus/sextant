# ADR-016: Data Classification and Outbound Boundaries

**Date:** 2026-05-18
**Status:** Active
**Relates to:** CRITICAL-2 in 2026-05-18 audit; backup.py; `.memory/`; `.atlas/`

---

## Context

Until 2026-05-18 the Atlas backup uploaded everything under `<workspace>/.memory\` and `<workspace>/.atlas\` (except logs) to Google Drive in cleartext via an rclone `drive` remote. That set included raw transcripts of every captured LLM conversation, FTS5 indices, and optionally the paranoid-mode kernel signing keys.

There was no documented rule for **what is allowed to leave the workstation, in what form, to where**. The audit (CRITICAL-2) treated this as a single point of failure: any Google account compromise exposed the corpus.

This ADR fixes the policy gap by defining three tiers and the outbound boundary for each.

---

## Decision: three data tiers

### Tier 1 -- Public-ish

Content that could be published without harm. OK to back up to any remote (encrypted or not). OK to publish to public git, public README, public Notion.

**Examples:**
- Open-source code in the workspace.
- Generic AGENTS.md / SKILL.md / runbook content with no identifying details.
- ADRs that describe architecture without revealing secrets or third-party customer data.
- Project READMEs.

**Boundary:** none. May leave the workstation freely.

### Tier 2 -- Personal

Content that is not catastrophic if leaked but should not be public. Outbound only via the **encrypted** backup path (crypt remote). Never intentionally shared.

**Examples:**
- `.memory/identity/user.md` and related identity files.
- `.memory/feedback/` rules that reflect personal preferences.
- `.memory/projects/<name>.md` project state, including pointers to internal tools.
- `.memory/sessions/<date>.md` session logs.
- `.atlas/transcripts/` captured conversations (cleartext NDJSON).
- `.atlas/state.sqlite` FTS5 + cursors.
- `.atlas/temporal_graph/` derived facts + entity timelines.
- `updates.txt`.

**Boundary:** the rclone `crypt` remote (`gdrive_crypt:` wrapping `gdrive:Atlas`). Cleartext never reaches the upstream filesystem.

### Tier 3 -- Secrets

Content that is catastrophic if leaked. Never leaves the workstation in any form, encrypted or otherwise. NTFS-restricted to the owning user. Excluded from all backups by name pattern, even when running through a crypt remote.

**Examples:**
- `ATLAS_MCP_TOKEN` (bearer for the MCP tunnel).
- `ATLAS_BRAIN_API_KEY` (bearer for Brain).
- `ATLAS_TELEGRAM_BOT_TOKEN`.
- `ATLAS_NTFY_TOPIC` (if used in a way that doubles as auth).
- The crypt remote's passphrase at `<workspace>/.atlas\.crypt-passphrase`.
- Kernel paranoid-mode keys: `enrollment.secret`, `jwt.secret`, `vault.key`, `event_signing_private.pem`, `event_signing_public.pem`, `*.pfx`, `*.p12`.
- Any `.env`, `.env.local`, `.env.production` file.
- Customer data from work (ExampleCorp), regardless of format.

**Boundary:** hard exclude in `backup.py` `_SECRET_PATTERNS`. Even if the crypt remote is misconfigured, secrets are not uploaded. Defense in depth.

---

## Migration from a cleartext remote

If an earlier backup left cleartext history on a remote, migrate by creating a crypt
remote, re-running the initial backup into it, verifying the mirror, archiving (not
deleting) the old path, pointing `ATLAS_RCLONE_REMOTE` at the crypt remote, and deleting
the archive only after a quarantine period. Archive-not-delete applies throughout.

## Tier audit (one-time, this ADR)

The 2026-05-18 audit must be followed by a one-time pass over `.memory/` and `.atlas/` to confirm every existing file is correctly classified. Specifically:

- Any `*.key`, `*.secret`, `*.pem`, `*.pfx`, `*.p12` under `.atlas/` -> Tier 3.
- Any `.crypt-passphrase` anywhere -> Tier 3.
- Any `.env` file in the workspace -> Tier 3, NTFS-restrict, exclude from backup.
- Everything else in `.memory/` -> Tier 2.
- Everything in `.atlas/transcripts/` -> Tier 2.
- Anything in `<workspace>\transfer\` containing ExampleCorp-specific data -> Tier 2 (and consider Tier 3 case-by-case).

The audit pass is tracked as ticket ATL-AUD-04.

---

## Consequences

- One source of policy. Any future "should we back this up" question has an answer.
- The `_SECRET_PATTERNS` list in `backup.py` is the implementation enforcement point. Adding a new Tier 3 file means appending to that list.
- New project memory files (`.memory/projects/<name>.md`) default to Tier 2. Documented in AGENTS.md.
- The Telegram bot token rotation is a Tier 3 hygiene event; the runbook covers it.
- Google Drive holds only ciphertext after the migration completes.
