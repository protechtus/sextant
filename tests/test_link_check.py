"""Tests for research report link validation (curation/link_check.py)."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch


def _write_report(path: Path, refs: list[str] = None, body: str = "Body.\n") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    fm = '---\ntitle: "Test Report"\ndate: 2026-05-20\n'
    if refs:
        fm += "references:\n" + "".join(f"  - {r}\n" for r in refs)
    fm += "---\n\n" + body
    path.write_text(fm, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# _should_skip
# ---------------------------------------------------------------------------

class TestShouldSkip:
    def test_skips_arxiv(self):
        from atlas_plus.curation.link_check import _should_skip
        assert _should_skip("https://arxiv.org/abs/1234.5678")

    def test_skips_linkedin(self):
        from atlas_plus.curation.link_check import _should_skip
        assert _should_skip("https://www.linkedin.com/in/test")

    def test_does_not_skip_example(self):
        from atlas_plus.curation.link_check import _should_skip
        assert not _should_skip("https://example.com/page")

    def test_does_not_skip_docs(self):
        from atlas_plus.curation.link_check import _should_skip
        assert not _should_skip("https://docs.example.com/guide")

    def test_handles_malformed_url(self):
        from atlas_plus.curation.link_check import _should_skip
        assert _should_skip("not-a-url")


# ---------------------------------------------------------------------------
# check_url (mocked HTTP)
# ---------------------------------------------------------------------------

class TestCheckUrl:
    def _mock_response(self, status_code: int):
        r = MagicMock()
        r.status_code = status_code
        return r

    def test_200_returns_ok_true(self):
        from atlas_plus.curation.link_check import check_url
        with patch("httpx.Client") as mock_cls:
            mock_cls.return_value.__enter__.return_value.head.return_value = self._mock_response(200)
            result = check_url("https://example.com/page")
        assert result["ok"] is True
        assert result["status"] == 200

    def test_404_returns_ok_false(self):
        from atlas_plus.curation.link_check import check_url
        with patch("httpx.Client") as mock_cls:
            mock_cls.return_value.__enter__.return_value.head.return_value = self._mock_response(404)
            result = check_url("https://example.com/gone")
        assert result["ok"] is False
        assert result["status"] == 404

    def test_403_returns_gated(self):
        from atlas_plus.curation.link_check import check_url
        with patch("httpx.Client") as mock_cls:
            mock_cls.return_value.__enter__.return_value.head.return_value = self._mock_response(403)
            result = check_url("https://example.com/private")
        assert result["ok"] is True  # auth-gated, not dead
        assert result["gated"] is True

    def test_skipped_domain_returns_ok_without_http(self):
        from atlas_plus.curation.link_check import check_url
        with patch("httpx.Client") as mock_cls:
            result = check_url("https://arxiv.org/abs/1234.5678")
        # Should not make any HTTP request
        mock_cls.assert_not_called()
        assert result["ok"] is True
        assert result["skipped"] is True

    def test_connection_error_returns_ok_false(self):
        from atlas_plus.curation.link_check import check_url
        import httpx
        with patch("httpx.Client") as mock_cls:
            mock_cls.return_value.__enter__.return_value.head.side_effect = httpx.ConnectError("timeout")
            mock_cls.return_value.__enter__.return_value.get.side_effect = httpx.ConnectError("timeout")
            result = check_url("https://dead.example.invalid/page")
        assert result["ok"] is False
        assert result["error"] is not None


# ---------------------------------------------------------------------------
# check_report_links
# ---------------------------------------------------------------------------

class TestCheckReportLinks:
    def test_returns_empty_for_report_without_references(self, tmp_path):
        from atlas_plus.curation.link_check import check_report_links
        p = _write_report(tmp_path / "r.md")  # no refs
        assert check_report_links(p) == []

    def test_returns_empty_for_missing_file(self, tmp_path):
        from atlas_plus.curation.link_check import check_report_links
        assert check_report_links(tmp_path / "missing.md") == []

    def test_returns_result_per_url(self, tmp_path):
        from atlas_plus.curation.link_check import check_report_links
        p = _write_report(tmp_path / "r.md",
                          refs=["https://example.com/a", "https://arxiv.org/abs/x"])
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        with patch("httpx.Client") as mock_cls:
            mock_cls.return_value.__enter__.return_value.head.return_value = mock_resp
            results = check_report_links(p)
        # arxiv.org is skipped, example.com is checked
        assert len(results) == 2
        non_skipped = [r for r in results if not r.get("skipped")]
        assert len(non_skipped) == 1
        assert non_skipped[0]["url"] == "https://example.com/a"


# ---------------------------------------------------------------------------
# remove_broken_links
# ---------------------------------------------------------------------------

class TestRemoveBrokenLinks:
    def test_removes_specified_urls(self, tmp_path):
        from atlas_plus.curation.link_check import remove_broken_links
        p = _write_report(tmp_path / "r.md",
                          refs=["https://good.example.com", "https://dead.example.com"])
        result = remove_broken_links(p, ["https://dead.example.com"])
        assert result is True
        content = p.read_text(encoding="utf-8")
        assert "dead.example.com" not in content.split("---")[1]  # not in frontmatter
        assert "good.example.com" in content

    def test_returns_false_when_no_urls_to_remove(self, tmp_path):
        from atlas_plus.curation.link_check import remove_broken_links
        p = _write_report(tmp_path / "r.md",
                          refs=["https://good.example.com"])
        assert remove_broken_links(p, ["https://notpresent.com"]) is False

    def test_returns_false_for_empty_broken_list(self, tmp_path):
        from atlas_plus.curation.link_check import remove_broken_links
        p = _write_report(tmp_path / "r.md", refs=["https://good.com"])
        assert remove_broken_links(p, []) is False

    def test_returns_false_for_missing_file(self, tmp_path):
        from atlas_plus.curation.link_check import remove_broken_links
        assert remove_broken_links(tmp_path / "missing.md", ["https://x.com"]) is False

    def test_removes_references_block_when_all_broken(self, tmp_path):
        from atlas_plus.curation.link_check import remove_broken_links
        p = _write_report(tmp_path / "r.md", refs=["https://dead.example.com"])
        remove_broken_links(p, ["https://dead.example.com"])
        content = p.read_text(encoding="utf-8")
        end = content.find("\n---", 3)
        fm = content[3:end]
        assert "references:" not in fm
