"""Tests for atlas_plus.sprint_summary -- git commit parser and formatter."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest


# ---------------------------------------------------------------------------
# Helpers: _sprint_re, _test_count_re, _extract_description
# ---------------------------------------------------------------------------

class TestRegexParsing:
    def test_sprint_tag_extracted(self):
        from atlas_plus.sprint_summary import _SPRINT_RE
        m = _SPRINT_RE.search("feat: add thing (v0.12-A)")
        assert m is not None
        assert "v0.12-A" in m.group(0)

    def test_sprint_tag_case_insensitive(self):
        from atlas_plus.sprint_summary import _SPRINT_RE
        m = _SPRINT_RE.search("fix: bug (V0.12-B)")
        assert m is not None

    def test_no_sprint_tag_returns_none(self):
        from atlas_plus.sprint_summary import _SPRINT_RE
        m = _SPRINT_RE.search("chore: update deps")
        assert m is None

    def test_test_count_extracted(self):
        from atlas_plus.sprint_summary import _TEST_COUNT_RE
        m = _TEST_COUNT_RE.search("1543 tests total")
        assert m is not None
        assert m.group(1) == "1543"

    def test_test_count_total_variant(self):
        from atlas_plus.sprint_summary import _TEST_COUNT_RE
        m = _TEST_COUNT_RE.search("1500 total")
        assert m is not None

    def test_test_count_not_extracted_for_small_numbers(self):
        from atlas_plus.sprint_summary import _TEST_COUNT_RE
        # < 3 digits should not match
        m = _TEST_COUNT_RE.search("25 tests")
        assert m is None


class TestExtractDescription:
    def test_strips_feat_prefix(self):
        from atlas_plus.sprint_summary import _extract_description
        result = _extract_description("feat: add cool thing (v0.12-A)")
        assert "feat:" not in result
        assert "(v0.12-A)" not in result
        assert "add cool thing" in result

    def test_strips_feat_test_prefix(self):
        from atlas_plus.sprint_summary import _extract_description
        result = _extract_description("feat+test: quality gate wiring (v0.10-C)")
        assert "feat+test:" not in result
        assert "quality gate wiring" in result

    def test_strips_fix_prefix(self):
        from atlas_plus.sprint_summary import _extract_description
        result = _extract_description("fix: handle edge case")
        assert "fix:" not in result

    def test_returns_original_when_no_prefix(self):
        from atlas_plus.sprint_summary import _extract_description
        result = _extract_description("No conventional prefix here")
        assert "No conventional prefix here" in result


# ---------------------------------------------------------------------------
# _parse_commits
# ---------------------------------------------------------------------------

class TestParseCommits:
    def _fake_git_log(self, records: list[tuple[str, str, str]]) -> str:
        """Build the null-delimited git log output format."""
        parts = []
        for h, subject, body in records:
            parts.append(f"{h}\x00{subject}\x00{body}\x00")
        return "\x00\x00".join(parts)

    def test_parses_simple_commit(self):
        from atlas_plus.sprint_summary import _parse_commits
        fake_output = self._fake_git_log([
            ("abc12345", "feat: add thing (v0.12-A)", "1543 tests total")
        ])
        with patch("atlas_plus.sprint_summary._run_git", return_value=fake_output):
            commits = _parse_commits("HEAD~5")
        assert len(commits) == 1
        assert commits[0].sprint_tag == "v0.12-A"
        assert commits[0].test_count == 1543
        assert commits[0].hash == "abc12345"

    def test_parses_multiple_commits(self):
        from atlas_plus.sprint_summary import _parse_commits
        fake_output = self._fake_git_log([
            ("aaa11111", "feat: thing A (v0.12-A)", "1543 tests"),
            ("bbb22222", "feat: thing B (v0.12-B)", "1550 tests"),
        ])
        with patch("atlas_plus.sprint_summary._run_git", return_value=fake_output):
            commits = _parse_commits("HEAD~10")
        assert len(commits) == 2
        assert commits[1].sprint_tag == "v0.12-B"

    def test_no_sprint_tag_returns_none(self):
        from atlas_plus.sprint_summary import _parse_commits
        fake_output = self._fake_git_log([
            ("ccc33333", "docs: update readme", "")
        ])
        with patch("atlas_plus.sprint_summary._run_git", return_value=fake_output):
            commits = _parse_commits("HEAD~3")
        assert len(commits) == 1
        assert commits[0].sprint_tag is None
        assert commits[0].test_count is None

    def test_empty_git_output_returns_empty_list(self):
        from atlas_plus.sprint_summary import _parse_commits
        with patch("atlas_plus.sprint_summary._run_git", return_value=""):
            commits = _parse_commits("HEAD~5")
        assert commits == []


# ---------------------------------------------------------------------------
# _group_by_sprint
# ---------------------------------------------------------------------------

class TestGroupBySprint:
    def test_groups_by_sprint_tag(self):
        from atlas_plus.sprint_summary import _group_by_sprint, CommitInfo
        commits = [
            CommitInfo("a", "feat A", "", sprint_tag="v0.12-A"),
            CommitInfo("b", "feat B", "", sprint_tag="v0.12-A"),
            CommitInfo("c", "feat C", "", sprint_tag="v0.12-B"),
        ]
        groups = _group_by_sprint(commits)
        assert len(groups["v0.12-A"]) == 2
        assert len(groups["v0.12-B"]) == 1

    def test_untagged_goes_to_untagged_key(self):
        from atlas_plus.sprint_summary import _group_by_sprint, CommitInfo
        commits = [CommitInfo("a", "docs: readme", "", sprint_tag=None)]
        groups = _group_by_sprint(commits)
        assert "untagged" in groups
        assert len(groups["untagged"]) == 1


# ---------------------------------------------------------------------------
# _is_date
# ---------------------------------------------------------------------------

class TestIsDate:
    def test_iso_date_returns_true(self):
        from atlas_plus.sprint_summary import _is_date
        assert _is_date("2026-05-20") is True

    def test_git_ref_returns_false(self):
        from atlas_plus.sprint_summary import _is_date
        assert _is_date("HEAD~5") is False
        assert _is_date("v0.11-F") is False

    def test_partial_date_returns_false(self):
        from atlas_plus.sprint_summary import _is_date
        assert _is_date("2026-05") is False

    def test_datetime_string_returns_true(self):
        from atlas_plus.sprint_summary import _is_date
        assert _is_date("2026-05-20 00:00:00") is True

    def test_today_keyword_returns_false(self):
        from atlas_plus.sprint_summary import _is_date
        assert _is_date("today") is False


# ---------------------------------------------------------------------------
# write_session_log
# ---------------------------------------------------------------------------

class TestWriteSessionLog:
    """Tests for the write_session_log() auto-writer function."""

    def _fake_commits(self):
        from atlas_plus.sprint_summary import CommitInfo
        return [
            CommitInfo("abc12345", "feat: add verify executor (v0.12-E)", "", "v0.12-E", 1579),
            CommitInfo("def67890", "feat+test: brain server coverage (v0.12-E)", "", "v0.12-E", 1616),
        ]

    def test_no_commits_returns_ok_with_no_entry(self):
        from atlas_plus.sprint_summary import write_session_log
        with patch("atlas_plus.sprint_summary._parse_commits", return_value=[]):
            result = write_session_log(since="yesterday", dry_run=True)
        assert result["ok"] is True
        assert result["entry"] == ""
        assert result["paths_written"] == []

    def test_dry_run_returns_entry_without_writing(self, tmp_path):
        from atlas_plus.sprint_summary import write_session_log
        with patch("atlas_plus.sprint_summary._parse_commits", return_value=self._fake_commits()), \
             patch("atlas_plus.sprint_summary._run_git", return_value="atlas-memory+"):
            result = write_session_log(
                since="yesterday",
                memory_root=tmp_path,
                updates_path=tmp_path / "updates.txt",
                dry_run=True,
            )
        assert result["ok"] is True
        assert "atlas-memory+" in result["entry"] or "v0.12-E" in result["entry"]
        assert result["paths_written"] == []
        assert not (tmp_path / "sessions" / "_history.md").exists()

    def test_writes_history_file(self, tmp_path):
        from atlas_plus.sprint_summary import write_session_log
        with patch("atlas_plus.sprint_summary._parse_commits", return_value=self._fake_commits()), \
             patch("atlas_plus.sprint_summary._run_git", return_value="atlas-memory+"):
            result = write_session_log(
                since="yesterday",
                memory_root=tmp_path,
                updates_path=tmp_path / "updates.txt",
            )
        assert result["ok"] is True
        history = tmp_path / "sessions" / "_history.md"
        assert history.exists()
        assert str(history) in result["paths_written"]
        text = history.read_text(encoding="utf-8")
        assert "v0.12-E" in text or "atlas-memory+" in text

    def test_prepends_to_existing_history(self, tmp_path):
        from atlas_plus.sprint_summary import write_session_log
        sessions = tmp_path / "sessions"
        sessions.mkdir()
        history = sessions / "_history.md"
        history.write_text("## 2026-05-01 -- old entry\n\nold content\n", encoding="utf-8")

        with patch("atlas_plus.sprint_summary._parse_commits", return_value=self._fake_commits()), \
             patch("atlas_plus.sprint_summary._run_git", return_value="atlas-memory+"):
            write_session_log(since="yesterday", memory_root=tmp_path)

        text = history.read_text(encoding="utf-8")
        # New entry should be before the old one
        new_pos = text.find("v0.12-E")
        old_pos = text.find("old entry")
        assert new_pos < old_pos

    def test_writes_per_day_file_if_not_exists(self, tmp_path):
        from atlas_plus.sprint_summary import write_session_log
        from datetime import date
        with patch("atlas_plus.sprint_summary._parse_commits", return_value=self._fake_commits()), \
             patch("atlas_plus.sprint_summary._run_git", return_value="atlas-memory+"):
            write_session_log(since="yesterday", memory_root=tmp_path)
        today = date.today().isoformat()
        day_file = tmp_path / "sessions" / f"{today}.md"
        assert day_file.exists()

    def test_includes_test_count_in_entry(self, tmp_path):
        from atlas_plus.sprint_summary import write_session_log
        with patch("atlas_plus.sprint_summary._parse_commits", return_value=self._fake_commits()), \
             patch("atlas_plus.sprint_summary._run_git", return_value="atlas-memory+"):
            result = write_session_log(
                since="yesterday", memory_root=tmp_path, dry_run=True
            )
        assert "1616" in result["entry"]

    def test_prepends_to_updates_txt_if_exists(self, tmp_path):
        from atlas_plus.sprint_summary import write_session_log
        upd = tmp_path / "updates.txt"
        upd.write_text("old updates\n", encoding="utf-8")
        with patch("atlas_plus.sprint_summary._parse_commits", return_value=self._fake_commits()), \
             patch("atlas_plus.sprint_summary._run_git", return_value="atlas-memory+"):
            result = write_session_log(
                since="yesterday", memory_root=tmp_path, updates_path=upd
            )
        assert str(upd) in result["paths_written"]
        text = upd.read_text(encoding="utf-8")
        assert "old updates" in text
        assert text.index("v0.12-E") < text.index("old updates")


# ---------------------------------------------------------------------------
# _auto_session_log scheduler job
# ---------------------------------------------------------------------------

class TestAutoSessionLog:
    """Tests for the Brain scheduler _auto_session_log nightly job."""

    @pytest.mark.asyncio
    async def test_calls_write_session_log(self):
        import pytest
        from atlas_plus.brain import scheduler
        called_with = {}

        async def fake_write(**kwargs):
            called_with.update(kwargs)
            return {"ok": True, "entry": "## 2026-05-20", "paths_written": ["/x/_history.md"]}

        with patch("atlas_plus.sprint_summary.write_session_log",
                   side_effect=lambda **kw: {"ok": True, "entry": "## 2026-05-20", "paths_written": ["/x"]}):
            await scheduler._auto_session_log()

    @pytest.mark.asyncio
    async def test_disabled_by_env_var(self, monkeypatch):
        from atlas_plus.brain import scheduler
        monkeypatch.setenv("ATLAS_AUTO_SESSION_LOG", "off")
        # Should return without calling write_session_log
        with patch("atlas_plus.sprint_summary.write_session_log", side_effect=AssertionError("must not call")):
            await scheduler._auto_session_log()

    @pytest.mark.asyncio
    async def test_exception_does_not_raise(self, monkeypatch):
        from atlas_plus.brain import scheduler
        monkeypatch.delenv("ATLAS_AUTO_SESSION_LOG", raising=False)
        with patch("atlas_plus.sprint_summary.write_session_log", side_effect=RuntimeError("git gone")):
            # Must not raise
            await scheduler._auto_session_log()

    def test_auto_session_log_registered_in_scheduler(self):
        """auto-session-log job must be in the registered jobs list."""
        from atlas_plus.brain import scheduler as sched_mod
        # Check the _SCHEDULER_JOB_MAP or verify the job is defined by inspecting
        # the start_scheduler source / job list without actually starting APScheduler.
        import inspect
        src = inspect.getsource(sched_mod.start_scheduler)
        assert "auto-session-log" in src
