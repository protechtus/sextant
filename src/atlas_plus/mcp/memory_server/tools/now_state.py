"""Temporal sensor: current wall-clock state across timezones.

This is the Atlas side of the clock-tui "perception" sensor. It is a
deliberately minimal, stdlib-only mirror of clock-tui's
``clock_tui.data.zones`` engine (see
``research/atlas-perception-layer.md`` Section 6 for why the math is
duplicated rather than imported: the SessionStart hook must stay
dependency-light and must never import a TUI package).

Two consumers:

- :func:`now_state` backs the ``now_state_tool`` MCP tool, letting the
  model ask for the current time in arbitrary zones on demand.
- :func:`world_clock_line` produces the compact one-line string the
  SessionStart hook injects into every session's context.

Both never raise: an unresolvable zone (missing tzdata, typo) becomes a
per-zone ``error`` entry, and total failure degrades to a bare UTC
reading so the session is never blocked.
"""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

#: Default world set for the on-demand MCP tool.
DEFAULT_ZONES: tuple[tuple[str, str], ...] = (
    ("UTC", "UTC"),
    ("America/Los_Angeles", "Los Angeles"),
    ("America/Denver", "Denver"),
    ("America/New_York", "New York"),
    ("Europe/London", "London"),
    ("Asia/Tokyo", "Tokyo"),
    ("Australia/Sydney", "Sydney"),
)

#: Compact set for the always-on SessionStart line (token budget is tight).
#: Local-ish anchor + UTC + the hub a support engineer most often
#: coordinates against.
HOOK_ZONES: tuple[tuple[str, str], ...] = (
    ("local", "local"),
    ("UTC", "UTC"),
    ("America/New_York", "New York"),
    ("Asia/Tokyo", "Tokyo"),
)


def _label_for(key: str) -> str:
    return key.split("/")[-1].replace("_", " ")


def _day_part(hour: int) -> str:
    if 5 <= hour <= 11:
        return "morning"
    if 12 <= hour <= 16:
        return "afternoon"
    if 17 <= hour <= 20:
        return "evening"
    return "night"


def home_zone() -> tuple[str, str]:
    """The lead zone of the hook line: ATLAS_HOME_TZ (an IANA key) when set and valid, else the
    machine's own local zone. It used to be hard-coded to the author's city, which every fresh
    install then printed as its anchor (sweep 2026-11-11, manual persona)."""
    import os

    key = os.environ.get("ATLAS_HOME_TZ", "").strip()
    if key:
        try:
            ZoneInfo(key)
            return key, _label_for(key)
        except Exception:  # noqa: BLE001  (a directory-like key raised PermissionError on Windows)
            pass
    return "local", "local"


def _zone_snapshot(key: str, label: str, now_utc: datetime) -> dict:
    if key == "local":
        key, label = home_zone()
    if key == "local":
        tz = datetime.now().astimezone().tzinfo
        local = now_utc.astimezone(tz)
        label = local.tzname() or "local"
    else:
        try:
            tz = ZoneInfo(key)
        except Exception:  # noqa: BLE001
            return {"key": key, "label": label, "error": "unknown zone"}
        local = now_utc.astimezone(tz)
    off = local.utcoffset()
    offset_hours = round(off.total_seconds() / 3600.0, 2) if off else 0.0
    return {
        "key": key,
        "label": label,
        "time": local.isoformat(timespec="seconds"),
        "hh_mm": local.strftime("%H:%M"),
        "weekday": local.strftime("%a"),
        "iso_date": local.date().isoformat(),
        "abbrev": local.tzname() or "",
        "utc_offset_hours": offset_hours,
        "is_dst": bool(local.dst()),
        "day_part": _day_part(local.hour),
        "day_delta": (local.date() - now_utc.date()).days,
    }


def now_state(zones: list[str] | None = None) -> dict:
    """Return a JSON-serializable now-state snapshot across ``zones``.

    Args:
        zones: IANA zone keys (e.g. ``["UTC", "Asia/Tokyo"]``). If
               omitted, a standard world set is used.

    Returns ``{"as_of_utc": iso, "zones": [{...}, ...]}``. Carries
    ``as_of_utc`` so the caller knows the snapshot's freshness.
    """
    now_utc = datetime.now(timezone.utc)
    if zones is None:
        pairs = list(DEFAULT_ZONES)
    else:
        pairs = [(k, _label_for(k)) for k in zones]
    return {
        "as_of_utc": now_utc.isoformat(timespec="seconds"),
        "zones": [_zone_snapshot(k, label, now_utc) for k, label in pairs],
    }


def world_clock_line(sep: str = "  |  ") -> str:
    """One-line world clock for the session hooks (SessionStart + per-turn).

    The lead zone (the machine's local zone, or ATLAS_HOME_TZ) gets the full
    treatment so the line reads as context, not a bare number:

        Now: Tue 2026-05-26 12:13 Denver MDT (afternoon)  |  UTC 18:13  |
        New York 14:13 EDT  |  Tokyo 03:13 JST (+1d)

    Trailing zones show label + time + abbrev with a (+1d)/(-1d) marker
    where they sit on a different calendar day than UTC. Falls back to a
    bare UTC reading if the tz database is unavailable, and never raises.
    """
    try:
        now_utc = datetime.now(timezone.utc)
        snaps = [_zone_snapshot(k, label, now_utc) for k, label in HOOK_ZONES]
        snaps = [s for s in snaps if not s.get("error")]
        if snaps:
            parts: list[str] = []
            for i, s in enumerate(snaps):
                abbrev = f" {s['abbrev']}" if s["abbrev"] and s["key"] != "UTC" and s["abbrev"] != s["label"] else ""
                if i == 0:
                    parts.append(
                        f"{s['weekday']} {s['iso_date']} {s['hh_mm']} "
                        f"{s['label']}{abbrev} ({s['day_part']})"
                    )
                else:
                    delta = s["day_delta"]
                    tag = "" if delta == 0 else (f" (+{delta}d)" if delta > 0 else f" ({delta}d)")
                    parts.append(f"{s['label']} {s['hh_mm']}{abbrev}{tag}")
            return "Now: " + sep.join(parts)
    except Exception:
        pass
    # Last-resort fallback: UTC only, no tz database needed.
    return f"Now: UTC {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')}"


__all__ = ["now_state", "world_clock_line", "home_zone", "DEFAULT_ZONES", "HOOK_ZONES"]
