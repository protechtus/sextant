"""Tests for atlas_plus.brain.escalation -- failure/quality escalation logic.

Covers should_escalate_failure, should_escalate_quality,
build_escalation_instruction, and create_escalation_task.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# TestShouldEscalateFailure
# ---------------------------------------------------------------------------

class TestShouldEscalateFailure:
    def test_returns_false_when_escalation_disabled(self):
        from atlas_plus.brain.escalation import should_escalate_failure
        with patch.dict("os.environ", {"ATLAS_CLAUDE_ESCALATION": "off"}):
            assert should_escalate_failure("brain", "sess-001", "agent") is False

    def test_returns_true_when_enabled_and_conditions_met(self):
        from atlas_plus.brain.escalation import should_escalate_failure
        with patch.dict("os.environ", {"ATLAS_CLAUDE_ESCALATION": "on"}):
            assert should_escalate_failure("brain", "sess-001", "agent") is True

    def test_returns_false_for_non_agent_kind(self):
        from atlas_plus.brain.escalation import should_escalate_failure
        with patch.dict("os.environ", {"ATLAS_CLAUDE_ESCALATION": "on"}):
            assert should_escalate_failure("brain", "sess-001", "temporal_build") is False
            assert should_escalate_failure("brain", "sess-001", "curation_report") is False
            assert should_escalate_failure("brain", "sess-001", "verify") is False

    def test_returns_false_for_escalated_session_prefix(self):
        from atlas_plus.brain.escalation import should_escalate_failure
        with patch.dict("os.environ", {"ATLAS_CLAUDE_ESCALATION": "on"}):
            assert should_escalate_failure("brain", "escalated-sess-001", "agent") is False

    def test_returns_false_for_terminal_agents(self):
        from atlas_plus.brain.escalation import should_escalate_failure
        with patch.dict("os.environ", {"ATLAS_CLAUDE_ESCALATION": "on"}):
            assert should_escalate_failure("claude-code", "sess-001", "agent") is False
            assert should_escalate_failure("opencode", "sess-001", "agent") is False
            assert should_escalate_failure("codex", "sess-001", "agent") is False

    def test_returns_false_for_batch_kind(self):
        from atlas_plus.brain.escalation import should_escalate_failure
        with patch.dict("os.environ", {"ATLAS_CLAUDE_ESCALATION": "on"}):
            # Only "agent" kind triggers escalation
            assert should_escalate_failure("brain", "sess-001", "batch") is False

    def test_returns_false_for_none_agent(self):
        from atlas_plus.brain.escalation import should_escalate_failure
        with patch.dict("os.environ", {"ATLAS_CLAUDE_ESCALATION": "on"}):
            # None agent is not in _TERMINAL_AGENTS -- should escalate
            assert should_escalate_failure(None, "sess-001", "agent") is True


# ---------------------------------------------------------------------------
# TestShouldEscalateQuality
# ---------------------------------------------------------------------------

class TestShouldEscalateQuality:
    def test_returns_false_when_quality_escalation_disabled(self):
        from atlas_plus.brain.escalation import should_escalate_quality
        with patch.dict("os.environ", {"ATLAS_CLAUDE_QUALITY_ESCALATION": "off"}):
            assert should_escalate_quality("brain", "sess-001", "agent", 1.0) is False

    def test_returns_false_when_quality_above_threshold(self):
        from atlas_plus.brain.escalation import should_escalate_quality
        with patch.dict("os.environ", {
            "ATLAS_CLAUDE_QUALITY_ESCALATION": "on",
            "ATLAS_CLAUDE_QUALITY_THRESHOLD": "3.5",
        }):
            assert should_escalate_quality("brain", "sess-001", "agent", 4.0) is False
            assert should_escalate_quality("brain", "sess-001", "agent", 3.5) is False

    def test_returns_true_when_quality_below_threshold(self):
        from atlas_plus.brain.escalation import should_escalate_quality
        with patch.dict("os.environ", {
            "ATLAS_CLAUDE_QUALITY_ESCALATION": "on",
            "ATLAS_CLAUDE_QUALITY_THRESHOLD": "3.5",
        }):
            assert should_escalate_quality("brain", "sess-001", "agent", 3.0) is True
            assert should_escalate_quality("brain", "sess-001", "agent", 1.0) is True

    def test_returns_false_for_non_agent_kind(self):
        from atlas_plus.brain.escalation import should_escalate_quality
        with patch.dict("os.environ", {"ATLAS_CLAUDE_QUALITY_ESCALATION": "on"}):
            assert should_escalate_quality("brain", "sess-001", "noop", 0.0) is False

    def test_returns_false_for_escalated_session(self):
        from atlas_plus.brain.escalation import should_escalate_quality
        with patch.dict("os.environ", {"ATLAS_CLAUDE_QUALITY_ESCALATION": "on"}):
            assert should_escalate_quality("brain", "escalated-sess", "agent", 0.0) is False

    def test_returns_false_for_terminal_agents(self):
        from atlas_plus.brain.escalation import should_escalate_quality
        with patch.dict("os.environ", {"ATLAS_CLAUDE_QUALITY_ESCALATION": "on"}):
            assert should_escalate_quality("claude-code", "sess", "agent", 0.0) is False


# ---------------------------------------------------------------------------
# TestBuildEscalationInstruction
# ---------------------------------------------------------------------------

class TestBuildEscalationInstruction:
    def test_includes_original_task_id(self):
        from atlas_plus.brain.escalation import build_escalation_instruction
        result = build_escalation_instruction("task-001", "Fix the bug", "failed")
        assert "task-001" in result

    def test_includes_reason(self):
        from atlas_plus.brain.escalation import build_escalation_instruction
        result = build_escalation_instruction("task-001", "Fix the bug", "low-quality")
        assert "low-quality" in result

    def test_includes_original_instruction(self):
        from atlas_plus.brain.escalation import build_escalation_instruction
        result = build_escalation_instruction("task-001", "Fix the authentication bug", "failed")
        assert "Fix the authentication bug" in result

    def test_includes_original_result_when_provided(self):
        from atlas_plus.brain.escalation import build_escalation_instruction
        result = build_escalation_instruction(
            "task-001", "Write tests", "failed", "ImportError: module not found"
        )
        assert "ImportError" in result

    def test_skips_result_section_when_not_provided(self):
        from atlas_plus.brain.escalation import build_escalation_instruction
        result = build_escalation_instruction("task-001", "Write tests", "failed")
        assert "Original result" not in result

    def test_truncates_long_original_result(self):
        from atlas_plus.brain.escalation import build_escalation_instruction
        long_result = "x" * 2000
        result = build_escalation_instruction("task-001", "task", "failed", long_result)
        # Result preview should be truncated to ~800 chars
        assert len(result) < 3000

    def test_contains_escalated_header_marker(self):
        from atlas_plus.brain.escalation import build_escalation_instruction
        result = build_escalation_instruction("task-001", "Do something", "failed")
        assert "ESCALATED" in result


# ---------------------------------------------------------------------------
# TestCreateEscalationTask
# ---------------------------------------------------------------------------

class TestCreateEscalationTask:
    def test_creates_task_with_claude_code_agent(self):
        fake_task = MagicMock()
        fake_task.task_id = "esc-task-001"
        with patch("atlas_plus.brain.tasks.create_task", return_value=fake_task):
            from atlas_plus.brain.escalation import create_escalation_task
            result = create_escalation_task("orig-001", "sess-001", "do something", "failed")
        assert result == "esc-task-001"

    def test_uses_escalated_prefix_in_session_id(self):
        fake_task = MagicMock()
        fake_task.task_id = "esc-002"
        with patch("atlas_plus.brain.tasks.create_task", return_value=fake_task) as mock_create:
            from atlas_plus.brain.escalation import create_escalation_task
            create_escalation_task("orig-001", "sess-XYZ", "do something", "blocked")
        _, kwargs = mock_create.call_args
        assert "escalated-sess-XYZ" in kwargs.get("session_id", "")

    def test_sets_priority_1(self):
        fake_task = MagicMock()
        fake_task.task_id = "esc-003"
        with patch("atlas_plus.brain.tasks.create_task", return_value=fake_task) as mock_create:
            from atlas_plus.brain.escalation import create_escalation_task
            create_escalation_task("orig-001", "sess-001", "do something", "failed")
        _, kwargs = mock_create.call_args
        assert kwargs.get("priority") == 1

    def test_returns_none_on_task_creation_error(self):
        with patch("atlas_plus.brain.tasks.create_task", side_effect=Exception("db error")):
            from atlas_plus.brain.escalation import create_escalation_task
            result = create_escalation_task("orig-001", "sess-001", "do something", "failed")
        assert result is None

    def test_enables_dedup(self):
        fake_task = MagicMock()
        fake_task.task_id = "esc-004"
        with patch("atlas_plus.brain.tasks.create_task", return_value=fake_task) as mock_create:
            from atlas_plus.brain.escalation import create_escalation_task
            create_escalation_task("orig-001", "sess-001", "do something", "failed")
        _, kwargs = mock_create.call_args
        assert kwargs.get("dedup") is True
