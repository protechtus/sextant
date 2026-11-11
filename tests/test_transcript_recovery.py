"""Tests for atlas_plus.transcripts.recovery -- partial file recovery and hash chain."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from atlas_plus.transcripts.store import TurnRecord, ZERO_HASH
from atlas_plus.transcripts.recovery import (
    RecoveryReport,
    recover_all,
    _validate_chain,
    _archive_torn_tail,
)


def _write_record(path: Path, rec: TurnRecord) -> None:
    """Append one TurnRecord line to a file in canonical JSON."""
    line = (rec.model_dump_json() + "\n").encode("utf-8")
    with open(path, "ab") as f:
        f.write(line)


def _make_chain(*contents: str, source: str = "test", conv_id: str = "c1") -> list[TurnRecord]:
    """Build a correctly chained sequence of TurnRecords."""
    records = []
    prev_hash = ZERO_HASH
    for i, content in enumerate(contents):
        rec = TurnRecord(
            turn_id=f"t-{i}",
            conversation_id=conv_id,
            source=source,
            role="user",
            content=content,
            ts=f"2026-05-21T03:0{i}:00+00:00",
            hash_prev=prev_hash,
        )
        rec.hash = rec.compute_hash()
        records.append(rec)
        prev_hash = rec.hash
    return records


# ---------------------------------------------------------------------------
# RecoveryReport
# ---------------------------------------------------------------------------

class TestRecoveryReport:
    def test_defaults_are_zero(self):
        r = RecoveryReport()
        assert r.partials_scanned == 0
        assert r.fully_recovered == 0
        assert r.torn_lines == 0
        assert r.errors == []

    def test_errors_is_mutable_list(self):
        r = RecoveryReport()
        r.errors.append("oops")
        assert r.errors == ["oops"]


# ---------------------------------------------------------------------------
# _validate_chain
# ---------------------------------------------------------------------------

class TestValidateChain:
    def test_valid_chain_returns_all_records(self, tmp_path):
        records = _make_chain("hello", "world", "sip")
        path = tmp_path / "conv.ndjson.partial"
        for rec in records:
            _write_record(path, rec)
        recovered, offset = _validate_chain(path)
        assert len(recovered) == 3
        assert offset == path.stat().st_size

    def test_broken_chain_stops_at_bad_line(self, tmp_path):
        records = _make_chain("good1", "good2")
        path = tmp_path / "conv.ndjson.partial"
        for rec in records:
            _write_record(path, rec)
        # Append a record with wrong hash_prev (chain break)
        bad = TurnRecord(
            turn_id="bad",
            conversation_id="c1",
            source="test",
            role="user",
            content="bad",
            hash_prev="deadbeef" * 8,  # wrong -- doesn't match records[-1].hash
        )
        bad.hash = bad.compute_hash()
        _write_record(path, bad)
        recovered, offset = _validate_chain(path)
        assert len(recovered) == 2  # only the good ones

    def test_empty_file_returns_empty(self, tmp_path):
        path = tmp_path / "empty.ndjson.partial"
        path.write_bytes(b"")
        recovered, offset = _validate_chain(path)
        assert recovered == []
        assert offset == 0

    def test_malformed_json_stops_scan(self, tmp_path):
        records = _make_chain("ok")
        path = tmp_path / "conv.ndjson.partial"
        for rec in records:
            _write_record(path, rec)
        # Append garbage
        with open(path, "ab") as f:
            f.write(b"NOT JSON\n")
        recovered, _ = _validate_chain(path)
        assert len(recovered) == 1


# ---------------------------------------------------------------------------
# _archive_torn_tail
# ---------------------------------------------------------------------------

class TestArchiveTornTail:
    def test_creates_archive_dir_and_torn_file(self, tmp_path):
        path = tmp_path / "conv.ndjson.partial"
        path.write_bytes(b"good\n" + b"torn_line\n")
        torn_count = _archive_torn_tail(path, last_good_offset=5)  # "good\n" = 5 bytes
        archive = tmp_path / "archive"
        assert archive.is_dir()
        torn_files = list(archive.glob("*.torn-*.ndjson"))
        assert len(torn_files) == 1
        assert torn_files[0].read_bytes() == b"torn_line\n"

    def test_truncates_partial_to_good_offset(self, tmp_path):
        path = tmp_path / "conv.ndjson.partial"
        path.write_bytes(b"good\n" + b"torn\n")
        _archive_torn_tail(path, last_good_offset=5)
        assert path.read_bytes() == b"good\n"

    def test_returns_torn_line_count(self, tmp_path):
        path = tmp_path / "conv.ndjson.partial"
        path.write_bytes(b"good\n" + b"line1\nline2\n")
        count = _archive_torn_tail(path, last_good_offset=5)
        assert count == 2

    def test_creates_readme_on_first_archive(self, tmp_path):
        path = tmp_path / "conv.ndjson.partial"
        path.write_bytes(b"good\ntorn\n")
        _archive_torn_tail(path, last_good_offset=5)
        assert (tmp_path / "archive" / "_README.md").exists()


# ---------------------------------------------------------------------------
# recover_all
# ---------------------------------------------------------------------------

class TestRecoverAll:
    def test_returns_empty_report_when_no_partials(self, tmp_path):
        store = MagicMock()
        store.list_partials.return_value = []
        index = MagicMock()
        report = recover_all(store=store, index=index)
        assert report.partials_scanned == 0

    def test_indexes_valid_turns(self, tmp_path):
        records = _make_chain("alpha", "beta")
        partial = tmp_path / "conv.ndjson.partial"
        for rec in records:
            _write_record(partial, rec)

        store = MagicMock()
        store.list_partials.return_value = [partial]
        index = MagicMock()

        report = recover_all(store=store, index=index)

        assert report.partials_scanned == 1
        assert report.indexed_turns == 2
        assert index.upsert_turn.call_count == 2

    def test_calls_rebroadcast_for_each_turn(self, tmp_path):
        records = _make_chain("hello", "world")
        partial = tmp_path / "conv.ndjson.partial"
        for rec in records:
            _write_record(partial, rec)

        store = MagicMock()
        store.list_partials.return_value = [partial]
        index = MagicMock()
        broadcast_calls = []
        report = recover_all(store=store, index=index, rebroadcast=broadcast_calls.append)

        assert report.rebroadcast_turns == 2
        assert len(broadcast_calls) == 2

    def test_archives_torn_tail_when_chain_breaks(self, tmp_path):
        records = _make_chain("good")
        partial = tmp_path / "conv.ndjson.partial"
        for rec in records:
            _write_record(partial, rec)
        with open(partial, "ab") as f:
            f.write(b"corrupt\n")

        store = MagicMock()
        store.list_partials.return_value = [partial]
        index = MagicMock()

        report = recover_all(store=store, index=index)

        assert report.truncated_partials == 1
        assert report.torn_lines > 0
        assert (tmp_path / "archive").is_dir()

    def test_continues_on_store_error(self, tmp_path):
        store = MagicMock()
        store.list_partials.return_value = [Path("/nonexistent/file.ndjson.partial")]
        index = MagicMock()

        report = recover_all(store=store, index=index)
        assert len(report.errors) > 0
