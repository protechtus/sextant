"""OpenAI-compatible capture proxy.

Forwarding HTTP proxy on `localhost:11435` (configurable) that captures every
`/v1/chat/completions` and `/v1/completions` request and response, including
streaming SSE. Captures into the canonical TranscriptStore.

Capture is best-effort and *side-effecting*: a failure in the capture logic
must not break the proxied call. The proxy returns the upstream response
verbatim.

Routing:

- Default upstream: env `OPENAI_PROXY_TARGET` or https://api.openai.com/v1.
- Per-request override: header `X-Atlas-Upstream: http://localhost:1234/v1`.
  Used for LM Studio, llama.cpp, Jan, GPT4All, OpenLLaMA-served models.

Streaming protocol: OpenAI SSE — lines `data: {...}` separated by `\\n\\n`,
terminated by `data: [DONE]\\n\\n`. Each delta is parsed; tool calls are
accumulated per index. On stream end (or upstream close) we write one
TurnRecord per role (user input, assistant output) — the input is a single
record summarising the last user message, the output is the assembled response.

Run: `python -m atlas_plus.transcripts.sources.openai_proxy`
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, AsyncIterator
from uuid import uuid4

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from atlas_plus.transcripts.index import TranscriptIndex
from atlas_plus.transcripts.store import TranscriptStore, TurnRecord

SOURCE = "openai-proxy"
DEFAULT_PORT = int(os.environ.get("ATLAS_OPENAI_PROXY_PORT", "11435"))
DEFAULT_TARGET = os.environ.get("OPENAI_PROXY_TARGET", "https://api.openai.com/v1")

log = logging.getLogger("atlas.openai-proxy")

app = FastAPI(title="Atlas OpenAI capture proxy", version="0.1.0")
_store = TranscriptStore()
_index = TranscriptIndex()
_client = httpx.AsyncClient(timeout=httpx.Timeout(connect=10.0, read=600.0, write=60.0, pool=10.0))


def _resolve_upstream(request: Request) -> str:
    return request.headers.get("X-Atlas-Upstream") or DEFAULT_TARGET


def _conv_id_for(body: dict[str, Any], request: Request) -> str:
    """Pick a stable conversation id.

    Strategy: first non-empty of: X-Atlas-Conversation-Id header, body['user']
    field (OpenAI's convention for end-user identification), a request-scoped
    UUID. Single-turn requests still get a fresh ID; multi-turn requests rely
    on the caller setting one of the first two.
    """
    return (
        request.headers.get("X-Atlas-Conversation-Id")
        or (isinstance(body.get("user"), str) and body["user"])
        or str(uuid4())
    )


def _capture_input(body: dict[str, Any], conv_id: str, model: str | None) -> None:
    """Persist the user-side turn (the last user message in `messages`)."""
    messages = body.get("messages") or []
    if not isinstance(messages, list):
        return
    user_msgs = [m for m in messages if isinstance(m, dict) and m.get("role") == "user"]
    if not user_msgs:
        return
    last = user_msgs[-1]
    rec = TurnRecord(
        conversation_id=conv_id,
        source=SOURCE,
        role="user",
        content=last.get("content"),
        model=model,
    )
    try:
        _store.append(rec)
        _index.upsert_turn(rec)
    except Exception as exc:
        log.warning("capture_input failed: %s", exc)


def _capture_output(content: Any, tool_calls: list[dict], conv_id: str, model: str | None,
                    tokens_in: int, tokens_out: int) -> None:
    rec = TurnRecord(
        conversation_id=conv_id,
        source=SOURCE,
        role="assistant",
        content=content if content is not None else "",
        tool_calls=tool_calls,
        model=model,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
    )
    try:
        _store.append(rec)
        _index.upsert_turn(rec)
    except Exception as exc:
        log.warning("capture_output failed: %s", exc)


def _accumulate_stream_chunk(state: dict[str, Any], chunk_obj: dict[str, Any]) -> None:
    choices = chunk_obj.get("choices") or []
    if not choices:
        return
    delta = choices[0].get("delta") or {}
    if "content" in delta and delta["content"] is not None:
        state["content_parts"].append(delta["content"])
    if "tool_calls" in delta:
        for tc in delta["tool_calls"]:
            idx = tc.get("index", 0)
            slot = state["tool_calls"].setdefault(idx, {"id": None, "function": {"name": "", "arguments": ""}})
            if tc.get("id"):
                slot["id"] = tc["id"]
            fn = tc.get("function") or {}
            if fn.get("name"):
                slot["function"]["name"] += fn["name"]
            if fn.get("arguments"):
                slot["function"]["arguments"] += fn["arguments"]
    usage = chunk_obj.get("usage")
    if isinstance(usage, dict):
        state["tokens_in"] = int(usage.get("prompt_tokens", state.get("tokens_in", 0)))
        state["tokens_out"] = int(usage.get("completion_tokens", state.get("tokens_out", 0)))


@app.post("/v1/chat/completions")
@app.post("/chat/completions")
async def chat_completions(request: Request):
    """POST /v1/chat/completions -- capture and proxy to the configured upstream LLM."""
    upstream = _resolve_upstream(request)
    raw_body = await request.body()
    try:
        body = json.loads(raw_body)
    except Exception:
        body = {}
    conv_id = _conv_id_for(body, request)
    model = body.get("model")

    _capture_input(body, conv_id, model)

    forward_headers = {
        k: v for k, v in request.headers.items()
        if k.lower() not in {"host", "content-length", "x-atlas-upstream", "x-atlas-conversation-id"}
    }

    is_stream = bool(body.get("stream"))
    if is_stream:
        async def stream() -> AsyncIterator[bytes]:
            """Proxy the upstream OpenAI SSE stream, accumulating deltas for capture, then write one TurnRecord on close."""
            state = {"content_parts": [], "tool_calls": {}, "tokens_in": 0, "tokens_out": 0}
            try:
                async with _client.stream(
                    "POST",
                    f"{upstream.rstrip('/')}/chat/completions",
                    headers=forward_headers,
                    content=raw_body,
                ) as resp:
                    async for raw_line in resp.aiter_raw():
                        yield raw_line
                        try:
                            for line in raw_line.decode("utf-8", errors="replace").splitlines():
                                if not line.startswith("data: "):
                                    continue
                                payload = line[len("data: "):].strip()
                                if not payload or payload == "[DONE]":
                                    continue
                                _accumulate_stream_chunk(state, json.loads(payload))
                        except Exception:
                            pass
            finally:
                content = "".join(state["content_parts"])
                tool_calls_list = [
                    {"id": v["id"], "name": v["function"]["name"], "input": _maybe_parse_json(v["function"]["arguments"])}
                    for v in state["tool_calls"].values()
                ]
                _capture_output(content, tool_calls_list, conv_id, model,
                                state.get("tokens_in", 0), state.get("tokens_out", 0))
        return StreamingResponse(stream(), media_type="text/event-stream")

    # Non-streaming path.
    upstream_resp = await _client.post(
        f"{upstream.rstrip('/')}/chat/completions",
        headers=forward_headers,
        content=raw_body,
    )
    body_out: dict[str, Any]
    try:
        body_out = upstream_resp.json()
    except Exception:
        body_out = {}
    choices = body_out.get("choices") or []
    msg = (choices[0].get("message") if choices else {}) or {}
    usage = body_out.get("usage") or {}
    _capture_output(
        msg.get("content"),
        msg.get("tool_calls") or [],
        conv_id,
        model,
        int(usage.get("prompt_tokens", 0)),
        int(usage.get("completion_tokens", 0)),
    )
    return JSONResponse(content=body_out, status_code=upstream_resp.status_code)


def _maybe_parse_json(s: str) -> Any:
    if not s:
        return s
    try:
        return json.loads(s)
    except Exception:
        return s


@app.get("/v1/health")
async def health():
    """GET /v1/health -- proxy health check (no upstream ping)."""
    return {"status": "ok", "source": SOURCE, "default_target": DEFAULT_TARGET}


def main() -> int:
    """Entry point: start the OpenAI-compatible capture proxy on DEFAULT_PORT."""
    import uvicorn
    logging.basicConfig(
        level=os.environ.get("ATLAS_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    log.info("starting OpenAI capture proxy on :%d -> %s", DEFAULT_PORT, DEFAULT_TARGET)
    uvicorn.run(app, host="127.0.0.1", port=DEFAULT_PORT, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
