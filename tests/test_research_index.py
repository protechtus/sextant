"""Tests for research index CLI and agent dedup UX fix."""
from __future__ import annotations

import json
import textwrap
from pathlib import Path
from unittest.mock import patch


def _write_report(d: Path, name: str, title: str, date: str,
                  score: float = None, refs: list = None, body_words: int = 100) -> Path:
    """Write a minimal research report with optional quality_score and references."""
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    fm = f'---\ntitle: "{title}"\ndate: {date}\nslug: {name.replace(".md", "")}\n'
    if score is not None:
        fm += f"quality_score: {score}\n"
    if refs:
        fm += f"references:\n" + "".join(f"  - {r}\n" for r in refs)
    fm += "---\n\n"
    body = " ".join(["word"] * body_words) + "\n"
    p.write_text(fm + body, encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# research-index CLI command
# ---------------------------------------------------------------------------

class TestResearchIndexCLI:
    def test_lists_reports_by_date(self, tmp_path):
        from typer.testing import CliRunner
        from atlas_plus.curation.cli import app

        rdir = tmp_path / "reports"
        _write_report(rdir, "a.md", "Research: SIP registration", "2026-05-18", score=0.8)
        _write_report(rdir, "b.md", "Research: WDAC policy", "2026-05-15", score=0.5)

        runner = CliRunner()
        result = runner.invoke(app, ["research-index", "--dir", str(rdir)])
        assert result.exit_code == 0
        output = result.output
        # Newer report should appear before older
        assert output.index("2026-05-18") < output.index("2026-05-15")

    def test_json_output_has_expected_keys(self, tmp_path):
        from typer.testing import CliRunner
        from atlas_plus.curation.cli import app

        rdir = tmp_path / "reports"
        _write_report(rdir, "r.md", "Research: Atlas memory", "2026-05-20",
                      score=0.75, refs=["https://example.com"])

        runner = CliRunner()
        result = runner.invoke(app, ["research-index", "--dir", str(rdir), "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output.strip())
        assert len(data) == 1
        rec = data[0]
        assert "file" in rec
        assert "title" in rec
        assert "date" in rec
        assert "quality_score" in rec
        assert "freshness_score" in rec
        assert "citation_count" in rec
        assert "word_count" in rec
        assert rec["citation_count"] == 1
        # Freshness should be between 0 and 1 for a recent report
        assert rec["freshness_score"] is None or 0.0 <= rec["freshness_score"] <= 1.0

    def test_since_filter_excludes_old_reports(self, tmp_path):
        from typer.testing import CliRunner
        from atlas_plus.curation.cli import app

        rdir = tmp_path / "reports"
        _write_report(rdir, "new.md", "Research: Atlas memory", "2026-05-18", score=0.8)
        _write_report(rdir, "old.md", "Research: SIP trunks", "2026-01-01", score=0.7)

        runner = CliRunner()
        result = runner.invoke(app, ["research-index", "--dir", str(rdir), "--since", "60", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output.strip())
        files = {r["file"] for r in data}
        assert "old.md" not in files

    def test_min_score_filter_excludes_high_quality(self, tmp_path):
        from typer.testing import CliRunner
        from atlas_plus.curation.cli import app

        rdir = tmp_path / "reports"
        _write_report(rdir, "good.md", "Research: Good topic", "2026-05-18", score=0.8)
        _write_report(rdir, "low.md", "Research: Bad topic", "2026-05-17", score=0.3)

        runner = CliRunner()
        result = runner.invoke(app, [
            "research-index", "--dir", str(rdir), "--min-score", "0.5", "--json"
        ])
        assert result.exit_code == 0
        data = json.loads(result.output.strip())
        files = {r["file"] for r in data}
        assert "low.md" in files
        assert "good.md" not in files

    def test_sort_by_score(self, tmp_path):
        from typer.testing import CliRunner
        from atlas_plus.curation.cli import app

        rdir = tmp_path / "reports"
        _write_report(rdir, "high.md", "Research: High quality", "2026-05-18", score=0.9)
        _write_report(rdir, "low.md", "Research: Low quality", "2026-05-19", score=0.2)

        runner = CliRunner()
        result = runner.invoke(app, [
            "research-index", "--dir", str(rdir), "--sort", "score", "--json"
        ])
        assert result.exit_code == 0
        data = json.loads(result.output.strip())
        # Sorted descending by score: high (0.9) first
        assert data[0]["quality_score"] >= data[1]["quality_score"]

    def test_shows_unscored_note(self, tmp_path):
        from typer.testing import CliRunner
        from atlas_plus.curation.cli import app

        rdir = tmp_path / "reports"
        _write_report(rdir, "scored.md", "Research: Scored", "2026-05-18", score=0.7)
        _write_report(rdir, "unscored.md", "Research: Unscored", "2026-05-17")  # no score

        runner = CliRunner()
        result = runner.invoke(app, ["research-index", "--dir", str(rdir)])
        assert result.exit_code == 0
        assert "unscored" in result.output.lower()

    def test_missing_dir_exits_cleanly(self, tmp_path):
        from typer.testing import CliRunner
        from atlas_plus.curation.cli import app

        runner = CliRunner()
        result = runner.invoke(app, ["research-index", "--dir", str(tmp_path / "nosuchdir")])
        assert result.exit_code == 0
        assert "not found" in result.output.lower()


# ---------------------------------------------------------------------------
# Agent dedup UX: ValueError becomes a done result, not a failed task
# ---------------------------------------------------------------------------

class TestAgentResearchDedup:
    def _make_research_agent(self):
        """Return a mock research agent descriptor."""
        from unittest.mock import MagicMock
        agent = MagicMock()
        agent.agent_type = "research"
        return agent

    def test_dedup_returns_done_string_not_failed_prefix(self):
        """When dedup fires, dispatch() returns a done message (no [research] failed: prefix)."""
        import sys
        import importlib

        # We need to call the dispatch block directly. Load agents module and find the dispatch fn.
        from atlas_plus.brain.agents import dispatch

        existing = {
            "file": "sip.md", "title": "Research: SIP", "date": "2026-05-18", "score": 0.65
        }
        with patch("atlas_plus.brain.research.find_similar_research_reports", return_value=[existing]):
            agent = self._make_research_agent()
            result = dispatch(agent, "SIP registration flow", session_id="test-session")

        # Should not contain failure prefix -- dedup is a success path
        assert "[research] failed:" not in result
        assert "Duplicate" in result or "duplicate" in result.lower() or "Similar" in result

    def test_force_prefix_calls_run_research_with_force_true(self):
        """--force prefix in instruction bypasses dedup."""
        from atlas_plus.brain.agents import dispatch
        from unittest.mock import MagicMock

        mock_result = ("Report body here.", Path("/tmp/report.md"))
        with patch("atlas_plus.brain.research.run_research", return_value=mock_result) as mock_run:
            agent = self._make_research_agent()
            result = dispatch(agent, "--force SIP registration flow", session_id="test")

        mock_run.assert_called_once_with("SIP registration flow", "test", force=True)
        assert "Research complete" in result

    def test_normal_instruction_calls_run_research_without_force(self):
        """Normal instruction passes force=False."""
        from atlas_plus.brain.agents import dispatch

        mock_result = ("Report body.", Path("/tmp/report.md"))
        with patch("atlas_plus.brain.research.run_research", return_value=mock_result) as mock_run, \
             patch("atlas_plus.brain.research.find_similar_research_reports", return_value=[]):
            agent = self._make_research_agent()
            dispatch(agent, "SIP registration flow", session_id="test")

        mock_run.assert_called_once_with("SIP registration flow", "test", force=False)


# ---------------------------------------------------------------------------
# compute_freshness_score
# ---------------------------------------------------------------------------

class TestComputeFreshnessScore:
    def test_today_returns_one(self):
        from atlas_plus.brain.research import compute_freshness_score
        from datetime import date
        today = date.today().isoformat()
        score = compute_freshness_score(today)
        assert score == 1.0

    def test_365_days_ago_returns_zero(self):
        from atlas_plus.brain.research import compute_freshness_score
        from datetime import date, timedelta
        old = (date.today() - timedelta(days=365)).isoformat()
        score = compute_freshness_score(old)
        assert score == 0.0

    def test_30_days_ago_above_90_percent(self):
        from atlas_plus.brain.research import compute_freshness_score
        from datetime import date, timedelta
        recent = (date.today() - timedelta(days=30)).isoformat()
        score = compute_freshness_score(recent)
        assert score is not None
        assert score >= 0.9  # 1 - 30/365 ~ 0.918

    def test_180_days_returns_roughly_half(self):
        from atlas_plus.brain.research import compute_freshness_score
        from datetime import date, timedelta
        old = (date.today() - timedelta(days=180)).isoformat()
        score = compute_freshness_score(old)
        assert score is not None
        assert 0.45 <= score <= 0.55

    def test_empty_string_returns_none(self):
        from atlas_plus.brain.research import compute_freshness_score
        assert compute_freshness_score("") is None

    def test_invalid_date_returns_none(self):
        from atlas_plus.brain.research import compute_freshness_score
        assert compute_freshness_score("not-a-date") is None

    def test_score_is_between_zero_and_one(self):
        from atlas_plus.brain.research import compute_freshness_score
        from datetime import date, timedelta
        for days in [0, 30, 90, 180, 270, 365, 500]:
            d = (date.today() - timedelta(days=days)).isoformat()
            score = compute_freshness_score(d)
            assert score is not None
            assert 0.0 <= score <= 1.0


# ---------------------------------------------------------------------------
# cross_refs column in research-index
# ---------------------------------------------------------------------------

class TestResearchIndexCrossRefs:
    """cross_refs: frontmatter field shows in XRef column of research-index."""

    def _write_report_with_crossrefs(self, d: Path, name: str, cross_refs: list) -> Path:
        d.mkdir(parents=True, exist_ok=True)
        p = d / name
        fm = f'---\ntitle: "Report {name}"\ndate: 2026-05-20\n'
        fm += "cross_refs:\n" + "".join(f"  - {r}\n" for r in cross_refs)
        fm += "---\n\nbody text here\n"
        p.write_text(fm, encoding="utf-8")
        return p

    def test_cross_ref_count_in_json_output(self, tmp_path):
        from typer.testing import CliRunner
        from atlas_plus.curation.cli import app
        import json

        rdir = tmp_path / "reports"
        self._write_report_with_crossrefs(
            rdir, "report-a.md",
            ["reference/tools.md", "reference/atlas.md", "reference/brain.md"],
        )
        runner = CliRunner()
        result = runner.invoke(app, ["research-index", "--dir", str(rdir), "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert len(data) == 1
        assert data[0]["cross_ref_count"] == 3

    def test_zero_cross_refs_when_field_absent(self, tmp_path):
        from typer.testing import CliRunner
        from atlas_plus.curation.cli import app
        import json

        rdir = tmp_path / "reports"
        _write_report(rdir, "no-xref.md", "No crossrefs report", "2026-05-20")
        runner = CliRunner()
        result = runner.invoke(app, ["research-index", "--dir", str(rdir), "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data[0]["cross_ref_count"] == 0

    def test_xref_column_visible_in_table_output(self, tmp_path):
        from typer.testing import CliRunner
        from atlas_plus.curation.cli import app

        rdir = tmp_path / "reports"
        self._write_report_with_crossrefs(rdir, "xref-report.md", ["ref/a.md", "ref/b.md"])
        runner = CliRunner()
        result = runner.invoke(app, ["research-index", "--dir", str(rdir)])
        assert result.exit_code == 0
        assert "XRef" in result.output

    def test_sort_by_crossrefs(self, tmp_path):
        from typer.testing import CliRunner
        from atlas_plus.curation.cli import app
        import json

        rdir = tmp_path / "reports"
        self._write_report_with_crossrefs(rdir, "many-refs.md", ["a.md", "b.md", "c.md"])
        self._write_report_with_crossrefs(rdir, "one-ref.md", ["a.md"])
        runner = CliRunner()
        result = runner.invoke(
            app, ["research-index", "--dir", str(rdir), "--sort", "crossrefs", "--json"]
        )
        assert result.exit_code == 0
        data = json.loads(result.output)
        # Most cross-refs first
        assert data[0]["cross_ref_count"] >= data[1]["cross_ref_count"]
