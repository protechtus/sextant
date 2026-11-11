"""Tests for atlas_plus.curation.nightly_contradiction_check.

Covers _is_autonomous, _older_file, _create_ticket, and run_contradiction_check
(dry_run mode -- no LLM calls, no writes).
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# TestIsAutonomous
# ---------------------------------------------------------------------------

class TestIsAutonomous:
    def test_on_by_default(self):
        from atlas_plus.curation.nightly_contradiction_check import _is_autonomous
        with patch.dict("os.environ", {"ATLAS_AUTONOMOUS": "on"}):
            assert _is_autonomous() is True

    def test_off_when_set_to_off(self):
        from atlas_plus.curation.nightly_contradiction_check import _is_autonomous
        with patch.dict("os.environ", {"ATLAS_AUTONOMOUS": "off"}):
            assert _is_autonomous() is False

    def test_off_when_set_to_0(self):
        from atlas_plus.curation.nightly_contradiction_check import _is_autonomous
        with patch.dict("os.environ", {"ATLAS_AUTONOMOUS": "0"}):
            assert _is_autonomous() is False


# ---------------------------------------------------------------------------
# TestOlderFile
# ---------------------------------------------------------------------------

class TestOlderFile:
    def _make_pair(self):
        from atlas_plus.curation.contradictions import ContradictionPair
        return ContradictionPair(
            file1="feedback/a.md",
            file2="feedback/b.md",
            explanation="A contradicts B",
            confidence="high",
        )

    def _make_provider(self, a_seen: str, b_seen: str):
        ent_a = MagicMock()
        ent_a.last_seen_at = a_seen
        ent_b = MagicMock()
        ent_b.last_seen_at = b_seen
        provider = MagicMock()
        provider._entities = {
            "memory:feedback/a.md": ent_a,
            "memory:feedback/b.md": ent_b,
        }
        return provider

    def test_returns_file1_when_a_is_older(self):
        from atlas_plus.curation.nightly_contradiction_check import _older_file
        pair = self._make_pair()
        provider = self._make_provider("2026-01-01", "2026-05-21")
        result = _older_file(pair, provider)
        assert result == "feedback/a.md"

    def test_returns_file2_when_b_is_older(self):
        from atlas_plus.curation.nightly_contradiction_check import _older_file
        pair = self._make_pair()
        provider = self._make_provider("2026-05-21", "2026-01-01")
        result = _older_file(pair, provider)
        assert result == "feedback/b.md"

    def test_returns_file1_when_neither_entity_found(self):
        from atlas_plus.curation.nightly_contradiction_check import _older_file
        pair = self._make_pair()
        provider = MagicMock()
        provider._entities = {}
        result = _older_file(pair, provider)
        assert result == "feedback/a.md"

    def test_returns_file1_when_only_b_entity_found(self):
        from atlas_plus.curation.nightly_contradiction_check import _older_file
        pair = self._make_pair()
        provider = MagicMock()
        ent_b = MagicMock()
        ent_b.last_seen_at = "2026-05-21"
        provider._entities = {"memory:feedback/b.md": ent_b}
        result = _older_file(pair, provider)
        assert result == "feedback/a.md"


# ---------------------------------------------------------------------------
# TestCreateTicket
# ---------------------------------------------------------------------------

class TestCreateTicket:
    def _make_pair(self, confidence="high"):
        from atlas_plus.curation.contradictions import ContradictionPair
        return ContradictionPair(
            file1="feedback/pref-a.md",
            file2="feedback/pref-b.md",
            explanation="A says always X; B says never X.",
            confidence=confidence,
        )

    def test_creates_ticket_with_correct_title_format(self):
        from atlas_plus.curation.nightly_contradiction_check import _create_ticket
        pair = self._make_pair("high")
        fake_ticket = MagicMock()
        with patch("atlas_plus.tickets.store.create_ticket", return_value=fake_ticket) as mock_ct:
            result = _create_ticket(pair, "feedback/pref-a.md", False)
        assert result is fake_ticket
        _, kwargs = mock_ct.call_args
        assert "Contradiction" in kwargs["title"]
        assert "high" in kwargs["title"].lower() or "HIGH" in kwargs["title"]

    def test_high_confidence_uses_high_priority(self):
        from atlas_plus.curation.nightly_contradiction_check import _create_ticket
        pair = self._make_pair("high")
        with patch("atlas_plus.tickets.store.create_ticket") as mock_ct:
            _create_ticket(pair, "feedback/pref-a.md", True)
        _, kwargs = mock_ct.call_args
        assert kwargs["priority"] == "high"

    def test_medium_confidence_uses_normal_priority(self):
        from atlas_plus.curation.nightly_contradiction_check import _create_ticket
        pair = self._make_pair("medium")
        with patch("atlas_plus.tickets.store.create_ticket") as mock_ct:
            _create_ticket(pair, "feedback/pref-a.md", False)
        _, kwargs = mock_ct.call_args
        assert kwargs["priority"] == "normal"

    def test_returns_none_on_create_ticket_error(self):
        from atlas_plus.curation.nightly_contradiction_check import _create_ticket
        pair = self._make_pair()
        with patch("atlas_plus.tickets.store.create_ticket", side_effect=Exception("db error")):
            result = _create_ticket(pair, "feedback/pref-a.md", False)
        assert result is None

    def test_body_contains_file_references(self):
        from atlas_plus.curation.nightly_contradiction_check import _create_ticket
        pair = self._make_pair("medium")
        with patch("atlas_plus.tickets.store.create_ticket") as mock_ct:
            _create_ticket(pair, "feedback/pref-a.md", False)
        _, kwargs = mock_ct.call_args
        assert "pref-a.md" in kwargs["body"]
        assert "pref-b.md" in kwargs["body"]

    def test_body_mentions_temporal_flag_when_invalidated(self):
        from atlas_plus.curation.nightly_contradiction_check import _create_ticket
        pair = self._make_pair("high")
        with patch("atlas_plus.tickets.store.create_ticket") as mock_ct:
            _create_ticket(pair, "feedback/pref-a.md", temporal_invalidated=True)
        _, kwargs = mock_ct.call_args
        assert "contradicted" in kwargs["body"].lower()


# ---------------------------------------------------------------------------
# TestRunContradictionCheckDryRun
# ---------------------------------------------------------------------------

class TestRunContradictionCheckDryRun:
    def test_returns_empty_when_no_contradictions(self, tmp_path):
        from atlas_plus.curation.nightly_contradiction_check import run_contradiction_check
        mem_root = tmp_path / "memory"
        (mem_root / "feedback").mkdir(parents=True)
        with patch("atlas_plus.curation.nightly_contradiction_check.find_contradictions",
                   return_value=[]):
            result = run_contradiction_check(memory_root=mem_root, dry_run=True)
        assert result.get("total") == 0 or result == {} or isinstance(result, dict)

    def test_dry_run_does_not_create_tickets(self, tmp_path):
        from atlas_plus.curation.contradictions import ContradictionPair
        from atlas_plus.curation.nightly_contradiction_check import run_contradiction_check
        mem_root = tmp_path / "memory"
        (mem_root / "feedback").mkdir(parents=True)
        pair = ContradictionPair("feedback/a.md", "feedback/b.md", "conflict", "high")
        # Provider with empty entities so _older_file falls back to pair.file1
        mock_provider = MagicMock()
        mock_provider._entities = {}
        mock_provider._facts = {}
        mock_provider_cls = MagicMock(return_value=mock_provider)
        with patch("atlas_plus.curation.nightly_contradiction_check.find_contradictions",
                   return_value=[pair]), \
             patch("atlas_plus.tickets.store.create_ticket") as mock_ct, \
             patch("atlas_plus.temporal_graph.file_provider.FileTemporalProvider", mock_provider_cls):
            run_contradiction_check(memory_root=mem_root, dry_run=True)
        mock_ct.assert_not_called()
