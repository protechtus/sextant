"""Tests for atlas_plus.temporal_graph.build -- build_temporal_graph wrapper.

Covers the source routing logic, error case for unknown source, and
result structure. Sources are mocked to avoid real I/O.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


def _mock_ingest_result(files_indexed=3, files_skipped=1, facts=10):
    return {"files_indexed": files_indexed, "files_skipped": files_skipped,
            "facts": facts, "warnings": []}


def _mock_stats():
    return {"episodes": 2, "entities": 8, "facts": 50, "facts_active": 45}


# ---------------------------------------------------------------------------
# TestBuildTemporalGraph
# ---------------------------------------------------------------------------

class TestBuildTemporalGraph:
    def _mock_source(self, result=None):
        src = MagicMock()
        src.ingest.return_value = result or _mock_ingest_result()
        return src

    def test_returns_error_for_unknown_source(self):
        from atlas_plus.temporal_graph.build import build_temporal_graph
        result = build_temporal_graph(source="invalid_source")
        assert "error" in result
        assert "Unknown source" in result["error"]

    def test_result_structure_has_required_keys(self, tmp_path):
        from atlas_plus.temporal_graph.build import build_temporal_graph
        mock_provider = MagicMock()
        mock_provider.stats.return_value = _mock_stats()
        mock_provider._manifest_file = tmp_path / "manifest.json"
        mock_mem_src = self._mock_source()
        mock_sess_src = self._mock_source()
        with patch("atlas_plus.temporal_graph.file_provider.FileTemporalProvider",
                   return_value=mock_provider), \
             patch("atlas_plus.temporal_graph.sources.Manifest"), \
             patch("atlas_plus.temporal_graph.sources.MemoryMarkdownSource",
                   return_value=mock_mem_src), \
             patch("atlas_plus.temporal_graph.sources.SessionLogSource",
                   return_value=mock_sess_src):
            result = build_temporal_graph(source="atlas", graph_root=tmp_path)
        assert "source" in result
        assert "files_indexed" in result
        assert "files_skipped" in result
        assert "facts_added" in result
        assert "index" in result
        assert "episodes" in result["index"]
        assert "entities" in result["index"]
        assert "facts_total" in result["index"]
        assert "facts_active" in result["index"]

    def test_atlas_source_calls_memory_and_sessions(self, tmp_path):
        from atlas_plus.temporal_graph.build import build_temporal_graph
        mock_provider = MagicMock()
        mock_provider.stats.return_value = _mock_stats()
        mock_provider._manifest_file = tmp_path / "manifest.json"
        mock_mem_src = self._mock_source()
        mock_sess_src = self._mock_source()
        mock_transcript_src = self._mock_source()
        with patch("atlas_plus.temporal_graph.file_provider.FileTemporalProvider",
                   return_value=mock_provider), \
             patch("atlas_plus.temporal_graph.sources.Manifest"), \
             patch("atlas_plus.temporal_graph.sources.MemoryMarkdownSource",
                   return_value=mock_mem_src), \
             patch("atlas_plus.temporal_graph.sources.SessionLogSource",
                   return_value=mock_sess_src), \
             patch("atlas_plus.temporal_graph.sources.ClaudeCodeTranscriptSource",
                   return_value=mock_transcript_src):
            result = build_temporal_graph(source="atlas", graph_root=tmp_path)
        mock_mem_src.ingest.assert_called_once()
        mock_sess_src.ingest.assert_called_once()
        # transcripts should NOT be called for source="atlas"
        mock_transcript_src.ingest.assert_not_called()

    def test_memory_source_calls_only_memory(self, tmp_path):
        from atlas_plus.temporal_graph.build import build_temporal_graph
        mock_provider = MagicMock()
        mock_provider.stats.return_value = _mock_stats()
        mock_provider._manifest_file = tmp_path / "manifest.json"
        mock_mem_src = self._mock_source()
        mock_sess_src = self._mock_source()
        with patch("atlas_plus.temporal_graph.file_provider.FileTemporalProvider",
                   return_value=mock_provider), \
             patch("atlas_plus.temporal_graph.sources.Manifest"), \
             patch("atlas_plus.temporal_graph.sources.MemoryMarkdownSource",
                   return_value=mock_mem_src), \
             patch("atlas_plus.temporal_graph.sources.SessionLogSource",
                   return_value=mock_sess_src):
            result = build_temporal_graph(source="memory", graph_root=tmp_path)
        mock_mem_src.ingest.assert_called_once()
        mock_sess_src.ingest.assert_not_called()
        assert result["source"] == "memory"

    def test_aggregates_totals_from_multiple_sources(self, tmp_path):
        from atlas_plus.temporal_graph.build import build_temporal_graph
        mock_provider = MagicMock()
        mock_provider.stats.return_value = _mock_stats()
        mock_provider._manifest_file = tmp_path / "manifest.json"
        mem_src = self._mock_source({"files_indexed": 5, "files_skipped": 1, "facts": 20, "warnings": []})
        sess_src = self._mock_source({"files_indexed": 3, "files_skipped": 0, "facts": 10, "warnings": []})
        with patch("atlas_plus.temporal_graph.file_provider.FileTemporalProvider",
                   return_value=mock_provider), \
             patch("atlas_plus.temporal_graph.sources.Manifest"), \
             patch("atlas_plus.temporal_graph.sources.MemoryMarkdownSource", return_value=mem_src), \
             patch("atlas_plus.temporal_graph.sources.SessionLogSource", return_value=sess_src):
            result = build_temporal_graph(source="atlas", graph_root=tmp_path)
        assert result["files_indexed"] == 8
        assert result["files_skipped"] == 1
        assert result["facts_added"] == 30

    def test_warnings_capped_at_5(self, tmp_path):
        from atlas_plus.temporal_graph.build import build_temporal_graph
        mock_provider = MagicMock()
        mock_provider.stats.return_value = _mock_stats()
        mock_provider._manifest_file = tmp_path / "manifest.json"
        many_warnings = [f"warn {i}" for i in range(10)]
        src = self._mock_source({"files_indexed": 1, "files_skipped": 0, "facts": 5, "warnings": many_warnings})
        with patch("atlas_plus.temporal_graph.file_provider.FileTemporalProvider",
                   return_value=mock_provider), \
             patch("atlas_plus.temporal_graph.sources.Manifest"), \
             patch("atlas_plus.temporal_graph.sources.MemoryMarkdownSource", return_value=src), \
             patch("atlas_plus.temporal_graph.sources.SessionLogSource", return_value=self._mock_source()):
            result = build_temporal_graph(source="atlas", graph_root=tmp_path)
        assert len(result["warnings"]) <= 5

    def test_index_comes_from_provider_stats(self, tmp_path):
        from atlas_plus.temporal_graph.build import build_temporal_graph
        mock_provider = MagicMock()
        mock_provider.stats.return_value = {
            "episodes": 7, "entities": 15, "facts": 100, "facts_active": 88
        }
        mock_provider._manifest_file = tmp_path / "manifest.json"
        src = self._mock_source()
        with patch("atlas_plus.temporal_graph.file_provider.FileTemporalProvider",
                   return_value=mock_provider), \
             patch("atlas_plus.temporal_graph.sources.Manifest"), \
             patch("atlas_plus.temporal_graph.sources.MemoryMarkdownSource", return_value=src), \
             patch("atlas_plus.temporal_graph.sources.SessionLogSource", return_value=self._mock_source()):
            result = build_temporal_graph(source="atlas", graph_root=tmp_path)
        assert result["index"]["episodes"] == 7
        assert result["index"]["entities"] == 15
        assert result["index"]["facts_active"] == 88

    def test_all_source_calls_all_four_sources(self, tmp_path):
        from atlas_plus.temporal_graph.build import build_temporal_graph
        mock_provider = MagicMock()
        mock_provider.stats.return_value = _mock_stats()
        mock_provider._manifest_file = tmp_path / "manifest.json"
        mem_src = self._mock_source()
        sess_src = self._mock_source()
        transcript_src = self._mock_source()
        mining_src = self._mock_source()
        with patch("atlas_plus.temporal_graph.file_provider.FileTemporalProvider",
                   return_value=mock_provider), \
             patch("atlas_plus.temporal_graph.sources.Manifest"), \
             patch("atlas_plus.temporal_graph.sources.MemoryMarkdownSource", return_value=mem_src), \
             patch("atlas_plus.temporal_graph.sources.SessionLogSource", return_value=sess_src), \
             patch("atlas_plus.temporal_graph.sources.ClaudeCodeTranscriptSource", return_value=transcript_src), \
             patch("atlas_plus.temporal_graph.sources.TranscriptMiningSource", return_value=mining_src):
            result = build_temporal_graph(source="all", graph_root=tmp_path)
        assert result["source"] == "all"
        mem_src.ingest.assert_called_once()
        sess_src.ingest.assert_called_once()
        transcript_src.ingest.assert_called_once()
        mining_src.ingest.assert_called_once()

    def test_sessions_source_calls_only_sessions(self, tmp_path):
        from atlas_plus.temporal_graph.build import build_temporal_graph
        mock_provider = MagicMock()
        mock_provider.stats.return_value = _mock_stats()
        mock_provider._manifest_file = tmp_path / "manifest.json"
        mem_src = self._mock_source()
        sess_src = self._mock_source()
        with patch("atlas_plus.temporal_graph.file_provider.FileTemporalProvider",
                   return_value=mock_provider), \
             patch("atlas_plus.temporal_graph.sources.Manifest"), \
             patch("atlas_plus.temporal_graph.sources.MemoryMarkdownSource", return_value=mem_src), \
             patch("atlas_plus.temporal_graph.sources.SessionLogSource", return_value=sess_src):
            result = build_temporal_graph(source="sessions", graph_root=tmp_path)
        assert result["source"] == "sessions"
        sess_src.ingest.assert_called_once()
        mem_src.ingest.assert_not_called()

    def test_fixtures_source_uses_fixture_source(self, tmp_path):
        from atlas_plus.temporal_graph.build import build_temporal_graph
        mock_provider = MagicMock()
        mock_provider.stats.return_value = _mock_stats()
        mock_provider._manifest_file = tmp_path / "manifest.json"
        fixture_src = self._mock_source()
        with patch("atlas_plus.temporal_graph.file_provider.FileTemporalProvider",
                   return_value=mock_provider), \
             patch("atlas_plus.temporal_graph.sources.Manifest"), \
             patch("atlas_plus.temporal_graph.sources.FixtureSource", return_value=fixture_src):
            result = build_temporal_graph(source="fixtures", graph_root=tmp_path)
        assert result["source"] == "fixtures"
        fixture_src.ingest.assert_called_once()

    def test_transcripts_source_calls_only_transcripts(self, tmp_path):
        from atlas_plus.temporal_graph.build import build_temporal_graph
        mock_provider = MagicMock()
        mock_provider.stats.return_value = _mock_stats()
        mock_provider._manifest_file = tmp_path / "manifest.json"
        transcript_src = self._mock_source()
        mem_src = self._mock_source()
        with patch("atlas_plus.temporal_graph.file_provider.FileTemporalProvider",
                   return_value=mock_provider), \
             patch("atlas_plus.temporal_graph.sources.Manifest"), \
             patch("atlas_plus.temporal_graph.sources.ClaudeCodeTranscriptSource", return_value=transcript_src), \
             patch("atlas_plus.temporal_graph.sources.MemoryMarkdownSource", return_value=mem_src):
            result = build_temporal_graph(source="transcripts", graph_root=tmp_path)
        assert result["source"] == "transcripts"
        transcript_src.ingest.assert_called_once()
        mem_src.ingest.assert_not_called()
