"""Nightly contradiction check and resolution.

Scans .memory/feedback/ and .memory/reference/ for contradictory facts using the
existing LLM-powered find_contradictions() tool, then applies resolution:

  HIGH confidence   -> temporal invalidation of older file's has_status fact
                       + human-review ticket (no auto-archive)
  MEDIUM confidence -> ticket only
  LOW confidence    -> ticket only

ATLAS_AUTONOMOUS gate:
  If ATLAS_AUTONOMOUS=off, produce a JSON report and print a summary, but make
  no modifications to the temporal graph or ticket store.

Writes a report to .atlas/contradiction-report-YYYY-MM-DD.json.
Designed to run as Step 3 of the nightly curation pipeline.

Entry points:
  python -m atlas_plus.curation.nightly_contradiction_check
  atlas-contradiction-check        (console script after uv sync)
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from atlas_plus.curation.contradictions import ContradictionPair, find_contradictions
from atlas_plus.mcp.memory_server.config import MEMORY_ROOT
from atlas_plus import paths as atlas_paths
from atlas_plus.autonomous import autonomous_enabled

STATE_DIR = atlas_paths.state_dir()
TICKET_PROJECT = "atlas-memory+"

# Scopes to scan. Sessions, identity, versions, archive are excluded.
_SCAN_SCOPES = ["feedback", "reference"]


def _is_autonomous() -> bool:
    return autonomous_enabled()  # canonical allowlist gate (audit 2026-09-21)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")


def _date_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _older_file(pair: ContradictionPair, provider) -> str:
    """Return the relative path of the older file in the contradiction pair.

    Uses the temporal graph entity's last_seen_at as a proxy for the file's
    last update date. Falls back to file1 if entities are not found or dates match.
    """
    eid_a = f"memory:{pair.file1}"
    eid_b = f"memory:{pair.file2}"
    ent_a = provider._entities.get(eid_a)
    ent_b = provider._entities.get(eid_b)

    if ent_a and ent_b:
        # Older = lower last_seen_at (updated less recently)
        if ent_a.last_seen_at <= ent_b.last_seen_at:
            return pair.file1
        return pair.file2

    # If only one entity found, the missing one was never indexed
    if ent_b and not ent_a:
        return pair.file1  # file1 not in graph = not recently indexed = treat as older
    return pair.file1  # default


def _invalidate_in_temporal_graph(older_key: str, pair: ContradictionPair, provider, now_iso: str) -> bool:
    """Invalidate the has_status fact for the older file and add a 'contradicted' fact.

    Returns True if invalidation was applied, False if the fact was not found.
    """
    from atlas_plus.temporal_graph.models import TemporalFact

    entity_id = f"memory:{older_key}"
    status_fact_id = f"f:{older_key}:has_status"

    if status_fact_id not in provider._facts:
        return False

    # Invalidate the existing has_status fact
    provider.invalidate_fact(
        status_fact_id,
        invalid_at=now_iso,
        reason=f"contradiction:{pair.file1}:{pair.file2}",
    )

    # Add a new has_status:contradicted fact
    ent = provider._entities.get(entity_id)
    name = ent.name if ent else older_key.split("/")[-1]
    new_fact_id = f"f:{older_key}:has_status:contradicted"

    provider.add_fact(TemporalFact(
        fact_id=new_fact_id,
        subject_id=entity_id,
        predicate="has_status",
        object_id="contradicted",
        fact_text=f"{name} status is contradicted.",
        valid_from=now_iso,
        observed_at=now_iso,
        ingested_at=now_iso,
        last_verified_at=now_iso,
        confidence=0.8,
        authority="memory",
        evidence_refs=[f"contradiction:{pair.file1}:{pair.file2}"],
    ))
    return True


def _create_ticket(pair: ContradictionPair, older_key: str, temporal_invalidated: bool) -> Optional[object]:
    """Create a human-review ticket for the contradiction."""
    from atlas_plus.tickets.store import create_ticket

    a_stem = pair.file1.split("/")[-1].replace(".md", "")
    b_stem = pair.file2.split("/")[-1].replace(".md", "")
    title = f"Contradiction ({pair.confidence}): {a_stem} vs {b_stem}"
    title = title[:200]

    action_line = (
        "Temporal graph has been flagged (has_status set to 'contradicted' for the older file)."
        if temporal_invalidated
        else "No automatic action taken -- review needed before archiving."
    )

    body = (
        f"## Contradiction Detected\n\n"
        f"**Confidence:** {pair.confidence.upper()}\n\n"
        f"**File A:** `{pair.file1}`\n"
        f"**File B:** `{pair.file2}`\n\n"
        f"**Explanation:** {pair.explanation}\n\n"
        f"**Older file (lower last_seen_at):** `{older_key}`\n\n"
        f"**Automatic action:** {action_line}\n\n"
        f"## Review Steps\n\n"
        f"- [ ] Read both files\n"
        f"- [ ] Determine which is the current truth\n"
        f"- [ ] Archive the stale one: `atlas-review-memory archive {older_key} --label contradicted`\n"
        f"- [ ] Update the surviving file if the other had useful nuance\n"
        f"- [ ] Close this ticket\n"
    )

    priority = "high" if pair.confidence == "high" else "normal"

    try:
        return create_ticket(
            title=title,
            project=TICKET_PROJECT,
            assignee="human",
            priority=priority,
            effort="S",
            body=body,
        )
    except Exception as e:
        # A swallowed failure here hides a HIGH-confidence contradiction from
        # the operator entirely -- at minimum say so in the job log.
        print(f"WARNING: contradiction ticket creation failed for {older_key}: {e}", file=sys.stderr)
        return None


def run_contradiction_check(
    memory_root: Optional[Path] = None,
    scopes: Optional[list[str]] = None,
    dry_run: bool = False,
    _provider=None,  # injected in tests to avoid hitting real .atlas/
) -> dict:
    """Run the nightly contradiction check.

    Args:
        memory_root: Override for .memory/ root (used in tests).
        scopes:      Subdirectories to scan. Defaults to feedback + reference.
        dry_run:     If True, skip LLM calls and run only keyword pre-filter.

    Returns a report dict with counts and per-pair results.
    """
    root = memory_root or MEMORY_ROOT
    autonomous = _is_autonomous()
    scopes = scopes or _SCAN_SCOPES
    now_iso = _now_iso()

    if _provider is not None:
        provider = _provider
    else:
        from atlas_plus.temporal_graph.file_provider import FileTemporalProvider
        graph_root = STATE_DIR / "temporal_graph"
        provider = FileTemporalProvider(graph_root=graph_root)

    all_pairs: list[ContradictionPair] = []
    for scope in scopes:
        try:
            pairs = find_contradictions(memory_root=root, scope=scope, dry_run=dry_run)
            all_pairs.extend(pairs)
        except Exception as e:
            print(f"[contradiction-check] Error scanning {scope}: {e}", file=sys.stderr)

    report: dict = {
        "date": _date_str(),
        "autonomous": autonomous,
        "dry_run": dry_run,
        "scopes": scopes,
        "total_pairs": len(all_pairs),
        "high": 0,
        "medium": 0,
        "low": 0,
        "temporal_invalidated": 0,
        "tickets_created": 0,
        "pairs": [],
    }

    for pair in all_pairs:
        confidence = pair.confidence.lower()
        if confidence == "high":
            report["high"] += 1
        elif confidence == "medium":
            report["medium"] += 1
        else:
            report["low"] += 1

        older_key = _older_file(pair, provider)
        invalidated = False
        ticket_id = None

        if autonomous and not dry_run:
            # HIGH: temporal invalidation + ticket
            # MEDIUM/LOW: ticket only
            if confidence == "high":
                invalidated = _invalidate_in_temporal_graph(older_key, pair, provider, now_iso)
                if invalidated:
                    report["temporal_invalidated"] += 1

            ticket = _create_ticket(pair, older_key, invalidated)
            if ticket:
                report["tickets_created"] += 1
                ticket_id = ticket.id

            # Mark both files' frontmatter with the contradiction reference
            try:
                from atlas_plus.mcp.memory_server.routing import mark_contradiction
                mark_contradiction(pair.file1, pair.file2)
            except Exception as e:
                print(
                    f"WARNING: mark_contradiction failed for {pair.file1} / {pair.file2}: {e}",
                    file=sys.stderr,
                )

        report["pairs"].append({
            "file1": pair.file1,
            "file2": pair.file2,
            "confidence": confidence,
            "explanation": pair.explanation,
            "older_file": older_key,
            "temporal_invalidated": invalidated,
            "ticket_id": ticket_id,
        })

    # Write report to .atlas/
    if not dry_run:
        report_path = STATE_DIR / f"contradiction-report-{_date_str()}.json"
        try:
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        except OSError as e:
            print(f"[contradiction-check] Could not write report: {e}", file=sys.stderr)

    return report


def main() -> None:
    """Entry point: run the nightly contradiction detection pass across all .memory/ files."""
    import argparse

    parser = argparse.ArgumentParser(
        description="Nightly contradiction check for Atlas .memory/ files.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--scope", action="append", dest="scopes",
        help="Scope to scan (can repeat). Defaults to: feedback reference",
    )
    parser.add_argument(
        "--dry-run", "-n", action="store_true",
        help="Pre-filter only -- skip LLM calls, make no modifications.",
    )
    parser.add_argument(
        "--report-only", action="store_true",
        help="Print existing report for today if available, then exit.",
    )
    args = parser.parse_args()

    if args.report_only:
        path = STATE_DIR / f"contradiction-report-{_date_str()}.json"
        if path.exists():
            print(path.read_text(encoding="utf-8"))
        else:
            print(f"No report found for today ({_date_str()}).")
        return

    scopes = args.scopes or _SCAN_SCOPES
    autonomous = _is_autonomous()

    print(f"[contradiction-check] Scanning scopes: {scopes}")
    print(f"[contradiction-check] Autonomous: {autonomous} | dry-run: {args.dry_run}")

    report = run_contradiction_check(scopes=scopes, dry_run=args.dry_run)

    print(
        f"[contradiction-check] Found {report['total_pairs']} pair(s): "
        f"{report['high']} high, {report['medium']} medium, {report['low']} low"
    )
    if report["temporal_invalidated"]:
        print(f"[contradiction-check] Temporal invalidations: {report['temporal_invalidated']}")
    if report["tickets_created"]:
        print(f"[contradiction-check] Tickets created: {report['tickets_created']}")
    if not autonomous:
        print("[contradiction-check] ATLAS_AUTONOMOUS=off -- no modifications made.")


if __name__ == "__main__":
    main()
