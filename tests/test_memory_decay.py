"""Tests for curation.decay -- half-life decay detection."""
from __future__ import annotations

import tempfile
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from atlas_plus.curation.decay import DecayCandidate, find_decayed_facts


def _write_md(path: Path, updated: str, half_life: int | None = None, decays: bool = True, mem_type: str = "reference") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fm_lines = [
        "---",
        f"id: {path.stem}",
        f"title: {path.stem}",
        f"type: {mem_type}",
        f"status: active",
        f"updated: {updated}",
    ]
    if half_life is not None:
        fm_lines.append(f"half_life: {half_life}")
    if not decays:
        fm_lines.append("decays: false")
    fm_lines.append("---\n\nContent here.")
    path.write_text("\n".join(fm_lines), encoding="utf-8")


class TestFindDecayedFacts:
    def test_no_files_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch("atlas_plus.curation.decay.MEMORY_ROOT", Path(tmp)):
                result = find_decayed_facts(threshold_days=30)
        assert result == []

    def test_fresh_file_not_flagged(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            today = date.today().isoformat()
            _write_md(tmp_path / "fresh.md", updated=today, half_life=30)
            with patch("atlas_plus.curation.decay.MEMORY_ROOT", tmp_path):
                result = find_decayed_facts(threshold_days=0)
        assert result == []

    def test_stale_file_with_explicit_half_life_flagged(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            old_date = (date.today() - timedelta(days=60)).isoformat()
            _write_md(tmp_path / "old.md", updated=old_date, half_life=30)
            with patch("atlas_plus.curation.decay.MEMORY_ROOT", tmp_path):
                result = find_decayed_facts(threshold_days=0)
        assert len(result) == 1
        assert result[0].path == "old.md"
        assert result[0].days_overdue == 30

    def test_constraint_type_never_flagged(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            old_date = (date.today() - timedelta(days=200)).isoformat()
            _write_md(tmp_path / "constraint.md", updated=old_date, half_life=30, mem_type="constraint")
            with patch("atlas_plus.curation.decay.MEMORY_ROOT", tmp_path):
                result = find_decayed_facts(threshold_days=0)
        assert result == []

    def test_decays_false_field_exempt(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            old_date = (date.today() - timedelta(days=200)).isoformat()
            _write_md(tmp_path / "permanent.md", updated=old_date, half_life=30, decays=False)
            with patch("atlas_plus.curation.decay.MEMORY_ROOT", tmp_path):
                result = find_decayed_facts(threshold_days=0)
        assert result == []

    def test_no_half_life_field_uses_threshold(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            old_date = (date.today() - timedelta(days=100)).isoformat()
            _write_md(tmp_path / "nohalf.md", updated=old_date)  # no half_life
            with patch("atlas_plus.curation.decay.MEMORY_ROOT", tmp_path):
                # threshold=90 means flag files older than 90 days
                result = find_decayed_facts(threshold_days=90)
        assert len(result) == 1
        assert result[0].days_overdue == 10

    def test_no_half_life_field_threshold_zero_excluded(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            old_date = (date.today() - timedelta(days=100)).isoformat()
            _write_md(tmp_path / "nohalf.md", updated=old_date)
            with patch("atlas_plus.curation.decay.MEMORY_ROOT", tmp_path):
                result = find_decayed_facts(threshold_days=0)
        assert result == []

    def test_sorted_by_days_overdue_descending(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            _write_md(tmp_path / "a.md", updated=(date.today() - timedelta(days=60)).isoformat(), half_life=10)
            _write_md(tmp_path / "b.md", updated=(date.today() - timedelta(days=90)).isoformat(), half_life=10)
            with patch("atlas_plus.curation.decay.MEMORY_ROOT", tmp_path):
                result = find_decayed_facts(threshold_days=0)
        assert len(result) == 2
        assert result[0].days_overdue >= result[1].days_overdue

    def test_scope_restricts_scan(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            old = (date.today() - timedelta(days=60)).isoformat()
            _write_md(tmp_path / "feedback" / "editor.md", updated=old, half_life=10)
            _write_md(tmp_path / "reference" / "tool.md", updated=old, half_life=10)
            with patch("atlas_plus.curation.decay.MEMORY_ROOT", tmp_path):
                result = find_decayed_facts(threshold_days=0, scope="feedback")
        paths = [r.path for r in result]
        assert all("feedback" in p for p in paths)
        assert not any("reference" in p for p in paths)

    def test_as_dict(self):
        c = DecayCandidate(
            path="test.md", title="Test", memory_type="reference",
            half_life_days=30, updated="2026-01-01", days_overdue=15,
        )
        d = c.as_dict()
        assert d["path"] == "test.md"
        assert d["days_overdue"] == 15
