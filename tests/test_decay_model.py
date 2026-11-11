"""Tests for the pure epistemic-decay model (atlas_plus.temporal_graph.decay_model).

Deterministic: no clock reads except where an explicit `now` is injected."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from atlas_plus.temporal_graph.decay_model import (
    DecayAction, DecayState, age_days_since, assess, assess_timestamp,
    decay_action, decay_ratio, decay_state, effective_confidence,
)


# --- decay_ratio ----------------------------------------------------------

def test_decay_ratio_basic():
    assert decay_ratio(0, 30) == 0.0
    assert decay_ratio(30, 30) == 1.0
    assert decay_ratio(60, 30) == 2.0


def test_decay_ratio_floors_negative_age_to_zero():
    # a future/clock-skewed timestamp reads as "now", not negative decay
    assert decay_ratio(-10, 30) == 0.0


def test_decay_ratio_degenerate_half_life_reads_as_fully_decayed():
    # H <= 0 must not divide-by-zero; any positive age -> enormous ratio
    assert decay_ratio(5, 0) > 1e6
    assert decay_ratio(0, 0) == 0.0


# --- decay_state boundaries ----------------------------------------------

@pytest.mark.parametrize("ratio,expected", [
    (0.0, DecayState.FRESH),
    (0.99, DecayState.FRESH),
    (1.0, DecayState.AGING),     # boundary is inclusive on the upper state
    (1.99, DecayState.AGING),
    (2.0, DecayState.STALE),
    (2.99, DecayState.STALE),
    (3.0, DecayState.EXPIRED),
    (10.0, DecayState.EXPIRED),
])
def test_decay_state_boundaries(ratio, expected):
    # half_life 100 so age == ratio*100
    assert decay_state(ratio * 100, 100) == expected


# --- effective_confidence (the erosion curve) ----------------------------

def test_effective_confidence_halves_each_half_life():
    assert effective_confidence(1.0, 0, 30) == pytest.approx(1.0)
    assert effective_confidence(1.0, 30, 30) == pytest.approx(0.5)
    assert effective_confidence(1.0, 60, 30) == pytest.approx(0.25)
    assert effective_confidence(1.0, 90, 30) == pytest.approx(0.125)


def test_effective_confidence_scales_with_base():
    assert effective_confidence(0.8, 30, 30) == pytest.approx(0.4)
    assert effective_confidence(0.5, 60, 30) == pytest.approx(0.125)


def test_effective_confidence_clamps_base_to_unit_interval():
    assert effective_confidence(5.0, 0, 30) == pytest.approx(1.0)
    assert effective_confidence(-1.0, 0, 30) == pytest.approx(0.0)


def test_effective_confidence_never_exceeds_base_and_floors_at_zero():
    assert effective_confidence(0.9, -5, 30) == pytest.approx(0.9)  # negative age floored
    assert effective_confidence(1.0, 10_000, 30) == pytest.approx(0.0, abs=1e-9)


# --- decay_action mapping -------------------------------------------------

def test_decay_action_mapping():
    assert decay_action(DecayState.FRESH) == DecayAction.RETAIN
    assert decay_action(DecayState.AGING) == DecayAction.SURFACE
    assert decay_action(DecayState.STALE) == DecayAction.REVERIFY
    assert decay_action(DecayState.EXPIRED) == DecayAction.QUARANTINE


# --- assess (integration) -------------------------------------------------

def test_assess_fresh_fact_retained():
    a = assess(0.9, age_days=5, half_life_days=30)
    assert a.state == DecayState.FRESH
    assert a.action == DecayAction.RETAIN
    assert not a.should_reverify
    assert a.effective_confidence == pytest.approx(0.9 * 2 ** (-5 / 30))


def test_assess_stale_fact_triggers_reverify():
    a = assess(1.0, age_days=75, half_life_days=30)  # r = 2.5 -> STALE
    assert a.state == DecayState.STALE
    assert a.action == DecayAction.REVERIFY
    assert a.should_reverify


def test_assess_expired_fact_quarantined():
    a = assess(1.0, age_days=120, half_life_days=30)  # r = 4 -> EXPIRED
    assert a.state == DecayState.EXPIRED
    assert a.action == DecayAction.QUARANTINE
    assert a.should_reverify
    assert a.effective_confidence < 0.07


def test_assess_as_dict_is_serialisable():
    d = assess(0.8, 45, 30).as_dict()
    assert d["state"] == "aging"
    assert d["action"] == "surface"
    assert set(d) >= {"state", "action", "effective_confidence", "ratio", "reason"}


# --- timestamp helpers ----------------------------------------------------

def test_age_days_since_parses_date_and_datetime():
    now = datetime(2026, 6, 5, tzinfo=timezone.utc)
    assert age_days_since("2026-05-06", now=now) == pytest.approx(30.0, abs=0.01)
    assert age_days_since("2026-06-04T00:00:00+00:00", now=now) == pytest.approx(1.0, abs=0.01)
    assert age_days_since("2026-06-04T00:00:00Z", now=now) == pytest.approx(1.0, abs=0.01)


def test_age_days_since_unparseable_returns_none():
    assert age_days_since("not-a-date") is None
    assert age_days_since("") is None


def test_assess_timestamp_treats_missing_provenance_as_expired():
    a = assess_timestamp(1.0, "garbage", half_life_days=30)
    assert a.state == DecayState.EXPIRED
    assert a.action == DecayAction.QUARANTINE


def test_assess_timestamp_fresh_when_recent():
    now = datetime(2026, 6, 5, tzinfo=timezone.utc)
    a = assess_timestamp(1.0, "2026-06-01", half_life_days=30, now=now)  # 4 days
    assert a.state == DecayState.FRESH
