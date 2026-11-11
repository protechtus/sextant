"""Tests for the nightly contradiction check (Direction B -- nightly learning).

Tests the full wire-up:
  - HIGH: temporal invalidation applied + ticket created
  - MEDIUM/LOW: ticket created, no temporal invalidation
  - ATLAS_AUTONOMOUS=off: no modifications, report only
  - No contradictions: report written, no tickets
  - Older file detection via temporal graph entity last_seen_at
  - Report JSON written to .atlas/
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from atlas_plus.curation.contradictions import ContradictionPair
from atlas_plus.curation.nightly_contradiction_check import (
    _older_file,
    run_contradiction_check,
)
from atlas_plus.temporal_graph.file_provider import FileTemporalProvider
from atlas_plus.temporal_graph.models import TemporalEntity, TemporalFact, _now_iso


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _provider(tmp_path: Path) -> FileTemporalProvider:
    return FileTemporalProvider(graph_root=tmp_path / "temporal_graph")


def _add_entity(p: FileTemporalProvider, rel_key: str, name: str, last_seen: str) -> None:
    entity_id = f"memory:{rel_key}"
    p.upsert_entity(TemporalEntity(
        entity_id=entity_id,
        name=name,
        labels=["MemoryFile"],
        aliases=[rel_key],
        first_seen_at="2026-01-01T00:00:00+00:00",
        last_seen_at=last_seen,
    ))
    p.add_fact(TemporalFact(
        fact_id=f"f:{rel_key}:has_status",
        subject_id=entity_id,
        predicate="has_status",
        object_id="active",
        fact_text=f"{name} status is active.",
        valid_from="2026-01-01T00:00:00+00:00",
        observed_at=_now_iso(),
        ingested_at=_now_iso(),
        confidence=0.95,
        authority="memory",
    ))


def _pair(file1: str, file2: str, confidence: str) -> ContradictionPair:
    return ContradictionPair(
        file1=file1,
        file2=file2,
        explanation=f"A says X; B says not-X. ({confidence})",
        confidence=confidence,
    )


def _fake_ticket():
    t = MagicMock()
    t.id = "ATL-TEST"
    return t


# ---------------------------------------------------------------------------
# Older file detection
# ---------------------------------------------------------------------------

class TestOlderFileDetection:
    def test_picks_file_with_lower_last_seen(self, tmp_path):
        p = _provider(tmp_path)
        _add_entity(p, "feedback/alpha.md", "Alpha", "2026-01-01T00:00:00+00:00")
        _add_entity(p, "feedback/beta.md", "Beta", "2026-05-01T00:00:00+00:00")
        pair = _pair("feedback/alpha.md", "feedback/beta.md", "high")
        assert _older_file(pair, p) == "feedback/alpha.md"

    def test_when_both_entities_missing_defaults_to_file1(self, tmp_path):
        p = _provider(tmp_path)
        pair = _pair("feedback/x.md", "feedback/y.md", "high")
        assert _older_file(pair, p) == "feedback/x.md"

    def test_when_only_one_entity_missing_returns_missing_one(self, tmp_path):
        p = _provider(tmp_path)
        _add_entity(p, "feedback/beta.md", "Beta", "2026-05-01T00:00:00+00:00")
        pair = _pair("feedback/alpha.md", "feedback/beta.md", "high")
        assert _older_file(pair, p) == "feedback/alpha.md"


# ---------------------------------------------------------------------------
# HIGH confidence: temporal invalidation + ticket
# ---------------------------------------------------------------------------

class TestHighConfidenceResolution:
    def test_invalidates_older_has_status_fact(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ATLAS_AUTONOMOUS", "on")
        atlas_dir = tmp_path / ".atlas"
        atlas_dir.mkdir()
        monkeypatch.setenv("ATLAS_STATE_DIR", str(atlas_dir))

        p = _provider(tmp_path)
        _add_entity(p, "feedback/alpha.md", "Alpha", "2026-01-01T00:00:00+00:00")
        _add_entity(p, "feedback/beta.md", "Beta", "2026-05-01T00:00:00+00:00")
        high = _pair("feedback/alpha.md", "feedback/beta.md", "high")

        with patch("atlas_plus.curation.nightly_contradiction_check.find_contradictions", return_value=[high]), \
             patch("atlas_plus.tickets.store.create_ticket", return_value=_fake_ticket()):
            report = run_contradiction_check(memory_root=tmp_path, scopes=["feedback"], _provider=p)

        assert report["temporal_invalidated"] == 1
        assert report["tickets_created"] == 1
        assert report["high"] == 1
        # Older file (alpha) has_status fact should now have invalid_at set
        alpha_fact = p._facts.get("f:feedback/alpha.md:has_status")
        assert alpha_fact is not None
        assert alpha_fact.invalid_at is not None
        # New 'contradicted' fact should exist
        new_fact = p._facts.get("f:feedback/alpha.md:has_status:contradicted")
        assert new_fact is not None
        assert new_fact.object_id == "contradicted"

    def test_ticket_has_high_priority(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ATLAS_AUTONOMOUS", "on")
        (tmp_path / ".atlas").mkdir()
        monkeypatch.setenv("ATLAS_STATE_DIR", str(tmp_path / ".atlas"))

        p = _provider(tmp_path)
        high = _pair("feedback/x.md", "feedback/y.md", "high")
        captured = {}

        def spy_create(**kwargs):
            captured.update(kwargs)
            return _fake_ticket()

        with patch("atlas_plus.curation.nightly_contradiction_check.find_contradictions", return_value=[high]), \
             patch("atlas_plus.tickets.store.create_ticket", side_effect=spy_create):
            run_contradiction_check(memory_root=tmp_path, scopes=["feedback"], _provider=p)

        assert captured.get("priority") == "high"


# ---------------------------------------------------------------------------
# MEDIUM/LOW confidence: ticket only, no temporal invalidation
# ---------------------------------------------------------------------------

class TestMediumLowConfidenceResolution:
    @pytest.mark.parametrize("confidence", ["medium", "low"])
    def test_no_temporal_invalidation(self, tmp_path, monkeypatch, confidence):
        monkeypatch.setenv("ATLAS_AUTONOMOUS", "on")
        (tmp_path / ".atlas").mkdir()
        monkeypatch.setenv("ATLAS_STATE_DIR", str(tmp_path / ".atlas"))

        p = _provider(tmp_path)
        _add_entity(p, "feedback/alpha.md", "Alpha", "2026-01-01T00:00:00+00:00")
        _add_entity(p, "feedback/beta.md", "Beta", "2026-05-01T00:00:00+00:00")
        pr = _pair("feedback/alpha.md", "feedback/beta.md", confidence)

        with patch("atlas_plus.curation.nightly_contradiction_check.find_contradictions", return_value=[pr]), \
             patch("atlas_plus.tickets.store.create_ticket", return_value=_fake_ticket()):
            report = run_contradiction_check(memory_root=tmp_path, scopes=["feedback"], _provider=p)

        assert report["temporal_invalidated"] == 0
        assert report["tickets_created"] == 1
        # Fact NOT invalidated
        alpha_fact = p._facts.get("f:feedback/alpha.md:has_status")
        assert alpha_fact.invalid_at is None

    @pytest.mark.parametrize("confidence", ["medium", "low"])
    def test_ticket_has_normal_priority(self, tmp_path, monkeypatch, confidence):
        monkeypatch.setenv("ATLAS_AUTONOMOUS", "on")
        (tmp_path / ".atlas").mkdir()
        monkeypatch.setenv("ATLAS_STATE_DIR", str(tmp_path / ".atlas"))

        p = _provider(tmp_path)
        pr = _pair("feedback/x.md", "feedback/y.md", confidence)
        captured = {}

        def spy_create(**kwargs):
            captured.update(kwargs)
            return _fake_ticket()

        with patch("atlas_plus.curation.nightly_contradiction_check.find_contradictions", return_value=[pr]), \
             patch("atlas_plus.tickets.store.create_ticket", side_effect=spy_create):
            run_contradiction_check(memory_root=tmp_path, scopes=["feedback"], _provider=p)

        assert captured.get("priority") == "normal"


# ---------------------------------------------------------------------------
# ATLAS_AUTONOMOUS=off: no modifications
# ---------------------------------------------------------------------------

class TestAutonomousOff:
    def test_no_ticket_no_invalidation(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ATLAS_AUTONOMOUS", "off")
        (tmp_path / ".atlas").mkdir()
        monkeypatch.setenv("ATLAS_STATE_DIR", str(tmp_path / ".atlas"))

        p = _provider(tmp_path)
        _add_entity(p, "feedback/alpha.md", "Alpha", "2026-01-01T00:00:00+00:00")
        _add_entity(p, "feedback/beta.md", "Beta", "2026-05-01T00:00:00+00:00")
        high = _pair("feedback/alpha.md", "feedback/beta.md", "high")

        with patch("atlas_plus.curation.nightly_contradiction_check.find_contradictions", return_value=[high]), \
             patch("atlas_plus.tickets.store.create_ticket") as mock_create:
            report = run_contradiction_check(memory_root=tmp_path, scopes=["feedback"], _provider=p)

        assert report["temporal_invalidated"] == 0
        assert report["tickets_created"] == 0
        mock_create.assert_not_called()
        assert report["high"] == 1
        assert report["pairs"][0]["ticket_id"] is None

    def test_report_autonomous_flag_is_false(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ATLAS_AUTONOMOUS", "off")
        atlas_dir = tmp_path / ".atlas"
        atlas_dir.mkdir()

        import atlas_plus.curation.nightly_contradiction_check as mod
        monkeypatch.setattr(mod, "STATE_DIR", atlas_dir)
        original = mod._date_str
        mod._date_str = lambda: "2026-05-17"
        p = _provider(tmp_path)
        try:
            with patch("atlas_plus.curation.nightly_contradiction_check.find_contradictions", return_value=[]):
                run_contradiction_check(memory_root=tmp_path, scopes=["feedback"], _provider=p)
        finally:
            mod._date_str = original

        import json
        data = json.loads((atlas_dir / "contradiction-report-2026-05-17.json").read_text())
        assert data["autonomous"] is False


# ---------------------------------------------------------------------------
# No contradictions
# ---------------------------------------------------------------------------

class TestNoContradictions:
    def test_clean_report_no_tickets(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ATLAS_AUTONOMOUS", "on")
        (tmp_path / ".atlas").mkdir()
        monkeypatch.setenv("ATLAS_STATE_DIR", str(tmp_path / ".atlas"))

        p = _provider(tmp_path)
        with patch("atlas_plus.curation.nightly_contradiction_check.find_contradictions", return_value=[]), \
             patch("atlas_plus.tickets.store.create_ticket") as mock_create:
            report = run_contradiction_check(memory_root=tmp_path, scopes=["feedback"], _provider=p)

        assert report["total_pairs"] == 0
        assert report["tickets_created"] == 0
        mock_create.assert_not_called()


# ---------------------------------------------------------------------------
# Report JSON structure
# ---------------------------------------------------------------------------

class TestReportOutput:
    def test_report_json_fields(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ATLAS_AUTONOMOUS", "on")
        atlas_dir = tmp_path / ".atlas"
        atlas_dir.mkdir()

        import atlas_plus.curation.nightly_contradiction_check as mod
        monkeypatch.setattr(mod, "STATE_DIR", atlas_dir)
        original = mod._date_str
        mod._date_str = lambda: "2026-05-17"
        p = _provider(tmp_path)
        pr = _pair("feedback/x.md", "reference/y.md", "medium")
        try:
            with patch("atlas_plus.curation.nightly_contradiction_check.find_contradictions", return_value=[pr]), \
                 patch("atlas_plus.tickets.store.create_ticket", return_value=_fake_ticket()):
                run_contradiction_check(memory_root=tmp_path, scopes=["feedback"], _provider=p)
        finally:
            mod._date_str = original

        import json
        data = json.loads((atlas_dir / "contradiction-report-2026-05-17.json").read_text())
        assert data["total_pairs"] == 1
        assert data["medium"] == 1
        assert data["scopes"] == ["feedback"]
        entry = data["pairs"][0]
        assert entry["confidence"] == "medium"
        assert entry["file1"] == "feedback/x.md"
        assert entry["file2"] == "reference/y.md"
        assert "explanation" in entry
        assert "older_file" in entry
        assert entry["temporal_invalidated"] is False
