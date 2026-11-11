"""Tests for the Claude auto-promote system in run_executor().

Covers:
- _detect_claude_strength pattern matching (all 6 groups + negative cases)
- run_executor promotion when ATLAS_CLAUDE_AUTO_PROMOTE=on
- run_executor suggestion-mode when env var is absent
- Explicit kinds bypass the strength check entirely
"""
from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from atlas_plus.brain.executors import (
    ExecutionResult,
    ExecutorContext,
    _detect_claude_strength,
    run_executor,
)
from atlas_plus.brain.tasks import Task


def _ctx(tmp: Path, kind: str, instruction: str) -> tuple[Task, ExecutorContext]:
    task = MagicMock(spec=Task)
    task.task_id = "test-task-001"
    task.kind = kind
    task.instruction = instruction
    task.session_id = None
    task.agent = None
    task.status = "running"  # required by brain_context.write_context_artifact
    ctx = ExecutorContext(
        task=task,
        artifact_root=tmp / "artifacts",
        db_path=tmp / "state.sqlite",
        worker_id="test-worker",
    )
    return task, ctx


def _fake_executor(result: ExecutionResult):
    """Return a callable that records invocations and returns a fixed ExecutionResult."""
    def _fn(ctx):
        return result
    return _fn


# ---------------------------------------------------------------------------
# _detect_claude_strength pattern tests
# ---------------------------------------------------------------------------

class TestDetectClaudeStrength:

    def test_code_review_explicit(self):
        assert _detect_claude_strength("please do a code review of this module") == "code_review"

    def test_code_review_variant(self):
        assert _detect_claude_strength("review this implementation for correctness") == "code_review"

    def test_code_review_audit(self):
        assert _detect_claude_strength("audit the security implementation") == "code_review"

    def test_code_analysis_explain(self):
        assert _detect_claude_strength("explain this function to me") == "code_analysis"

    def test_code_analysis_analyze(self):
        assert _detect_claude_strength("analyze this error and the traceback") == "code_analysis"

    def test_code_analysis_trace(self):
        assert _detect_claude_strength("trace the exception traceback") == "code_analysis"

    def test_complex_coding_refactor(self):
        assert _detect_claude_strength("refactor the authentication module") == "complex_coding"

    def test_complex_coding_redesign(self):
        assert _detect_claude_strength("redesign the database layer") == "complex_coding"

    def test_complex_coding_architect(self):
        assert _detect_claude_strength("architect a new retry system") == "complex_coding"

    def test_sip_protocol_trace(self):
        assert _detect_claude_strength("analyze the sip trace headers") == "sip_protocol"

    def test_sip_protocol_pcap(self):
        assert _detect_claude_strength("review the pcap file for anomalies") == "sip_protocol"

    def test_sip_protocol_rfc(self):
        assert _detect_claude_strength("check compliance with RFC 3261") == "sip_protocol"

    def test_sip_protocol_rtp_analysis(self):
        assert _detect_claude_strength("do an rtp analysis of the call") == "sip_protocol"

    def test_research_synthesis(self):
        assert _detect_claude_strength("synthesize the findings from these reports") == "research_synthesis"

    def test_research_synthesis_compare(self):
        assert _detect_claude_strength("compare these approaches for SIP registration") == "research_synthesis"

    def test_research_synthesis_evaluate(self):
        assert _detect_claude_strength("evaluate these options and pick the best tradeoffs") == "research_synthesis"

    def test_doc_extraction_summarize_code(self):
        assert _detect_claude_strength("summarize this code file") == "doc_extraction"

    def test_doc_extraction_what_does_do(self):
        assert _detect_claude_strength("what does this function do") == "doc_extraction"

    def test_doc_extraction_extract_key(self):
        assert _detect_claude_strength("extract key findings from this report") == "doc_extraction"

    def test_returns_none_for_generic_summary(self):
        """Short generic summaries go to local model -- no promotion needed."""
        assert _detect_claude_strength("summarize the last 7 days") is None

    def test_returns_none_for_weather_query(self):
        assert _detect_claude_strength("what is the weather in New York") is None

    def test_returns_none_for_simple_list(self):
        assert _detect_claude_strength("list all pending tickets") is None

    def test_returns_none_for_memory_write(self):
        assert _detect_claude_strength("remember that the deploy is on Friday") is None

    def test_case_insensitive(self):
        assert _detect_claude_strength("CODE REVIEW of authentication.py") == "code_review"

    def test_returns_none_empty_string(self):
        assert _detect_claude_strength("") is None


# ---------------------------------------------------------------------------
# run_executor auto-promote integration tests
# ---------------------------------------------------------------------------

class TestRunExecutorAutoPromote:
    """Integration tests for run_executor() auto-promote behavior.

    NOTE: _EXECUTORS holds direct function references, so patching the function
    by name (patch("...._agent")) does NOT intercept dispatch through the dict.
    All tests that need to intercept an executor use patch.dict on _EXECUTORS.
    """

    def test_promotes_to_claude_task_when_env_on(self, tmp_path, monkeypatch):
        """When ATLAS_CLAUDE_AUTO_PROMOTE=on and pattern matches, dispatches claude_task."""
        monkeypatch.setenv("ATLAS_CLAUDE_AUTO_PROMOTE", "on")
        task, ctx = _ctx(tmp_path, "agent", "code review of the auth module")
        dispatched = []

        def fake_claude(ctx):
            dispatched.append("claude_task")
            return ExecutionResult(status="done", text="Claude output", artifact_path=None)

        with patch.dict("atlas_plus.brain.executors._EXECUTORS", {"claude_task": fake_claude}):
            result = run_executor(task, ctx)

        assert dispatched == ["claude_task"]
        assert result.status == "done"
        assert "claude-suggest" not in result.text

    def test_suggests_when_env_off(self, tmp_path, monkeypatch):
        """When env var is absent, pattern match produces suggestion in result text."""
        monkeypatch.delenv("ATLAS_CLAUDE_AUTO_PROMOTE", raising=False)
        task, ctx = _ctx(tmp_path, "agent", "code review of the auth module")
        agent_result = ExecutionResult(status="done", text="Local model output.", artifact_path=None)

        with patch.dict("atlas_plus.brain.executors._EXECUTORS", {"agent": _fake_executor(agent_result)}):
            result = run_executor(task, ctx)

        assert result.status == "done"
        assert "claude-suggest" in result.text
        assert "code_review" in result.text
        assert "Local model output." in result.text

    def test_no_suggestion_for_generic_instruction(self, tmp_path, monkeypatch):
        """Generic instructions (no pattern match) produce no suggestion note."""
        monkeypatch.delenv("ATLAS_CLAUDE_AUTO_PROMOTE", raising=False)
        task, ctx = _ctx(tmp_path, "agent", "list all pending tickets")
        agent_result = ExecutionResult(status="done", text="Ticket list here.", artifact_path=None)

        with patch.dict("atlas_plus.brain.executors._EXECUTORS", {"agent": _fake_executor(agent_result)}):
            result = run_executor(task, ctx)

        assert "claude-suggest" not in result.text
        assert result.text == "Ticket list here."

    def test_explicit_kind_bypasses_pattern_check(self, tmp_path, monkeypatch):
        """Tasks with explicit non-agent kind skip auto-promote even if instruction matches."""
        monkeypatch.setenv("ATLAS_CLAUDE_AUTO_PROMOTE", "on")
        task, ctx = _ctx(tmp_path, "verify", "code review of the auth module")
        verify_result = ExecutionResult(status="done", text="Verify ran.", artifact_path=None)

        with patch.dict("atlas_plus.brain.executors._EXECUTORS", {"verify": _fake_executor(verify_result)}):
            result = run_executor(task, ctx)

        assert result.text == "Verify ran."
        assert "claude-suggest" not in result.text

    def test_sip_pattern_triggers_promotion(self, tmp_path, monkeypatch):
        """SIP-specific instruction promotes to claude_task when env is on."""
        monkeypatch.setenv("ATLAS_CLAUDE_AUTO_PROMOTE", "on")
        task, ctx = _ctx(tmp_path, "agent", "analyze the sip trace headers for registration failure")
        dispatched = []

        def fake_claude(ctx):
            dispatched.append("claude_task")
            return ExecutionResult(status="done", text="SIP analysis done.", artifact_path=None)

        with patch.dict("atlas_plus.brain.executors._EXECUTORS", {"claude_task": fake_claude}):
            result = run_executor(task, ctx)

        assert dispatched == ["claude_task"]

    def test_suggestion_preserves_original_status(self, tmp_path, monkeypatch):
        """Suggestion mode preserves the original executor's status (e.g. failed)."""
        monkeypatch.delenv("ATLAS_CLAUDE_AUTO_PROMOTE", raising=False)
        task, ctx = _ctx(tmp_path, "agent", "refactor the entire codebase")
        failed_result = ExecutionResult(status="failed", text="Ollama timed out.", artifact_path=None)

        with patch.dict("atlas_plus.brain.executors._EXECUTORS", {"agent": _fake_executor(failed_result)}):
            result = run_executor(task, ctx)

        assert result.status == "failed"
        assert "claude-suggest" in result.text
        assert "Ollama timed out." in result.text

    def test_suggestion_preserves_artifact_path(self, tmp_path, monkeypatch):
        """Suggestion mode preserves the original artifact_path."""
        monkeypatch.delenv("ATLAS_CLAUDE_AUTO_PROMOTE", raising=False)
        task, ctx = _ctx(tmp_path, "agent", "explain this module")
        sentinel_path = tmp_path / "some-artifact.md"
        with_artifact = ExecutionResult(status="done", text="Explanation here.", artifact_path=sentinel_path)

        with patch.dict("atlas_plus.brain.executors._EXECUTORS", {"agent": _fake_executor(with_artifact)}):
            result = run_executor(task, ctx)

        assert result.artifact_path == sentinel_path

    def test_research_synthesis_triggers_suggestion(self, tmp_path, monkeypatch):
        """research_synthesis group produces suggestion with correct group name."""
        monkeypatch.delenv("ATLAS_CLAUDE_AUTO_PROMOTE", raising=False)
        task, ctx = _ctx(tmp_path, "agent", "synthesize findings from these three research reports")
        summary_result = ExecutionResult(status="done", text="Summary.", artifact_path=None)

        with patch.dict("atlas_plus.brain.executors._EXECUTORS", {"agent": _fake_executor(summary_result)}):
            result = run_executor(task, ctx)

        assert "research_synthesis" in result.text
        assert "claude-suggest" in result.text
