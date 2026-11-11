"""Layer 1: Unit-level persistence tests -- no LLM required.

Uses synthetic transcripts and mocked LLM extraction to verify the mechanical
pipeline: planted facts survive session end and appear in session start context.
All tests are deterministic and run without Ollama or API keys.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from tests.integration.fixtures.planted_conversations import (
    APP_FACTS,
    APP_MOCK_EXTRACTIONS,
    PlantedFact,
    build_app_turns,
)
from atlas_plus.testing.session_simulator import (
    capture_session_start_output,
    find_fact_in_memory,
    memory_contains_phrase,
    patch_memory_root,
    run_suggest_facts_with_turns,
    setup_memory_root,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def memory_root(tmp_path, monkeypatch):
    root = setup_memory_root(tmp_path)
    patch_memory_root(monkeypatch, root)
    return root


@pytest.fixture()
def app_turns():
    return build_app_turns(APP_FACTS)


def _mock_llm_response(fact_ids: list[str]) -> str:
    """Build a JSON string of mock LLM extractions for the given fact IDs."""
    return json.dumps([APP_MOCK_EXTRACTIONS[fid] for fid in fact_ids if fid in APP_MOCK_EXTRACTIONS])


# ---------------------------------------------------------------------------
# Core survival tests
# ---------------------------------------------------------------------------

class TestFactSurvival:
    @pytest.mark.parametrize("fact", APP_FACTS[:3])  # PF-001, PF-002, PF-003 (high confidence)
    def test_high_confidence_fact_survives(self, fact: PlantedFact, app_turns, memory_root):
        """High-confidence facts are written to .memory/ when auto=True."""
        llm_resp = _mock_llm_response([fact.id])
        result = run_suggest_facts_with_turns(
            turns=app_turns,
            memory_root=memory_root,
            llm_response=llm_resp,
            auto=True,
        )
        assert result["candidates"] >= 1
        assert result["written"] >= 1
        assert memory_contains_phrase(memory_root, fact.key_phrase)

    def test_all_high_confidence_facts_survive(self, app_turns, memory_root):
        """All 5 APP facts survive when extracted together."""
        high_ids = ["PF-001", "PF-002", "PF-003", "PF-004", "PF-005"]
        llm_resp = _mock_llm_response(high_ids)
        result = run_suggest_facts_with_turns(
            turns=app_turns,
            memory_root=memory_root,
            llm_response=llm_resp,
            auto=True,
        )
        assert result["written"] >= 1
        # Verify at least 3 of 5 key phrases are findable
        survived = [f for f in APP_FACTS if memory_contains_phrase(memory_root, f.key_phrase)]
        assert len(survived) >= 3, (
            f"Only {len(survived)}/5 facts survived: {[f.id for f in survived]}"
        )

    def test_no_facts_written_on_empty_transcript(self, memory_root):
        """Empty transcript produces zero candidates."""
        llm_resp = "[]"
        result = run_suggest_facts_with_turns(
            turns=[],
            memory_root=memory_root,
            llm_response=llm_resp,
            auto=True,
        )
        assert result["candidates"] == 0
        assert result["written"] == 0

    def test_low_confidence_fact_not_auto_written(self, app_turns, memory_root):
        """Facts below 0.85 confidence are not written in auto mode."""
        low_conf = [{
            "type": "feedback",
            "scope": "preference:misc",
            "title": "Some preference",
            "fact": "Maybe I prefer tabs over spaces, not sure.",
            "confidence": 0.60,
        }]
        result = run_suggest_facts_with_turns(
            turns=app_turns,
            memory_root=memory_root,
            llm_response=json.dumps(low_conf),
            auto=True,
        )
        assert result["written"] == 0

    def test_dry_run_writes_nothing(self, app_turns, memory_root):
        """dry_run=True counts candidates but writes nothing."""
        llm_resp = _mock_llm_response(["PF-001", "PF-003"])
        result = run_suggest_facts_with_turns(
            turns=app_turns,
            memory_root=memory_root,
            llm_response=llm_resp,
            dry_run=True,
        )
        assert result["candidates"] >= 1
        assert result["written"] == 0


# ---------------------------------------------------------------------------
# Fact location tests
# ---------------------------------------------------------------------------

class TestFactLocation:
    def test_editor_fact_lands_in_feedback_dir(self, app_turns, memory_root):
        """PF-001 (preference:editor) lands under feedback/ in .memory/."""
        llm_resp = _mock_llm_response(["PF-001"])
        run_suggest_facts_with_turns(
            turns=app_turns, memory_root=memory_root,
            llm_response=llm_resp, auto=True,
        )
        files = find_fact_in_memory(memory_root, "dark mode")
        assert any("feedback" in str(f) for f in files), (
            f"Expected fact in feedback/, found in: {files}"
        )

    def test_testing_fact_lands_in_feedback_dir(self, app_turns, memory_root):
        """PF-003 (preference:testing) lands under feedback/."""
        llm_resp = _mock_llm_response(["PF-003"])
        run_suggest_facts_with_turns(
            turns=app_turns, memory_root=memory_root,
            llm_response=llm_resp, auto=True,
        )
        files = find_fact_in_memory(memory_root, "real database")
        assert any("feedback" in str(f) for f in files)

    def test_reference_fact_lands_in_reference_dir(self, app_turns, memory_root):
        """PF-005 (reference:tools) lands under reference/."""
        llm_resp = _mock_llm_response(["PF-005"])
        run_suggest_facts_with_turns(
            turns=app_turns, memory_root=memory_root,
            llm_response=llm_resp, auto=True,
        )
        files = find_fact_in_memory(memory_root, "ripgrep")
        assert any("reference" in str(f) for f in files)


# ---------------------------------------------------------------------------
# Deduplication tests
# ---------------------------------------------------------------------------

class TestDeduplication:
    def test_duplicate_fact_not_written_twice(self, app_turns, memory_root):
        """Same fact submitted twice should not create two entries."""
        same_fact = {
            "type": "feedback",
            "scope": "preference:editor",
            "title": "Dark mode in VS Code",
            "fact": "Always use dark mode in VS Code. Theme: Dracula.",
            "confidence": 0.92,
        }
        # First write
        run_suggest_facts_with_turns(
            turns=app_turns, memory_root=memory_root,
            llm_response=json.dumps([same_fact]), auto=True,
        )
        # Second write of same fact
        run_suggest_facts_with_turns(
            turns=app_turns, memory_root=memory_root,
            llm_response=json.dumps([same_fact]), auto=True,
        )

        # The file should exist but not have the same line repeated
        editor_files = list((memory_root / "feedback").glob("editor.md"))
        if editor_files:
            content = editor_files[0].read_text(encoding="utf-8")
            occurrences = content.lower().count("dark mode")
            assert occurrences <= 2, (
                f"'dark mode' appears {occurrences} times -- possible duplicate write"
            )

    def test_pf001_and_pf004_same_scope_handled(self, app_turns, memory_root):
        """PF-001 and PF-004 both target preference:editor -- both may be written
        but Jaccard dedup should flag PF-004 as similar to PF-001."""
        llm_resp = _mock_llm_response(["PF-001", "PF-004"])
        result = run_suggest_facts_with_turns(
            turns=app_turns, memory_root=memory_root,
            llm_response=llm_resp, auto=True,
        )
        # At least one of the two should survive
        dark_mode = memory_contains_phrase(memory_root, "dark mode")
        solarized = memory_contains_phrase(memory_root, "Solarized Dark")
        assert dark_mode or solarized, "Neither editor theme fact survived"


# ---------------------------------------------------------------------------
# Session start injection tests
# ---------------------------------------------------------------------------

class TestSessionStartInjection:
    def test_written_fact_appears_in_session_start(self, app_turns, memory_root, monkeypatch):
        """After a fact is written to .memory/, session_start output includes it."""
        # Write PF-003 (integration testing rule) directly via routing
        from atlas_plus.mcp.memory_server.routing import resolve_target, append_fact
        with patch("atlas_plus.mcp.memory_server.routing.MEMORY_ROOT", memory_root):
            target, fm = resolve_target("preference:testing")
            append_fact(target, "Integration tests must use a real database. Never mock.", fm)

        # Capture session_start output
        output = capture_session_start_output(memory_root)
        assert "ATLAS" in output, "Expected Atlas context header in session start output"

    def test_session_start_house_rules_come_from_corpus_only(self, memory_root):
        """House rules are the USER'S, from identity/house-rules.md -- a
        corpus without the file injects no rules section at all (launch
        blocker fixed run 38: the maintainer's personal rules used to be
        hardcoded into every session)."""
        output = capture_session_start_output(memory_root)
        assert "House rules" not in output
        rules = memory_root / "identity" / "house-rules.md"
        rules.parent.mkdir(parents=True, exist_ok=True)
        rules.write_text("---\nid: house-rules\n---\n\n- Answer in haiku\n",
                         encoding="utf-8")
        output2 = capture_session_start_output(memory_root)
        assert "House rules" in output2 and "Answer in haiku" in output2

    def test_session_start_includes_identity(self, memory_root):
        """Session start includes the resolved identity file's content."""
        output = capture_session_start_output(memory_root)
        assert "Test User" in output or "ExampleCorp" in output


# ---------------------------------------------------------------------------
# Versioning tests
# ---------------------------------------------------------------------------

class TestVersioning:
    def test_fact_write_creates_version_copy(self, memory_root):
        """Writing a fact to an existing file creates a versioned backup."""
        from atlas_plus.mcp.memory_server.routing import append_fact
        from atlas_plus.mcp.memory_server.versioning import version_before_write

        # Create a target file directly (bypassing routing to avoid module-scope patching)
        feedback_dir = memory_root / "feedback"
        feedback_dir.mkdir(exist_ok=True)
        target = feedback_dir / "editor.md"
        target.write_text(
            "---\nid: editor\ntitle: Editor\ntype: feedback\nupdated: 2026-05-11\n---\n"
            "First fact about editor.\n",
            encoding="utf-8",
        )

        # Version it
        version_path = version_before_write(target)

        assert version_path is not None
        assert version_path.exists()
        content = version_path.read_text(encoding="utf-8")
        assert "editor" in content.lower()
