"""Security-audit verdict surface for the Atlas control plane (Helm).

The weekly read-only host audit (scripts/security-audit.ps1) writes a rolling
index of its runs to ``<ATLAS_STATE_DIR>/security/index.json`` -- newest first,
each record::

    {"ts": "2026-06-12T13:00:00Z", "verdict": "CLEAN|REVIEW|ATTENTION",
     "high": 0, "medium": 0, "report": "audit-<stamp>.md", "sarif": "..."}

This module reads that index and turns the latest run into an operator-facing
health summary -- the verdict plus whether the audit actually ran recently -- so
Helm can answer "is the host clean, and is the audit still running?" at a glance
(WS1 single-pane consolidation).

Pure functions (``summarize_security`` / the render helpers) so it unit-tests
without the file or a server; only ``read_audit_index`` touches disk, and it
never raises -- a missing or malformed index reads as "no data".
"""
from __future__ import annotations

import html as _html
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from atlas_plus import paths as atlas_paths

# The audit runs weekly (Mon 06:00 UTC). Past this age the latest verdict is
# treated as STALE -- i.e. the audit itself may have stopped running -- so a
# week-old "CLEAN" does not read as reassuring. One week plus grace.
_AUDIT_MAX_AGE_HOURS = 216  # 9 days

_STATUS_CLASS = {
    "attention": "text-red-400",
    "stale": "text-yellow-400",
    "review": "text-yellow-400",
    "clean": "text-green-400",
    "unknown": "text-gray-500",
}


def _state_dir() -> Path:
    return atlas_paths.state_dir()


def audit_index_path() -> Path:
    return _state_dir() / "security" / "index.json"


def read_audit_index(path: Optional[Path] = None) -> list[dict]:
    """Read the security-audit index (newest first). Never raises.

    Returns [] when the file is missing or unreadable. Heals the legacy
    ``{value: ...}`` wrapper records the PowerShell writer also guards against,
    and keeps only real run records (those carrying a ``verdict``).
    """
    p = Path(path) if path is not None else audit_index_path()
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    for rec in raw:
        if isinstance(rec, dict) and rec.get("value") and not rec.get("verdict"):
            rec = rec["value"]
        if isinstance(rec, dict) and rec.get("verdict"):
            out.append(rec)
    return out


def _parse_ts(ts) -> Optional[datetime]:
    if not ts:
        return None
    s = str(ts).strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except (ValueError, TypeError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _humanize_age(hours: Optional[float]) -> str:
    if hours is None:
        return "never"
    if hours < 1:
        return f"{max(1, int(hours * 60))}m ago"
    if hours < 48:
        return f"{int(round(hours))}h ago"
    return f"{int(round(hours / 24))}d ago"


def summarize_security(index: list[dict], now: Optional[datetime] = None) -> dict:
    """Summarize the latest security-audit run.

    Returns {status, verdict, high, medium, report, ts, age_hours, age_label,
    stale}. status precedence: "attention" (high findings) > "stale" (audit not
    current / undatable) > "review" (medium findings) > "clean"; "unknown" when
    there is no audit data at all.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    if not index:
        return {"status": "unknown", "verdict": None, "high": 0, "medium": 0,
                "report": None, "ts": None, "age_hours": None,
                "age_label": "never", "stale": False}

    latest = index[0]
    verdict = str(latest.get("verdict", "")).upper()
    ts = _parse_ts(latest.get("ts"))
    age_hours = max(0.0, (now - ts).total_seconds() / 3600.0) if ts else None
    stale = (age_hours is not None and age_hours > _AUDIT_MAX_AGE_HOURS) or ts is None

    if verdict == "ATTENTION":
        status = "attention"
    elif stale:
        status = "stale"
    elif verdict == "REVIEW":
        status = "review"
    elif verdict == "CLEAN":
        status = "clean"
    else:
        status = "unknown"

    return {
        "status": status,
        "verdict": verdict or None,
        "high": int(latest.get("high") or 0),
        "medium": int(latest.get("medium") or 0),
        "report": latest.get("report"),
        "ts": latest.get("ts"),
        "age_hours": age_hours,
        "age_label": _humanize_age(age_hours),
        "stale": stale,
    }


def _audit_title(summary: dict) -> str:
    if summary["status"] == "unknown":
        return ("no security-audit runs found "
                "(security-audit.ps1 writes .atlas/security/index.json)")
    v = summary.get("verdict") or "?"
    note = " -- STALE, audit may not be running" if summary["stale"] else ""
    return (f"security audit {v}: high={summary['high']} medium={summary['medium']}, "
            f"last run {summary['age_label']}{note}")


def rollup_span(summary: dict) -> str:
    """A compact status-bar span: 'sec: <status>' with a count/age hint."""
    status = summary["status"]
    cls = _STATUS_CLASS.get(status, "text-gray-500")
    extra = ""
    if status == "attention":
        extra = f" ({summary['high']}h)"
    elif status == "review":
        extra = f" ({summary['medium']}m)"
    elif status == "stale":
        extra = f" ({summary['age_label']})"
    return (f'<span class="{cls}" title="{_html.escape(_audit_title(summary))}">'
            f'sec: {status}{extra}</span>')


def render_panel(summary: dict) -> str:
    """Pure: the dashboard 'Security audit' card fragment."""
    status = summary["status"]
    cls = _STATUS_CLASS.get(status, "text-gray-500")
    if status == "unknown":
        return ('<div class="text-xs text-slate-500">no audit runs found '
                '<span class="text-slate-600">(.atlas/security/index.json)</span></div>')
    verdict = _html.escape(summary.get("verdict") or "?")
    stale_note = ' <span class="text-yellow-400">(stale)</span>' if summary["stale"] else ""
    report = _html.escape(str(summary.get("report") or ""))
    parts = [
        f'<div class="text-xs"><span class="{cls} font-bold">{verdict}</span>'
        f' <span class="text-slate-500">{_html.escape(summary["age_label"])}{stale_note}</span></div>',
        f'<div class="text-xs text-slate-400 mt-1">high: {summary["high"]} &nbsp; '
        f'medium: {summary["medium"]}</div>',
    ]
    if report:
        parts.append(f'<div class="text-xs text-slate-600 mt-1 truncate" title="{report}">'
                     f'{report}</div>')
    return "".join(parts)
