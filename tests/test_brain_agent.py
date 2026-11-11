"""Tests for atlas_plus.brain.agent -- provider-agnostic loop with tool dispatch.

The seam is `_brain_chat`, which returns a provider-neutral ChatResponse
(Increment 2 of docs/LLM-AGNOSTIC-BRAIN-tool-calling.md, 2026-07-31); mocks
build ChatResponse/ToolCall instead of raw Ollama dicts.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from atlas_plus.brain import agent as ag
from atlas_plus.brain import history as hist
from atlas_plus.llm import ChatResponse, ToolCall


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ollama_text_response(text: str = "Hello from brain.") -> ChatResponse:
    return ChatResponse(content=text)


def _ollama_tool_response(tool_name: str, args: dict, follow_up_text: str = "Done.") -> list[ChatResponse]:
    """Returns a two-element sequence: tool-call response then text response."""
    return [
        ChatResponse(content="", tool_calls=[ToolCall(id="call_0", name=tool_name, arguments=args)]),
        _ollama_text_response(follow_up_text),
    ]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.fixture()
def db(tmp_path):
    return tmp_path / "state.sqlite"


def test_simple_text_response(tmp_path, db):
    with patch.object(ag, "_brain_chat", return_value=_ollama_text_response("hi!")):
        result = ag.chat(
            session_id="test-s1",
            user_message="hello",
            ollama_base_url="http://localhost:11434",
            memory_root=tmp_path,
            history_db_path=db,
        )

    assert result.text == "hi!"
    assert result.tool_calls_count == 0


def test_tool_call_dispatched(tmp_path, db):
    responses = _ollama_tool_response(
        "memory_search", {"query": "atlas"}, follow_up_text="Found some facts."
    )
    call_iter = iter(responses)

    with patch.object(ag, "_brain_chat", side_effect=lambda *a, **kw: next(call_iter)):
        with patch.object(ag.brain_tools, "dispatch", return_value='{"results": []}') as mock_dispatch:
            result = ag.chat(
                session_id="test-s2",
                user_message="what do you know about atlas?",
                ollama_base_url="http://localhost:11434",
                memory_root=tmp_path,
                history_db_path=db,
            )

    mock_dispatch.assert_called_once_with("memory_search", {"query": "atlas"})
    assert result.text == "Found some facts."
    assert result.tool_calls_count == 1


def test_circuit_breaker(tmp_path, db):
    infinite_tool = ChatResponse(
        content="", tool_calls=[ToolCall(id="call_0", name="memory_list", arguments={})],
    )

    with patch.object(ag, "_brain_chat", return_value=infinite_tool):
        with patch.object(ag.brain_tools, "dispatch", return_value='{"files": []}'):
            result = ag.chat(
                session_id="test-circuit",
                user_message="loop forever",
                ollama_base_url="http://localhost:11434",
                memory_root=tmp_path,
                history_db_path=db,
            )

    assert result.tool_calls_count == ag._MAX_TOOL_ROUNDS
    assert "maximum tool call rounds" in result.text


def test_ollama_unreachable(tmp_path, db):
    import httpx

    with patch.object(ag, "_brain_chat", side_effect=httpx.ConnectError("refused")):
        result = ag.chat(
            session_id="test-err",
            user_message="hello",
            ollama_base_url="http://localhost:11434",
            memory_root=tmp_path,
            history_db_path=db,
        )

    assert "Brain error" in result.text
    assert result.tool_calls_count == 0


def test_tool_args_string_parsed(tmp_path, db):
    """JSON-string arguments decode in the PROVIDER layer (test_llm_tools);
    the loop contract is that dispatch always receives a parsed dict."""
    string_args_response = ChatResponse(
        content="",
        tool_calls=[ToolCall(id="call_0", name="memory_search", arguments={"query": "test"})],
    )
    responses = [string_args_response, _ollama_text_response("parsed ok")]
    call_iter = iter(responses)

    with patch.object(ag, "_brain_chat", side_effect=lambda *a, **kw: next(call_iter)):
        with patch.object(ag.brain_tools, "dispatch", return_value="{}") as mock_dispatch:
            result = ag.chat(
                session_id="test-strargs",
                user_message="search for test",
                ollama_base_url="http://localhost:11434",
                memory_root=tmp_path,
                history_db_path=db,
            )

    mock_dispatch.assert_called_once_with("memory_search", {"query": "test"})
    assert result.text == "parsed ok"


# ---------------------------------------------------------------------------
# Extended coverage
# ---------------------------------------------------------------------------

class TestAgentHistoryPersistence:
    def test_user_message_persisted_to_history(self, tmp_path):
        """chat() must write user message to history DB."""
        db = tmp_path / "state.sqlite"
        with patch.object(ag, "_brain_chat", return_value=_ollama_text_response("ok")):
            ag.chat(
                session_id="persist-test",
                user_message="remember this",
                ollama_base_url="http://localhost:11434",
                memory_root=tmp_path,
                history_db_path=db,
            )
        msgs = hist.get_messages("persist-test", db_path=db)
        roles = [m["role"] for m in msgs]
        assert "user" in roles
        assert "assistant" in roles

    def test_subsequent_call_loads_prior_history(self, tmp_path):
        """Second chat() call for same session has prior context in messages."""
        db = tmp_path / "state.sqlite"
        with patch.object(ag, "_brain_chat", return_value=_ollama_text_response("first")) as mock:
            ag.chat(
                session_id="context-test",
                user_message="turn one",
                ollama_base_url="http://localhost:11434",
                memory_root=tmp_path,
                history_db_path=db,
            )
        with patch.object(ag, "_brain_chat", return_value=_ollama_text_response("second")) as mock2:
            ag.chat(
                session_id="context-test",
                user_message="turn two",
                ollama_base_url="http://localhost:11434",
                memory_root=tmp_path,
                history_db_path=db,
            )
        # The second call should have been given prior messages in its context
        call_args = mock2.call_args
        messages_arg = call_args[0][0]  # first positional arg
        roles = [m["role"] for m in messages_arg if m["role"] != "system"]
        assert "user" in roles  # prior turns present

    def test_caller_identity_stored_in_history(self, tmp_path):
        """caller_identity is passed to add_message so rate limiting can use it."""
        db = tmp_path / "state.sqlite"
        with patch.object(ag, "_brain_chat", return_value=_ollama_text_response("hi")), \
             patch.object(hist, "add_message") as mock_add:
            ag.chat(
                session_id="identity-test",
                user_message="hello",
                ollama_base_url="http://localhost:11434",
                memory_root=tmp_path,
                history_db_path=db,
                caller_identity="user-123",
            )
        # First add_message call is for the user message; check identity passed
        first_call_kwargs = mock_add.call_args_list[0][1]
        assert first_call_kwargs.get("caller_identity") == "user-123"


class TestAgentResponseFields:
    def test_response_has_session_id(self, tmp_path):
        db = tmp_path / "state.sqlite"
        with patch.object(ag, "_brain_chat", return_value=_ollama_text_response("hi")):
            result = ag.chat(
                session_id="resp-test",
                user_message="hello",
                ollama_base_url="http://localhost:11434",
                memory_root=tmp_path,
                history_db_path=db,
            )
        assert result.session_id == "resp-test"

    def test_response_tool_log_populated(self, tmp_path):
        db = tmp_path / "state.sqlite"
        responses = _ollama_tool_response("memory_list", {}, follow_up_text="listed")
        call_iter = iter(responses)
        with patch.object(ag, "_brain_chat", side_effect=lambda *a, **kw: next(call_iter)):
            with patch.object(ag.brain_tools, "dispatch", return_value='[]'):
                result = ag.chat(
                    session_id="log-test",
                    user_message="list memory",
                    ollama_base_url="http://localhost:11434",
                    memory_root=tmp_path,
                    history_db_path=db,
                )
        assert result.tool_calls_count == 1
        assert len(result.tool_log) == 1
        assert result.tool_log[0]["tool"] == "memory_list"


# ---------------------------------------------------------------------------
# progress_fn callback
# ---------------------------------------------------------------------------

class TestProgressFn:
    """chat() fires progress_fn before each tool dispatch."""

    def test_progress_fn_called_for_each_tool(self, tmp_path):
        db = tmp_path / "state.sqlite"
        responses = _ollama_tool_response("memory_search", {"query": "sip"}, "Done.")
        call_iter = iter(responses)
        events: list[dict] = []

        def capture(event: dict) -> None:
            events.append(event)

        with patch.object(ag, "_brain_chat", side_effect=lambda *a, **kw: next(call_iter)), \
             patch.object(ag.brain_tools, "dispatch", return_value="results"):
            ag.chat(
                session_id="prog-test",
                user_message="search for sip",
                ollama_base_url="http://localhost:11434",
                memory_root=tmp_path,
                history_db_path=db,
                progress_fn=capture,
            )
        assert len(events) == 1
        assert events[0]["type"] == "tool_call"
        assert events[0]["tool"] == "memory_search"
        assert events[0]["round"] == 1

    def test_progress_fn_not_called_when_no_tools(self, tmp_path):
        db = tmp_path / "state.sqlite"
        events: list[dict] = []

        with patch.object(ag, "_brain_chat", return_value=_ollama_text_response("hi")):
            ag.chat(
                session_id="no-tool-test",
                user_message="hello",
                ollama_base_url="http://localhost:11434",
                memory_root=tmp_path,
                history_db_path=db,
                progress_fn=events.append,
            )
        assert events == []

    def test_progress_fn_exception_does_not_abort_chat(self, tmp_path):
        """A raising progress_fn must not stop the agent loop."""
        db = tmp_path / "state.sqlite"
        responses = _ollama_tool_response("memory_list", {}, "Done.")
        call_iter = iter(responses)

        def bad_fn(event: dict) -> None:
            raise RuntimeError("progress error")

        with patch.object(ag, "_brain_chat", side_effect=lambda *a, **kw: next(call_iter)), \
             patch.object(ag.brain_tools, "dispatch", return_value="[]"):
            result = ag.chat(
                session_id="bad-fn-test",
                user_message="list",
                ollama_base_url="http://localhost:11434",
                memory_root=tmp_path,
                history_db_path=db,
                progress_fn=bad_fn,
            )
        assert result.text == "Done."

    def test_progress_fn_called_multiple_rounds(self, tmp_path):
        """Each tool call in a multi-round chain fires its own progress event."""
        db = tmp_path / "state.sqlite"
        # Two tool-call rounds followed by a text response
        r1 = ChatResponse(content="", tool_calls=[ToolCall(id="c1", name="t1", arguments={})])
        r2 = ChatResponse(content="", tool_calls=[ToolCall(id="c2", name="t2", arguments={})])
        r3 = _ollama_text_response("all done")
        responses = iter([r1, r2, r3])
        events: list[dict] = []

        with patch.object(ag, "_brain_chat", side_effect=lambda *a, **kw: next(responses)), \
             patch.object(ag.brain_tools, "dispatch", return_value="ok"):
            ag.chat(
                session_id="multi-round",
                user_message="go",
                ollama_base_url="http://localhost:11434",
                memory_root=tmp_path,
                history_db_path=db,
                progress_fn=events.append,
            )
        assert len(events) == 2
        assert events[0]["tool"] == "t1"
        assert events[0]["round"] == 1
        assert events[1]["tool"] == "t2"
        assert events[1]["round"] == 2
