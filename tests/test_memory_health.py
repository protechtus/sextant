"""Tests for memory file health scoring (curation/health.py)."""
from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path


def _write_md(path: Path, fm: str, body: str = "Content.\n") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{fm}\n---\n\n{body}", encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# compute_file_health
# ---------------------------------------------------------------------------

class TestComputeFileHealth:
    def _today(self) -> str:
        return date.today().isoformat()

    def _days_ago(self, n: int) -> str:
        return (date.today() - timedelta(days=n)).isoformat()

    def test_perfect_score_all_factors(self, tmp_path):
        from atlas_plus.curation.health import compute_file_health
        p = _write_md(tmp_path / "alex.md",
                      f"title: Alex\ntype: identity\ntags: [identity]\n"
                      f"last_confirmed: {self._today()}\nupdated: {self._today()}\nconfidence: high")
        result = compute_file_health(p)
        assert result["score"] == 1.0
        assert result["issues"] == []

    def test_zero_score_no_metadata(self, tmp_path):
        from atlas_plus.curation.health import compute_file_health
        p = _write_md(tmp_path / "bare.md", "title: Bare file")
        result = compute_file_health(p)
        assert result["score"] == 0.0
        assert "no_tags" in result["issues"]
        assert "unconfirmed" in result["issues"]
        assert "no_updated" in result["issues"]
        assert "no_confidence" in result["issues"]

    def test_missing_tags_costs_025(self, tmp_path):
        from atlas_plus.curation.health import compute_file_health
        p = _write_md(tmp_path / "f.md",
                      f"title: F\nlast_confirmed: {self._today()}\n"
                      f"updated: {self._today()}\nconfidence: medium")
        result = compute_file_health(p)
        assert result["score"] == 0.75
        assert "no_tags" in result["issues"]

    def test_stale_confirmed_costs_025(self, tmp_path):
        from atlas_plus.curation.health import compute_file_health
        old_date = self._days_ago(120)
        p = _write_md(tmp_path / "f.md",
                      f"title: F\ntags: [test]\nlast_confirmed: {old_date}\n"
                      f"updated: {self._today()}\nconfidence: low")
        result = compute_file_health(p)
        assert result["score"] == 0.75
        assert "stale_confirmed" in result["issues"]

    def test_fresh_confirmed_gets_full_credit(self, tmp_path):
        from atlas_plus.curation.health import compute_file_health
        recent = self._days_ago(30)
        p = _write_md(tmp_path / "f.md",
                      f"title: F\ntags: [test]\nlast_confirmed: {recent}\n"
                      f"updated: {self._today()}\nconfidence: high")
        result = compute_file_health(p)
        assert result["score"] == 1.0

    def test_returns_zero_for_missing_file(self, tmp_path):
        from atlas_plus.curation.health import compute_file_health
        p = tmp_path / "missing.md"
        result = compute_file_health(p)
        assert result["score"] == 0.0

    def test_returns_zero_for_no_frontmatter(self, tmp_path):
        from atlas_plus.curation.health import compute_file_health
        p = tmp_path / "plain.md"
        p.write_text("No frontmatter.\n", encoding="utf-8")
        result = compute_file_health(p)
        assert result["score"] == 0.0


# ---------------------------------------------------------------------------
# scan_memory_health
# ---------------------------------------------------------------------------

class TestScanMemoryHealth:
    def _setup(self, tmp_path: Path) -> Path:
        mem = tmp_path / ".memory"
        today = date.today().isoformat()
        _write_md(mem / "identity" / "alex.md",
                  f"title: Alex\ntype: identity\ntags: [identity]\n"
                  f"last_confirmed: {today}\nupdated: {today}\nconfidence: high")
        _write_md(mem / "feedback" / "editor.md",
                  "title: Editor\ntype: feedback")  # score=0
        _write_md(mem / "reference" / "examplecorp.md",
                  f"title: ExampleCorp\ntype: reference\ntags: [examplecorp]\nupdated: {today}")  # score=0.5
        return mem

    def test_returns_sorted_by_score_ascending(self, tmp_path):
        from atlas_plus.curation.health import scan_memory_health
        mem = self._setup(tmp_path)
        results = scan_memory_health(mem)
        scores = [r["score"] for r in results]
        assert scores == sorted(scores)

    def test_skips_archive_dir(self, tmp_path):
        from atlas_plus.curation.health import scan_memory_health
        mem = tmp_path / ".memory"
        _write_md(mem / "feedback" / "real.md", "title: Real")
        _write_md(mem / "archive" / "old.md", "title: Old")
        results = scan_memory_health(mem)
        paths = [r["path"] for r in results]
        assert all("archive" not in p for p in paths)

    def test_scope_restricts_scan(self, tmp_path):
        from atlas_plus.curation.health import scan_memory_health
        mem = self._setup(tmp_path)
        results = scan_memory_health(mem, scope="feedback")
        paths = [r["path"] for r in results]
        assert all(p.startswith("feedback/") for p in paths)

    def test_result_has_expected_keys(self, tmp_path):
        from atlas_plus.curation.health import scan_memory_health
        mem = self._setup(tmp_path)
        results = scan_memory_health(mem)
        assert len(results) > 0
        rec = results[0]
        assert "path" in rec
        assert "score" in rec
        assert "issues" in rec
        assert "factors" in rec
        assert "title" in rec


# ---------------------------------------------------------------------------
# health-score CLI command
# ---------------------------------------------------------------------------

class TestHealthScoreCLI:
    def _setup(self, tmp_path: Path) -> Path:
        mem = tmp_path / ".memory"
        today = date.today().isoformat()
        _write_md(mem / "identity" / "alex.md",
                  f"title: Alex\ntags: [identity]\nlast_confirmed: {today}\n"
                  f"updated: {today}\nconfidence: high")
        _write_md(mem / "feedback" / "editor.md", "title: Editor\ntype: feedback")
        return mem

    def test_shows_unhealthy_files(self, tmp_path):
        from typer.testing import CliRunner
        from atlas_plus.curation.cli import app
        from unittest.mock import patch
        mem = self._setup(tmp_path)
        runner = CliRunner()
        with patch("atlas_plus.curation.cli.MEMORY_ROOT", mem):
            result = runner.invoke(app, ["health-score"])
        assert result.exit_code == 0
        # editor.md has score 0.0, should appear
        assert "feedback/editor.md" in result.output

    def test_json_output(self, tmp_path):
        from typer.testing import CliRunner
        from atlas_plus.curation.cli import app
        from unittest.mock import patch
        mem = self._setup(tmp_path)
        runner = CliRunner()
        with patch("atlas_plus.curation.cli.MEMORY_ROOT", mem):
            result = runner.invoke(app, ["health-score", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output.strip())
        assert isinstance(data, list)
        files = {r["path"] for r in data}
        assert "feedback/editor.md" in files

    def test_max_health_filter(self, tmp_path):
        from typer.testing import CliRunner
        from atlas_plus.curation.cli import app
        from unittest.mock import patch
        import json as _json
        mem = self._setup(tmp_path)
        runner = CliRunner()
        with patch("atlas_plus.curation.cli.MEMORY_ROOT", mem):
            result = runner.invoke(app, ["health-score", "--max-health", "0.25", "--json"])
        assert result.exit_code == 0
        data = _json.loads(result.output.strip())
        # Only files with score <= 0.25 should appear
        assert all(r["score"] <= 0.25 for r in data)
