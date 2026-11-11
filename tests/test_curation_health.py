"""Tests for atlas_plus.curation.health -- file health scoring."""
from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path


def _write_md(path: Path, frontmatter: str, body: str = "Content.") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{frontmatter}\n---\n\n{body}\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# compute_file_health
# ---------------------------------------------------------------------------

class TestComputeFileHealth:
    def test_fully_annotated_scores_1(self, tmp_path):
        from atlas_plus.curation.health import compute_file_health
        from datetime import date
        today = date.today().isoformat()
        f = tmp_path / "full.md"
        _write_md(f, f"title: Full\ntags: [sip]\nlast_confirmed: {today}\nupdated: {today}\nconfidence: high")
        result = compute_file_health(f, today=date.today())
        assert result["score"] == 1.0

    def test_bare_file_scores_zero(self, tmp_path):
        from atlas_plus.curation.health import compute_file_health
        f = tmp_path / "bare.md"
        _write_md(f, "title: Bare")
        result = compute_file_health(f)
        assert result["score"] == 0.0
        assert len(result["issues"]) > 0

    def test_no_frontmatter_returns_zero(self, tmp_path):
        from atlas_plus.curation.health import compute_file_health
        f = tmp_path / "raw.md"
        f.write_text("No frontmatter here.\n", encoding="utf-8")
        result = compute_file_health(f)
        assert result["score"] == 0.0
        assert "no_frontmatter" in result["issues"]

    def test_missing_file_returns_zero(self, tmp_path):
        from atlas_plus.curation.health import compute_file_health
        result = compute_file_health(tmp_path / "nonexistent.md")
        assert result["score"] == 0.0

    def test_stale_confirmed_scores_zero_for_confirmed_factor(self, tmp_path):
        from atlas_plus.curation.health import compute_file_health
        old_date = (date.today() - timedelta(days=100)).isoformat()
        f = tmp_path / "stale.md"
        _write_md(f, f"title: Stale\ntags: [sip]\nlast_confirmed: {old_date}\nupdated: 2026-05-01\nconfidence: medium")
        result = compute_file_health(f, today=date.today())
        assert result["factors"]["confirmed"] == 0.0
        assert "stale_confirmed" in result["issues"]

    def test_tags_factor_set_when_tags_present(self, tmp_path):
        from atlas_plus.curation.health import compute_file_health
        f = tmp_path / "tagged.md"
        _write_md(f, "title: Tagged\ntags: [networking]")
        result = compute_file_health(f)
        assert result["factors"]["tags"] == 0.25

    def test_confidence_high_medium_low_all_valid(self, tmp_path):
        from atlas_plus.curation.health import compute_file_health
        for conf in ("high", "medium", "low"):
            f = tmp_path / f"{conf}.md"
            _write_md(f, f"title: C\nconfidence: {conf}")
            result = compute_file_health(f)
            assert result["factors"]["confidence"] == 0.25

    def test_result_has_expected_keys(self, tmp_path):
        from atlas_plus.curation.health import compute_file_health
        f = tmp_path / "x.md"
        _write_md(f, "title: X")
        result = compute_file_health(f)
        for key in ("score", "factors", "issues", "frontmatter"):
            assert key in result
        for factor in ("tags", "confirmed", "updated", "confidence"):
            assert factor in result["factors"]


# ---------------------------------------------------------------------------
# scan_memory_health
# ---------------------------------------------------------------------------

class TestScanMemoryHealth:
    def test_empty_directory_returns_empty(self, tmp_path):
        from atlas_plus.curation.health import scan_memory_health
        mem = tmp_path / ".memory"
        mem.mkdir()
        results = scan_memory_health(mem)
        assert results == []

    def test_returns_results_sorted_by_score_asc(self, tmp_path):
        from atlas_plus.curation.health import scan_memory_health
        from datetime import date
        today = date.today().isoformat()
        mem = tmp_path / ".memory"
        # File with no metadata (score 0) and fully annotated (score 1)
        _write_md(mem / "zero.md", "title: Zero")
        _write_md(mem / "full.md",
                  f"title: Full\ntags: [x]\nlast_confirmed: {today}\nupdated: {today}\nconfidence: high")
        results = scan_memory_health(mem)
        assert len(results) == 2
        assert results[0]["score"] <= results[1]["score"]

    def test_skips_archive_directory(self, tmp_path):
        from atlas_plus.curation.health import scan_memory_health
        mem = tmp_path / ".memory"
        _write_md(mem / "archive" / "old.md", "title: Archived")
        _write_md(mem / "identity" / "current.md", "title: Current")
        results = scan_memory_health(mem)
        paths = [r["path"] for r in results]
        assert not any("archive" in p for p in paths)
        assert any("current" in p for p in paths)

    def test_min_health_filter(self, tmp_path):
        from atlas_plus.curation.health import scan_memory_health
        from datetime import date
        today = date.today().isoformat()
        mem = tmp_path / ".memory"
        _write_md(mem / "zero.md", "title: Zero")
        _write_md(mem / "full.md",
                  f"title: Full\ntags: [x]\nlast_confirmed: {today}\nupdated: {today}\nconfidence: high")
        results = scan_memory_health(mem, min_health=0.5)
        # Only files with score < min_health are included (score < 0.5)
        assert all(r["score"] < 0.5 for r in results)

    def test_limit_respected(self, tmp_path):
        from atlas_plus.curation.health import scan_memory_health
        mem = tmp_path / ".memory"
        for i in range(5):
            _write_md(mem / f"file{i}.md", "title: F")
        results = scan_memory_health(mem, limit=3)
        assert len(results) <= 3

    def test_result_has_expected_keys(self, tmp_path):
        from atlas_plus.curation.health import scan_memory_health
        mem = tmp_path / ".memory"
        _write_md(mem / "x.md", "title: X")
        results = scan_memory_health(mem)
        if results:
            for key in ("path", "score", "issues", "factors", "title"):
                assert key in results[0]
