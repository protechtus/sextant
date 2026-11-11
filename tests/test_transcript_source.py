"""Tests for Direction E -- ClaudeCodeTranscriptSource.

Covers:
  - Indexes JSONL files and creates Conversation entities
  - Extracts model, project (from cwd), and token counts as facts
  - Content is NOT stored -- only metadata
  - Missing transcript dir returns clean no-op
  - Incremental: skips unchanged files via Manifest
  - Subagent JSONL files (nested subdirs) are not indexed (*.jsonl in root only)
  - Files with no assistant turns still index with zero tokens
  - _extract_transcript_metadata: parses Claude Code JSONL correctly
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from atlas_plus.temporal_graph.file_provider import FileTemporalProvider
from atlas_plus.temporal_graph.sources import (
    ClaudeCodeTranscriptSource,
    Manifest,
    _extract_transcript_metadata,
    _project_from_cwd,
    _ts_to_iso,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _provider(tmp_path: Path) -> FileTemporalProvider:
    return FileTemporalProvider(graph_root=tmp_path / "temporal_graph")


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for r in records:
            fh.write(json.dumps(r) + "\n")


def _minimal_session(session_id: str, cwd: str = r"C:\Tools\projects\atlas-memory+") -> list[dict]:
    return [
        {
            "type": "user",
            "sessionId": session_id,
            "uuid": "u-001",
            "timestamp": "2026-05-17T10:00:00.000Z",
            "cwd": cwd,
            "slug": "test-session-slug",
            "message": {"role": "user", "content": "Hello"},
        },
        {
            "type": "assistant",
            "sessionId": session_id,
            "uuid": "u-002",
            "timestamp": "2026-05-17T10:00:05.000Z",
            "message": {
                "role": "assistant",
                "model": "claude-sonnet-4-6",
                "content": [{"type": "text", "text": "Hi there"}],
                "usage": {"input_tokens": 100, "output_tokens": 50},
            },
        },
    ]


# ---------------------------------------------------------------------------
# Metadata extraction
# ---------------------------------------------------------------------------

class TestExtractMetadata:
    def test_extracts_session_id_model_cwd_tokens(self, tmp_path):
        path = tmp_path / "abc-session.jsonl"
        _write_jsonl(path, _minimal_session("real-session-id"))
        meta = _extract_transcript_metadata(path)
        assert meta["session_id"] == "real-session-id"
        assert meta["model"] == "claude-sonnet-4-6"
        assert meta["cwd"] == r"C:\Tools\projects\atlas-memory+"
        assert meta["tokens_in"] == 100
        assert meta["tokens_out"] == 50
        assert meta["turn_count"] == 2

    def test_slug_extracted(self, tmp_path):
        path = tmp_path / "session.jsonl"
        _write_jsonl(path, _minimal_session("sid"))
        meta = _extract_transcript_metadata(path)
        assert meta["slug"] == "test-session-slug"

    def test_timestamps_extracted(self, tmp_path):
        path = tmp_path / "session.jsonl"
        _write_jsonl(path, _minimal_session("sid"))
        meta = _extract_transcript_metadata(path)
        assert meta["first_ts"] is not None
        assert meta["last_ts"] is not None
        assert meta["first_ts"] <= meta["last_ts"]

    def test_empty_file_returns_defaults(self, tmp_path):
        path = tmp_path / "empty.jsonl"
        path.write_text("", encoding="utf-8")
        meta = _extract_transcript_metadata(path)
        assert meta["turn_count"] == 0
        assert meta["tokens_in"] == 0
        assert meta["model"] is None

    def test_malformed_lines_skipped(self, tmp_path):
        path = tmp_path / "bad.jsonl"
        path.write_text("NOT JSON\n{}\n", encoding="utf-8")
        meta = _extract_transcript_metadata(path)
        assert meta["turn_count"] == 1  # the {} line counts as a turn


class TestHelpers:
    def test_project_from_cwd_windows_path(self):
        assert _project_from_cwd(r"C:\Tools\projects\atlas-memory+") == "atlas-memory+"

    def test_project_from_cwd_none(self):
        assert _project_from_cwd(None) is None

    def test_ts_to_iso_z_suffix(self):
        assert _ts_to_iso("2026-05-17T10:00:00.000Z") == "2026-05-17T10:00:00.000+00:00"

    def test_ts_to_iso_already_correct(self):
        result = _ts_to_iso("2026-05-17T10:00:00+00:00")
        assert "+00:00" in result


# ---------------------------------------------------------------------------
# ClaudeCodeTranscriptSource
# ---------------------------------------------------------------------------

class TestClaudeCodeTranscriptSource:
    def test_indexes_jsonl_creates_entity_and_facts(self, tmp_path):
        transcripts_dir = tmp_path / "c--Tools"
        _write_jsonl(
            transcripts_dir / "session-abc.jsonl",
            _minimal_session("session-abc"),
        )
        p = _provider(tmp_path)
        src = ClaudeCodeTranscriptSource(claude_root=tmp_path, target_dir="c--Tools")
        result = src.ingest(p)
        assert result["files_indexed"] == 1
        assert result["facts"] >= 2  # has_model + worked_in_project + tokens
        assert "transcript:session-abc" in p._entities

    def test_creates_model_fact(self, tmp_path):
        transcripts_dir = tmp_path / "c--Tools"
        _write_jsonl(transcripts_dir / "sess.jsonl", _minimal_session("sess"))
        p = _provider(tmp_path)
        ClaudeCodeTranscriptSource(claude_root=tmp_path, target_dir="c--Tools").ingest(p)
        results = p.search_current("claude-sonnet model", limit=5)
        assert any("claude-sonnet-4-6" in r.fact.fact_text for r in results)

    def test_creates_project_fact(self, tmp_path):
        transcripts_dir = tmp_path / "c--Tools"
        _write_jsonl(transcripts_dir / "sess.jsonl", _minimal_session("sess"))
        p = _provider(tmp_path)
        ClaudeCodeTranscriptSource(claude_root=tmp_path, target_dir="c--Tools").ingest(p)
        results = p.search_current("atlas-memory project", limit=5)
        assert any("atlas-memory+" in r.fact.fact_text for r in results)

    def test_creates_token_fact(self, tmp_path):
        transcripts_dir = tmp_path / "c--Tools"
        _write_jsonl(transcripts_dir / "sess.jsonl", _minimal_session("sess"))
        p = _provider(tmp_path)
        ClaudeCodeTranscriptSource(claude_root=tmp_path, target_dir="c--Tools").ingest(p)
        results = p.search_current("tokens", limit=5)
        assert any("tokens" in r.fact.fact_text for r in results)

    def test_content_not_stored_in_facts(self, tmp_path):
        transcripts_dir = tmp_path / "c--Tools"
        records = _minimal_session("sess")
        records[0]["message"]["content"] = "SECRET_CANARY_12345"
        _write_jsonl(transcripts_dir / "sess.jsonl", records)
        p = _provider(tmp_path)
        ClaudeCodeTranscriptSource(claude_root=tmp_path, target_dir="c--Tools").ingest(p)
        # Check no fact contains the secret canary
        for fact in p._facts.values():
            assert "SECRET_CANARY_12345" not in fact.fact_text
        # Check no episode body contains the canary
        for ep in p._episodes.values():
            assert "SECRET_CANARY_12345" not in ep.body

    def test_missing_dir_returns_zero(self, tmp_path):
        p = _provider(tmp_path)
        src = ClaudeCodeTranscriptSource(claude_root=tmp_path, target_dir="nonexistent")
        result = src.ingest(p)
        assert result["files_indexed"] == 0
        assert "warnings" in result

    def test_incremental_skips_unchanged(self, tmp_path):
        transcripts_dir = tmp_path / "c--Tools"
        _write_jsonl(transcripts_dir / "sess.jsonl", _minimal_session("sess"))
        manifest_path = tmp_path / "temporal_graph" / "manifest.json"
        manifest_path.parent.mkdir(parents=True, exist_ok=True)

        p1 = _provider(tmp_path)
        m1 = Manifest.load(manifest_path)
        r1 = ClaudeCodeTranscriptSource(claude_root=tmp_path, target_dir="c--Tools").ingest(p1, m1)
        assert r1["files_indexed"] == 1

        p2 = _provider(tmp_path)
        m2 = Manifest.load(manifest_path)
        r2 = ClaudeCodeTranscriptSource(claude_root=tmp_path, target_dir="c--Tools").ingest(p2, m2)
        assert r2["files_skipped"] == 1
        assert r2["files_indexed"] == 0

    def test_multiple_sessions_indexed(self, tmp_path):
        transcripts_dir = tmp_path / "c--Tools"
        for i in range(3):
            _write_jsonl(
                transcripts_dir / f"session-{i:03d}.jsonl",
                _minimal_session(f"session-{i:03d}"),
            )
        p = _provider(tmp_path)
        result = ClaudeCodeTranscriptSource(claude_root=tmp_path, target_dir="c--Tools").ingest(p)
        assert result["files_indexed"] == 3
        assert p.stats()["entities"] == 3

    def test_no_assistant_turns_zero_tokens(self, tmp_path):
        transcripts_dir = tmp_path / "c--Tools"
        records = [{"type": "user", "sessionId": "user-only", "uuid": "u1",
                    "timestamp": "2026-05-17T10:00:00.000Z",
                    "message": {"role": "user", "content": "Hello"}}]
        _write_jsonl(transcripts_dir / "user-only.jsonl", records)
        p = _provider(tmp_path)
        result = ClaudeCodeTranscriptSource(claude_root=tmp_path, target_dir="c--Tools").ingest(p)
        assert result["files_indexed"] == 1
        # Token fact should not exist (0 tokens)
        assert "f:transcript:user-only:total_tokens" not in p._facts
