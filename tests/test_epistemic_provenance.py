"""Tests for Epistemic Provenance (Angle 5).

Covers:
- first_observed, source, confidence written on new file creation
- last_confirmed updated on append to existing file
- Confidence mapping (float -> high/medium/low; source override)
- confirm_fact() updates timestamps without appending content
- mark_contradiction() writes to both files' contradictions: lists
- _update_frontmatter_field() and _append_to_frontmatter_list() helpers
- memory_confirm() tool-level function
"""
from __future__ import annotations

import tempfile
from datetime import date
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from atlas_plus.mcp.memory_server.routing import (
    _append_to_frontmatter_list,
    _map_confidence,
    _update_frontmatter_field,
    append_fact,
    confirm_fact,
    mark_contradiction,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fm(path: Path) -> dict:
    """Parse YAML frontmatter from a file."""
    raw = path.read_text(encoding="utf-8")
    if not raw.startswith("---"):
        return {}
    end = raw.find("\n---", 3)
    if end == -1:
        return {}
    return yaml.safe_load(raw[3:end]) or {}


def _write_existing(path: Path, extra_fm: dict = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fm = {"id": path.stem, "title": path.stem, "type": "reference", "status": "active", "updated": "2026-01-01"}
    if extra_fm:
        fm.update(extra_fm)
    lines = ["---"] + [f"{k}: {v}" for k, v in fm.items()] + ["---", "", "## 2026-01-01", "", "Old fact."]
    path.write_text("\n".join(lines), encoding="utf-8")


# ---------------------------------------------------------------------------
# _map_confidence
# ---------------------------------------------------------------------------

class TestMapConfidence:
    def test_human_sources_always_high(self):
        for src in ("human-direct", "human", "telegram"):
            assert _map_confidence(None, src) == "high"
            assert _map_confidence(0.1, src) == "high"

    def test_none_score_maps_to_medium(self):
        assert _map_confidence(None, "agent") == "medium"

    def test_high_score_maps_to_high(self):
        assert _map_confidence(0.90, "agent") == "high"
        assert _map_confidence(0.85, "agent") == "high"

    def test_medium_score_maps_to_medium(self):
        assert _map_confidence(0.75, "agent") == "medium"
        assert _map_confidence(0.70, "suggest-facts") == "medium"

    def test_low_score_maps_to_low(self):
        assert _map_confidence(0.60, "agent") == "low"
        assert _map_confidence(0.0, "agent") == "low"


# ---------------------------------------------------------------------------
# _update_frontmatter_field
# ---------------------------------------------------------------------------

class TestUpdateFrontmatterField:
    def test_updates_existing_field(self):
        raw = "---\nid: foo\nupdated: 2026-01-01\n---\n\nbody"
        result = _update_frontmatter_field(raw, "updated", "2026-05-19")
        assert "updated: 2026-05-19" in result
        assert "2026-01-01" not in result

    def test_adds_missing_field(self):
        raw = "---\nid: foo\n---\n\nbody"
        result = _update_frontmatter_field(raw, "last_confirmed", "2026-05-19")
        assert "last_confirmed: 2026-05-19" in result

    def test_no_frontmatter_returns_unchanged(self):
        raw = "No frontmatter here"
        assert _update_frontmatter_field(raw, "key", "val") == raw

    def test_idempotent_on_same_value(self):
        raw = "---\nid: foo\nupdated: 2026-05-19\n---\n\nbody"
        result = _update_frontmatter_field(raw, "updated", "2026-05-19")
        assert result.count("updated:") == 1


# ---------------------------------------------------------------------------
# _append_to_frontmatter_list
# ---------------------------------------------------------------------------

class TestAppendToFrontmatterList:
    def test_creates_new_list_field(self):
        raw = "---\nid: foo\n---\n\nbody"
        result = _append_to_frontmatter_list(raw, "contradictions", "ref/other.md")
        assert "contradictions: [ref/other.md]" in result

    def test_appends_to_existing_list(self):
        raw = "---\nid: foo\ncontradictions: [ref/a.md]\n---\n\nbody"
        result = _append_to_frontmatter_list(raw, "contradictions", "ref/b.md")
        assert "ref/a.md" in result
        assert "ref/b.md" in result

    def test_no_duplicates(self):
        raw = "---\nid: foo\ncontradictions: [ref/a.md]\n---\n\nbody"
        result = _append_to_frontmatter_list(raw, "contradictions", "ref/a.md")
        assert result.count("ref/a.md") == 1

    def test_no_frontmatter_returns_unchanged(self):
        raw = "No frontmatter"
        assert _append_to_frontmatter_list(raw, "contradictions", "x.md") == raw


# ---------------------------------------------------------------------------
# append_fact -- provenance fields on new file
# ---------------------------------------------------------------------------

class TestAppendFactProvenanceNewFile:
    def test_first_observed_set_on_create(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "test.md"
            append_fact(path, "A new fact.", {}, source="agent")
            fm = _fm(path)
        assert str(fm.get("first_observed")) == date.today().isoformat()

    def test_source_set_on_create(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "test.md"
            append_fact(path, "A fact.", {}, source="suggest-facts")
            fm = _fm(path)
        assert fm.get("source") == "suggest-facts"

    def test_confidence_high_on_create_with_high_score(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "test.md"
            append_fact(path, "A fact.", {}, source="agent", confidence=0.92)
            fm = _fm(path)
        assert fm.get("confidence") == "high"

    def test_confidence_medium_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "test.md"
            append_fact(path, "A fact.", {}, source="agent")
            fm = _fm(path)
        assert fm.get("confidence") == "medium"

    def test_confidence_high_for_human_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "test.md"
            append_fact(path, "A fact.", {}, source="human-direct", confidence=0.1)
            fm = _fm(path)
        assert fm.get("confidence") == "high"

    def test_first_observed_not_in_body(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "test.md"
            append_fact(path, "A fact.", {}, source="agent")
            content = path.read_text()
        body = content.split("\n---\n", 1)[1]
        assert "first_observed" not in body


# ---------------------------------------------------------------------------
# append_fact -- provenance fields on existing file
# ---------------------------------------------------------------------------

class TestAppendFactProvenanceExistingFile:
    def test_last_confirmed_added_on_update(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "test.md"
            _write_existing(path)
            append_fact(path, "New fact.", {}, source="agent")
            fm = _fm(path)
        assert str(fm.get("last_confirmed")) == date.today().isoformat()

    def test_first_observed_not_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "test.md"
            _write_existing(path, extra_fm={"first_observed": "2026-01-01"})
            append_fact(path, "New fact.", {}, source="agent")
            fm = _fm(path)
        assert str(fm.get("first_observed")) == "2026-01-01"

    def test_updated_bumped_on_update(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "test.md"
            _write_existing(path)
            append_fact(path, "New fact.", {}, source="agent")
            fm = _fm(path)
        assert str(fm.get("updated")) == date.today().isoformat()


# ---------------------------------------------------------------------------
# confirm_fact
# ---------------------------------------------------------------------------

class TestConfirmFact:
    def test_updates_last_confirmed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "test.md"
            _write_existing(path)
            result = confirm_fact(path)
        assert result is True

    def test_confirm_writes_last_confirmed_field(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "test.md"
            _write_existing(path)
            confirm_fact(path)
            fm = _fm(path)
        assert str(fm.get("last_confirmed")) == date.today().isoformat()

    def test_confirm_does_not_append_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "test.md"
            _write_existing(path)
            original_body = path.read_text().split("\n---\n", 1)[1]
            confirm_fact(path)
            new_body = path.read_text().split("\n---\n", 1)[1]
        assert new_body == original_body

    def test_confirm_nonexistent_returns_false(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "nonexistent.md"
            result = confirm_fact(path)
        assert result is False


# ---------------------------------------------------------------------------
# mark_contradiction
# ---------------------------------------------------------------------------

class TestMarkContradiction:
    def test_adds_contradiction_to_both_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            a = tmp_path / "ref" / "a.md"
            b = tmp_path / "ref" / "b.md"
            _write_existing(a)
            _write_existing(b)
            with patch("atlas_plus.mcp.memory_server.routing.MEMORY_ROOT", tmp_path):
                ua, ub = mark_contradiction("ref/a.md", "ref/b.md")
            assert ua is True
            assert ub is True
            fm_a = _fm(a)
            fm_b = _fm(b)
        assert "ref/b.md" in str(fm_a.get("contradictions", ""))
        assert "ref/a.md" in str(fm_b.get("contradictions", ""))

    def test_nonexistent_file_skipped_gracefully(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            a = tmp_path / "exists.md"
            _write_existing(a)
            with patch("atlas_plus.mcp.memory_server.routing.MEMORY_ROOT", tmp_path):
                ua, ub = mark_contradiction("exists.md", "missing.md")
            assert ua is True
            assert ub is False

    def test_idempotent_no_duplicate_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            a = tmp_path / "a.md"
            _write_existing(a)
            b = tmp_path / "b.md"
            _write_existing(b)
            with patch("atlas_plus.mcp.memory_server.routing.MEMORY_ROOT", tmp_path):
                mark_contradiction("a.md", "b.md")
                mark_contradiction("a.md", "b.md")
            content = a.read_text()
        assert content.count("b.md") == 1


# ---------------------------------------------------------------------------
# memory_confirm tool-level function
# ---------------------------------------------------------------------------

class TestMemoryConfirmTool:
    def test_confirm_tool_ok(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            path = tmp_path / "ref.md"
            _write_existing(path)
            from atlas_plus.mcp.memory_server.tools.remember import memory_confirm
            # memory_confirm now uses _safe_resolve from read.py, which reads its own
            # MEMORY_ROOT import -- patch that namespace in addition to config.
            with patch("atlas_plus.mcp.memory_server.config.MEMORY_ROOT", tmp_path), \
                 patch("atlas_plus.mcp.memory_server.tools.read.MEMORY_ROOT", tmp_path), \
                 patch("atlas_plus.mcp.memory_server.routing.MEMORY_ROOT", tmp_path):
                result = memory_confirm("ref.md")
        assert result["ok"] is True
        assert result["confirmed"] == date.today().isoformat()

    def test_confirm_tool_missing_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            from atlas_plus.mcp.memory_server.tools.remember import memory_confirm
            with patch("atlas_plus.mcp.memory_server.config.MEMORY_ROOT", Path(tmp)), \
                 patch("atlas_plus.mcp.memory_server.tools.read.MEMORY_ROOT", Path(tmp)):
                result = memory_confirm("missing.md")
        assert result["ok"] is False
        assert "not found" in result["error"].lower()


# ---------------------------------------------------------------------------
# memory_list_confirmed
# ---------------------------------------------------------------------------

class TestMemoryListConfirmed:
    def _write_confirmed(self, path: Path, last_confirmed: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            f"---\nid: {path.stem}\ntitle: {path.stem}\ntype: reference\n"
            f"last_confirmed: {last_confirmed}\nconfidence: high\nsource: agent\n---\n\nContent.\n",
            encoding="utf-8",
        )

    def test_returns_recently_confirmed(self):
        today = date.today().isoformat()
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            self._write_confirmed(tmp_path / "recent.md", today)
            from atlas_plus.mcp.memory_server.tools.list import memory_list_confirmed
            with patch("atlas_plus.mcp.memory_server.tools.list.MEMORY_ROOT", tmp_path):
                results = memory_list_confirmed(since_days=7)
        assert len(results) == 1
        assert results[0]["path"] == "recent.md"
        assert results[0]["last_confirmed"] == today
        assert results[0]["confidence"] == "high"

    def test_excludes_old_confirmations(self):
        old_date = "2024-01-01"
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            self._write_confirmed(tmp_path / "old.md", old_date)
            from atlas_plus.mcp.memory_server.tools.list import memory_list_confirmed
            with patch("atlas_plus.mcp.memory_server.tools.list.MEMORY_ROOT", tmp_path):
                results = memory_list_confirmed(since_days=7)
        assert results == []

    def test_sorted_newest_first(self):
        today = date.today().isoformat()
        yesterday = (date.today() - __import__("datetime").timedelta(days=1)).isoformat()
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            self._write_confirmed(tmp_path / "yesterday.md", yesterday)
            self._write_confirmed(tmp_path / "today.md", today)
            from atlas_plus.mcp.memory_server.tools.list import memory_list_confirmed
            with patch("atlas_plus.mcp.memory_server.tools.list.MEMORY_ROOT", tmp_path):
                results = memory_list_confirmed(since_days=7)
        assert len(results) == 2
        assert results[0]["last_confirmed"] >= results[1]["last_confirmed"]

    def test_skips_files_without_last_confirmed(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            p = tmp_path / "no_confirm.md"
            p.write_text("---\nid: x\ntitle: x\n---\nContent.\n", encoding="utf-8")
            from atlas_plus.mcp.memory_server.tools.list import memory_list_confirmed
            with patch("atlas_plus.mcp.memory_server.tools.list.MEMORY_ROOT", tmp_path):
                results = memory_list_confirmed(since_days=7)
        assert results == []

    def test_empty_memory_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            from atlas_plus.mcp.memory_server.tools.list import memory_list_confirmed
            with patch("atlas_plus.mcp.memory_server.tools.list.MEMORY_ROOT", Path(tmp) / "nonexistent"):
                results = memory_list_confirmed()
        assert results == []
