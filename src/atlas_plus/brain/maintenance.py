"""Maintenance-lane health policy for Helm's "last maintenance run" surface.

The Brain scheduler records a heartbeat per job in scheduler_runs (see
atlas_plus.brain.tasks.record_job_run / list_job_runs). This module turns that
raw ledger into an operator-facing health summary: which nightly/weekly
maintenance lanes have run recently, which are stale, and which errored.

Pure functions only -- no DB or HTTP -- so the policy is unit-testable and both
the Helm status-bar rollup and the detailed dashboard panel share one source of
truth.

The job set mirrors start_scheduler() in scheduler.py; the drift-guard test in
test_scheduler_heartbeat.py fails if they diverge. Only the memory/knowledge
maintenance lanes are tracked here -- the high-frequency operational jobs
(ticket-dispatch, sla-check, zombie-cleanup) are intentionally excluded from the
"maintenance" surface because they say nothing about whether memory upkeep ran.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

# Staleness thresholds (the grace period before a lane is flagged "stale"):
#   daily  -- ~1.5 days, so one missed night does not trip the alarm.
#   weekly -- ~8 days, so one missed weekly run does.
_DAILY_GRACE_HOURS = 36
_WEEKLY_GRACE_HOURS = 192

# job_id -> label/cadence/threshold. Order here is the display order (daily
# lanes first, then weekly). Keep this in sync with start_scheduler(); the
# drift-guard test enforces it.
MAINTENANCE_JOBS: list[dict] = [
    {"job_id": "db-backup",          "label": "DB backup",            "cadence": "daily",  "max_age_hours": _DAILY_GRACE_HOURS},
    {"job_id": "temporal-build",     "label": "Temporal graph build", "cadence": "daily",  "max_age_hours": _DAILY_GRACE_HOURS},
    {"job_id": "suggest-facts",      "label": "Fact suggestions",     "cadence": "daily",  "max_age_hours": _DAILY_GRACE_HOURS},
    {"job_id": "curation",           "label": "Curation report",      "cadence": "daily",  "max_age_hours": _DAILY_GRACE_HOURS},
    {"job_id": "research-batch",     "label": "Research batch",       "cadence": "daily",  "max_age_hours": _DAILY_GRACE_HOURS},
    {"job_id": "patterns-rebuild",   "label": "Patterns rebuild",     "cadence": "daily",  "max_age_hours": _DAILY_GRACE_HOURS},
    {"job_id": "embed-index",        "label": "Semantic embed index", "cadence": "daily",  "max_age_hours": _DAILY_GRACE_HOURS},
    {"job_id": "memory-trends",      "label": "Memory trends",        "cadence": "daily",  "max_age_hours": _DAILY_GRACE_HOURS},
    {"job_id": "research-quality",   "label": "Research quality",     "cadence": "daily",  "max_age_hours": _DAILY_GRACE_HOURS},
    {"job_id": "suggest-tags",       "label": "Tag suggestions",      "cadence": "daily",  "max_age_hours": _DAILY_GRACE_HOURS},
    {"job_id": "auto-session-log",   "label": "Auto session log",     "cadence": "daily",  "max_age_hours": _DAILY_GRACE_HOURS},
    {"job_id": "dream",              "label": "Nightly dream",        "cadence": "daily",  "max_age_hours": _DAILY_GRACE_HOURS},
    {"job_id": "gaps-report",        "label": "Memory gaps",          "cadence": "weekly", "max_age_hours": _WEEKLY_GRACE_HOURS},
    {"job_id": "reflexion-loop",     "label": "Reflexion loop",       "cadence": "weekly", "max_age_hours": _WEEKLY_GRACE_HOURS},
    {"job_id": "convergence-report", "label": "Convergence scan",     "cadence": "weekly", "max_age_hours": _WEEKLY_GRACE_HOURS},
    {"job_id": "unconfirmed-decay",  "label": "Unconfirmed decay",    "cadence": "weekly", "max_age_hours": _WEEKLY_GRACE_HOURS},
    {"job_id": "decay-report",       "label": "Decay report",         "cadence": "weekly", "max_age_hours": _WEEKLY_GRACE_HOURS},
    {"job_id": "memory-agent",       "label": "Memory agent scan",    "cadence": "weekly", "max_age_hours": _WEEKLY_GRACE_HOURS},
    {"job_id": "narrative-weekly",   "label": "Narrative weekly",     "cadence": "weekly", "max_age_hours": _WEEKLY_GRACE_HOURS},
    {"job_id": "reverify",           "label": "Re-verify memories",   "cadence": "weekly", "max_age_hours": _WEEKLY_GRACE_HOURS},
    {"job_id": "claude-supervisor",  "label": "Claude supervisor",    "cadence": "weekly", "max_age_hours": _WEEKLY_GRACE_HOURS},
    {"job_id": "weekly-digest",      "label": "Weekly digest push",   "cadence": "weekly", "max_age_hours": _WEEKLY_GRACE_HOURS},
]

# Operational lanes deliberately NOT surfaced as "maintenance" -- they fire every
# few minutes and tell you nothing about whether memory upkeep happened. Named
# here so the drift-guard test can assert they are excluded on purpose.
OPERATIONAL_JOBS = frozenset({"ticket-dispatch", "sla-check", "zombie-cleanup"})


def maintenance_job_ids() -> set:
    """The job_ids this surface tracks -- used by the drift-guard test."""
    return {j["job_id"] for j in MAINTENANCE_JOBS}


def _humanize_age(hours: Optional[float]) -> str:
    if hours is None:
        return "never"
    if hours < 1:
        return f"{max(1, int(hours * 60))}m ago"
    if hours < 48:
        return f"{int(round(hours))}h ago"
    return f"{int(round(hours / 24))}d ago"


def _parse_ts(ts) -> Optional[datetime]:
    try:
        dt = datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def classify_job(job: dict, run: Optional[dict], now: datetime) -> dict:
    """Return the health row for one maintenance job given its heartbeat (or None).

    status is one of: "ok", "stale", "error", "unknown" (never recorded a run).
    """
    row = {
        "job_id": job["job_id"],
        "label": job["label"],
        "cadence": job["cadence"],
        "last_run_at": None,
        "age_hours": None,
        "age_label": "never",
        "outcome": None,
        "error": None,
        "run_count": 0,
        "status": "unknown",
    }
    ts = _parse_ts(run.get("last_run_at")) if run else None
    if ts is None:
        return row
    age_hours = max(0.0, (now - ts).total_seconds() / 3600.0)
    row.update(
        last_run_at=run.get("last_run_at"),
        age_hours=age_hours,
        age_label=_humanize_age(age_hours),
        outcome=run.get("outcome"),
        error=run.get("error"),
        run_count=run.get("run_count", 0),
    )
    if run.get("outcome") == "error":
        row["status"] = "error"
    elif age_hours > job["max_age_hours"]:
        row["status"] = "stale"
    else:
        row["status"] = "ok"
    return row


def summarize_maintenance(runs, now: Optional[datetime] = None) -> dict:
    """Summarize maintenance-lane health from the scheduler_runs ledger.

    runs: iterable of heartbeat dicts (job_id, last_run_at, outcome, error,
          run_count) as returned by tasks.list_job_runs().
    now:  reference time (UTC); defaults to the current time. A naive datetime
          is treated as UTC.

    Returns {overall, counts, jobs}: overall is the worst status present
    (error > stale > unknown > ok); counts tallies each status; jobs is the
    per-lane breakdown ordered as MAINTENANCE_JOBS.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    by_id = {r.get("job_id"): r for r in (runs or [])}
    rows = [classify_job(job, by_id.get(job["job_id"]), now) for job in MAINTENANCE_JOBS]

    counts = {"ok": 0, "stale": 0, "error": 0, "unknown": 0}
    for row in rows:
        counts[row["status"]] = counts.get(row["status"], 0) + 1

    if counts["error"]:
        overall = "error"
    elif counts["stale"]:
        overall = "stale"
    elif counts["ok"]:
        overall = "ok"
    else:
        overall = "unknown"

    return {"overall": overall, "counts": counts, "jobs": rows}
