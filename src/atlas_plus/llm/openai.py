"""OpenAI provider: chat + embeddings via the REST API (httpx). Works with the real OpenAI
API and any OpenAI-compatible endpoint (LM Studio, vLLM, LiteLLM proxy, ...) via OPENAI_BASE_URL.
"""

from __future__ import annotations

import json
import os
from typing import Callable, Optional

from .base import ChatResponse, ModelProvider, ToolCall

PostFn = Callable[[str, dict, dict, float], dict]


def _httpx_post(url: str, json: dict, headers: dict, timeout: float) -> dict:
    import httpx

    resp = httpx.post(url, json=json, headers=headers, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


class OpenAIProvider(ModelProvider):
    name = "openai"

    def __init__(self, *, base_url: Optional[str] = None, api_key: Optional[str] = None,
                 default_model: Optional[str] = None, default_embed_model: Optional[str] = None,
                 post: Optional[PostFn] = None) -> None:
        super().__init__(
            default_model=default_model or os.environ.get("ATLAS_OPENAI_MODEL", "gpt-4o-mini"),
            default_embed_model=default_embed_model or os.environ.get("ATLAS_OPENAI_EMBED_MODEL",
                                                                      "text-embedding-3-small"),
        )
        self.base_url = (base_url or os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")).rstrip("/")
        self.api_key = api_key if api_key is not None else os.environ.get("OPENAI_API_KEY", "")
        self._post = post or _httpx_post

    def available(self) -> bool:
        # local OpenAI-compatible servers often need no key
        return bool(self.api_key) or "localhost" in self.base_url or "127.0.0.1" in self.base_url

    def _headers(self) -> dict:
        h = {"Content-Type": "application/json"}
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        return h

    def _chat(self, messages, *, model, temperature, max_tokens, timeout, **kw):
        payload = {
            "model": model,
            "messages": [m.as_dict() for m in messages],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        tools = kw.get("tools")
        if tools:
            payload["tools"] = [{"type": "function", "function": dict(t)} for t in tools]
        data = self._post(f"{self.base_url}/chat/completions", payload, self._headers(), timeout)
        choices = data.get("choices") or [{}]
        msg = (choices[0].get("message") or {})
        content = (msg.get("content") or "").strip()
        calls: list[ToolCall] = []
        for i, tc in enumerate(msg.get("tool_calls") or []):
            fn = tc.get("function") or {}
            args = fn.get("arguments")
            if isinstance(args, str):
                try:
                    args = json.loads(args) if args.strip() else {}
                except ValueError:
                    args = {"_raw": args}
            calls.append(ToolCall(id=str(tc.get("id") or f"call_{i}"),
                                  name=str(fn.get("name", "")), arguments=args or {}))
        usage = (data.get("usage") or {}) if isinstance(data, dict) else {}
        return ChatResponse(content=content, model=model, tool_calls=calls,
                            raw=data if isinstance(data, dict) else {},
                            prompt_tokens=int(usage.get("prompt_tokens") or 0),
                            completion_tokens=int(usage.get("completion_tokens") or 0))

    def format_assistant_message(self, response):
        msg = {"role": "assistant", "content": response.content or None}
        if response.tool_calls:
            msg["tool_calls"] = [
                {"id": c.id, "type": "function",
                 "function": {"name": c.name, "arguments": json.dumps(c.arguments)}}
                for c in response.tool_calls
            ]
        return msg

    def format_tool_result(self, call, output):
        return [{"role": "tool", "tool_call_id": call.id, "content": str(output)}]

    def embed(self, text, *, model=None, timeout=30.0):
        m = model or self.default_embed_model
        data = self._post(f"{self.base_url}/embeddings", {"model": m, "input": text}, self._headers(), timeout)
        items = data.get("data") or [{}]
        return list((items[0]).get("embedding", []) or [])
