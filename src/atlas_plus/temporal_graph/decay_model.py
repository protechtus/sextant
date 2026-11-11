"""Epistemic decay model: confidence as a value that ERODES with time and ACTS.

Most memory systems treat a stored fact's confidence as a constant and, at most,
down-rank stale results. Atlas treats confidence as *epistemic*: it decays as a
fact goes unverified, and that decayed value drives BEHAVIOR - how a fact is
classified (fresh -> aging -> stale -> expired), whether retrieval will still
assert it, and whether the system should re-verify or quarantine it.

This module is pure and deterministic (no I/O; the caller injects `now` or an
already-computed age), so the policy is identical and fully unit-testable
wherever it is applied: the temporal-graph ranker, the file-level memory decay
scan, and the Brain's autonomous re-verification loop.

Model
-----
A fact has a base confidence c0 in [0, 1] and a half-life H (days) - the period
over which, absent re-verification, belief in it halves. Given an age a (days
since last_verified_at), the effective confidence is

    c(a) = c0 * 2 ** (-a / H)

The age/half-life ratio r = a / H classifies the fact's epistemic state and the
action the system should take:

    r < 1      FRESH     c > 0.50*c0   RETAIN      trust as-is
    1 <= r < 2 AGING     c ~ .25-.50   SURFACE     flag for awareness, no action yet
    2 <= r < 3 STALE     c ~ .12-.25   REVERIFY    enqueue autonomous re-verification
    r >= 3     EXPIRED   c < 0.12*c0   QUARANTINE  do not assert; re-verify or archive

The thresholds are deliberately tied to the half-life (not absolute day counts)
so a fast-moving fact (H=7) and a slow one (H=365) are judged on the same
epistemic scale.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Optional


# Ratio (age / half_life) boundaries between epistemic states.
AGING_RATIO = 1.0
STALE_RATIO = 2.0
EXPIRED_RATIO = 3.0


class DecayState(str, Enum):
    """Epistemic state of a fact as a function of how far past its half-life it is."""

    FRESH = "fresh"
    AGING = "aging"
    STALE = "stale"
    EXPIRED = "expired"


class DecayAction(str, Enum):
    """What the system should DO with a fact in a given decay state."""

    RETAIN = "retain"          # trust and use as-is
    SURFACE = "surface"        # show with a staleness note; no autonomous action
    REVERIFY = "reverify"      # enqueue an autonomous re-verification task
    QUARANTINE = "quarantine"  # do not assert; re-verify urgently or archive


_STATE_ACTION = {
    DecayState.FRESH: DecayAction.RETAIN,
    DecayState.AGING: DecayAction.SURFACE,
    DecayState.STALE: DecayAction.REVERIFY,
    DecayState.EXPIRED: DecayAction.QUARANTINE,
}


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def decay_ratio(age_days: float, half_life_days: float) -> float:
    """age / half_life, with age floored at 0 (future/skewed timestamps are 'now')
    and half_life floored at a tiny epsilon so a degenerate H<=0 reads as fully decayed."""
    age = max(0.0, age_days)
    half_life = max(1e-9, half_life_days)
    return age / half_life


def decay_state(age_days: float, half_life_days: float) -> DecayState:
    """Classify a fact's epistemic state from its age / half-life ratio."""
    r = decay_ratio(age_days, half_life_days)
    if r < AGING_RATIO:
        return DecayState.FRESH
    if r < STALE_RATIO:
        return DecayState.AGING
    if r < EXPIRED_RATIO:
        return DecayState.STALE
    return DecayState.EXPIRED


def effective_confidence(base_confidence: float, age_days: float, half_life_days: float) -> float:
    """Confidence eroded by epistemic decay: c0 * 2**(-age/half_life), in [0, c0].

    age is floored at 0; half_life at a tiny epsilon. base_confidence is clamped to [0, 1]."""
    c0 = _clamp(base_confidence, 0.0, 1.0)
    age = max(0.0, age_days)
    half_life = max(1e-9, half_life_days)
    return c0 * math.pow(2.0, -age / half_life)


def decay_action(state: DecayState) -> DecayAction:
    """Map an epistemic state to the action the system should take."""
    return _STATE_ACTION[state]


@dataclass
class DecayAssessment:
    """A fact's full decay verdict: state, prescribed action, eroded confidence, and why."""

    state: DecayState
    action: DecayAction
    effective_confidence: float
    base_confidence: float
    age_days: float
    half_life_days: float
    ratio: float
    reason: str

    @property
    def should_reverify(self) -> bool:
        return self.action in (DecayAction.REVERIFY, DecayAction.QUARANTINE)

    def as_dict(self) -> dict:
        return {
            "state": self.state.value,
            "action": self.action.value,
            "effective_confidence": round(self.effective_confidence, 4),
            "base_confidence": round(self.base_confidence, 4),
            "age_days": round(self.age_days, 2),
            "half_life_days": self.half_life_days,
            "ratio": round(self.ratio, 3),
            "reason": self.reason,
        }


def assess(base_confidence: float, age_days: float, half_life_days: float) -> DecayAssessment:
    """Turn (base confidence, age, half-life) into a behavioral decay verdict."""
    state = decay_state(age_days, half_life_days)
    action = decay_action(state)
    eff = effective_confidence(base_confidence, age_days, half_life_days)
    r = decay_ratio(age_days, half_life_days)
    reason = (
        f"{int(round(age_days))}d old vs {half_life_days}d half-life (r={r:.2f}); "
        f"confidence {_clamp(base_confidence, 0.0, 1.0):.2f} -> {eff:.2f}"
    )
    return DecayAssessment(
        state=state,
        action=action,
        effective_confidence=eff,
        base_confidence=_clamp(base_confidence, 0.0, 1.0),
        age_days=max(0.0, age_days),
        half_life_days=half_life_days,
        ratio=r,
        reason=reason,
    )


def _parse_iso(ts: str) -> Optional[datetime]:
    ts = ts.strip()
    if not ts:
        return None
    if ts.endswith("Z"):
        ts = ts[:-1] + "+00:00"
    elif len(ts) == 10:
        ts = ts + "T00:00:00+00:00"
    try:
        dt = datetime.fromisoformat(ts)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def age_days_since(timestamp_iso: str, now: Optional[datetime] = None) -> Optional[float]:
    """Days between an ISO 8601 timestamp (date or datetime) and `now` (default: utcnow).
    Returns None if the timestamp cannot be parsed."""
    dt = _parse_iso(timestamp_iso)
    if dt is None:
        return None
    ref = now or datetime.now(timezone.utc)
    if ref.tzinfo is None:
        ref = ref.replace(tzinfo=timezone.utc)
    return (ref - dt).total_seconds() / 86400.0


def assess_timestamp(base_confidence: float, timestamp_iso: str, half_life_days: float,
                     now: Optional[datetime] = None) -> DecayAssessment:
    """Convenience: assess directly from a last-verified ISO timestamp.
    An unparseable/empty timestamp is treated as maximally stale (EXPIRED)."""
    age = age_days_since(timestamp_iso, now=now)
    if age is None:
        age = half_life_days * (EXPIRED_RATIO + 1.0)  # no provenance -> treat as expired
    return assess(base_confidence, age, half_life_days)
