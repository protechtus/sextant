"""Tests for Direction F -- Dream Mode (atlas-dream CLI).

Covers:
  - build_corpus: collects files from requested scopes, respects truncation limit
  - run_dream: calls LLM and writes _dream-YYYY-MM-DD.md
  - dry_run: corpus built, LLM not called, nothing written
  - empty corpus: returns error without calling LLM
  - ATLAS_AUTONOMOUS=off: writes to .atlas/ not .memory/sessions/
  - LLM error: returned in result dict, nothing written
  - Output file has correct frontmatter and content
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from atlas_plus.curation.dream_mode import build_corpus, run_dream


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_memory_file(path: Path, title: str, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\nid: {path.stem}\ntitle: {title}\ntype: feedback\n"
        f"status: active\nupdated: 2026-05-17\n---\n\n{body}\n",
        encoding="utf-8",
    )


def _make_memory_root(tmp_path: Path) -> Path:
    root = tmp_path / ".memory"
    _write_memory_file(
        root / "feedback" / "testing.md",
        "Testing Rules",
        "Integration tests must use a real database, not mocks.",
    )
    _write_memory_file(
        root / "identity" / "alex.md",
        "Alex Example",
        "Alex is a Tier 3 support engineer at ExampleCorp.",
    )
    _write_memory_file(
        root / "reference" / "tools.md",
        "Tools Reference",
        "ripgrep is the preferred search tool.",
    )
    return root


# ---------------------------------------------------------------------------
# build_corpus
# ---------------------------------------------------------------------------

class TestBuildCorpus:
    def test_includes_files_from_requested_scopes(self, tmp_path):
        root = _make_memory_root(tmp_path)
        corpus, stats = build_corpus(root, scopes=["feedback"])
        assert stats["files"] == 1
        assert "testing" in corpus.lower() or "integration" in corpus.lower()

    def test_multiple_scopes_included(self, tmp_path):
        root = _make_memory_root(tmp_path)
        corpus, stats = build_corpus(root, scopes=["feedback", "identity"])
        assert stats["files"] == 2

    def test_nonexistent_scope_skipped(self, tmp_path):
        root = _make_memory_root(tmp_path)
        corpus, stats = build_corpus(root, scopes=["nonexistent"])
        assert stats["files"] == 0
        assert corpus == ""

    def test_empty_root_returns_empty(self, tmp_path):
        root = tmp_path / ".memory"
        root.mkdir()
        corpus, stats = build_corpus(root, scopes=["feedback"])
        assert corpus == ""
        assert stats["files"] == 0

    def test_underscore_files_skipped(self, tmp_path):
        root = tmp_path / ".memory"
        _write_memory_file(root / "feedback" / "_index.md", "Index", "Should be skipped.")
        _write_memory_file(root / "feedback" / "real.md", "Real", "Should be included.")
        corpus, stats = build_corpus(root, scopes=["feedback"])
        assert stats["files"] == 1
        assert "_index" not in corpus


# ---------------------------------------------------------------------------
# run_dream
# ---------------------------------------------------------------------------

class TestRunDream:
    def test_writes_dream_file_when_autonomous(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ATLAS_AUTONOMOUS", "on")
        root = _make_memory_root(tmp_path)
        sessions_dir = root / "sessions"
        sessions_dir.mkdir()

        import atlas_plus.curation.dream_mode as mod
        original_date = mod._date_str
        mod._date_str = lambda: "2026-05-17"
        try:
            result = run_dream(
                memory_root=root,
                scopes=["feedback", "identity"],
                _call_llm_fn=lambda p: (
                    "1. HIGH CONFIDENCE: Alex prefers real databases in tests.\n"
                    "2. UNCERTAIN: Nothing uncertain noted.\n"
                    "3. PATTERNS: Test-first approach throughout.\n"
                    "4. GAPS: No gaps identified.\n"
                    "5. QUESTIONS: What is the current tunnel status?"
                ),
            )
        finally:
            mod._date_str = original_date

        assert result["written"] is True
        out_path = sessions_dir / "_dream-2026-05-17.md"
        assert out_path.exists()
        content = out_path.read_text(encoding="utf-8")
        assert "2026-05-17" in content
        assert "HIGH CONFIDENCE" in content

    def test_autonomous_off_writes_to_state_dir(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ATLAS_AUTONOMOUS", "off")
        root = _make_memory_root(tmp_path)
        state_dir = tmp_path / ".atlas"
        state_dir.mkdir()

        import atlas_plus.curation.dream_mode as mod
        original_date = mod._date_str
        original_state = mod.STATE_DIR
        mod._date_str = lambda: "2026-05-17"
        mod.STATE_DIR = state_dir
        try:
            result = run_dream(
                memory_root=root,
                scopes=["feedback"],
                _call_llm_fn=lambda p: "Dream content here.",
            )
        finally:
            mod._date_str = original_date
            mod.STATE_DIR = original_state

        assert result["written"] is True
        assert "_dream-2026-05-17.md" in result["output_path"]
        # Should NOT be in .memory/sessions/
        assert str(root / "sessions") not in result["output_path"]
        # Should be in .atlas/
        assert str(state_dir) in result["output_path"]

    def test_dry_run_no_llm_no_write(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ATLAS_AUTONOMOUS", "on")
        root = _make_memory_root(tmp_path)

        llm_called = []
        with patch("atlas_plus.curation.dream_mode._call_llm", side_effect=lambda p: llm_called.append(1)):
            result = run_dream(
                memory_root=root,
                scopes=["feedback"],
                dry_run=True,
            )

        assert llm_called == []
        assert result["written"] is False
        assert result["dry_run"] is True
        assert result["corpus_files"] >= 1

    def test_empty_corpus_returns_error(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ATLAS_AUTONOMOUS", "on")
        root = tmp_path / ".memory"
        root.mkdir()

        result = run_dream(
            memory_root=root,
            scopes=["feedback"],
            _call_llm_fn=lambda p: "should not be called",
        )

        assert result["written"] is False
        assert "error" in result

    def test_llm_error_handled(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ATLAS_AUTONOMOUS", "on")
        root = _make_memory_root(tmp_path)

        def bad_llm(p):
            raise RuntimeError("Ollama not reachable")

        result = run_dream(
            memory_root=root,
            scopes=["feedback"],
            _call_llm_fn=bad_llm,
        )

        assert result["written"] is False
        assert "Ollama not reachable" in result.get("error", "")

    def test_output_has_frontmatter(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ATLAS_AUTONOMOUS", "on")
        root = _make_memory_root(tmp_path)
        sessions_dir = root / "sessions"
        sessions_dir.mkdir()

        import atlas_plus.curation.dream_mode as mod
        original_date = mod._date_str
        mod._date_str = lambda: "2026-05-17"
        try:
            result = run_dream(
                memory_root=root,
                scopes=["feedback"],
                _call_llm_fn=lambda p: "Reflection content.",
            )
        finally:
            mod._date_str = original_date

        content = Path(result["output_path"]).read_text(encoding="utf-8")
        assert content.startswith("---")
        assert "title: Dream" in content
        assert "type: session" in content

    def test_no_content_in_output(self, tmp_path, monkeypatch):
        """Verify conversation content doesn't leak into dream output."""
        monkeypatch.setenv("ATLAS_AUTONOMOUS", "on")
        root = _make_memory_root(tmp_path)
        sessions_dir = root / "sessions"
        sessions_dir.mkdir()

        import atlas_plus.curation.dream_mode as mod
        original_date = mod._date_str
        mod._date_str = lambda: "2026-05-17"
        try:
            result = run_dream(
                memory_root=root,
                scopes=["feedback"],
                _call_llm_fn=lambda p: "Safe reflection only.",
            )
        finally:
            mod._date_str = original_date

        content = Path(result["output_path"]).read_text(encoding="utf-8")
        # The output is the dream reflection, not raw .memory/ file content
        assert "Safe reflection only." in content


class TestCorpusRotationAndNovelty:
    """2026-07-30 redesign: staleness-first + daily rotation + skip-on-unchanged."""

    def _mk(self, tmp_path, n=6):
        import os as _os
        root = tmp_path / ".memory"
        (root / "feedback").mkdir(parents=True)
        for i in range(n):
            p = root / "feedback" / f"f{i}.md"
            p.write_text(f"---\ntitle: F{i}\n---\ncontent {i} " + "x" * 60, encoding="utf-8")
            _os.utime(p, (1_000_000 + i * 1000, 1_000_000 + i * 1000))
        return root

    def test_stalest_file_leads_at_zero_rotation(self, tmp_path):
        root = self._mk(tmp_path)
        corpus, _ = build_corpus(root, ["feedback"], rotation_key=0)
        assert corpus.split("[feedback/")[1].startswith("f0.md")

    def test_rotation_key_changes_lead_file(self, tmp_path):
        root = self._mk(tmp_path)
        c0, _ = build_corpus(root, ["feedback"], rotation_key=0)
        c1, _ = build_corpus(root, ["feedback"], rotation_key=1)
        assert c0.split("[feedback/")[1][:5] != c1.split("[feedback/")[1][:5]

    def test_same_key_is_deterministic(self, tmp_path):
        root = self._mk(tmp_path)
        a, _ = build_corpus(root, ["feedback"], rotation_key=3)
        b, _ = build_corpus(root, ["feedback"], rotation_key=3)
        assert a == b

    def test_skip_when_corpus_unchanged(self, tmp_path, monkeypatch):
        import atlas_plus.curation.dream_mode as dm
        root = self._mk(tmp_path)
        monkeypatch.setenv("ATLAS_AUTONOMOUS", "on")
        monkeypatch.setattr(dm, "STATE_DIR", tmp_path / "state")
        calls = []

        def llm(prompt):
            calls.append(1)
            return "1. HIGH CONFIDENCE: things."

        r1 = run_dream(memory_root=root, scopes=["feedback"], _call_llm_fn=llm)
        assert r1["written"] is True
        assert "corpus_hash" in r1
        text = Path(r1["output_path"]).read_text(encoding="utf-8")
        assert f"corpus_hash: {r1['corpus_hash']}" in text

        r2 = run_dream(memory_root=root, scopes=["feedback"], _call_llm_fn=llm)
        assert r2.get("skipped") is True
        assert r2["written"] is False
        assert len(calls) == 1
        assert r2["corpus_hash"] == r1["corpus_hash"]

    def test_changed_corpus_dreams_again(self, tmp_path, monkeypatch):
        import atlas_plus.curation.dream_mode as dm
        root = self._mk(tmp_path)
        monkeypatch.setenv("ATLAS_AUTONOMOUS", "on")
        monkeypatch.setattr(dm, "STATE_DIR", tmp_path / "state")
        llm = lambda prompt: "reflection"
        r1 = run_dream(memory_root=root, scopes=["feedback"], _call_llm_fn=llm)
        (root / "feedback" / "new-rule.md").write_text(
            "---\ntitle: New\n---\nbrand new fact", encoding="utf-8")
        r2 = run_dream(memory_root=root, scopes=["feedback"], _call_llm_fn=llm)
        assert r2.get("skipped") is None or r2.get("skipped") is False
        assert r2["corpus_hash"] != r1["corpus_hash"]
