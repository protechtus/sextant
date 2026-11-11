"""Tests for the generic transcript drop-directory ingestion (ROADMAP 7)."""
from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setenv("ATLAS_TRANSCRIPT_ROOT", str(tmp_path / "transcripts"))
    monkeypatch.setenv("ATLAS_TRANSCRIPT_INDEX", str(tmp_path / "index.sqlite"))
    drop = tmp_path / "drop"
    drop.mkdir()
    monkeypatch.setenv("ATLAS_TRANSCRIPT_DROP", str(drop))
    return drop


class TestParseShapes:
    def test_jsonl(self, world):
        f = world / "chat-a.jsonl"
        f.write_text(
            json.dumps({"role": "user", "content": "hello"}) + "\n"
            + json.dumps({"role": "assistant", "content": "hi", "model": "gpt-x"}) + "\n"
            + "not json\n",
            encoding="utf-8",
        )
        from atlas_plus.transcripts.sources.drop import ingest
        result = ingest()
        assert result["files"] == 1
        assert result["turns"] == 2
        assert result["skipped"] == 1

    def test_json_messages_wrapper(self, world):
        f = world / "thread-b.json"
        f.write_text(json.dumps({"messages": [
            {"role": "user", "content": "q"},
            {"role": "assistant", "content": "a"},
            {"role": "narrator", "content": "invalid role"},
        ]}), encoding="utf-8")
        from atlas_plus.transcripts.sources.drop import ingest
        result = ingest()
        assert result["turns"] == 2
        assert result["skipped"] == 1

    def test_markdown_becomes_document_turn(self, world):
        (world / "meeting-notes.md").write_text("# Notes\n\nwe decided things", encoding="utf-8")
        from atlas_plus.transcripts.sources.drop import ingest
        assert ingest()["turns"] == 1


class TestIngestBehavior:
    def test_ingested_files_archived_not_deleted(self, world):
        (world / "c.jsonl").write_text(
            json.dumps({"role": "user", "content": "x"}) + "\n", encoding="utf-8")
        from atlas_plus.transcripts.sources.drop import ingest
        ingest()
        assert not (world / "c.jsonl").exists()
        archived = list((world / "_ingested").iterdir())
        assert len(archived) == 1 and archived[0].name.startswith("c.")

    def test_rerun_does_not_double_ingest(self, world):
        (world / "d.jsonl").write_text(
            json.dumps({"role": "user", "content": "once"}) + "\n", encoding="utf-8")
        from atlas_plus.transcripts.sources.drop import ingest
        assert ingest()["turns"] == 1
        assert ingest()["turns"] == 0

    def test_turns_are_searchable_after_ingest(self, world):
        (world / "e.jsonl").write_text(
            json.dumps({"role": "user", "content": "the xylophone incident"}) + "\n",
            encoding="utf-8")
        from atlas_plus.transcripts.sources.drop import ingest
        ingest()
        from atlas_plus.transcripts.cli import app
        result = CliRunner().invoke(app, ["search", "xylophone", "--json"])
        assert result.exit_code == 0
        assert "e" in result.output  # conversation id = file stem

    def test_cli_ingest_command(self, world):
        (world / "f.md").write_text("dropped note", encoding="utf-8")
        from atlas_plus.transcripts.cli import app
        result = CliRunner().invoke(app, ["ingest", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["turns"] == 1

    def test_cli_empty_drop_dir_message(self, world):
        from atlas_plus.transcripts.cli import app
        result = CliRunner().invoke(app, ["ingest"])
        assert result.exit_code == 0
        assert "Nothing to ingest" in result.output


class TestArchiveCollision:
    """Deep-pass probe (2026-09-27): same-stem re-drop within one second
    raised FileExistsError after indexing but before archiving -- the file
    stayed in the drop dir and would double-ingest on the next run."""

    def test_same_stem_same_second_redrops(self, world, monkeypatch):
        import time as _time
        # Freeze only the pattern drop.py uses; accept any signature so the
        # datetime methods elsewhere keep working.
        real = _time.strftime
        monkeypatch.setattr(
            _time, "strftime",
            lambda fmt, *a: "20260927-000000" if fmt == "%Y%m%d-%H%M%S" else real(fmt, *a),
        )
        from atlas_plus.transcripts.sources.drop import ingest
        for round_no in (1, 2, 3):
            (world / "same.jsonl").write_text(
                json.dumps({"role": "user", "content": f"round-{round_no}"}) + "\n",
                encoding="utf-8")
            assert ingest()["turns"] == 1  # every round lands, no exception
        archived = sorted(p.name for p in (world / "_ingested").iterdir())
        assert len(archived) == 3 and len(set(archived)) == 3


class TestReIngestIdempotent:
    """Expert review 2026-09-27: a crash between store-append and archive
    left the file in place; the re-run must UPSERT identical turn ids, not
    mint fresh UUIDs and duplicate every turn in the index."""

    def test_replayed_file_does_not_duplicate_turns(self, world):
        f = world / "crashy.jsonl"
        payload = json.dumps({"role": "user", "content": "the emu clause"}) + "\n"
        f.write_text(payload, encoding="utf-8")
        from atlas_plus.transcripts.sources.drop import ingest
        ingest()
        # Simulate the crash aftermath: same file reappears untouched.
        f.write_text(payload, encoding="utf-8")
        ingest()
        from atlas_plus.transcripts.index import TranscriptIndex
        hits = list(TranscriptIndex().search("emu clause"))
        assert len(hits) == 1, f"replay duplicated the turn: {len(hits)} rows"
