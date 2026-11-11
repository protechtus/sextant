"""Tests for curation.rederive -- re-derivable memory detection."""
from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from atlas_plus.curation.rederive import RederiveCandidate, _tokenize, find_rederivable


class TestTokenize:
    def test_filters_short_words(self):
        tokens = _tokenize("is the a an")
        assert tokens == set()

    def test_extracts_code_identifiers(self):
        tokens = _tokenize("memory_root config_path ATLAS_MCP_TOKEN")
        assert "memory_root" in tokens
        assert "atlas_mcp_token" in tokens

    def test_lowercases(self):
        tokens = _tokenize("FastAPI Uvicorn")
        assert "fastapi" in tokens


class TestFindRederivable:
    def _write_mem(self, path: Path, body: str, mem_type: str = "reference") -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            f"---\nid: {path.stem}\ntitle: {path.stem}\ntype: {mem_type}\nstatus: active\n---\n\n{body}",
            encoding="utf-8",
        )

    def _write_doc(self, path: Path, body: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")

    def test_empty_memory_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "workspace"
            mem = Path(tmp) / "memory"
            ws.mkdir()
            mem.mkdir()
            with patch("atlas_plus.curation.rederive.MEMORY_ROOT", mem), \
                 patch("atlas_plus.curation.rederive._WORKSPACE_ROOT", ws):
                result = find_rederivable(threshold=0.1)
        assert result == []

    def test_no_doc_files_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "workspace"
            mem = Path(tmp) / "memory"
            ws.mkdir()
            self._write_mem(mem / "ref.md", "The Atlas memory system stores facts about projects.")
            with patch("atlas_plus.curation.rederive.MEMORY_ROOT", mem), \
                 patch("atlas_plus.curation.rederive._WORKSPACE_ROOT", ws):
                result = find_rederivable(threshold=0.1)
        assert result == []

    def test_high_overlap_flagged(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "workspace"
            mem = Path(tmp) / "memory"
            # Doc and memory share many words
            shared = " ".join([
                "atlas memory system stores facts about projects reference files markdown",
                "sessions temporal graph faiss index brain server scheduler worker",
                "queries search results confidence bands annotations scores",
            ])
            self._write_doc(ws / "AGENTS.md", shared)
            self._write_mem(mem / "atlas.md", shared)
            with patch("atlas_plus.curation.rederive.MEMORY_ROOT", mem), \
                 patch("atlas_plus.curation.rederive._WORKSPACE_ROOT", ws):
                result = find_rederivable(threshold=0.1)
        assert len(result) == 1
        assert result[0].path == "atlas.md"
        assert result[0].similarity >= 0.5

    def test_low_overlap_not_flagged(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "workspace"
            mem = Path(tmp) / "memory"
            self._write_doc(ws / "README.md", "python fastapi uvicorn typer rich sqlalchemy")
            self._write_mem(mem / "personal.md",
                "prefers direct communication avoids corporate jargon values technical depth")
            with patch("atlas_plus.curation.rederive.MEMORY_ROOT", mem), \
                 patch("atlas_plus.curation.rederive._WORKSPACE_ROOT", ws):
                result = find_rederivable(threshold=0.30)
        assert result == []

    def test_constraint_type_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "workspace"
            mem = Path(tmp) / "memory"
            shared = " ".join(["constraint production forbidden deployment rollback policy enforce version always"] * 3)
            self._write_doc(ws / "AGENTS.md", shared)
            self._write_mem(mem / "rule.md", shared, mem_type="constraint")
            with patch("atlas_plus.curation.rederive.MEMORY_ROOT", mem), \
                 patch("atlas_plus.curation.rederive._WORKSPACE_ROOT", ws):
                result = find_rederivable(threshold=0.1)
        assert result == []

    def test_sorted_by_similarity_descending(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "workspace"
            mem = Path(tmp) / "memory"
            # Use enough unique tokens (>=20) for the doc to pass the index guard
            doc_words = (
                "atlas memory brain scheduler temporal worker search index queries faiss "
                "session results confidence annotations scores rebuild ingestion pipeline "
                "curation decay duplicate stale archive decision reference feedback"
            )
            self._write_doc(ws / "AGENTS.md", doc_words)
            self._write_mem(mem / "high.md", doc_words)  # perfect overlap
            self._write_mem(mem / "low.md",
                "user prefers terse responses without trailing summaries")
            with patch("atlas_plus.curation.rederive.MEMORY_ROOT", mem), \
                 patch("atlas_plus.curation.rederive._WORKSPACE_ROOT", ws):
                result = find_rederivable(threshold=0.1)
        assert len(result) >= 1
        for i in range(len(result) - 1):
            assert result[i].similarity >= result[i + 1].similarity

    def test_as_dict_fields(self):
        c = RederiveCandidate(
            path="ref/tool.md",
            title="Tool reference",
            memory_type="reference",
            matched_doc="AGENTS.md",
            similarity=0.45,
            sample_overlap=["atlas", "memory", "brain"],
        )
        d = c.as_dict()
        assert d["path"] == "ref/tool.md"
        assert d["similarity"] == 0.45
        assert "atlas" in d["sample_overlap"]
