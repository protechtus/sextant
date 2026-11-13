"""Tests for the atlas-temporal CLI -- build, query, history, stats, reset, mining source."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from atlas_plus.temporal_graph import cli


def _write_jsonl_transcript(path: Path, tool_names: list[str]) -> None:
    """Write a minimal Claude Code JSONL transcript with given tool use records."""
    path.parent.mkdir(parents=True, exist_ok=True)
    records = []
    for name in tool_names:
        records.append({
            "type": "assistant",
            "timestamp": "2026-05-20T10:00:00Z",
            "sessionId": path.stem,
            "message": {
                "role": "assistant",
                "model": "claude-sonnet-4-6",
                "content": [{"type": "tool_use", "name": name, "input": {}}],
                "usage": {"input_tokens": 10, "output_tokens": 5,
                          "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0},
            },
        })
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")


def _make_provider(tmp_path: Path):
    """Return a FileTemporalProvider populated with the Atlas Phase 4 fixture data."""
    from atlas_plus.temporal_graph.file_provider import FileTemporalProvider
    from atlas_plus.temporal_graph.sources import FixtureSource, ATLAS_PHASE4_FIXTURE
    p = FileTemporalProvider(graph_root=tmp_path / "graph")
    FixtureSource(ATLAS_PHASE4_FIXTURE).ingest(p)
    return p


def test_build_command_uses_shared_build_helper(monkeypatch, tmp_path):
    calls: list[tuple[str, object]] = []

    def fake_build_temporal_graph(source="atlas", graph_root=None):
        calls.append((source, graph_root))
        return {
            "source": source,
            "files_indexed": 2,
            "files_skipped": 3,
            "facts_added": 4,
            "warnings": ["sample warning"],
            "index": {
                "episodes": 5,
                "entities": 6,
                "facts_total": 7,
                "facts_active": 8,
            },
        }

    monkeypatch.setattr(cli, "build_temporal_graph", fake_build_temporal_graph, raising=False)

    result = CliRunner().invoke(
        cli.app,
        ["build", "--source", "all", "--graph-root", str(tmp_path / "graph")],
    )

    assert result.exit_code == 0
    assert calls == [("all", tmp_path / "graph")]
    assert "indexed=2 skipped=3 facts=4" in result.output
    assert "Index: 5 episodes, 6 entities, 7 facts (8 active)." in result.output
    # No retraction/compaction keys in the result -> no noise lines.
    assert "retracted=" not in result.output
    assert "compacted" not in result.output


def test_build_command_surfaces_retraction_and_compaction(monkeypatch, tmp_path):
    """Maintenance the build performs is visible, not silent (2026-10-10):
    retract-on-missing counts and compaction savings print when present."""
    def fake_build_temporal_graph(source="atlas", graph_root=None):
        return {
            "source": source,
            "files_indexed": 1,
            "files_skipped": 0,
            "files_retracted": 2,
            "facts_invalidated": 7,
            "facts_added": 3,
            "warnings": [],
            "compaction": {
                "entities.jsonl": {"compacted": True, "bytes_before": 1000000, "bytes_after": 10000},
            },
            "index": {"episodes": 1, "entities": 1, "facts_total": 3, "facts_active": 3},
        }

    monkeypatch.setattr(cli, "build_temporal_graph", fake_build_temporal_graph, raising=False)
    result = CliRunner().invoke(cli.app, ["build", "--source", "memory"])
    assert result.exit_code == 0
    assert "retracted=2 vanished file(s), 7 fact(s) invalidated" in result.output
    assert "compacted entities.jsonl: 1,000,000 -> 10,000 bytes" in result.output


def test_build_helper_aggregates_retraction_counts(tmp_path, monkeypatch):
    """build_temporal_graph sums files_retracted/facts_invalidated from its
    sources instead of dropping them (the counters were born 2026-10-09 but
    the aggregate silently discarded them until 2026-10-10)."""
    from atlas_plus.temporal_graph import build as build_mod
    from atlas_plus.temporal_graph import sources as sources_mod

    class FakeSource:
        def ingest(self, provider, manifest=None):
            return {
                "files_indexed": 0, "files_skipped": 1,
                "files_retracted": 3, "facts_invalidated": 9,
                "facts": 0, "warnings": [],
            }

    monkeypatch.setattr(sources_mod, "MemoryMarkdownSource", lambda: FakeSource())
    result = build_mod.build_temporal_graph(source="memory", graph_root=tmp_path / "g")
    assert result["files_retracted"] == 3
    assert result["facts_invalidated"] == 9


# ---------------------------------------------------------------------------
# stats
# ---------------------------------------------------------------------------

def test_stats_empty_graph(tmp_path):
    result = CliRunner().invoke(
        cli.app, ["stats", "--graph-root", str(tmp_path / "graph")]
    )
    assert result.exit_code == 0
    assert "Episodes:" in result.output
    assert "Facts (total):" in result.output


def test_stats_populated_graph(tmp_path):
    _make_provider(tmp_path)
    result = CliRunner().invoke(
        cli.app, ["stats", "--graph-root", str(tmp_path / "graph")]
    )
    assert result.exit_code == 0
    assert "1" in result.output  # at least one entity/fact


# ---------------------------------------------------------------------------
# query
# ---------------------------------------------------------------------------

def test_query_empty_graph_exits_with_message(tmp_path):
    result = CliRunner().invoke(
        cli.app, ["query", "anything", "--graph-root", str(tmp_path / "graph")]
    )
    assert result.exit_code == 1
    assert "empty" in result.output.lower() or "build" in result.output.lower()


def test_query_current_mode_returns_results(tmp_path):
    _make_provider(tmp_path)
    result = CliRunner().invoke(
        cli.app,
        ["query", "Atlas MCP Tunnel", "--graph-root", str(tmp_path / "graph"), "--mode", "current"],
    )
    assert result.exit_code == 0


def test_query_invalid_mode_exits_nonzero(tmp_path):
    _make_provider(tmp_path)
    result = CliRunner().invoke(
        cli.app,
        ["query", "anything", "--graph-root", str(tmp_path / "graph"), "--mode", "bad-mode"],
    )
    assert result.exit_code != 0


# ---------------------------------------------------------------------------
# history
# ---------------------------------------------------------------------------

def test_history_shows_entity(tmp_path):
    _make_provider(tmp_path)
    result = CliRunner().invoke(
        cli.app,
        ["history", "Atlas MCP Tunnel", "--graph-root", str(tmp_path / "graph")],
    )
    assert result.exit_code == 0
    assert "Atlas MCP Tunnel" in result.output


# ---------------------------------------------------------------------------
# reset
# ---------------------------------------------------------------------------

def test_reset_with_yes_flag_clears_state(tmp_path):
    _make_provider(tmp_path)
    # Confirm something exists first
    from atlas_plus.temporal_graph.file_provider import FileTemporalProvider
    p = FileTemporalProvider(graph_root=tmp_path / "graph")
    assert p.stats()["facts"] > 0

    result = CliRunner().invoke(
        cli.app,
        ["reset", "--graph-root", str(tmp_path / "graph"), "--yes"],
    )
    assert result.exit_code == 0
    assert "reset" in result.output.lower()


def test_reset_without_yes_aborts(tmp_path):
    result = CliRunner().invoke(
        cli.app,
        ["reset", "--graph-root", str(tmp_path / "graph")],
        input="n\n",  # respond "no" to confirmation
    )
    assert result.exit_code != 0


# ---------------------------------------------------------------------------
# mining source
# ---------------------------------------------------------------------------

def test_build_mining_source_indexes_jsonl(tmp_path):
    """build --source mining reads JSONL transcripts and adds tool pattern facts."""
    from unittest.mock import patch
    from atlas_plus.temporal_graph import sources as _src

    # TranscriptMiningSource uses (claude_root / <this workspace's Claude Code project dir>);
    # ATLAS_CLAUDE_PROJECT_DIR names it explicitly here (sweep 14: it was hard-coded).
    import os
    transcript_dir = tmp_path / "c--Tools"
    _write_jsonl_transcript(transcript_dir / "sess001.jsonl", ["Read", "Read", "Edit"])

    from atlas_plus.temporal_graph.build import build_temporal_graph

    with patch.object(_src, "_CLAUDE_CODE_ROOT", tmp_path), patch.dict(os.environ, {"ATLAS_CLAUDE_PROJECT_DIR": "c--Tools"}):
        result = build_temporal_graph(
            source="mining",
            graph_root=tmp_path / "graph",
        )
    assert "error" not in result
    assert result["files_indexed"] >= 1
    assert result["facts_added"] >= 1


def test_build_mining_unknown_source_returns_error(tmp_path):
    """build_temporal_graph returns an error dict for unknown source names."""
    from atlas_plus.temporal_graph.build import build_temporal_graph
    result = build_temporal_graph(source="nonexistent", graph_root=tmp_path / "graph")
    assert "error" in result
    assert "mining" in result["error"]  # valid sources listed in error message


def test_build_cli_mining_source(tmp_path, monkeypatch):
    """atlas-temporal build --source mining exits 0 when transcripts exist."""
    from unittest.mock import patch
    from atlas_plus.temporal_graph import sources as _src

    transcript_dir = tmp_path / "c--Tools"
    _write_jsonl_transcript(transcript_dir / "sess002.jsonl", ["Bash", "Bash", "Read"])

    with patch.object(_src, "_CLAUDE_CODE_ROOT", tmp_path):
        result = CliRunner().invoke(
            cli.app,
            ["build", "--source", "mining", "--graph-root", str(tmp_path / "graph")],
        )
    assert result.exit_code == 0
    assert "indexed=" in result.output


# ---------------------------------------------------------------------------
# query --json
# ---------------------------------------------------------------------------

class TestQueryJsonFlag:
    def test_empty_graph_returns_json_error(self, tmp_path):
        result = CliRunner().invoke(
            cli.app,
            ["query", "anything", "--graph-root", str(tmp_path / "graph"), "--json"],
        )
        assert result.exit_code == 1
        data = json.loads(result.output)
        assert "error" in data
        assert data["results"] == []

    def test_populated_graph_returns_json_array(self, tmp_path):
        _make_provider(tmp_path)
        result = CliRunner().invoke(
            cli.app,
            ["query", "Atlas MCP Tunnel", "--graph-root", str(tmp_path / "graph"),
             "--mode", "current", "--json"],
        )
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert "query" in data
        assert "results" in data
        assert isinstance(data["results"], list)

    def test_json_result_has_expected_keys(self, tmp_path):
        _make_provider(tmp_path)
        result = CliRunner().invoke(
            cli.app,
            ["query", "Atlas MCP Tunnel", "--graph-root", str(tmp_path / "graph"), "--json"],
        )
        assert result.exit_code == 0
        data = json.loads(result.output)
        if data["results"]:
            r = data["results"][0]
            assert "score" in r
            assert "fact" in r
            assert "valid_from" in r
            assert "provenance" in r


# ---------------------------------------------------------------------------
# history --json
# ---------------------------------------------------------------------------

class TestHistoryJsonFlag:
    def test_json_output_has_entity_and_facts(self, tmp_path):
        _make_provider(tmp_path)
        result = CliRunner().invoke(
            cli.app,
            ["history", "Atlas MCP Tunnel", "--graph-root", str(tmp_path / "graph"), "--json"],
        )
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert "entity" in data
        assert "facts" in data
        assert "name" in data["entity"]

    def test_json_entity_has_expected_keys(self, tmp_path):
        _make_provider(tmp_path)
        result = CliRunner().invoke(
            cli.app,
            ["history", "Atlas MCP Tunnel", "--graph-root", str(tmp_path / "graph"), "--json"],
        )
        assert result.exit_code == 0
        entity = json.loads(result.output)["entity"]
        assert "labels" in entity
        assert "aliases" in entity
        assert "first_seen_at" in entity

    def test_json_facts_have_expected_keys(self, tmp_path):
        _make_provider(tmp_path)
        result = CliRunner().invoke(
            cli.app,
            ["history", "Atlas MCP Tunnel", "--graph-root", str(tmp_path / "graph"), "--json"],
        )
        assert result.exit_code == 0
        facts = json.loads(result.output)["facts"]
        if facts:
            f = facts[0]
            assert "fact_text" in f
            assert "valid_from" in f
            assert "is_current" in f


# ---------------------------------------------------------------------------
# stats --json
# ---------------------------------------------------------------------------

class TestStatsJsonFlag:
    def test_json_output_has_expected_keys(self, tmp_path):
        result = CliRunner().invoke(
            cli.app,
            ["stats", "--graph-root", str(tmp_path / "graph"), "--json"],
        )
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert "episodes" in data
        assert "entities" in data
        assert "facts" in data
        assert "facts_active" in data
        assert "graph_root" in data

    def test_json_populated_graph(self, tmp_path):
        _make_provider(tmp_path)
        result = CliRunner().invoke(
            cli.app,
            ["stats", "--graph-root", str(tmp_path / "graph"), "--json"],
        )
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["entities"] >= 1
        assert data["facts"] >= 1


# ---------------------------------------------------------------------------
# entities command
# ---------------------------------------------------------------------------

class TestEntitiesCommand:
    def test_empty_graph_shows_message(self, tmp_path):
        result = CliRunner().invoke(
            cli.app,
            ["entities", "--graph-root", str(tmp_path / "graph")],
        )
        assert result.exit_code == 0
        assert "No entities" in result.output or result.output.strip() == ""

    def test_populated_graph_lists_entities(self, tmp_path):
        _make_provider(tmp_path)
        result = CliRunner().invoke(
            cli.app,
            ["entities", "--graph-root", str(tmp_path / "graph")],
        )
        assert result.exit_code == 0
        assert "Atlas" in result.output

    def test_json_output_is_array(self, tmp_path):
        _make_provider(tmp_path)
        result = CliRunner().invoke(
            cli.app,
            ["entities", "--graph-root", str(tmp_path / "graph"), "--json"],
        )
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert isinstance(data, list)
        assert len(data) >= 1

    def test_json_entity_keys(self, tmp_path):
        _make_provider(tmp_path)
        result = CliRunner().invoke(
            cli.app,
            ["entities", "--graph-root", str(tmp_path / "graph"), "--json"],
        )
        assert result.exit_code == 0
        entities = json.loads(result.output)
        e = entities[0]
        assert "name" in e
        assert "labels" in e
        assert "aliases" in e

    def test_empty_graph_json_returns_empty_array(self, tmp_path):
        result = CliRunner().invoke(
            cli.app,
            ["entities", "--graph-root", str(tmp_path / "graph"), "--json"],
        )
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data == []

    def test_limit_respected(self, tmp_path):
        _make_provider(tmp_path)
        result = CliRunner().invoke(
            cli.app,
            ["entities", "--graph-root", str(tmp_path / "graph"), "--json", "--limit", "2"],
        )
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert len(data) <= 2

    def test_sorted_alphabetically(self, tmp_path):
        _make_provider(tmp_path)
        result = CliRunner().invoke(
            cli.app,
            ["entities", "--graph-root", str(tmp_path / "graph"), "--json"],
        )
        assert result.exit_code == 0
        names = [e["name"] for e in json.loads(result.output)]
        assert names == sorted(names, key=str.lower)
