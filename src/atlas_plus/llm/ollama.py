"""Ollama provider: local models via /api/chat and /api/embed."""

from __future__ import annotations

import json
import os
from typing import Callable, Optional

from .base import ChatResponse, ModelProvider, ToolCall

# Injectable transport: (url, json, timeout) -> parsed json dict. Real default uses httpx.
PostFn = Callable[[str, dict, float], dict]


_SSL_CTX = None


def _shared_ssl_context():
    global _SSL_CTX
    if _SSL_CTX is None:
        import httpx as _httpx
        try:
            _SSL_CTX = _httpx.create_ssl_context()
        except Exception:  # noqa: BLE001
            _SSL_CTX = True
    return _SSL_CTX


def _httpx_post(url: str, json: dict, timeout: float) -> dict:
    import httpx

    resp = httpx.post(url, json=json, timeout=timeout, verify=_shared_ssl_context())  # one TLS context, not one per call (AG54: 0.43 s each)
    resp.raise_for_status()
    return resp.json()


class OllamaProvider(ModelProvider):
    name = "ollama"

    def __init__(self, *, base_url: Optional[str] = None, default_model: Optional[str] = None,
                 default_embed_model: Optional[str] = None, post: Optional[PostFn] = None) -> None:
        super().__init__(
            default_model=default_model or os.environ.get("ATLAS_BRAIN_MODEL", "qwen3:8b"),
            default_embed_model=default_embed_model or os.environ.get("ATLAS_EMBED_MODEL", "nomic-embed-text"),
        )
        self.base_url = (base_url or __import__('atlas_plus.llm.ollama_url', fromlist=['default_ollama_url']).default_ollama_url()).rstrip("/")
        self._post = post or _httpx_post

    def _chat(self, messages, *, model, temperature, max_tokens, timeout, **kw):
        payload = {
            "model": model,
            "messages": [m.as_dict() for m in messages],
            "stream": False,
            "options": {"temperature": temperature},
        }
        tools = kw.get("tools")
        if tools:
            payload["tools"] = [{"type": "function", "function": dict(t)} for t in tools]
        data = self._post(f"{self.base_url}/api/chat", payload, timeout)
        msg = (data.get("message") or {}) if isinstance(data, dict) else {}
        content = (msg.get("content") or "").strip()
        calls: list[ToolCall] = []
        for i, tc in enumerate(msg.get("tool_calls") or []):
            fn = tc.get("function") or {}
            args = fn.get("arguments") or {}
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except ValueError:
                    args = {"_raw": args}
            calls.append(ToolCall(id=str(tc.get("id") or f"call_{i}"),
                                  name=str(fn.get("name", "")), arguments=args))
        return ChatResponse(content=content, model=model, tool_calls=calls,
                            raw=data if isinstance(data, dict) else {},
                            prompt_tokens=int(data.get("prompt_eval_count") or 0)
                            if isinstance(data, dict) else 0,
                            completion_tokens=int(data.get("eval_count") or 0)
                            if isinstance(data, dict) else 0)

    # Ollama has no call ids: results correlate positionally.
    def format_assistant_message(self, response):
        msg = {"role": "assistant", "content": response.content}
        if response.tool_calls:
            msg["tool_calls"] = [
                {"function": {"name": c.name, "arguments": c.arguments}}
                for c in response.tool_calls
            ]
        return msg

    def format_tool_result(self, call, output):
        return [{"role": "tool", "content": str(output)}]

    def embed(self, text, *, model=None, timeout=30.0):
        # /api/embed, not legacy /api/embeddings: newer Ollama hard-errors
        # the legacy endpoint on over-context input instead of truncating
        # (same break found in the memory embedder, 2026-10-04).
        m = model or self.default_embed_model
        data = self._post(f"{self.base_url}/api/embed", {"model": m, "input": text}, timeout)
        rows = (data or {}).get("embeddings") or []
        return list(rows[0]) if rows else []
