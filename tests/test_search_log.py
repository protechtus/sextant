"""Unit tests for mcp/memory_server/tools/search_log.py.

Covers: log_search (NDJSON write, miss detection, silent error swallowing),
load_log (empty when missing, time-window filtering, malformed-line tolerance),
and top_miss_queries (grouping, sorting, miss-only filter).
All tests use a temporary log file; the real .atlas/ is never touched.
"""
from __future__ import annotations

import json
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _ago(hours: float) -> datetime:
    return _now() - timedelta(hours=hours)


# ---------------------------------------------------------------------------
# log_search
# ---------------------------------------------------------------------------

class TestLogSearch:
    def test_creates_ndjson_record(self, tmp_path):
        from atlas_plus.mcp.memory_server.tools.search_log import log_search
        log_file = tmp_path / "logs" / "mcp-search.ndjson"

        with patch("atlas_plus.mcp.memory_server.tools.search_log.LOG_FILE", log_file):
            log_search("atlas brain", "", 0.85, 3)

        assert log_file.exists()
        record = json.loads(log_file.read_text())
        assert record["query"] == "atlas brain"
        assert record["result_count"] == 3
        assert record["miss"] is False

    def test_marks_miss_when_result_count_zero(self, tmp_path):
        from atlas_plus.mcp.memory_server.tools.search_log import log_search
        log_file = tmp_path / "logs" / "mcp-search.ndjson"

        with patch("atlas_plus.mcp.memory_server.tools.search_log.LOG_FILE", log_file):
            log_search("unknown topic", "", 0.0, 0)

        record = json.loads(log_file.read_text())
        assert record["miss"] is True

    def test_marks_miss_when_score_below_threshold(self, tmp_path):
        from atlas_plus.mcp.memory_server.tools.search_log import log_search
        log_file = tmp_path / "logs" / "mcp-search.ndjson"

        with patch("atlas_plus.mcp.memory_server.tools.search_log.LOG_FILE", log_file):
            # Low score even with results
            log_search("weak match", "", 0.1, 2)

        record = json.loads(log_file.read_text())
        assert record["miss"] is True

    def test_appends_multiple_records(self, tmp_path):
        from atlas_plus.mcp.memory_server.tools.search_log import log_search
        log_file = tmp_path / "logs" / "mcp-search.ndjson"

        with patch("atlas_plus.mcp.memory_server.tools.search_log.LOG_FILE", log_file):
            log_search("query one", "", 0.9, 5)
            log_search("query two", "feedback", 0.0, 0)

        lines = [l for l in log_file.read_text().splitlines() if l.strip()]
        assert len(lines) == 2

    def test_swallows_write_error_silently(self, tmp_path):
        from atlas_plus.mcp.memory_server.tools.search_log import log_search

        # Point log file to a path inside a non-writable location (simulate by patching open)
        with patch("builtins.open", side_effect=PermissionError("no write")):
            with patch("atlas_plus.mcp.memory_server.tools.search_log.LOG_FILE",
                       tmp_path / "logs" / "mcp-search.ndjson"):
                log_search("test", "", 0.5, 1)  # should not raise

    def test_stores_scope_and_caller(self, tmp_path):
        from atlas_plus.mcp.memory_server.tools.search_log import log_search
        log_file = tmp_path / "logs" / "mcp-search.ndjson"

        with patch("atlas_plus.mcp.memory_server.tools.search_log.LOG_FILE", log_file):
            log_search("query", "reference", 0.6, 2, caller_identity="telegram")

        record = json.loads(log_file.read_text())
        assert record["scope"] == "reference"
        assert record["caller_identity"] == "telegram"


# ---------------------------------------------------------------------------
# load_log
# ---------------------------------------------------------------------------

class TestLoadLog:
    def test_returns_empty_when_file_missing(self, tmp_path):
        from atlas_plus.mcp.memory_server.tools.search_log import load_log
        missing = tmp_path / "mcp-search.ndjson"

        with patch("atlas_plus.mcp.memory_server.tools.search_log.LOG_FILE", missing):
            result = load_log()

        assert result == []

    def test_returns_recent_records(self, tmp_path):
        from atlas_plus.mcp.memory_server.tools.search_log import load_log
        log_file = tmp_path / "mcp-search.ndjson"

        recent = _now() - timedelta(hours=1)
        record = {"ts": _iso(recent), "query": "recent", "scope": "", "top_score": 0.9,
                  "result_count": 1, "miss": False, "caller_identity": "mcp"}
        log_file.write_text(json.dumps(record) + "\n", encoding="utf-8")

        with patch("atlas_plus.mcp.memory_server.tools.search_log.LOG_FILE", log_file):
            result = load_log(since_hours=24)

        assert len(result) == 1
        assert result[0]["query"] == "recent"

    def test_filters_old_records(self, tmp_path):
        from atlas_plus.mcp.memory_server.tools.search_log import load_log
        log_file = tmp_path / "mcp-search.ndjson"

        old = _now() - timedelta(hours=200)
        record = {"ts": _iso(old), "query": "old", "scope": "", "top_score": 0.9,
                  "result_count": 1, "miss": False, "caller_identity": "mcp"}
        log_file.write_text(json.dumps(record) + "\n", encoding="utf-8")

        with patch("atlas_plus.mcp.memory_server.tools.search_log.LOG_FILE", log_file):
            result = load_log(since_hours=24)

        assert result == []

    def test_skips_malformed_lines(self, tmp_path):
        from atlas_plus.mcp.memory_server.tools.search_log import load_log
        log_file = tmp_path / "mcp-search.ndjson"

        recent = _now() - timedelta(hours=1)
        good = json.dumps({"ts": _iso(recent), "query": "good", "scope": "",
                           "top_score": 0.8, "result_count": 2, "miss": False,
                           "caller_identity": "mcp"})
        log_file.write_text("not json at all\n" + good + "\n{broken\n", encoding="utf-8")

        with patch("atlas_plus.mcp.memory_server.tools.search_log.LOG_FILE", log_file):
            result = load_log(since_hours=24)

        assert len(result) == 1
        assert result[0]["query"] == "good"

    def test_returns_all_within_window(self, tmp_path):
        from atlas_plus.mcp.memory_server.tools.search_log import load_log
        log_file = tmp_path / "mcp-search.ndjson"

        lines = []
        for i in range(5):
            ts = _now() - timedelta(hours=i * 10)
            r = {"ts": _iso(ts), "query": f"query-{i}", "scope": "", "top_score": 0.7,
                 "result_count": 1, "miss": False, "caller_identity": "mcp"}
            lines.append(json.dumps(r))
        log_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

        with patch("atlas_plus.mcp.memory_server.tools.search_log.LOG_FILE", log_file):
            # Only first 3 are within 25h window (0h, 10h, 20h)
            result = load_log(since_hours=25)

        assert len(result) == 3


# ---------------------------------------------------------------------------
# top_miss_queries
# ---------------------------------------------------------------------------

class TestTopMissQueries:
    def _write_log(self, log_file: Path, entries: list[dict]) -> None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        lines = []
        for e in entries:
            r = {
                "ts": e.get("ts", _iso(_now() - timedelta(hours=1))),
                "query": e["query"],
                "scope": e.get("scope", ""),
                "top_score": e.get("top_score", 0.0),
                "result_count": e.get("result_count", 0),
                "miss": e.get("miss", True),
                "caller_identity": "mcp",
            }
            lines.append(json.dumps(r))
        log_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def test_empty_log_returns_empty(self, tmp_path):
        from atlas_plus.mcp.memory_server.tools.search_log import top_miss_queries
        log_file = tmp_path / "mcp-search.ndjson"

        with patch("atlas_plus.mcp.memory_server.tools.search_log.LOG_FILE", log_file):
            result = top_miss_queries()

        assert result == []

    def test_groups_same_query_lowercase(self, tmp_path):
        from atlas_plus.mcp.memory_server.tools.search_log import top_miss_queries
        log_file = tmp_path / "mcp-search.ndjson"

        entries = [
            {"query": "SIP Registration", "miss": True},
            {"query": "sip registration", "miss": True},
            {"query": "SIP REGISTRATION", "miss": True},
        ]
        self._write_log(log_file, entries)

        with patch("atlas_plus.mcp.memory_server.tools.search_log.LOG_FILE", log_file):
            result = top_miss_queries()

        assert len(result) == 1
        assert result[0]["miss_count"] == 3

    def test_sorts_by_miss_count_descending(self, tmp_path):
        from atlas_plus.mcp.memory_server.tools.search_log import top_miss_queries
        log_file = tmp_path / "mcp-search.ndjson"

        entries = [
            {"query": "rare query", "miss": True},
            {"query": "common query", "miss": True},
            {"query": "common query", "miss": True},
            {"query": "common query", "miss": True},
        ]
        self._write_log(log_file, entries)

        with patch("atlas_plus.mcp.memory_server.tools.search_log.LOG_FILE", log_file):
            result = top_miss_queries()

        assert result[0]["query"].lower() == "common query"
        assert result[0]["miss_count"] == 3

    def test_excludes_queries_with_zero_misses(self, tmp_path):
        from atlas_plus.mcp.memory_server.tools.search_log import top_miss_queries
        log_file = tmp_path / "mcp-search.ndjson"

        entries = [
            {"query": "good hit", "miss": False, "result_count": 3, "top_score": 0.9},
            {"query": "miss query", "miss": True},
        ]
        self._write_log(log_file, entries)

        with patch("atlas_plus.mcp.memory_server.tools.search_log.LOG_FILE", log_file):
            result = top_miss_queries()

        queries = [r["query"].lower() for r in result]
        assert "good hit" not in queries
        assert "miss query" in queries

    def test_respects_limit_parameter(self, tmp_path):
        from atlas_plus.mcp.memory_server.tools.search_log import top_miss_queries
        log_file = tmp_path / "mcp-search.ndjson"

        entries = [{"query": f"query-{i}", "miss": True} for i in range(10)]
        self._write_log(log_file, entries)

        with patch("atlas_plus.mcp.memory_server.tools.search_log.LOG_FILE", log_file):
            result = top_miss_queries(limit=3)

        assert len(result) == 3


class TestCrashTolerance:
    """load_log's per-line try/except is load-bearing: NDJSON logs get torn lines when the
    machine crashes mid-append (this host has a history of hard resets). A refactor that
    'simplifies' to whole-file parsing would silently break the miss-analysis pipeline
    (top_miss_queries -> next-actions gaps) after any crash. Pin the tolerance."""

    def test_torn_line_is_skipped_valid_lines_survive(self, tmp_path):
        import json
        from unittest.mock import patch
        from datetime import datetime, timezone
        from atlas_plus.mcp.memory_server.tools.search_log import load_log

        log_file = tmp_path / "mcp-search.ndjson"
        now = datetime.now(timezone.utc).isoformat()
        good = json.dumps({"ts": now, "query": "alpha", "miss": True})
        torn = '{"ts": "' + now + '", "query": "bet'   # crash mid-append
        log_file.write_text(good + "\n" + torn + "\n" + good + "\n", encoding="utf-8")

        with patch("atlas_plus.mcp.memory_server.tools.search_log.LOG_FILE", log_file):
            records = load_log()
        assert len(records) == 2
        assert all(r["query"] == "alpha" for r in records)

    def test_record_without_ts_is_skipped(self, tmp_path):
        import json
        from unittest.mock import patch
        from datetime import datetime, timezone
        from atlas_plus.mcp.memory_server.tools.search_log import load_log

        log_file = tmp_path / "mcp-search.ndjson"
        now = datetime.now(timezone.utc).isoformat()
        no_ts = json.dumps({"query": "orphan-record", "miss": True})
        good = json.dumps({"ts": now, "query": "alpha", "miss": False})
        log_file.write_text(no_ts + "\n" + good + "\n", encoding="utf-8")

        with patch("atlas_plus.mcp.memory_server.tools.search_log.LOG_FILE", log_file):
            records = load_log()
        assert len(records) == 1
        assert records[0]["query"] == "alpha"
