"""Tests for atlas_plus.curation.link_check -- URL validation for research reports.

Covers _should_skip, check_url (mocked), check_report_links, remove_broken_links.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# TestShouldSkip
# ---------------------------------------------------------------------------

class TestShouldSkip:
    def test_skips_doi_domain(self):
        from atlas_plus.curation.link_check import _should_skip
        assert _should_skip("https://doi.org/10.1234/something") is True

    def test_skips_arxiv_domain(self):
        from atlas_plus.curation.link_check import _should_skip
        assert _should_skip("https://arxiv.org/abs/2404.12345") is True

    def test_skips_linkedin(self):
        from atlas_plus.curation.link_check import _should_skip
        assert _should_skip("https://www.linkedin.com/in/johndoe") is True

    def test_does_not_skip_allowed_domains(self):
        from atlas_plus.curation.link_check import _should_skip
        assert _should_skip("https://example.com/some-page") is False
        assert _should_skip("https://python.org/docs") is False
        assert _should_skip("https://github.com/repo") is False

    def test_skips_invalid_url(self):
        from atlas_plus.curation.link_check import _should_skip
        assert _should_skip("not-a-url") is True
        assert _should_skip("") is True

    def test_skips_subdomain_of_blocked_domain(self):
        from atlas_plus.curation.link_check import _should_skip
        # e.g. journals.springer.com should be skipped
        assert _should_skip("https://link.springer.com/article/123") is True


# ---------------------------------------------------------------------------
# TestCheckUrl
# ---------------------------------------------------------------------------

class TestCheckUrl:
    def test_returns_skipped_for_blocked_domain(self):
        from atlas_plus.curation.link_check import check_url
        result = check_url("https://doi.org/10.1234/test")
        assert result["skipped"] is True
        assert result["ok"] is True

    def test_returns_ok_for_200_response(self):
        from atlas_plus.curation.link_check import check_url
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.head.return_value = mock_resp
        with patch("httpx.Client", return_value=mock_client):
            result = check_url("https://example.com/page")
        assert result["ok"] is True
        assert result["status"] == 200
        assert result["gated"] is False

    def test_returns_not_ok_for_404_response(self):
        from atlas_plus.curation.link_check import check_url
        mock_resp = MagicMock()
        mock_resp.status_code = 404
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.head.return_value = mock_resp
        with patch("httpx.Client", return_value=mock_client):
            result = check_url("https://example.com/missing")
        assert result["ok"] is False
        assert result["status"] == 404

    def test_returns_gated_for_403_response(self):
        from atlas_plus.curation.link_check import check_url
        mock_resp = MagicMock()
        mock_resp.status_code = 403
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.head.return_value = mock_resp
        with patch("httpx.Client", return_value=mock_client):
            result = check_url("https://example.com/protected")
        assert result["gated"] is True
        assert result["ok"] is True

    def test_returns_error_on_connection_failure(self):
        from atlas_plus.curation.link_check import check_url
        import httpx
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.head.side_effect = Exception("Connection refused")
        with patch("httpx.Client", return_value=mock_client):
            result = check_url("https://example.com/page")
        assert result["ok"] is False
        assert result["error"] is not None


# ---------------------------------------------------------------------------
# TestCheckReportLinks
# ---------------------------------------------------------------------------

class TestCheckReportLinks:
    def test_returns_empty_for_missing_file(self, tmp_path):
        from atlas_plus.curation.link_check import check_report_links
        missing = tmp_path / "missing.md"
        assert check_report_links(missing) == []

    def test_returns_empty_for_no_frontmatter(self, tmp_path):
        from atlas_plus.curation.link_check import check_report_links
        p = tmp_path / "plain.md"
        p.write_text("# No frontmatter", encoding="utf-8")
        assert check_report_links(p) == []

    def test_returns_empty_when_no_references(self, tmp_path):
        from atlas_plus.curation.link_check import check_report_links
        p = tmp_path / "report.md"
        p.write_text("---\ntitle: Test\ntype: research\n---\n\nBody.", encoding="utf-8")
        assert check_report_links(p) == []

    def test_checks_references_urls(self, tmp_path):
        from atlas_plus.curation.link_check import check_report_links
        p = tmp_path / "report.md"
        p.write_text(
            "---\ntitle: Test\nreferences:\n  - https://example.com/a\n  - https://doi.org/123\n---\nBody.",
            encoding="utf-8",
        )
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.head.return_value = mock_resp
        with patch("httpx.Client", return_value=mock_client):
            results = check_report_links(p)
        # doi.org is skipped + example.com is checked
        assert len(results) >= 1


# ---------------------------------------------------------------------------
# TestRemoveBrokenLinks
# ---------------------------------------------------------------------------

class TestRemoveBrokenLinks:
    def test_returns_false_for_empty_broken_list(self, tmp_path):
        from atlas_plus.curation.link_check import remove_broken_links
        p = tmp_path / "report.md"
        p.write_text("---\ntitle: T\n---\nBody.", encoding="utf-8")
        assert remove_broken_links(p, []) is False

    def test_returns_false_for_missing_file(self, tmp_path):
        from atlas_plus.curation.link_check import remove_broken_links
        missing = tmp_path / "missing.md"
        assert remove_broken_links(missing, ["https://broken.com"]) is False

    def test_removes_broken_url_from_references(self, tmp_path):
        from atlas_plus.curation.link_check import remove_broken_links
        p = tmp_path / "report.md"
        p.write_text(
            "---\ntitle: Research\nreferences:\n  - https://good.com\n  - https://broken.com\n---\n\nBody.",
            encoding="utf-8",
        )
        result = remove_broken_links(p, ["https://broken.com"])
        assert result is True
        content = p.read_text(encoding="utf-8")
        assert "broken.com" not in content
        assert "good.com" in content

    def test_returns_false_when_url_not_in_references(self, tmp_path):
        from atlas_plus.curation.link_check import remove_broken_links
        p = tmp_path / "report.md"
        p.write_text(
            "---\ntitle: Research\nreferences:\n  - https://good.com\n---\n\nBody.",
            encoding="utf-8",
        )
        result = remove_broken_links(p, ["https://nonexistent.com"])
        assert result is False

    def test_preserves_body_content(self, tmp_path):
        from atlas_plus.curation.link_check import remove_broken_links
        p = tmp_path / "report.md"
        p.write_text(
            "---\ntitle: Research\nreferences:\n  - https://broken.com\n---\n\n# Section\nImportant text here.",
            encoding="utf-8",
        )
        remove_broken_links(p, ["https://broken.com"])
        content = p.read_text(encoding="utf-8")
        assert "Important text here" in content
