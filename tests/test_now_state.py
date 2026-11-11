"""Tests for the temporal sensor (now_state tool + SessionStart line).

This module mirrors clock-tui's pure engine but reads the real clock,
so assertions use time-independent invariants: fixed UTC offsets for
zones that do not observe DST (Tokyo +9, Kolkata +5:30, UTC +0), and
structural / robustness properties that hold at any instant.
"""

from __future__ import annotations

from atlas_plus.mcp.memory_server.tools.now_state import (
    HOOK_ZONES,
    now_state,
    world_clock_line,
)


def _zone(state: dict, key: str) -> dict:
    return next(z for z in state["zones"] if z["key"] == key)


def test_now_state_has_freshness_stamp_and_zones():
    state = now_state()
    assert "as_of_utc" in state
    assert isinstance(state["zones"], list) and state["zones"]


def test_utc_zone_is_zero_offset_no_dst():
    z = _zone(now_state(["UTC"]), "UTC")
    assert z["utc_offset_hours"] == 0.0
    assert z["is_dst"] is False
    assert z["abbrev"] == "UTC"
    assert z["day_delta"] == 0


def test_tokyo_fixed_offset_no_dst():
    # Japan does not observe DST: always UTC+9, JST, year-round.
    z = _zone(now_state(["Asia/Tokyo"]), "Asia/Tokyo")
    assert z["utc_offset_hours"] == 9.0
    assert z["is_dst"] is False
    assert z["abbrev"] == "JST"


def test_kolkata_half_hour_offset():
    # India: UTC+5:30, no DST.
    z = _zone(now_state(["Asia/Kolkata"]), "Asia/Kolkata")
    assert z["utc_offset_hours"] == 5.5
    assert z["is_dst"] is False


def test_unknown_zone_yields_error_not_crash():
    state = now_state(["Not/AZone", "UTC"])
    assert _zone(state, "Not/AZone")["error"] == "unknown zone"
    assert _zone(state, "UTC")["utc_offset_hours"] == 0.0


def test_bare_keys_get_derived_labels():
    z = _zone(now_state(["Asia/Tokyo"]), "Asia/Tokyo")
    assert z["label"] == "Tokyo"


def test_zone_snapshot_has_expected_fields():
    z = _zone(now_state(["America/Denver"]), "America/Denver")
    assert set(z) >= {
        "key", "label", "time", "hh_mm", "weekday", "iso_date", "abbrev",
        "utc_offset_hours", "is_dst", "day_part", "day_delta",
    }


def test_world_clock_line_nonempty_and_shaped():
    line = world_clock_line()
    assert line.startswith("Now: ")
    assert "UTC" in line
    # HOOK_ZONES has multiple entries, joined by the separator.
    assert "|" in line
    # Lead zone carries a weekday abbreviation.
    assert any(d in line for d in ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"))


def test_hook_zones_includes_utc_anchor():
    keys = [k for k, _ in HOOK_ZONES]
    assert "UTC" in keys
