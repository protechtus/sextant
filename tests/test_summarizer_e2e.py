"""P2-T9: Summarizer smoke tests — mocked LLM, deterministic pipeline."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from atlas_plus.transcripts.store import TurnRecord
from atlas_plus.summarizer.chunker import Chunk, chunk_turns, needs_reduce
from atlas_plus.summarizer.loader import _parse_since
from atlas_plus.summarizer.prompts import map_prompt, reduce_prompt
from atlas_plus.summarizer.cli import _parse_json_response


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _turn(role: str, content: str, ts: str = "2026-05-03T10:00:00+00:00") -> TurnRecord:
    return TurnRecord(
        conversation_id="conv-test",
        source="claude-code",
        role=role,
        content=content,
        ts=ts,
    )


def _turns(n: int, chars_each: int = 100) -> list[TurnRecord]:
    """Generate n alternating user/assistant turns."""
    from datetime import timedelta
    roles = ["user", "assistant"]
    base = datetime(2026, 5, 3, 10, 0, 0, tzinfo=timezone.utc)
    return [
        _turn(
            roles[i % 2],
            "x" * chars_each,
            (base + timedelta(minutes=i)).isoformat(),
        )
        for i in range(n)
    ]


_GOLDEN_MAP_RESPONSE = json.dumps({
    "worked_on": ["built the thing"],
    "completed": ["thing is done"],
    "pending": ["tests"],
    "files_touched": ["src/foo.py"],
    "lessons": ["always version before edit"],
})

_GOLDEN_REDUCE_RESPONSE = json.dumps({
    "title": "Built thing and wrote tests",
    "worked_on": ["built the thing"],
    "completed": ["thing is done"],
    "pending": ["tests"],
    "files_touched": ["src/foo.py"],
    "lessons": ["always version before edit"],
})


# ---------------------------------------------------------------------------
# loader._parse_since
# ---------------------------------------------------------------------------

class TestParseSince:
    def test_none_returns_none(self):
        assert _parse_since(None) is None

    def test_today_returns_midnight_utc(self):
        dt = _parse_since("today")
        assert dt is not None
        assert dt.hour == 0 and dt.minute == 0 and dt.second == 0
        assert dt.tzinfo is not None

    def test_hours(self):
        dt = _parse_since("2h")
        assert dt is not None
        now = datetime.now(timezone.utc)
        diff = now - dt
        assert 7190 < diff.total_seconds() < 7210  # ~2 hours

    def test_minutes(self):
        dt = _parse_since("30m")
        assert dt is not None
        now = datetime.now(timezone.utc)
        diff = now - dt
        assert 1790 < diff.total_seconds() < 1810  # ~30 minutes

    def test_iso_string(self):
        dt = _parse_since("2026-05-03T10:00:00")
        assert dt is not None
        assert dt.year == 2026 and dt.month == 5 and dt.day == 3

    def test_invalid_raises(self):
        with pytest.raises(ValueError, match="Cannot parse"):
            _parse_since("banana")


# ---------------------------------------------------------------------------
# chunker
# ---------------------------------------------------------------------------

class TestChunker:
    def test_empty_returns_empty(self):
        assert chunk_turns([]) == []

    def test_single_chunk_for_short_transcript(self):
        turns = _turns(5)
        chunks = chunk_turns(turns, token_budget=6000)
        assert len(chunks) == 1
        assert not needs_reduce(chunks)

    def test_multi_chunk_for_long_transcript(self):
        # 500 turns * 500 chars = 250_000 chars > 6000 tokens * 4 chars
        turns = _turns(500, chars_each=500)
        chunks = chunk_turns(turns, token_budget=6000)
        assert len(chunks) > 1
        assert needs_reduce(chunks)

    def test_turn_boundaries_preserved(self):
        turns = _turns(10, chars_each=100)
        chunks = chunk_turns(turns, token_budget=1)  # tiny budget forces 1 turn per chunk
        total_turns = sum(len(c.turns) for c in chunks)
        assert total_turns == len(turns)

    def test_chunk_indices_and_totals(self):
        turns = _turns(200, chars_each=500)
        chunks = chunk_turns(turns, token_budget=6000)
        for i, c in enumerate(chunks):
            assert c.index == i
            assert c.total == len(chunks)

    def test_chunk_text_contains_role_tags(self):
        turns = [_turn("user", "hello"), _turn("assistant", "world")]
        chunks = chunk_turns(turns)
        assert "[USER]" in chunks[0].text
        assert "[ASSISTANT]" in chunks[0].text
        assert "hello" in chunks[0].text
        assert "world" in chunks[0].text

    def test_start_end_ts_populated(self):
        turns = _turns(5)
        chunks = chunk_turns(turns)
        assert chunks[0].start_ts != ""
        assert chunks[0].end_ts != ""


# ---------------------------------------------------------------------------
# prompts
# ---------------------------------------------------------------------------

class TestPrompts:
    def test_map_prompt_contains_chunk_info(self):
        prompt = map_prompt("some transcript", chunk_index=0, chunk_total=3)
        assert "chunk 1 of 3" in prompt.lower()
        assert "some transcript" in prompt
        assert "JSON" in prompt

    def test_map_prompt_asks_for_json_keys(self):
        prompt = map_prompt("text", 0, 1)
        for key in ("worked_on", "completed", "pending", "files_touched", "lessons"):
            assert key in prompt

    def test_reduce_prompt_contains_all_summaries(self):
        summaries = ["summary A", "summary B", "summary C"]
        prompt = reduce_prompt(summaries, date="2026-05-03")
        for s in summaries:
            assert s in prompt
        assert "2026-05-03" in prompt

    def test_reduce_prompt_asks_for_title(self):
        prompt = reduce_prompt(["x"], date="2026-05-03")
        assert "title" in prompt


# ---------------------------------------------------------------------------
# cli._parse_json_response
# ---------------------------------------------------------------------------

class TestParseJsonResponse:
    def test_clean_json(self):
        obj = {"title": "hello", "worked_on": ["a"], "completed": [], "pending": [], "files_touched": [], "lessons": []}
        result = _parse_json_response(json.dumps(obj))
        assert result["title"] == "hello"

    def test_markdown_fenced_json(self):
        obj = {"title": "fenced", "worked_on": [], "completed": [], "pending": [], "files_touched": [], "lessons": []}
        fenced = f"```json\n{json.dumps(obj)}\n```"
        result = _parse_json_response(fenced)
        assert result["title"] == "fenced"

    def test_malformed_returns_fallback(self):
        result = _parse_json_response("this is not json at all")
        assert "title" in result
        assert "completed" in result

    def test_json_embedded_in_prose(self):
        obj = {"title": "embedded", "worked_on": [], "completed": [], "pending": [], "files_touched": [], "lessons": []}
        text = f"Here is your JSON: {json.dumps(obj)} Hope that helps!"
        result = _parse_json_response(text)
        assert result["title"] == "embedded"


# ---------------------------------------------------------------------------
# End-to-end pipeline with mocked LLM — single chunk (no reduce)
# ---------------------------------------------------------------------------

class TestPipelineSingleChunk:
    def test_map_only_pipeline(self, tmp_path):
        """10 turns → 1 chunk → mocked map → render → write to temp dir."""
        turns = _turns(10)
        chunks = chunk_turns(turns, token_budget=6000)
        assert len(chunks) == 1

        with patch("atlas_plus.summarizer.providers.complete") as mock_complete:
            mock_complete.return_value = (_GOLDEN_MAP_RESPONSE, "mock")
            from atlas_plus.summarizer.providers import complete
            text, provider = complete(map_prompt(chunks[0].text, 0, 1))

        summary = _parse_json_response(text)
        # Map response has no "title" field — that's added only in the reduce step
        assert "thing is done" in summary["completed"]
        assert "thing is done" in summary.get("completed", [])

    def test_full_pipeline_writes_files(self, tmp_path, monkeypatch):
        """Full single-chunk pipeline writes session log and updates.txt."""
        import atlas_plus.summarizer.renderer as renderer_mod
        import atlas_plus.autonomous as auto_mod

        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        updates_txt = tmp_path / "updates.txt"
        updates_txt.write_text("====header====\n", encoding="utf-8")
        versions_dir = tmp_path / "versions"

        monkeypatch.setattr(renderer_mod, "MEMORY_SESSIONS_DIR", sessions_dir)
        monkeypatch.setattr(renderer_mod, "UPDATES_TXT", updates_txt)
        monkeypatch.setattr(renderer_mod, "VERSIONS_DIR", versions_dir)
        monkeypatch.setenv("ATLAS_AUTONOMOUS", "on")

        turns = _turns(10)
        chunks = chunk_turns(turns)
        summary = _parse_json_response(_GOLDEN_MAP_RESPONSE)

        from atlas_plus.summarizer.prompts import render_session_log, render_updates_entry
        session_text = render_session_log(summary, "2026-05-03")
        updates_text = render_updates_entry(summary, "2026-05-03")

        from atlas_plus.summarizer.renderer import write_outputs
        result = write_outputs(session_text, updates_text, "2026-05-03", dry_run=False)

        assert (sessions_dir / "2026-05-03.md").exists()
        content = (sessions_dir / "2026-05-03.md").read_text(encoding="utf-8")
        assert "2026-05-03" in content

    def test_dry_run_writes_nothing(self, tmp_path, monkeypatch):
        import atlas_plus.summarizer.renderer as renderer_mod

        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        updates_txt = tmp_path / "updates.txt"
        versions_dir = tmp_path / "versions"

        monkeypatch.setattr(renderer_mod, "MEMORY_SESSIONS_DIR", sessions_dir)
        monkeypatch.setattr(renderer_mod, "UPDATES_TXT", updates_txt)
        monkeypatch.setattr(renderer_mod, "VERSIONS_DIR", versions_dir)

        from atlas_plus.summarizer.renderer import write_outputs
        result = write_outputs("session text", "updates text", "2026-05-03", dry_run=True)

        assert not (sessions_dir / "2026-05-03.md").exists()
        assert not updates_txt.exists()
        assert "session_log" in result


# ---------------------------------------------------------------------------
# End-to-end pipeline with mocked LLM — map-reduce (>1 chunk)
# ---------------------------------------------------------------------------

class TestPipelineMapReduce:
    def test_map_reduce_pipeline(self):
        """200 turns → multiple chunks → mocked map+reduce → valid summary."""
        turns = _turns(200, chars_each=500)
        chunks = chunk_turns(turns, token_budget=6000)
        assert needs_reduce(chunks)

        chunk_summaries: list[str] = []
        with patch("atlas_plus.summarizer.providers.complete") as mock_complete:
            mock_complete.return_value = (_GOLDEN_MAP_RESPONSE, "mock")
            from atlas_plus.summarizer.providers import complete
            from atlas_plus.summarizer.prompts import map_prompt as mp
            for chunk in chunks:
                text, _ = complete(mp(chunk.text, chunk.index, chunk.total))
                chunk_summaries.append(text)

        assert len(chunk_summaries) == len(chunks)

        with patch("atlas_plus.summarizer.providers.complete") as mock_reduce:
            mock_reduce.return_value = (_GOLDEN_REDUCE_RESPONSE, "mock")
            from atlas_plus.summarizer.prompts import reduce_prompt as rp
            from atlas_plus.summarizer.providers import complete
            final_text, _ = complete(rp(chunk_summaries, "2026-05-03"))

        summary = _parse_json_response(final_text)
        assert summary["title"] == "Built thing and wrote tests"
        assert len(summary["completed"]) > 0
