"""Tests for the Helm security-audit verdict surface (WS1).

Covers:
  - security_status.read_audit_index   -- tolerant JSON reader (never raises)
  - security_status.summarize_security -- clean/review/attention/stale/unknown policy
  - rollup_span / render_panel         -- pure HTML renderers
  - GET /api/security/audit            -- Helm fragment smoke test
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone

import pytest

# Mirror test_web_app.py env EXACTLY so the app's auth constant matches no matter
# which test module imports the app first (setdefault is order-safe only when the
# values agree).
_TEST_KEY = "test-key-abc"
os.environ.setdefault("ATLAS_WEB_API_KEY", _TEST_KEY)
os.environ.setdefault("ATLAS_BRAIN_URL", "http://127.0.0.1:9999")
os.environ.setdefault("ATLAS_BRAIN_API_KEY", "brain-key")
os.environ.setdefault("ATLAS_MCP_TOKEN", "mcp-token")
AUTH = {"Authorization": f"Bearer {_TEST_KEY}"}

from fastapi.testclient import TestClient  # noqa: E402

from atlas_plus.web import security_status as sec  # noqa: E402
from atlas_plus.web.app import app  # noqa: E402

_client = TestClient(app, raise_server_exceptions=False)

_NOW = datetime(2026, 6, 13, 12, 0, 0, tzinfo=timezone.utc)


def _ts(hours_ago: float) -> str:
    """An index-style 'Z'-suffixed UTC timestamp N hours before the fixed now."""
    return (_NOW - timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _rec(verdict: str, hours_ago: float = 2, high: int = 0, medium: int = 0,
         report: str = "audit-x.md") -> dict:
    return {"ts": _ts(hours_ago), "verdict": verdict, "high": high,
            "medium": medium, "report": report, "sarif": "audit-x.sarif"}


# ---------------------------------------------------------------------------
# read_audit_index -- tolerant reader
# ---------------------------------------------------------------------------

def test_read_index_returns_records(tmp_path):
    p = tmp_path / "index.json"
    p.write_text(json.dumps([_rec("CLEAN"), _rec("REVIEW", medium=2)]), encoding="utf-8")
    out = sec.read_audit_index(p)
    assert [r["verdict"] for r in out] == ["CLEAN", "REVIEW"]


def test_read_index_missing_file_is_empty(tmp_path):
    assert sec.read_audit_index(tmp_path / "nope.json") == []


def test_read_index_bad_json_is_empty(tmp_path):
    p = tmp_path / "index.json"
    p.write_text("{not json", encoding="utf-8")
    assert sec.read_audit_index(p) == []


def test_read_index_non_list_is_empty(tmp_path):
    p = tmp_path / "index.json"
    p.write_text(json.dumps({"verdict": "CLEAN"}), encoding="utf-8")
    assert sec.read_audit_index(p) == []


def test_read_index_heals_value_wrapper(tmp_path):
    p = tmp_path / "index.json"
    p.write_text(json.dumps([{"value": _rec("ATTENTION", high=1)}]), encoding="utf-8")
    out = sec.read_audit_index(p)
    assert len(out) == 1 and out[0]["verdict"] == "ATTENTION"


def test_read_index_drops_records_without_verdict(tmp_path):
    p = tmp_path / "index.json"
    p.write_text(json.dumps([{"ts": _ts(1)}, _rec("CLEAN")]), encoding="utf-8")
    out = sec.read_audit_index(p)
    assert len(out) == 1 and out[0]["verdict"] == "CLEAN"


# ---------------------------------------------------------------------------
# summarize_security -- the policy
# ---------------------------------------------------------------------------

def test_summarize_empty_is_unknown():
    s = sec.summarize_security([], now=_NOW)
    assert s["status"] == "unknown"
    assert s["age_label"] == "never"


def test_summarize_clean_fresh():
    s = sec.summarize_security([_rec("CLEAN", hours_ago=2)], now=_NOW)
    assert s["status"] == "clean"
    assert s["stale"] is False


def test_summarize_review_on_medium():
    s = sec.summarize_security([_rec("REVIEW", hours_ago=5, medium=3)], now=_NOW)
    assert s["status"] == "review"
    assert s["medium"] == 3


def test_summarize_attention_on_high():
    s = sec.summarize_security([_rec("ATTENTION", hours_ago=5, high=2)], now=_NOW)
    assert s["status"] == "attention"
    assert s["high"] == 2


def test_summarize_clean_but_old_is_stale():
    # Older than the 9-day (216h) window -> the audit is not running.
    s = sec.summarize_security([_rec("CLEAN", hours_ago=300)], now=_NOW)
    assert s["status"] == "stale"
    assert s["stale"] is True


def test_summarize_attention_beats_stale():
    s = sec.summarize_security([_rec("ATTENTION", hours_ago=400, high=1)], now=_NOW)
    assert s["status"] == "attention"  # high findings outrank staleness


def test_summarize_unparseable_ts_is_stale():
    s = sec.summarize_security([{"verdict": "CLEAN", "ts": "garbage", "high": 0, "medium": 0}],
                               now=_NOW)
    assert s["status"] == "stale"


def test_summarize_uses_latest_record():
    s = sec.summarize_security([_rec("ATTENTION", hours_ago=1, high=1), _rec("CLEAN", hours_ago=200)],
                               now=_NOW)
    assert s["status"] == "attention"  # index[0] is newest


def test_summarize_naive_now_treated_as_utc():
    s = sec.summarize_security([_rec("CLEAN", hours_ago=2)], now=_NOW.replace(tzinfo=None))
    assert s["status"] == "clean"


# ---------------------------------------------------------------------------
# renderers
# ---------------------------------------------------------------------------

def test_rollup_span_clean_is_green():
    span = sec.rollup_span(sec.summarize_security([_rec("CLEAN")], now=_NOW))
    assert "sec: clean" in span and "text-green-400" in span


def test_rollup_span_attention_shows_high_count():
    span = sec.rollup_span(sec.summarize_security([_rec("ATTENTION", high=3)], now=_NOW))
    assert "sec: attention (3h)" in span and "text-red-400" in span


def test_rollup_span_unknown_is_gray():
    span = sec.rollup_span(sec.summarize_security([], now=_NOW))
    assert "sec: unknown" in span and "text-gray-500" in span


def test_render_panel_unknown_mentions_no_runs():
    assert "no audit runs" in sec.render_panel(sec.summarize_security([], now=_NOW))


def test_render_panel_shows_verdict_and_counts():
    html = sec.render_panel(sec.summarize_security([_rec("REVIEW", medium=4)], now=_NOW))
    assert "REVIEW" in html and "medium: 4" in html


# ---------------------------------------------------------------------------
# endpoint smoke test
# ---------------------------------------------------------------------------

def test_security_audit_endpoint_requires_auth():
    r = _client.get("/api/security/audit", headers={"Authorization": "Bearer wrong"})
    assert r.status_code in (401, 403)


def test_security_audit_endpoint_renders(monkeypatch):
    monkeypatch.setattr(
        "atlas_plus.web.security_status.read_audit_index",
        lambda *a, **k: [_rec("ATTENTION", hours_ago=3, high=1, medium=2, report="audit-now.md")],
    )
    r = _client.get("/api/security/audit", headers=AUTH)
    assert r.status_code == 200
    assert "ATTENTION" in r.text
    assert "audit-now.md" in r.text
