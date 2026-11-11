"""Cross-provider tool-calling seam (LLM-agnostic Brain, Increment 1).

Per docs/LLM-AGNOSTIC-BRAIN-tool-calling.md: providers accept the neutral
Atlas tool schema, parse their own wire responses into ChatResponse.tool_calls,
and serialize assistant/tool-result history turns. All transports injected;
no model is ever touched.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from atlas_plus.llm import (
    AnthropicProvider,
    OllamaProvider,
    OpenAIProvider,
    ProviderError,
    ToolCall,
)
from atlas_plus.llm.base import ChatResponse, ModelProvider

NEUTRAL_TOOLS = [{
    "name": "memory_search",
    "description": "Search the memory corpus.",
    "parameters": {"type": "object", "properties": {"query": {"type": "string"}},
                   "required": ["query"]},
}]


# ---------------------------------------------------------------------------
# Ollama
# ---------------------------------------------------------------------------

class TestOllamaTools:
    def _provider(self, response, captured):
        def post(url, payload, timeout):
            captured["url"] = url
            captured["payload"] = payload
            return response
        return OllamaProvider(post=post, default_model="qwen3:8b")

    def test_request_wraps_neutral_schema(self):
        captured = {}
        p = self._provider({"message": {"content": "hi"}}, captured)
        p.chat("q", tools=NEUTRAL_TOOLS)
        sent = captured["payload"]["tools"]
        assert sent == [{"type": "function", "function": NEUTRAL_TOOLS[0]}]

    def test_no_tools_key_when_absent(self):
        captured = {}
        p = self._provider({"message": {"content": "hi"}}, captured)
        p.chat("q")
        assert "tools" not in captured["payload"]

    def test_parses_tool_calls_with_dict_args(self):
        resp = {"message": {"content": "", "tool_calls": [
            {"function": {"name": "memory_search", "arguments": {"query": "sextant"}}},
        ]}}
        p = self._provider(resp, {})
        out = p.chat("q", tools=NEUTRAL_TOOLS)
        assert out.tool_calls == [ToolCall(id="call_0", name="memory_search",
                                           arguments={"query": "sextant"})]

    def test_parses_json_string_args(self):
        resp = {"message": {"content": "", "tool_calls": [
            {"function": {"name": "t", "arguments": '{"a": 1}'}},
        ]}}
        p = self._provider(resp, {})
        assert p.chat("q").tool_calls[0].arguments == {"a": 1}

    def test_assistant_and_result_serializers(self):
        p = OllamaProvider(post=lambda *a: {}, default_model="m")
        r = ChatResponse(content="", tool_calls=[ToolCall("call_0", "t", {"a": 1})])
        assistant = p.format_assistant_message(r)
        assert assistant["role"] == "assistant"
        assert assistant["tool_calls"][0]["function"]["name"] == "t"
        result = p.format_tool_result(r.tool_calls[0], "out")
        assert result == [{"role": "tool", "content": "out"}]


# ---------------------------------------------------------------------------
# OpenAI-compatible
# ---------------------------------------------------------------------------

class TestOpenAITools:
    def _provider(self, response, captured):
        def post(url, payload, headers, timeout):
            captured["url"] = url
            captured["payload"] = payload
            return response
        return OpenAIProvider(post=post, api_key="k", default_model="gpt-4o-mini")

    def test_request_wraps_neutral_schema(self):
        captured = {}
        p = self._provider({"choices": [{"message": {"content": "hi"}}]}, captured)
        p.chat("q", tools=NEUTRAL_TOOLS)
        assert captured["payload"]["tools"][0]["function"]["name"] == "memory_search"

    def test_parses_tool_calls_decoding_json_arguments(self):
        resp = {"choices": [{"message": {"content": None, "tool_calls": [
            {"id": "abc123", "type": "function",
             "function": {"name": "memory_search", "arguments": '{"query": "x"}'}},
        ]}}]}
        p = self._provider(resp, {})
        calls = p.chat("q", tools=NEUTRAL_TOOLS).tool_calls
        assert calls == [ToolCall(id="abc123", name="memory_search", arguments={"query": "x"})]

    def test_bad_json_arguments_preserved_raw(self):
        resp = {"choices": [{"message": {"tool_calls": [
            {"id": "1", "function": {"name": "t", "arguments": "not json"}},
        ]}}]}
        p = self._provider(resp, {})
        assert p.chat("q").tool_calls[0].arguments == {"_raw": "not json"}

    def test_assistant_serializer_reencodes_json(self):
        p = self._provider({}, {})
        r = ChatResponse(content="", tool_calls=[ToolCall("id9", "t", {"a": 1})])
        msg = p.format_assistant_message(r)
        assert msg["tool_calls"][0]["id"] == "id9"
        assert json.loads(msg["tool_calls"][0]["function"]["arguments"]) == {"a": 1}

    def test_tool_result_carries_call_id(self):
        p = self._provider({}, {})
        out = p.format_tool_result(ToolCall("id9", "t", {}), "res")
        assert out == [{"role": "tool", "tool_call_id": "id9", "content": "res"}]


# ---------------------------------------------------------------------------
# Anthropic
# ---------------------------------------------------------------------------

class _FakeAnthropicClient:
    def __init__(self, response):
        self._response = response
        self.captured = None
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs):
        self.captured = kwargs
        return self._response


def _anthropic_response(blocks):
    return SimpleNamespace(content=blocks)


class TestAnthropicTools:
    def test_request_translates_parameters_to_input_schema(self):
        client = _FakeAnthropicClient(_anthropic_response([SimpleNamespace(type="text", text="hi")]))
        p = AnthropicProvider(client=client, default_model="claude-haiku-4-5-20251001")
        p.chat("q", tools=NEUTRAL_TOOLS)
        sent = client.captured["tools"]
        assert sent[0]["name"] == "memory_search"
        assert sent[0]["input_schema"]["required"] == ["query"]
        assert "parameters" not in sent[0]

    def test_parses_tool_use_blocks(self):
        blocks = [
            SimpleNamespace(type="text", text="Thinking. "),
            SimpleNamespace(type="tool_use", id="tu_1", name="memory_search",
                            input={"query": "decay"}),
        ]
        client = _FakeAnthropicClient(_anthropic_response(blocks))
        p = AnthropicProvider(client=client, default_model="m")
        out = p.chat("q", tools=NEUTRAL_TOOLS)
        assert out.content == "Thinking."
        assert out.tool_calls == [ToolCall(id="tu_1", name="memory_search",
                                           arguments={"query": "decay"})]

    def test_assistant_turn_is_content_blocks(self):
        p = AnthropicProvider(client=_FakeAnthropicClient(None), default_model="m")
        r = ChatResponse(content="ok", tool_calls=[ToolCall("tu_1", "t", {"a": 1})])
        msg = p.format_assistant_message(r)
        assert msg["role"] == "assistant"
        assert msg["content"][0] == {"type": "text", "text": "ok"}
        assert msg["content"][1]["type"] == "tool_use"

    def test_tool_result_is_user_turn_with_block(self):
        p = AnthropicProvider(client=_FakeAnthropicClient(None), default_model="m")
        out = p.format_tool_result(ToolCall("tu_1", "t", {}), "res")
        assert out[0]["role"] == "user"
        assert out[0]["content"][0]["tool_use_id"] == "tu_1"


# ---------------------------------------------------------------------------
# Base / normalizer behavior underpinning the loop
# ---------------------------------------------------------------------------

class TestSeamFoundations:
    def test_normalizer_preserves_extra_keys_and_raw_content(self):
        from atlas_plus.llm import normalize_messages
        wire = {"role": "assistant", "content": [{"type": "text", "text": "x"}],
                "tool_calls": [{"id": "1"}]}
        msgs = normalize_messages([wire])
        assert msgs[0].extra == {"tool_calls": [{"id": "1"}]}
        assert msgs[0].as_dict() == wire

    def test_base_provider_refuses_tool_serialization(self):
        p = ModelProvider()
        with pytest.raises(ProviderError):
            p.format_assistant_message(ChatResponse(content=""))
        with pytest.raises(ProviderError):
            p.format_tool_result(ToolCall("1", "t", {}), "x")

    def test_round_trip_history_openai(self):
        """A full simulated round: request -> tool call -> result -> follow-up request
        carries wire-correct history through normalize_messages untouched."""
        captured = {}

        def post(url, payload, headers, timeout):
            captured.setdefault("payloads", []).append(payload)
            if len(captured["payloads"]) == 1:
                return {"choices": [{"message": {"content": None, "tool_calls": [
                    {"id": "c1", "function": {"name": "t", "arguments": "{}"}}]}}]}
            return {"choices": [{"message": {"content": "done"}}]}

        p = OpenAIProvider(post=post, api_key="k", default_model="m")
        history = [{"role": "user", "content": "go"}]
        first = p.chat(history, tools=NEUTRAL_TOOLS)
        assert first.tool_calls
        history.append(p.format_assistant_message(first))
        history.extend(p.format_tool_result(first.tool_calls[0], "tool says hi"))
        second = p.chat(history, tools=NEUTRAL_TOOLS)
        assert second.content == "done"
        sent = captured["payloads"][1]["messages"]
        assert sent[1]["tool_calls"][0]["id"] == "c1"
        assert sent[2] == {"role": "tool", "tool_call_id": "c1", "content": "tool says hi"}
