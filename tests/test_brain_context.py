"""Tests for Brain context pack artifact creation."""

from __future__ import annotations

import pytest
from unittest.mock import patch

from atlas_plus.brain import context as brain_context
from atlas_plus.brain import tasks as ts


@pytest.fixture()
def db(tmp_path):
    return tmp_path / "state.sqlite"


def test_build_context_pack_is_bounded(tmp_path):
    memory_root = tmp_path / ".memory"
    research_root = tmp_path / "research"
    memory_root.mkdir()
    research_root.mkdir()
    (memory_root / "INDEX.md").write_text("# Hot Cache\n" + ("a" * 200), encoding="utf-8")
    (research_root / "RESEARCH-PLAN.md").write_text("# Research Plan\n" + ("b" * 200), encoding="utf-8")

    pack = brain_context.build_context_pack(
        instruction="summarize research",
        memory_root=memory_root,
        research_root=research_root,
        max_chars=220,
    )

    assert "summarize research" in pack
    assert len(pack) <= 220


def test_write_context_artifact_creates_markdown(tmp_path, db):
    task = ts.create_task("summarize", task_id="task-context", db_path=db)

    path = brain_context.write_context_artifact(
        task,
        artifact_root=tmp_path,
        memory_root=tmp_path,
        research_root=tmp_path,
    )

    assert path.name == "context.md"
    assert path.read_text(encoding="utf-8").startswith("---")


def test_gather_temporal_context_returns_empty_when_graph_empty(tmp_path):
    """_gather_temporal_context returns '' when the graph has no facts."""
    from unittest.mock import patch, MagicMock
    mock_provider = MagicMock()
    mock_provider.stats.return_value = {"facts_active": 0}

    with patch("atlas_plus.temporal_graph.file_provider.FileTemporalProvider",
               return_value=mock_provider):
        result = brain_context._gather_temporal_context("atlas-memory+")

    assert result == ""


def test_gather_temporal_context_returns_facts_when_graph_populated(tmp_path):
    """_gather_temporal_context returns formatted facts when results exist."""
    from unittest.mock import patch, MagicMock
    from atlas_plus.temporal_graph.models import TemporalFact, TemporalEntity, _now_iso

    mock_fact = TemporalFact(
        fact_id="f-test",
        subject_id="ent-atlas",
        predicate="is_live_at",
        object_id="tunnel",
        fact_text="Atlas MCP Tunnel is live.",
        valid_from="2026-05-16T00:00:00+00:00",
        observed_at=_now_iso(),
        ingested_at=_now_iso(),
    )
    mock_entity = TemporalEntity(
        entity_id="ent-atlas",
        name="Atlas MCP Tunnel",
        labels=["Project"],
        first_seen_at="2026-05-16T00:00:00+00:00",
        last_seen_at="2026-05-16T00:00:00+00:00",
    )

    # Build a mock SearchResult-like object
    mock_result = MagicMock()
    mock_result.fact = mock_fact
    mock_result.entity = mock_entity

    mock_provider = MagicMock()
    mock_provider.stats.return_value = {"facts_active": 1}
    mock_provider.search_current.return_value = [mock_result]

    with patch("atlas_plus.temporal_graph.file_provider.FileTemporalProvider",
               return_value=mock_provider):
        result = brain_context._gather_temporal_context("atlas tunnel")

    assert "Atlas MCP Tunnel" in result
    assert "is live" in result.lower() or "Atlas MCP Tunnel is live" in result


def test_build_context_pack_includes_temporal_section(tmp_path):
    """build_context_pack adds Temporal Context section when graph has facts."""
    from unittest.mock import patch

    with patch.object(brain_context, "_gather_temporal_context", return_value="- [2026-05-16] Atlas: is live."):
        pack = brain_context.build_context_pack(
            instruction="atlas status",
            memory_root=tmp_path,
            research_root=tmp_path,
            include_temporal=True,
        )

    assert "Temporal Context" in pack
    assert "is live" in pack


def test_build_context_pack_skips_temporal_when_disabled(tmp_path):
    """build_context_pack omits Temporal Context when include_temporal=False."""
    with patch.object(brain_context, "_gather_temporal_context", return_value="- something"):
        pack = brain_context.build_context_pack(
            instruction="test",
            memory_root=tmp_path,
            research_root=tmp_path,
            include_temporal=False,
        )

    assert "Temporal Context" not in pack


# ---------------------------------------------------------------------------
# Additional coverage
# ---------------------------------------------------------------------------

class TestClipHelper:
    def test_clip_short_text_unchanged(self):
        from atlas_plus.brain.context import _clip
        text = "short"
        assert _clip(text, 100) == text

    def test_clip_long_text_truncated_with_marker(self):
        from atlas_plus.brain.context import _clip
        long_text = "x" * 500
        result = _clip(long_text, 100)
        assert len(result) <= 100 + 60  # some slack for the truncation marker
        assert "Truncated" in result

    def test_clip_exactly_at_limit_unchanged(self):
        from atlas_plus.brain.context import _clip
        text = "a" * 50
        assert _clip(text, 50) == text


class TestReadHead:
    def test_read_head_missing_file_returns_empty(self, tmp_path):
        from atlas_plus.brain.context import _read_head
        result = _read_head(tmp_path / "nonexistent.md", 200)
        assert result == ""

    def test_read_head_truncates_to_max_chars(self, tmp_path):
        from atlas_plus.brain.context import _read_head
        f = tmp_path / "big.md"
        f.write_text("x" * 500, encoding="utf-8")
        result = _read_head(f, 100)
        assert len(result) <= 100

    def test_read_head_returns_content_for_existing_file(self, tmp_path):
        from atlas_plus.brain.context import _read_head
        f = tmp_path / "facts.md"
        f.write_text("# Context\nAtlas is live.", encoding="utf-8")
        result = _read_head(f, 200)
        assert "Atlas is live." in result


class TestBuildContextPackEdgeCases:
    def test_empty_roots_produce_minimal_pack(self, tmp_path):
        from atlas_plus.brain.context import build_context_pack
        pack = build_context_pack(
            instruction="test",
            memory_root=tmp_path,
            research_root=tmp_path,
        )
        assert "test" in pack  # instruction always present
        assert isinstance(pack, str)

    def test_max_chars_enforced_with_large_files(self, tmp_path):
        from atlas_plus.brain.context import build_context_pack
        (tmp_path / "INDEX.md").write_text("x" * 10000, encoding="utf-8")
        pack = build_context_pack(
            instruction="instruction",
            memory_root=tmp_path,
            research_root=tmp_path,
            max_chars=500,
        )
        assert len(pack) <= 500
