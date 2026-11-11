"""Token-budget preflight: can this agent finish a task before the rate-limit window bites?

A task killed mid-flight by the 5h rolling window is the worst failure mode -- wasted tokens plus
half-done, often corrupt work. This module answers, BEFORE the work starts: given an estimate of a
task's gross token cost, is there enough headroom in the current 5h window to finish it (with a
safety margin), and if not, how long until the window rolls off enough -- or should the task be
scoped down / deferred / split.

The estimate is the agent's (rough) gross-token guess; SIZE_PRESETS give ballparks when no estimate
is available. The empirical ceiling is a historical-max proxy for the real cap, so the verdict is
deliberately conservative: when in doubt it says do not start.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from .meter import (UsageEvent, WINDOW_HOURS, load_events, max_rolling_window,
                    minutes_until_freed, window_sum)

# Rough GROSS-token ballparks for a task when the agent cannot estimate precisely.
# Recalibrate from real usage_meter deltas; a fresh-agent "report" was ~220k gross.
SIZE_PRESETS = {
    "small": 100_000,     # a focused edit + tests
    "medium": 400_000,    # a multi-file feature with tests + docs
    "large": 1_200_000,   # a subsystem / several features / a review sweep
    "xl": 3_000_000,      # a migration, audit, or large fan-out
}

DEFAULT_MARGIN = 0.25     # require 25% headroom over the estimate before saying "proceed"
DEFAULT_CEILING_DAYS = 14


@dataclass
class BudgetSnapshot:
    """A point-in-time read of the rate-limit window."""

    used_5h: int
    ceiling: int            # empirical max 5h-rolling gross spend
    headroom: int           # max(0, ceiling - used_5h)
    burn_1h: int            # last hour gross (instantaneous burn)
    output_5h: int
    events: list[UsageEvent] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "used_5h": self.used_5h,
            "empirical_5h_ceiling": self.ceiling,
            "headroom": self.headroom,
            "burn_last_1h": self.burn_1h,
            "output_5h": self.output_5h,
        }


@dataclass
class BudgetVerdict:
    """The preflight decision for a specific estimated task cost."""

    status: str                 # "proceed" | "tight" | "insufficient"
    estimated_tokens: int
    required: int               # estimated * (1 + margin)
    headroom: int
    deficit: int                # max(0, required - headroom)
    margin: float
    minutes_until_enough: Optional[float]   # wait time for the window to free the deficit
    recommendation: str
    snapshot: BudgetSnapshot

    @property
    def ok(self) -> bool:
        return self.status == "proceed"

    def as_dict(self) -> dict:
        return {
            "status": self.status,
            "ok": self.ok,
            "estimated_tokens": self.estimated_tokens,
            "required_with_margin": self.required,
            "headroom": self.headroom,
            "deficit": self.deficit,
            "margin": self.margin,
            "minutes_until_enough": (round(self.minutes_until_enough, 1)
                                     if self.minutes_until_enough is not None else None),
            "recommendation": self.recommendation,
            "snapshot": self.snapshot.as_dict(),
        }


def snapshot(*, events: Optional[list[UsageEvent]] = None, ceiling_days: int = DEFAULT_CEILING_DAYS,
             window_budget: int = 0, now: Optional[datetime] = None) -> BudgetSnapshot:
    """Read current rate-limit-window usage. Pass `events` to avoid disk (tests); otherwise loads
    transcripts. `window_budget` overrides the empirical ceiling when the real cap is known."""
    if events is None:
        events = load_events(max(ceiling_days * 24, 168), now=now)
    used_5h = window_sum(events, WINDOW_HOURS, now=now)
    ceiling = window_budget or max_rolling_window(events, WINDOW_HOURS)
    return BudgetSnapshot(
        used_5h=used_5h,
        ceiling=ceiling,
        headroom=max(0, ceiling - used_5h),
        burn_1h=window_sum(events, 1, now=now),
        output_5h=window_sum(events, WINDOW_HOURS, "output", now=now),
        events=events,
    )


def _recommend(status: str, deficit: int, minutes: Optional[float], estimated: int) -> str:
    from .meter import human
    if status == "proceed":
        return "Proceed: enough 5h headroom to finish with margin."
    if status == "tight":
        return ("Proceed with caution: headroom barely covers the estimate. Checkpoint often "
                "(commit/persist every increment) and be ready to stop cleanly; consider trimming scope.")
    # insufficient
    if minutes is None:
        return (f"Do NOT start: short by ~{human(deficit)} gross and the current 5h window cannot "
                f"free that by waiting. Split the task, scope it down, or run it across multiple "
                f"5h windows with durable checkpoints between.")
    if minutes <= 1:
        return (f"Do NOT start yet: short by ~{human(deficit)}; the window frees it in under a "
                f"minute -- wait briefly, then re-check.")
    return (f"Do NOT start now: short by ~{human(deficit)} gross. Wait ~{minutes:.0f} min for the "
            f"5h window to roll off enough, or split/scope-down the task. Starting now risks a "
            f"mid-task kill and corrupt half-done work.")


def preflight(estimated_tokens: int, *, margin: float = DEFAULT_MARGIN,
              snap: Optional[BudgetSnapshot] = None, events: Optional[list[UsageEvent]] = None,
              ceiling_days: int = DEFAULT_CEILING_DAYS, window_budget: int = 0,
              now: Optional[datetime] = None) -> BudgetVerdict:
    """Decide whether to start a task estimated at `estimated_tokens` gross.

    proceed       headroom >= estimate * (1 + margin)
    tight         estimate <= headroom < estimate * (1 + margin)
    insufficient  headroom < estimate
    """
    if snap is None:
        snap = snapshot(events=events, ceiling_days=ceiling_days, window_budget=window_budget, now=now)
    estimated = max(0, int(estimated_tokens))
    required = int(estimated * (1 + margin))
    headroom = snap.headroom
    if headroom >= required:
        status = "proceed"
    elif headroom >= estimated:
        status = "tight"
    else:
        status = "insufficient"
    deficit = max(0, required - headroom)
    minutes = minutes_until_freed(snap.events, deficit, WINDOW_HOURS, now=now) if deficit else 0.0
    rec = _recommend(status, deficit, minutes, estimated)
    return BudgetVerdict(
        status=status, estimated_tokens=estimated, required=required, headroom=headroom,
        deficit=deficit, margin=margin, minutes_until_enough=minutes, recommendation=rec, snapshot=snap,
    )


def preflight_size(size: str, *, margin: float = DEFAULT_MARGIN, **kw) -> BudgetVerdict:
    """Preflight by named size when no explicit token estimate is available."""
    key = (size or "").strip().lower()
    if key not in SIZE_PRESETS:
        raise ValueError(f"unknown size {size!r}; choose one of {sorted(SIZE_PRESETS)}")
    return preflight(SIZE_PRESETS[key], margin=margin, **kw)
