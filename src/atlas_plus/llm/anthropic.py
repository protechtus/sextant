"""Anthropic provider: Claude via the official SDK. Chat only (no embeddings)."""

from __future__ import annotations

import os
from typing import Optional

from .base import ChatResponse, ModelProvider, ProviderUnavailable, ToolCall


class AnthropicProvider(ModelProvider):
    name = "anthropic"

    def __init__(self, *, api_key: Optional[str] = None, default_model: Optional[str] = None,
                 client: object = None) -> None:
        super().__init__(
            default_model=default_model or os.environ.get("ATLAS_SUMMARIZER_CLAUDE_MODEL",
                                                          "claude-haiku-4-5-20251001"),
        )
        self.api_key = api_key if api_key is not None else os.environ.get("ANTHROPIC_API_KEY", "")
        self._client = client  # injectable for tests

    def available(self) -> bool:
        return self._client is not None or bool(self.api_key)

    def _get_client(self):
        if self._client is not None:
            return self._client
        if not self.api_key:
            raise ProviderUnavailable("ANTHROPIC_API_KEY not set")
        import anthropic

        return anthropic.Anthropic(api_key=self.api_key)

    def _chat(self, messages, *, model, temperature, max_tokens, timeout, **kw):
        client = self._get_client()
        system = "\n".join(str(m.content) for m in messages if m.role == "system").strip()
        convo = [m.as_dict() for m in messages if m.role != "system"]
        kwargs = dict(model=model, max_tokens=max_tokens, temperature=temperature, messages=convo)
        if system:
            kwargs["system"] = system
        tools = kw.get("tools")
        if tools:
            # Anthropic wire schema: parameters -> input_schema.
            kwargs["tools"] = [
                {"name": t.get("name", ""), "description": t.get("description", ""),
                 "input_schema": t.get("parameters") or {"type": "object", "properties": {}}}
                for t in tools
            ]
        resp = client.messages.create(**kwargs)
        text_parts: list[str] = []
        calls: list[ToolCall] = []
        try:
            for block in resp.content:
                btype = getattr(block, "type", "")
                if btype == "tool_use":
                    calls.append(ToolCall(id=str(getattr(block, "id", "")),
                                          name=str(getattr(block, "name", "")),
                                          arguments=dict(getattr(block, "input", {}) or {})))
                else:
                    text_parts.append(getattr(block, "text", ""))
            content = "".join(text_parts)
        except Exception:  # noqa: BLE001 - tolerate SDK shape drift
            content = str(getattr(resp, "content", resp))
        usage = getattr(resp, "usage", None)
        return ChatResponse(content=content.strip(), model=model, tool_calls=calls, raw={},
                            prompt_tokens=int(getattr(usage, "input_tokens", 0) or 0),
                            completion_tokens=int(getattr(usage, "output_tokens", 0) or 0))

    # Anthropic: tool calls and results are CONTENT BLOCKS; results return as
    # a "user" turn, not a "tool" turn (see LLM-AGNOSTIC-BRAIN design table).
    def format_assistant_message(self, response):
        blocks: list[dict] = []
        if response.content:
            blocks.append({"type": "text", "text": response.content})
        for c in response.tool_calls:
            blocks.append({"type": "tool_use", "id": c.id, "name": c.name, "input": c.arguments})
        return {"role": "assistant", "content": blocks}

    def format_tool_result(self, call, output):
        return [{
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": call.id, "content": str(output)}],
        }]
