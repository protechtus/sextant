"""Tests for curation.decay module: DecayCandidate and find_decayed_facts."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch


def _write_mem(path: Path, *, half_life: int | None = None, updated: str = "2025-01-01",
               title: str = "Test", mem_type: str = "reference",
               decays: bool | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"title: {title}", f"type: {mem_type}", f"updated: {updated}"]
    if half_life is not None:
        lines.append(f"half_life: {half_life}")
    if decays is not None:
        lines.append(f"decays: {str(decays).lower()}")
    path.write_text("---\n" + "\n".join(lines) + "\n---\n\nBody.", encoding="utf-8")


# ---------------------------------------------------------------------------
# DecayCandidate.as_dict
# ---------------------------------------------------------------------------

class TestDecayCandidateAsDict:
    def test_as_dict_has_all_keys(self):
        from atlas_plus.curation.decay import DecayCandidate
        c = DecayCandidate(
            path="reference/old.md", title="Old Fact", memory_type="reference",
            half_life_days=30, updated="2025-01-01", days_overdue=60,
        )
        d = c.as_dict()
        assert d["path"] == "reference/old.md"
        assert d["title"] == "Old Fact"
        assert d["half_life_days"] == 30
        assert d["days_overdue"] == 60

    def test_as_dict_is_json_serialisable(self):
        import json
        from atlas_plus.curation.decay import DecayCandidate
        c = DecayCandidate("a.md", "T", "reference", 30, "2025-01-01", 5)
        json.dumps(c.as_dict())


# ---------------------------------------------------------------------------
# find_decayed_facts
# ---------------------------------------------------------------------------

class TestFindDecayedFacts:
    def test_empty_root_returns_empty(self, tmp_path):
        import atlas_plus.curation.decay as mod
        with patch.object(mod, "MEMORY_ROOT", tmp_path):
            result = mod.find_decayed_facts()
        assert result == []

    def test_missing_scope_returns_empty(self, tmp_path):
        import atlas_plus.curation.decay as mod
        with patch.object(mod, "MEMORY_ROOT", tmp_path):
            result = mod.find_decayed_facts(scope="nonexistent")
        assert result == []

    def test_file_without_updated_skipped(self, tmp_path):
        import atlas_plus.curation.decay as mod
        f = tmp_path / "reference" / "no-date.md"
        f.parent.mkdir(parents=True)
        f.write_text("---\ntitle: T\nhalf_life: 30\ntype: reference\n---\n\nbody", encoding="utf-8")
        with patch.object(mod, "MEMORY_ROOT", tmp_path):
            result = mod.find_decayed_facts()
        assert result == []

    def test_decays_false_exempt(self, tmp_path):
        import atlas_plus.curation.decay as mod
        _write_mem(tmp_path / "reference" / "exempt.md",
                   half_life=30, updated="2025-01-01", decays=False)
        with patch.object(mod, "MEMORY_ROOT", tmp_path):
            result = mod.find_decayed_facts()
        assert result == []

    def test_constraint_type_exempt(self, tmp_path):
        import atlas_plus.curation.decay as mod
        _write_mem(tmp_path / "reference" / "constraint.md",
                   half_life=30, updated="2025-01-01", mem_type="constraint")
        with patch.object(mod, "MEMORY_ROOT", tmp_path):
            result = mod.find_decayed_facts()
        assert result == []

    def test_zero_threshold_skips_files_without_half_life(self, tmp_path):
        import atlas_plus.curation.decay as mod
        _write_mem(tmp_path / "reference" / "no-hl.md", updated="2025-01-01")
        with patch.object(mod, "MEMORY_ROOT", tmp_path):
            result = mod.find_decayed_facts(threshold_days=0)
        assert result == []

    def test_flags_file_past_half_life(self, tmp_path):
        import atlas_plus.curation.decay as mod
        _write_mem(tmp_path / "reference" / "stale.md", half_life=30, updated="2025-01-01")
        with patch.object(mod, "MEMORY_ROOT", tmp_path):
            result = mod.find_decayed_facts()
        assert len(result) == 1
        assert result[0].path == "reference/stale.md"
        assert result[0].days_overdue > 0

    def test_fresh_file_not_flagged(self, tmp_path):
        import atlas_plus.curation.decay as mod
        from datetime import date, timedelta
        recent = (date.today() - timedelta(days=5)).isoformat()
        _write_mem(tmp_path / "reference" / "fresh.md", half_life=30, updated=recent)
        with patch.object(mod, "MEMORY_ROOT", tmp_path):
            result = mod.find_decayed_facts()
        assert result == []

    def test_default_threshold_applied_when_no_half_life(self, tmp_path):
        import atlas_plus.curation.decay as mod
        _write_mem(tmp_path / "reference" / "no-hl.md", updated="2025-01-01")
        with patch.object(mod, "MEMORY_ROOT", tmp_path):
            result = mod.find_decayed_facts(threshold_days=30)
        assert len(result) == 1
        assert result[0].half_life_days == 30

    def test_sorted_by_days_overdue_descending(self, tmp_path):
        import atlas_plus.curation.decay as mod
        _write_mem(tmp_path / "reference" / "moderately-stale.md",
                   half_life=30, updated="2025-06-01")
        _write_mem(tmp_path / "reference" / "very-stale.md",
                   half_life=30, updated="2024-01-01")
        with patch.object(mod, "MEMORY_ROOT", tmp_path):
            result = mod.find_decayed_facts()
        if len(result) >= 2:
            assert result[0].days_overdue >= result[1].days_overdue

    def test_scope_restricts_scan(self, tmp_path):
        import atlas_plus.curation.decay as mod
        _write_mem(tmp_path / "reference" / "stale.md", half_life=30, updated="2025-01-01")
        _write_mem(tmp_path / "feedback" / "stale.md", half_life=30, updated="2025-01-01")
        with patch.object(mod, "MEMORY_ROOT", tmp_path):
            result = mod.find_decayed_facts(scope="feedback")
        assert all("feedback" in c.path for c in result)

    def test_archive_dir_excluded(self, tmp_path):
        import atlas_plus.curation.decay as mod
        _write_mem(tmp_path / "archive" / "old.md", half_life=30, updated="2024-01-01")
        with patch.object(mod, "MEMORY_ROOT", tmp_path):
            result = mod.find_decayed_facts()
        assert result == []
