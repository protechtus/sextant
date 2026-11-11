"""Tests for Direction A -- Reflexion Loop.

Tests the full pipeline:
  - REVISE action: old fact invalidated, new fact added with supersedes link
  - SKIP action: no modifications made
  - TICKET action: human-review ticket created, no temporal changes
  - REVISE with no revised_text: falls back to TICKET
  - ATLAS_AUTONOMOUS=off: REVISE downgraded to TICKET, no temporal changes
  - dry_run: LLM not called, facts collected only
  - Empty index: clean report returned
  - Facts older than lookback window are excluded
  - LLM parse errors are handled gracefully
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from atlas_plus.curation.reflexion_loop import (
    _parse_critique,
    run_reflexion_loop,
)
from atlas_plus.temporal_graph.file_provider import FileTemporalProvider
from atlas_plus.temporal_graph.models import TemporalFact, _now_iso


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _provider(tmp_path: Path) -> FileTemporalProvider:
    return FileTemporalProvider(graph_root=tmp_path / "temporal_graph")


def _recent_fact(p: FileTemporalProvider, fact_id: str, text: str) -> TemporalFact:
    """Add a fact with ingested_at = now (within any reasonable lookback window)."""
    now = _now_iso()
    fact = TemporalFact(
        fact_id=fact_id,
        subject_id="e-test",
        predicate="has_rule",
        object_id="test",
        fact_text=text,
        valid_from="2026-01-01T00:00:00+00:00",
        observed_at=now,
        ingested_at=now,
        confidence=0.9,
        authority="memory",
    )
    p.add_fact(fact)
    return fact


def _old_fact(p: FileTemporalProvider, fact_id: str, text: str) -> TemporalFact:
    """Add a fact with ingested_at far in the past."""
    fact = TemporalFact(
        fact_id=fact_id,
        subject_id="e-test",
        predicate="has_rule",
        object_id="test",
        fact_text=text,
        valid_from="2025-01-01T00:00:00+00:00",
        observed_at="2025-01-01T00:00:00+00:00",
        ingested_at="2025-01-01T00:00:00+00:00",
        confidence=0.9,
        authority="memory",
    )
    p.add_fact(fact)
    return fact


def _fake_ticket():
    t = MagicMock()
    t.id = "ATL-REFLEXION"
    return t


def _llm_response(items: list[dict]) -> str:
    return json.dumps(items)


# ---------------------------------------------------------------------------
# LLM response parsing
# ---------------------------------------------------------------------------

class TestParseCritique:
    def test_parses_revise(self):
        facts = [{"fact_id": "f-001", "fact_text": "old text"}]
        raw = json.dumps([{
            "fact_id": "f-001",
            "action": "REVISE",
            "reason": "outdated",
            "revised_text": "new better text",
        }])
        result = _parse_critique(raw, facts)
        assert len(result) == 1
        assert result[0]["action"] == "REVISE"
        assert result[0]["revised_text"] == "new better text"

    def test_parses_skip(self):
        facts = [{"fact_id": "f-001", "fact_text": "text"}]
        raw = json.dumps([{"fact_id": "f-001", "action": "SKIP", "reason": "fine", "revised_text": ""}])
        result = _parse_critique(raw, facts)
        assert result[0]["action"] == "SKIP"

    def test_parses_ticket(self):
        facts = [{"fact_id": "f-001", "fact_text": "text"}]
        raw = json.dumps([{"fact_id": "f-001", "action": "TICKET", "reason": "uncertain", "revised_text": ""}])
        result = _parse_critique(raw, facts)
        assert result[0]["action"] == "TICKET"

    def test_ignores_unknown_fact_ids(self):
        facts = [{"fact_id": "f-001", "fact_text": "text"}]
        raw = json.dumps([{"fact_id": "f-UNKNOWN", "action": "REVISE", "reason": "", "revised_text": "x"}])
        assert _parse_critique(raw, facts) == []

    def test_handles_malformed_json(self):
        facts = [{"fact_id": "f-001", "fact_text": "text"}]
        assert _parse_critique("NOT JSON AT ALL", facts) == []

    def test_strips_markdown_fences(self):
        facts = [{"fact_id": "f-001", "fact_text": "text"}]
        raw = '```json\n[{"fact_id":"f-001","action":"SKIP","reason":"ok","revised_text":""}]\n```'
        result = _parse_critique(raw, facts)
        assert len(result) == 1
        assert result[0]["action"] == "SKIP"


# ---------------------------------------------------------------------------
# REVISE action
# ---------------------------------------------------------------------------

class TestReviseAction:
    def test_revise_invalidates_old_and_adds_new(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ATLAS_AUTONOMOUS", "on")
        atlas_dir = tmp_path / ".atlas"
        atlas_dir.mkdir()

        import atlas_plus.curation.reflexion_loop as mod
        monkeypatch.setattr(mod, "STATE_DIR", atlas_dir)

        p = _provider(tmp_path)
        _recent_fact(p, "f-001", "Always use mocks in tests.")

        llm_resp = _llm_response([{
            "fact_id": "f-001",
            "action": "REVISE",
            "reason": "Mocks hide real database bugs.",
            "revised_text": "Integration tests must use a real database, not mocks.",
        }])

        with patch("atlas_plus.curation.reflexion_loop._call_llm", return_value=llm_resp):
            report = run_reflexion_loop(lookback_days=30, _provider=p)

        assert report["revised"] == 1
        # Old fact should have invalid_at set
        old = p._facts["f-001"]
        assert old.invalid_at is not None
        # New revised fact should exist
        revised = next((f for f in p._facts.values() if f.supersedes_fact_id == "f-001"), None)
        assert revised is not None
        assert revised.fact_text == "Integration tests must use a real database, not mocks."
        assert revised.confidence == 0.85

    def test_revise_without_text_falls_back_to_ticket(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ATLAS_AUTONOMOUS", "on")
        atlas_dir = tmp_path / ".atlas"
        atlas_dir.mkdir()

        import atlas_plus.curation.reflexion_loop as mod
        monkeypatch.setattr(mod, "STATE_DIR", atlas_dir)

        p = _provider(tmp_path)
        _recent_fact(p, "f-001", "Some fact.")

        llm_resp = _llm_response([{
            "fact_id": "f-001", "action": "REVISE", "reason": "unclear", "revised_text": "",
        }])

        with patch("atlas_plus.curation.reflexion_loop._call_llm", return_value=llm_resp), \
             patch("atlas_plus.tickets.store.create_ticket", return_value=_fake_ticket()):
            report = run_reflexion_loop(lookback_days=30, _provider=p)

        # No revision (no text), ticket created instead
        assert report["revised"] == 0
        assert report["tickets_created"] == 1
        # Old fact NOT invalidated
        assert p._facts["f-001"].invalid_at is None


# ---------------------------------------------------------------------------
# SKIP action
# ---------------------------------------------------------------------------

class TestSkipAction:
    def test_skip_makes_no_changes(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ATLAS_AUTONOMOUS", "on")
        (tmp_path / ".atlas").mkdir()

        import atlas_plus.curation.reflexion_loop as mod
        monkeypatch.setattr(mod, "STATE_DIR", tmp_path / ".atlas")

        p = _provider(tmp_path)
        _recent_fact(p, "f-001", "Good fact.")

        llm_resp = _llm_response([{
            "fact_id": "f-001", "action": "SKIP", "reason": "accurate", "revised_text": "",
        }])

        with patch("atlas_plus.curation.reflexion_loop._call_llm", return_value=llm_resp), \
             patch("atlas_plus.tickets.store.create_ticket") as mock_t:
            report = run_reflexion_loop(lookback_days=30, _provider=p)

        assert report["skipped"] == 1
        assert report["revised"] == 0
        mock_t.assert_not_called()
        assert p._facts["f-001"].invalid_at is None


# ---------------------------------------------------------------------------
# TICKET action
# ---------------------------------------------------------------------------

class TestTicketAction:
    def test_ticket_creates_ticket_no_temporal_change(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ATLAS_AUTONOMOUS", "on")
        (tmp_path / ".atlas").mkdir()

        import atlas_plus.curation.reflexion_loop as mod
        monkeypatch.setattr(mod, "STATE_DIR", tmp_path / ".atlas")

        p = _provider(tmp_path)
        _recent_fact(p, "f-001", "Uncertain fact.")

        llm_resp = _llm_response([{
            "fact_id": "f-001", "action": "TICKET", "reason": "ambiguous", "revised_text": "",
        }])

        with patch("atlas_plus.curation.reflexion_loop._call_llm", return_value=llm_resp), \
             patch("atlas_plus.tickets.store.create_ticket", return_value=_fake_ticket()):
            report = run_reflexion_loop(lookback_days=30, _provider=p)

        assert report["tickets_created"] == 1
        assert report["revised"] == 0
        assert p._facts["f-001"].invalid_at is None


# ---------------------------------------------------------------------------
# ATLAS_AUTONOMOUS=off
# ---------------------------------------------------------------------------

class TestAutonomousOff:
    def test_revise_downgraded_to_ticket(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ATLAS_AUTONOMOUS", "off")
        (tmp_path / ".atlas").mkdir()

        import atlas_plus.curation.reflexion_loop as mod
        monkeypatch.setattr(mod, "STATE_DIR", tmp_path / ".atlas")

        p = _provider(tmp_path)
        _recent_fact(p, "f-001", "Stale fact.")

        llm_resp = _llm_response([{
            "fact_id": "f-001", "action": "REVISE", "reason": "stale", "revised_text": "Updated fact.",
        }])

        with patch("atlas_plus.curation.reflexion_loop._call_llm", return_value=llm_resp), \
             patch("atlas_plus.tickets.store.create_ticket", return_value=_fake_ticket()):
            report = run_reflexion_loop(lookback_days=30, _provider=p)

        # No temporal changes -- autonomous=off
        assert report["revised"] == 0
        assert report["tickets_created"] == 1
        assert p._facts["f-001"].invalid_at is None


# ---------------------------------------------------------------------------
# Dry run
# ---------------------------------------------------------------------------

class TestDryRun:
    def test_dry_run_no_llm_no_changes(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ATLAS_AUTONOMOUS", "on")
        (tmp_path / ".atlas").mkdir()

        import atlas_plus.curation.reflexion_loop as mod
        monkeypatch.setattr(mod, "STATE_DIR", tmp_path / ".atlas")

        p = _provider(tmp_path)
        _recent_fact(p, "f-001", "A fact.")

        with patch("atlas_plus.curation.reflexion_loop._call_llm") as mock_llm, \
             patch("atlas_plus.tickets.store.create_ticket") as mock_t:
            report = run_reflexion_loop(lookback_days=30, dry_run=True, _provider=p)

        mock_llm.assert_not_called()
        mock_t.assert_not_called()
        assert report["dry_run"] is True
        assert report["facts_reviewed"] == 1
        assert report["results"][0]["action"] == "DRY_RUN"


# ---------------------------------------------------------------------------
# Lookback window filtering
# ---------------------------------------------------------------------------

class TestLookbackFilter:
    def test_old_facts_excluded(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ATLAS_AUTONOMOUS", "on")
        (tmp_path / ".atlas").mkdir()

        import atlas_plus.curation.reflexion_loop as mod
        monkeypatch.setattr(mod, "STATE_DIR", tmp_path / ".atlas")

        p = _provider(tmp_path)
        _recent_fact(p, "f-recent", "Recent fact.")
        _old_fact(p, "f-old", "Old fact from 2025.")

        with patch("atlas_plus.curation.reflexion_loop._call_llm", return_value="[]"):
            report = run_reflexion_loop(lookback_days=7, _provider=p)

        # Only the recent fact should be reviewed
        assert report["facts_reviewed"] == 1
        reviewed_ids = [r["fact_id"] for r in report["results"]]
        assert "f-old" not in reviewed_ids

    def test_empty_index_returns_clean_report(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ATLAS_AUTONOMOUS", "on")
        (tmp_path / ".atlas").mkdir()

        import atlas_plus.curation.reflexion_loop as mod
        monkeypatch.setattr(mod, "STATE_DIR", tmp_path / ".atlas")

        p = _provider(tmp_path)
        with patch("atlas_plus.curation.reflexion_loop._call_llm") as mock_llm:
            report = run_reflexion_loop(lookback_days=7, _provider=p)

        mock_llm.assert_not_called()
        assert report["facts_reviewed"] == 0
        assert report["revised"] == 0
