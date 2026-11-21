"""Direction A -- Reflexion Loop: self-critique on recent facts.

Inspired by Shinn et al. (2023) "Reflexion: Language Agents with Verbal
Reinforcement Learning." The loop reviews facts written to .memory/ in the
last N days and asks an LLM: "Is this still accurate? Does it conflict with
anything? What's missing?" Produces revisions, invalidations, or supplements.

Resolution rules:
  REVISE  -> update the fact text in the temporal graph (invalidate old, add new)
  SKIP    -> no action needed
  TICKET  -> file a proposal for the Helm desk (ambiguous or high-stakes); the operator's click makes the ticket

ATLAS_AUTONOMOUS gate:
  If ATLAS_AUTONOMOUS=off, all actions are downgraded to TICKET only (proposals, since ATL-073 2026-11-20:
  before that every REVISE wrote a human ticket straight into the ticket store, 35 in one night on 11-14).
  No writes to .memory/ or temporal graph are made.

Entry points:
  python -m atlas_plus.curation.reflexion_loop [--days N] [--dry-run]
  atlas-reflexion-loop       (console script after uv sync)

Report written to .atlas/reflexion-report-YYYY-MM-DD.json.
"""

from __future__ import annotations

from atlas_plus._fsutil import make_private_dir
import json
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional
from atlas_plus import paths as atlas_paths
from atlas_plus.autonomous import autonomous_enabled

STATE_DIR = atlas_paths.state_dir()
MEMORY_ROOT = atlas_paths.memory_root()

_DEFAULT_LOOKBACK_DAYS = 7
_BATCH_SIZE = 5  # facts per LLM call
# Stop early when the model is down: 3 consecutive failed batches instead of running the worker's
# 900 s clock out on an unreachable Ollama (every weekly run 11-01..11-15 ended that way; review 2026-11-20).
_MAX_CONSECUTIVE_LLM_ERRORS = 3


def _is_autonomous() -> bool:
    return autonomous_enabled()  # canonical allowlist gate (audit 2026-09-21)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")


def _date_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _cutoff_iso(days: int) -> str:
    dt = datetime.now(timezone.utc) - timedelta(days=days)
    return dt.strftime("%Y-%m-%dT%H:%M:%S+00:00")


# ---------------------------------------------------------------------------
# LLM critique prompt
# ---------------------------------------------------------------------------

_CRITIQUE_PROMPT = """\
You are auditing a personal AI memory system. Review the following facts that \
were recently written. For each fact, decide:

  REVISE  -- the fact is inaccurate, stale, or misleading; provide corrected text
  SKIP    -- the fact is fine as written; no action needed
  TICKET  -- the fact raises a question worth human review but you are uncertain

Return ONLY valid JSON, one object per fact:
[
  {{
    "fact_id": "<id>",
    "action": "REVISE" | "SKIP" | "TICKET",
    "reason": "<one sentence>",
    "revised_text": "<new fact text if action=REVISE, else empty string>"
  }}
]

FACTS TO REVIEW:
{facts_block}

JSON:"""


def _build_facts_block(facts: list[dict]) -> str:
    lines = []
    for f in facts:
        lines.append(
            f"ID: {f['fact_id']}\n"
            f"Text: {f['fact_text']}\n"
            f"Written: {f['valid_from'][:10]}\n"
            f"Authority: {f['authority']}"
        )
    return "\n\n".join(lines)


def _call_llm(prompt: str) -> str:
    from atlas_plus.summarizer.providers import complete, ProviderError
    try:
        text, _ = complete(prompt)
        return text.strip()
    except ProviderError as e:
        raise RuntimeError(f"LLM provider error: {e}") from e


def _parse_critique(text: str, facts: list[dict]) -> list[dict]:
    """Parse LLM JSON response. Returns list of {fact_id, action, reason, revised_text}."""
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        text = "\n".join(l for l in lines[1:] if not l.startswith("```")).strip()
    start = text.find("[")
    end = text.rfind("]") + 1
    if start < 0 or end <= start:
        return []
    try:
        items = json.loads(text[start:end])
    except json.JSONDecodeError:
        return []
    valid_ids = {f["fact_id"] for f in facts}
    results = []
    for item in items:
        if not isinstance(item, dict):
            continue
        fid = item.get("fact_id", "")
        action = item.get("action", "SKIP").upper()
        if fid not in valid_ids or action not in ("REVISE", "SKIP", "TICKET"):
            continue
        results.append({
            "fact_id": fid,
            "action": action,
            "reason": str(item.get("reason", "")).strip()[:300],
            "revised_text": str(item.get("revised_text", "")).strip()[:500],
        })
    return results


# ---------------------------------------------------------------------------
# Resolution actions
# ---------------------------------------------------------------------------

def _apply_revision(critique: dict, provider, now_iso: str) -> bool:
    """Invalidate the old fact and add a revised version. Returns True on success."""
    fact_id = critique["fact_id"]
    new_text = critique["revised_text"]
    if not new_text:
        return False
    if fact_id not in provider._facts:
        return False

    old_fact = provider._facts[fact_id]

    try:
        provider.invalidate_fact(
            fact_id,
            invalid_at=now_iso,
            reason=f"reflexion_revision:{critique['reason'][:80]}",
        )
    except (KeyError, ValueError):
        return False

    from atlas_plus.temporal_graph.models import TemporalFact
    new_id = f"{fact_id}:revised:{now_iso[:10]}"
    provider.add_fact(TemporalFact(
        fact_id=new_id,
        subject_id=old_fact.subject_id,
        predicate=old_fact.predicate,
        object_id=old_fact.object_id,
        fact_text=new_text,
        valid_from=now_iso,
        observed_at=now_iso,
        ingested_at=now_iso,
        last_verified_at=now_iso,
        confidence=0.85,
        authority=old_fact.authority,
        evidence_refs=[f"reflexion:{_date_str()}"],
        supersedes_fact_id=fact_id,
    ))
    return True


def _propose_review(critique: dict, fact_text: str) -> Optional[dict]:
    """File a review proposal for the desk (ATL-073). Approval creates the human ticket."""
    from atlas_plus.brain.learning import add_proposal

    short_id = critique["fact_id"][:60]
    title = f"Reflexion review: {short_id}"[:200]
    body = (
        f"## Reflexion Loop Review\n\n"
        f"**Fact ID:** `{critique['fact_id']}`\n\n"
        f"**Current text:** {fact_text}\n\n"
        f"**LLM assessment:** {critique['reason']}\n\n"
        f"## Steps\n\n"
        f"- [ ] Verify whether the fact is still accurate\n"
        f"- [ ] Update the source `.memory/` file if needed\n"
        f"- [ ] Run `atlas-temporal build --source memory` to refresh the index\n"
        f"- [ ] Close this ticket\n"
    )
    try:
        return add_proposal(
            "reflexion", "ticket", title, body,
            {"project": "atlas-memory+", "assignee": "human", "priority": "normal", "effort": "S",
             "fact_id": critique["fact_id"]},
        )
    except Exception as e:
        # Do not hide a failed operator-facing proposal -- say so in the job log.
        print(f"WARNING: reflexion proposal failed: {e}", file=sys.stderr)
        return None


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def run_reflexion_loop(
    lookback_days: int = _DEFAULT_LOOKBACK_DAYS,
    dry_run: bool = False,
    _provider=None,
) -> dict:
    """Review recently written facts via LLM critique.

    Args:
        lookback_days: How far back to look for recently written facts.
        dry_run:       If True, collect facts but skip LLM calls and modifications.
        _provider:     Inject a FileTemporalProvider instance (used in tests).

    Returns a report dict with counts and per-fact results.
    """
    autonomous = _is_autonomous()
    now_iso = _now_iso()
    cutoff = _cutoff_iso(lookback_days)

    if _provider is not None:
        provider = _provider
    else:
        from atlas_plus.temporal_graph.file_provider import FileTemporalProvider
        provider = FileTemporalProvider(graph_root=STATE_DIR / "temporal_graph")

    # Collect recently written active facts (ingested_at >= cutoff)
    recent: list[dict] = []
    for fact in provider._facts.values():
        if fact.invalid_at is not None:
            continue  # already invalidated
        if fact.ingested_at >= cutoff:
            recent.append({
                "fact_id": fact.fact_id,
                "fact_text": fact.fact_text,
                "valid_from": fact.valid_from,
                "ingested_at": fact.ingested_at,
                "authority": fact.authority,
                "confidence": fact.confidence,
            })

    report: dict = {
        "date": _date_str(),
        "lookback_days": lookback_days,
        "autonomous": autonomous,
        "dry_run": dry_run,
        "facts_reviewed": len(recent),
        "revised": 0,
        "proposals_created": 0,
        "skipped": 0,
        "errors": 0,
        "aborted": None,
        "results": [],
    }

    if not recent:
        _write_report(report)
        return report

    if dry_run:
        for f in recent:
            report["results"].append({
                "fact_id": f["fact_id"],
                "action": "DRY_RUN",
                "reason": "(dry-run -- LLM not called)",
                "applied": False,
            })
        _write_report(report)
        return report

    consecutive_errors = 0
    # Process in batches
    for batch_start in range(0, len(recent), _BATCH_SIZE):
        batch = recent[batch_start: batch_start + _BATCH_SIZE]
        facts_block = _build_facts_block(batch)
        prompt = _CRITIQUE_PROMPT.format(facts_block=facts_block)

        try:
            raw = _call_llm(prompt)
        except RuntimeError as e:
            print(f"[reflexion-loop] LLM error on batch {batch_start}: {e}", file=sys.stderr)
            report["errors"] += len(batch)
            consecutive_errors += 1
            if consecutive_errors >= _MAX_CONSECUTIVE_LLM_ERRORS:
                remaining = len(recent) - batch_start - len(batch)
                report["errors"] += remaining
                report["aborted"] = (
                    f"stopped after {consecutive_errors} consecutive LLM errors; "
                    f"{remaining} fact(s) not reviewed (provider down?)"
                )
                print(f"[reflexion-loop] {report['aborted']}", file=sys.stderr)
                break
            continue
        consecutive_errors = 0

        critiques = _parse_critique(raw, batch)

        for critique in critiques:
            action = critique["action"]
            fact_text = next(
                (f["fact_text"] for f in batch if f["fact_id"] == critique["fact_id"]),
                "",
            )
            applied = False

            if action == "SKIP":
                report["skipped"] += 1
                applied = True

            elif action == "REVISE":
                if autonomous and not dry_run and critique["revised_text"]:
                    applied = _apply_revision(critique, provider, now_iso)
                    if applied:
                        report["revised"] += 1
                else:
                    # Autonomous off or no revised text: propose a review instead
                    proposal = _propose_review(critique, fact_text)
                    if proposal:
                        report["proposals_created"] += 1
                    applied = True

            elif action == "TICKET":
                proposal = _propose_review(critique, fact_text)
                if proposal:
                    report["proposals_created"] += 1
                applied = True

            report["results"].append({
                "fact_id": critique["fact_id"],
                "action": action if autonomous else f"{action}->TICKET(autonomous=off)",
                "reason": critique["reason"],
                "applied": applied,
            })

    _write_report(report)
    return report


def _write_report(report: dict) -> None:
    path = STATE_DIR / f"reflexion-report-{_date_str()}.json"
    try:
        make_private_dir(STATE_DIR)
        path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    except OSError as e:
        print(f"[reflexion-loop] Could not write report: {e}", file=sys.stderr)


def main() -> None:
    """Entry point for the `atlas-reflexion` CLI command.

    Parses --days (lookback window) and --dry-run, then calls run_reflexion_loop().
    Prints a summary: facts reviewed, revised, tickets created, skipped. Writes
    a dated report to .atlas/reports/reflexion-YYYY-MM-DD.md via _write_report().
    ATLAS_AUTONOMOUS=off: revisions downgraded to ticket creation (no memory writes).
    """
    import argparse

    parser = argparse.ArgumentParser(
        description="Reflexion loop: LLM self-critique on recently written Atlas facts.",
    )
    parser.add_argument(
        "--days", "-d", type=int, default=_DEFAULT_LOOKBACK_DAYS,
        help=f"Lookback window in days (default: {_DEFAULT_LOOKBACK_DAYS})",
    )
    parser.add_argument(
        "--dry-run", "-n", action="store_true",
        help="Collect facts but skip LLM calls and all modifications.",
    )
    args = parser.parse_args()

    autonomous = _is_autonomous()
    print(f"[reflexion-loop] Lookback: {args.days}d | autonomous: {autonomous} | dry-run: {args.dry_run}")

    report = run_reflexion_loop(lookback_days=args.days, dry_run=args.dry_run)

    print(
        f"[reflexion-loop] Reviewed {report['facts_reviewed']} fact(s): "
        f"{report['revised']} revised, {report['proposals_created']} proposals, "
        f"{report['skipped']} skipped, {report['errors']} errors"
    )
    if not autonomous:
        print("[reflexion-loop] ATLAS_AUTONOMOUS=off -- no temporal changes made.")


if __name__ == "__main__":
    main()
