"""Tests for research report citation extraction and frontmatter injection."""
from __future__ import annotations

import textwrap
from pathlib import Path


# ---------------------------------------------------------------------------
# extract_report_citations
# ---------------------------------------------------------------------------

class TestExtractReportCitations:
    def test_extracts_markdown_link_urls(self):
        from atlas_plus.brain.research import extract_report_citations
        text = "See [RFC 3261](https://tools.ietf.org/html/rfc3261) for details."
        urls = extract_report_citations(text)
        assert "https://tools.ietf.org/html/rfc3261" in urls

    def test_extracts_bare_urls(self):
        from atlas_plus.brain.research import extract_report_citations
        text = "More info at https://example.com/page and https://other.org."
        urls = extract_report_citations(text)
        assert "https://example.com/page" in urls
        assert "https://other.org" in urls

    def test_strips_trailing_punctuation(self):
        from atlas_plus.brain.research import extract_report_citations
        text = "Reference: https://example.com/path."
        urls = extract_report_citations(text)
        assert "https://example.com/path" in urls
        assert "https://example.com/path." not in urls

    def test_deduplicates(self):
        from atlas_plus.brain.research import extract_report_citations
        text = (
            "[Title](https://example.com/a) and again [Other](https://example.com/a). "
            "Also bare: https://example.com/a"
        )
        urls = extract_report_citations(text)
        assert urls.count("https://example.com/a") == 1

    def test_returns_sorted_list(self):
        from atlas_plus.brain.research import extract_report_citations
        text = "[B](https://zzz.com) [A](https://aaa.com)"
        urls = extract_report_citations(text)
        assert urls == sorted(urls)

    def test_returns_empty_when_no_urls(self):
        from atlas_plus.brain.research import extract_report_citations
        assert extract_report_citations("No links here.") == []

    def test_skips_very_short_urls(self):
        from atlas_plus.brain.research import extract_report_citations
        text = "Short: https://x.io"  # <= 12 chars
        urls = extract_report_citations(text)
        assert not any(len(u) <= 12 for u in urls)

    def test_extracts_references_section(self):
        from atlas_plus.brain.research import extract_report_citations
        text = textwrap.dedent("""\
        ## Recommendations
        Use SIP over TLS.

        ## References
        - [RFC 3261](https://tools.ietf.org/html/rfc3261)
        - [ExampleCorp Docs](https://docs.examplecorp.com/voip-guide)
        """)
        urls = extract_report_citations(text)
        assert "https://tools.ietf.org/html/rfc3261" in urls
        assert "https://docs.examplecorp.com/voip-guide" in urls


# ---------------------------------------------------------------------------
# inject_references_frontmatter
# ---------------------------------------------------------------------------

class TestInjectReferencesFrontmatter:
    def _write_report(self, tmp_path: Path, body: str, extra_fm: str = "") -> Path:
        """Write a minimal research report markdown file."""
        p = tmp_path / "report.md"
        fm = f"---\ntitle: Test Report\ndate: 2026-05-20\n{extra_fm}---\n\n{body}\n"
        p.write_text(fm, encoding="utf-8")
        return p

    def test_injects_urls_into_new_file(self, tmp_path):
        from atlas_plus.brain.research import inject_references_frontmatter
        body = "## References\n- [RFC 3261](https://tools.ietf.org/html/rfc3261)\n"
        p = self._write_report(tmp_path, body)
        count = inject_references_frontmatter(p)
        assert count == 1
        content = p.read_text(encoding="utf-8")
        assert "references:" in content
        assert "https://tools.ietf.org/html/rfc3261" in content

    def test_returns_zero_when_no_urls(self, tmp_path):
        from atlas_plus.brain.research import inject_references_frontmatter
        p = self._write_report(tmp_path, "No external references.")
        count = inject_references_frontmatter(p)
        assert count == 0

    def test_idempotent_when_already_complete(self, tmp_path):
        from atlas_plus.brain.research import inject_references_frontmatter
        url = "https://tools.ietf.org/html/rfc3261"
        body = f"See [RFC 3261]({url}) for details."
        p = self._write_report(tmp_path, body, extra_fm=f"references:\n  - {url}\n")
        original = p.read_text(encoding="utf-8")
        count = inject_references_frontmatter(p)
        assert count == 0
        assert p.read_text(encoding="utf-8") == original

    def test_updates_when_new_url_discovered(self, tmp_path):
        from atlas_plus.brain.research import inject_references_frontmatter
        url1 = "https://tools.ietf.org/html/rfc3261"
        url2 = "https://docs.examplecorp.com/voip-guide"
        body = f"See [RFC 3261]({url1}) and [ExampleCorp]({url2})."
        p = self._write_report(tmp_path, body, extra_fm=f"references:\n  - {url1}\n")
        count = inject_references_frontmatter(p)
        assert count == 2  # both URLs in updated output
        content = p.read_text(encoding="utf-8")
        assert url1 in content
        assert url2 in content

    def test_replaces_old_references_block(self, tmp_path):
        from atlas_plus.brain.research import inject_references_frontmatter
        old_url = "https://old.example.com/gone"
        new_url = "https://tools.ietf.org/html/rfc3261"
        body = f"New link: [{new_url}]({new_url})"
        p = self._write_report(tmp_path, body, extra_fm=f"references:\n  - {old_url}\n")
        inject_references_frontmatter(p)
        content = p.read_text(encoding="utf-8")
        assert new_url in content
        # old URL is not in the report body so only new URL should be in references
        fm_end = content.find("\n---", 3)
        fm = content[3:fm_end]
        assert old_url not in fm

    def test_returns_zero_for_missing_file(self, tmp_path):
        from atlas_plus.brain.research import inject_references_frontmatter
        p = tmp_path / "nonexistent.md"
        assert inject_references_frontmatter(p) == 0

    def test_returns_zero_when_no_frontmatter(self, tmp_path):
        from atlas_plus.brain.research import inject_references_frontmatter
        p = tmp_path / "nofm.md"
        p.write_text("No frontmatter here. https://example.com/page\n", encoding="utf-8")
        assert inject_references_frontmatter(p) == 0

    def test_multiple_urls_written_sorted(self, tmp_path):
        from atlas_plus.brain.research import inject_references_frontmatter
        body = "[B](https://zzz.example.com/b) and [A](https://aaa.example.com/a)"
        p = self._write_report(tmp_path, body)
        inject_references_frontmatter(p)
        content = p.read_text(encoding="utf-8")
        pos_a = content.find("aaa.example.com")
        pos_z = content.find("zzz.example.com")
        assert 0 < pos_a < pos_z  # sorted: aaa before zzz


# ---------------------------------------------------------------------------
# citations CLI command
# ---------------------------------------------------------------------------

class TestCitationsCLI:
    def _write_report(self, d: Path, name: str, body: str) -> Path:
        p = d / name
        p.write_text(f"---\ntitle: {name}\ndate: 2026-05-20\n---\n\n{body}\n", encoding="utf-8")
        return p

    def test_dry_run_does_not_write(self, tmp_path):
        from typer.testing import CliRunner
        from atlas_plus.curation.cli import app

        rdir = tmp_path / "reports"
        rdir.mkdir()
        self._write_report(rdir, "report.md", "See [RFC 3261](https://tools.ietf.org/html/rfc3261).")
        original = (rdir / "report.md").read_text()

        runner = CliRunner()
        result = runner.invoke(app, ["citations", "--dir", str(rdir), "--dry-run"])
        assert result.exit_code == 0
        # File should not have changed
        assert (rdir / "report.md").read_text() == original

    def test_injects_when_not_dry_run(self, tmp_path):
        from typer.testing import CliRunner
        from atlas_plus.curation.cli import app

        rdir = tmp_path / "reports"
        rdir.mkdir()
        self._write_report(rdir, "report.md", "See [RFC 3261](https://tools.ietf.org/html/rfc3261).")

        runner = CliRunner()
        result = runner.invoke(app, ["citations", "--dir", str(rdir)])
        assert result.exit_code == 0
        content = (rdir / "report.md").read_text()
        assert "references:" in content

    def test_skips_files_without_urls(self, tmp_path):
        from typer.testing import CliRunner
        from atlas_plus.curation.cli import app

        rdir = tmp_path / "reports"
        rdir.mkdir()
        self._write_report(rdir, "report.md", "No external references.")

        runner = CliRunner()
        result = runner.invoke(app, ["citations", "--dir", str(rdir)])
        assert result.exit_code == 0
        assert "up to date" in result.output or "No citations" in result.output

    def test_json_output(self, tmp_path):
        import json
        from typer.testing import CliRunner
        from atlas_plus.curation.cli import app

        rdir = tmp_path / "reports"
        rdir.mkdir()
        self._write_report(rdir, "r1.md", "See [RFC 3261](https://tools.ietf.org/html/rfc3261).")
        self._write_report(rdir, "r2.md", "No external references.")

        runner = CliRunner()
        result = runner.invoke(app, ["citations", "--dir", str(rdir), "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output.strip())
        assert isinstance(data, list)
        # Only r1 should appear (r2 has no URLs)
        assert len(data) == 1
        assert data[0]["file"] == "r1.md"

    def test_missing_directory_exits_cleanly(self, tmp_path):
        from typer.testing import CliRunner
        from atlas_plus.curation.cli import app

        runner = CliRunner()
        result = runner.invoke(app, ["citations", "--dir", str(tmp_path / "nosuchdir")])
        assert result.exit_code == 0  # exits cleanly with a message
        assert "not found" in result.output.lower()


# ---------------------------------------------------------------------------
# score_research_report
# ---------------------------------------------------------------------------

class TestScoreResearchReport:
    _FULL_REPORT = textwrap.dedent("""\
    ## Summary
    Atlas is a personal knowledge system.

    ## Key Findings
    - Finding one is specific and concrete.
    - Finding two gives actionable guidance.
    - Finding three rounds out the picture.

    ## Context
    Background on personal knowledge management and how Atlas fits in.

    ## Gaps and Open Questions
    What remains unknown about long-term memory retention.

    ## Recommendations
    Adopt Atlas for daily knowledge capture.

    ## References
    - [RFC 3261](https://tools.ietf.org/html/rfc3261)
    - [ExampleCorp Docs](https://docs.examplecorp.com/voip-guide)
    - [Atlas GitHub](https://github.com/example/atlas)
    """)

    def test_perfect_score_on_full_report(self):
        from atlas_plus.brain.research import score_research_report
        result = score_research_report(self._FULL_REPORT)
        # Citations, sections, and findings are all perfect; word count is low in this short fixture
        assert result["score"] >= 0.75
        assert result["factors"]["citations"] == 1.0
        assert result["factors"]["sections"] == 1.0
        assert result["factors"]["findings"] == 1.0

    def test_zero_score_on_empty_report(self):
        from atlas_plus.brain.research import score_research_report
        result = score_research_report("")
        assert result["score"] == 0.0

    def test_partial_score_single_finding_no_citations(self):
        from atlas_plus.brain.research import score_research_report
        text = (
            "## Summary\nBrief overview.\n\n"
            "## Key Findings\n- One finding.\n\n"
            "## Context\nSome context.\n\n"
            "## Gaps and Open Questions\nSome gaps.\n\n"
            "## Recommendations\nAct on this."
        )
        result = score_research_report(text)
        assert result["factors"]["citations"] == 0.0
        assert result["factors"]["findings"] == 0.5
        assert 0.3 < result["score"] < 0.9

    def test_raw_counts_match_content(self):
        from atlas_plus.brain.research import score_research_report
        result = score_research_report(self._FULL_REPORT)
        assert result["raw"]["citation_count"] == 3
        assert result["raw"]["findings_count"] == 3
        assert result["raw"]["section_count"] == 5

    def test_returns_score_between_zero_and_one(self):
        from atlas_plus.brain.research import score_research_report
        for text in ["", "Hello world", self._FULL_REPORT]:
            r = score_research_report(text)
            assert 0.0 <= r["score"] <= 1.0


# ---------------------------------------------------------------------------
# inject_quality_score
# ---------------------------------------------------------------------------

class TestInjectQualityScore:
    def _write_report(self, tmp_path: Path, body: str) -> Path:
        p = tmp_path / "report.md"
        p.write_text(f"---\ntitle: Test\ndate: 2026-05-20\n---\n\n{body}\n", encoding="utf-8")
        return p

    def test_injects_quality_score_frontmatter(self, tmp_path):
        from atlas_plus.brain.research import inject_quality_score
        body = (
            "## Summary\nAtlas works.\n\n"
            "## Key Findings\n- Point 1.\n- Point 2.\n- Point 3.\n\n"
            "## Context\nContext info.\n\n"
            "## Gaps and Open Questions\nOpen questions.\n\n"
            "## Recommendations\nRecommendations.\n\n"
            "## References\n- [RFC 3261](https://tools.ietf.org/html/rfc3261)\n"
        )
        p = self._write_report(tmp_path, body)
        result = inject_quality_score(p)
        assert isinstance(result, dict)
        assert "score" in result
        content = p.read_text(encoding="utf-8")
        assert "quality_score:" in content
        assert "quality_factors:" in content

    def test_returns_empty_dict_for_missing_file(self, tmp_path):
        from atlas_plus.brain.research import inject_quality_score
        assert inject_quality_score(tmp_path / "missing.md") == {}

    def test_returns_empty_dict_when_no_frontmatter(self, tmp_path):
        from atlas_plus.brain.research import inject_quality_score
        p = tmp_path / "nofm.md"
        p.write_text("No frontmatter. https://example.com/page\n", encoding="utf-8")
        assert inject_quality_score(p) == {}

    def test_overwrites_existing_quality_score(self, tmp_path):
        from atlas_plus.brain.research import inject_quality_score
        body = "## Summary\nBrief.\n\n## Key Findings\n- One.\n"
        p = tmp_path / "report.md"
        p.write_text(
            "---\ntitle: Test\nquality_score: 0.999\n---\n\n" + body,
            encoding="utf-8",
        )
        result = inject_quality_score(p)
        content = p.read_text(encoding="utf-8")
        # Old score should be gone; new score should differ from 0.999
        assert "quality_score: 0.999" not in content
        assert f"quality_score: {result['score']}" in content


# ---------------------------------------------------------------------------
# quality CLI command
# ---------------------------------------------------------------------------

class TestQualityCLI:
    def _write_report(self, d: Path, name: str, body: str) -> Path:
        p = d / name
        p.write_text(f"---\ntitle: {name}\ndate: 2026-05-20\n---\n\n{body}\n", encoding="utf-8")
        return p

    def test_scores_reports_without_writing(self, tmp_path):
        from typer.testing import CliRunner
        from atlas_plus.curation.cli import app

        rdir = tmp_path / "reports"
        rdir.mkdir()
        body = (
            "## Summary\nAtlas.\n\n## Key Findings\n- One.\n- Two.\n- Three.\n\n"
            "## Context\nCtx.\n\n## Gaps and Open Questions\nGaps.\n\n"
            "## Recommendations\nRecs.\n"
        )
        self._write_report(rdir, "report.md", body)
        original = (rdir / "report.md").read_text()

        runner = CliRunner()
        result = runner.invoke(app, ["quality", "--dir", str(rdir)])
        assert result.exit_code == 0
        # No write flag: frontmatter should not have quality_score
        assert (rdir / "report.md").read_text() == original

    def test_writes_quality_score_when_flag_set(self, tmp_path):
        from typer.testing import CliRunner
        from atlas_plus.curation.cli import app

        rdir = tmp_path / "reports"
        rdir.mkdir()
        self._write_report(rdir, "report.md", "## Summary\nAtlas works.\n")

        runner = CliRunner()
        result = runner.invoke(app, ["quality", "--dir", str(rdir), "--write"])
        assert result.exit_code == 0
        content = (rdir / "report.md").read_text()
        assert "quality_score:" in content

    def test_json_output_contains_expected_keys(self, tmp_path):
        import json
        from typer.testing import CliRunner
        from atlas_plus.curation.cli import app

        rdir = tmp_path / "reports"
        rdir.mkdir()
        self._write_report(rdir, "report.md", "## Summary\nAtlas.\n")

        runner = CliRunner()
        result = runner.invoke(app, ["quality", "--dir", str(rdir), "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output.strip())
        assert len(data) == 1
        assert "score" in data[0]
        assert "factors" in data[0]
        assert "raw" in data[0]

    def test_missing_directory_exits_cleanly(self, tmp_path):
        from typer.testing import CliRunner
        from atlas_plus.curation.cli import app

        runner = CliRunner()
        result = runner.invoke(app, ["quality", "--dir", str(tmp_path / "nosuchdir")])
        assert result.exit_code == 0
        assert "not found" in result.output.lower()
