# Decay-as-behavior: memory confidence that acts

Status: BUILT + operational (2026-06-05)
Code: `temporal_graph/decay_model.py`, `curation/decay.py`, `curation/reverify.py`,
`brain/executors.py` (kind=reverify), `brain/scheduler.py` (Sunday 04:20 UTC)
Tests: `test_decay_model.py` (24), `test_curation_decay.py`, `test_reverify.py` (17),
`test_reverify_executor.py` (4)

## The thesis

Most memory systems treat a stored fact's confidence as a constant. The best of them
(Graphiti, Zep) add *bi-temporal invalidation*: when a newer fact contradicts an older
one, the old edge is marked invalid-from a date but retained for history. That is a big
step past "vector store of frozen embeddings" - but it is still **reactive**: a fact only
loses standing when something explicitly contradicts it. Nothing happens to a fact that is
simply *getting old without being re-confirmed*.

Atlas takes the rarer position: **confidence is epistemic and it decays, and that decay
drives action.** A fact the system has not re-verified in a long time is not just ranked
lower - it is re-checked, and depending on the result its clock is reset, it is quarantined,
or the human is asked. The memory maintains its own freshness.

This is the difference between *"we down-rank stale results"* (common) and *"the system
notices its own knowledge going stale and does something about it"* (rare).

## The model (`temporal_graph/decay_model.py`)

A fact has a base confidence `c0` and a half-life `H` (days) - the period over which,
absent re-verification, belief halves. For an age `a` (days since `last_verified_at`):

    effective_confidence = c0 * 2 ** (-a / H)

The age/half-life ratio `r = a / H` classifies an **epistemic state** that maps to a
**prescribed action**:

| ratio r   | state    | effective conf | action      | meaning                            |
|-----------|----------|----------------|-------------|------------------------------------|
| r < 1     | FRESH    | > 0.50 c0      | RETAIN      | trust as-is                        |
| 1 <= r<2  | AGING    | ~ .25-.50 c0   | SURFACE     | show with a staleness note         |
| 2 <= r<3  | STALE    | ~ .12-.25 c0   | REVERIFY    | enqueue autonomous re-verification |
| r >= 3    | EXPIRED  | < 0.12 c0      | QUARANTINE  | do not assert; re-verify or archive|

Thresholds are tied to the half-life, not absolute days, so a fast-moving fact (`H=7`) and
a slow one (`H=365`) are judged on the same epistemic scale. The module is pure and
deterministic (the caller injects `now`/age), so the identical policy drives the
temporal-graph ranker, the file-level memory scan, and the autonomous loop.

## The closed loop

    detect            classify              re-verify                 act
    ------            --------              ---------                 ---
    half-life scan -> decay_model.assess -> model: still true? -----> STILL_VALID -> reset clock (gated)
    (curation/decay)  state + action        (curation/reverify,        OUTDATED    -> quarantine list (report)
                      + reverify_queue       LLM-agnostic)              NEEDS_HUMAN -> human-review list (report)

1. **Detect** - `curation/decay.find_decayed_facts()` scans `.memory/` for files past their
   `half_life:` window. Every candidate is now run through `decay_model.assess()` and carries
   its `state`, `action`, and eroded `effective_confidence`.
2. **Queue** - `reverify_queue()` selects the STALE/EXPIRED candidates (the ones whose action
   is REVERIFY/QUARANTINE).
3. **Re-verify** - `curation/reverify.reverify_fact()` asks the model whether each fact still
   holds. The model call routes through the provider layer (role `brain`), so re-verification
   is itself LLM-agnostic and the verdict parser defaults to NEEDS_HUMAN when unsure.
4. **Act** - `run_reverification()` drains the queue:
   - STILL_VALID -> `reset_clock()` bumps `updated:` + `last_verified_at:` (the decay clock
     restarts), but only when the autonomous mutation gate is on.
   - OUTDATED -> listed under `quarantine` in the run report. Nothing is moved,
     flagged, or deleted (archive-not-delete); acting on the list is the
     operator's call.
   - NEEDS_HUMAN -> listed under `human_review` in the run report.

## How it runs

- **On demand / scheduled**: Brain task `kind=reverify` (`brain/executors._reverify_scan`),
  scheduled weekly (Sunday 04:20 UTC, right after the decay report).
- **Safety gates**:
  - `ATLAS_REVERIFY_RESET` (default `off`) - the clock-reset is the only memory mutation in
    the loop; with the gate off, the weekly pass *assesses and reports* but never edits a
    memory file. Turn it on once the verdicts are trusted.
  - `ATLAS_REVERIFY_LIMIT` (default `10`) - caps model calls per run.
- **Per-file control**: `decays: false` or `type: constraint` exempts a memory; `half_life: N`
  sets its window; an optional `confidence:` frontmatter sets the base `c0`.

## Why this is a differentiator

- vs **vector stores** (Mem0, txtai, most RAG memory): they rank by semantic similarity with
  at best a recency tiebreak; nothing erodes or re-checks a stored fact.
- vs **bi-temporal graphs** (Graphiti, Zep): they invalidate on *contradiction* and keep
  history - excellent, but reactive. They do not treat un-reconfirmed age as a first-class
  signal, and they do not autonomously re-verify.
- Atlas adds the **active** layer on top: time-eroded confidence -> epistemic state ->
  prescribed action -> a scheduled Brain loop that re-verifies and files every verdict
  in a report (resetting decay clocks itself only behind its own opt-in gate).

The honest framing: invalidate-not-delete (the temporal core) is the table-stakes-plus that
several systems have; **decay-as-behavior is the part that is rare**. Lead with it.

## What is next (not yet built)

- Human-confirm surface: NEEDS_HUMAN verdicts -> Telegram yes/no that calls `reset_clock` or
  archives on reply.
- Temporal-graph integration: apply `effective_confidence` as a retrieval gate (EXPIRED facts
  flagged "do not assert") in addition to the file-level loop.
- Web panel: show the decay queue grouped by action, with per-fact verdicts.
- Quarantine store: a concrete destination + provenance for OUTDATED facts (mirrors the
  Inspiration Engine quarantine).
