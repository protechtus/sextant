"""Tests for atlas_plus.curation.weekly_report -- weekly cleanup report generation."""
from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch


def _write_md(path: Path, frontmatter: str, body: str = "Content.") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{frontmatter}\n---\n\n{body}\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# generate_report
# ---------------------------------------------------------------------------

class TestGenerateReport:
    def test_returns_tuple_of_str_and_path(self, tmp_path):
        from atlas_plus.curation.weekly_report import generate_report
        with patch("atlas_plus.curation.weekly_report.MEMORY_ROOT", tmp_path), \
             patch("atlas_plus.curation.duplicates.MEMORY_ROOT", tmp_path), \
             patch("atlas_plus.curation.stale.MEMORY_ROOT", tmp_path):
            report, path = generate_report(memory_root=tmp_path, dry_run=True)
        assert isinstance(report, str)
        assert isinstance(path, Path)

    def test_report_contains_header(self, tmp_path):
        from atlas_plus.curation.weekly_report import generate_report
        with patch("atlas_plus.curation.duplicates.MEMORY_ROOT", tmp_path), \
             patch("atlas_plus.curation.stale.MEMORY_ROOT", tmp_path):
            report, _ = generate_report(memory_root=tmp_path, dry_run=True)
        today = date.today().isoformat()
        assert "Atlas Memory Curation Report" in report
        assert today in report

    def test_dry_run_does_not_write_file(self, tmp_path):
        from atlas_plus.curation.weekly_report import generate_report
        with patch("atlas_plus.curation.duplicates.MEMORY_ROOT", tmp_path), \
             patch("atlas_plus.curation.stale.MEMORY_ROOT", tmp_path):
            _, path = generate_report(memory_root=tmp_path, dry_run=True)
        assert not path.exists()

    def test_no_dry_run_writes_file(self, tmp_path):
        from atlas_plus.curation.weekly_report import generate_report
        with patch("atlas_plus.curation.duplicates.MEMORY_ROOT", tmp_path), \
             patch("atlas_plus.curation.stale.MEMORY_ROOT", tmp_path):
            _, path = generate_report(memory_root=tmp_path, dry_run=False)
        assert path.exists()
        assert path.suffix == ".md"

    def test_empty_memory_shows_no_duplicates(self, tmp_path):
        from atlas_plus.curation.weekly_report import generate_report
        with patch("atlas_plus.curation.duplicates.MEMORY_ROOT", tmp_path), \
             patch("atlas_plus.curation.stale.MEMORY_ROOT", tmp_path):
            report, _ = generate_report(memory_root=tmp_path, dry_run=True)
        assert "No potential duplicates" in report

    def test_empty_memory_shows_no_stale(self, tmp_path):
        from atlas_plus.curation.weekly_report import generate_report
        with patch("atlas_plus.curation.duplicates.MEMORY_ROOT", tmp_path), \
             patch("atlas_plus.curation.stale.MEMORY_ROOT", tmp_path):
            report, _ = generate_report(memory_root=tmp_path, dry_run=True)
        assert "No stale files" in report

    def test_duplicate_files_appear_in_report(self, tmp_path):
        from atlas_plus.curation.weekly_report import generate_report
        body = "networking voip sip rtp protocol registration codecs media"
        _write_md(tmp_path / "a.md", f"title: A\n", body)
        _write_md(tmp_path / "b.md", f"title: B\n", body)
        with patch("atlas_plus.curation.duplicates.MEMORY_ROOT", tmp_path), \
             patch("atlas_plus.curation.stale.MEMORY_ROOT", tmp_path):
            report, _ = generate_report(memory_root=tmp_path, dup_threshold=0.3, dry_run=True)
        assert "Potential Duplicates" in report

    def test_stale_files_appear_in_report(self, tmp_path):
        from atlas_plus.curation.weekly_report import generate_report
        old_date = (date.today() - timedelta(days=300)).isoformat()
        _write_md(tmp_path / "old.md", f"title: Old\nupdated: {old_date}")
        with patch("atlas_plus.curation.duplicates.MEMORY_ROOT", tmp_path), \
             patch("atlas_plus.curation.stale.MEMORY_ROOT", tmp_path):
            report, _ = generate_report(memory_root=tmp_path, stale_months=1, dry_run=True)
        assert "Stale Files" in report


# ---------------------------------------------------------------------------
# _scan_decisions (internal but well-scoped)
# ---------------------------------------------------------------------------

class TestScanDecisions:
    def test_no_decisions_dir_returns_empty(self, tmp_path):
        from atlas_plus.curation.weekly_report import _scan_decisions
        result = _scan_decisions(tmp_path)
        assert result == []

    def test_returns_stale_decisions(self, tmp_path):
        from atlas_plus.curation.weekly_report import _scan_decisions
        decisions_dir = tmp_path / "decisions"
        decisions_dir.mkdir()
        old_made = (date.today() - timedelta(days=120)).isoformat()
        (decisions_dir / "adr-001.md").write_text(
            f"---\ntitle: Use SQLite\nmade: {old_made}\n---\n\nContent.\n",
            encoding="utf-8",
        )
        result = _scan_decisions(tmp_path, stale_days=90)
        assert len(result) == 1
        assert result[0]["slug"] == "adr-001"

    def test_skips_recent_decisions(self, tmp_path):
        from atlas_plus.curation.weekly_report import _scan_decisions
        decisions_dir = tmp_path / "decisions"
        decisions_dir.mkdir()
        recent_made = (date.today() - timedelta(days=10)).isoformat()
        (decisions_dir / "recent.md").write_text(
            f"---\ntitle: Recent\nmade: {recent_made}\nlast_reviewed: {recent_made}\n---\nContent.\n",
            encoding="utf-8",
        )
        result = _scan_decisions(tmp_path, stale_days=90)
        assert result == []

    def test_skips_underscore_files(self, tmp_path):
        from atlas_plus.curation.weekly_report import _scan_decisions
        decisions_dir = tmp_path / "decisions"
        decisions_dir.mkdir()
        old = (date.today() - timedelta(days=200)).isoformat()
        (decisions_dir / "_template.md").write_text(
            f"---\ntitle: Template\nmade: {old}\n---\nContent.\n",
            encoding="utf-8",
        )
        result = _scan_decisions(tmp_path, stale_days=90)
        assert result == []
