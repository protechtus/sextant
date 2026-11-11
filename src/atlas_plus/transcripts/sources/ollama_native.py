"""Ollama native capture proxy.

Forwarding HTTP proxy on `localhost:11436` (configurable) that captures every
`/api/chat` and `/api/generate` request and response. Ollama uses
*line-delimited JSON* streaming (one JSON object per line, no SSE framing),
terminated by a chunk with `"done": true`.

Capture is best-effort and *side-effecting*: a failure in the capture logic
must not break the proxied call. The proxy returns the upstream response
verbatim.

Run: `python -m atlas_plus.transcripts.sources.ollama_native`
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, AsyncIterator
from uuid import uuid4

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from atlas_plus.transcripts.index import TranscriptIndex
from atlas_plus.transcripts.store import TranscriptStore, TurnRecord

SOURCE = "ollama-native"
DEFAULT_PORT = int(os.environ.get("ATLAS_OLLAMA_PROXY_PORT", "11436"))
DEFAULT_UPSTREAM = os.environ.get("OLLAMA_UPSTREAM", "http://localhost:11434")

log = logging.getLogger("atlas.ollama-proxy")

app = FastAPI(title="Atlas Ollama capture proxy", version="0.1.0")
_store = TranscriptStore()
_index = TranscriptIndex()
_client = httpx.AsyncClient(timeout=httpx.Timeout(connect=10.0, read=600.0, write=60.0, pool=10.0))


def _conv_id_for(body: dict[str, Any], request: Request) -> str:
    return request.headers.get("X-Atlas-Conversation-Id") or str(uuid4())


def _capture_user(body: dict[str, Any], conv_id: str, model: str | None) -> None:
    messages = body.get("messages")
    prompt = body.get("prompt")
    content: Any
    if messages and isinstance(messages, list):
        user = [m for m in messages if isinstance(m, dict) and m.get("role") == "user"]
        if not user:
            return
        content = user[-1].get("content")
    elif prompt is not None:
        content = prompt
    else:
        return
    rec = TurnRecord(
        conversation_id=conv_id,
        source=SOURCE,
        role="user",
        content=content,
        model=model,
    )
    try:
        _store.append(rec)
        _index.upsert_turn(rec)
    except Exception as exc:
        log.warning("capture_user failed: %s", exc)


def _capture_assistant(content: str, conv_id: str, model: str | None,
                       prompt_tokens: int, completion_tokens: int) -> None:
    rec = TurnRecord(
        conversation_id=conv_id,
        source=SOURCE,
        role="assistant",
        content=content,
        model=model,
        tokens_in=prompt_tokens,
        tokens_out=completion_tokens,
    )
    try:
        _store.append(rec)
        _index.upsert_turn(rec)
    except Exception as exc:
        log.warning("capture_assistant failed: %s", exc)


@app.post("/api/chat")
async def api_chat(request: Request):
    """POST /api/chat -- capture Ollama chat request and proxy to upstream."""
    return await _proxy(request, "/api/chat", chat_mode=True)


@app.post("/api/generate")
async def api_generate(request: Request):
    """POST /api/generate -- capture Ollama generate request and proxy to upstream."""
    return await _proxy(request, "/api/generate", chat_mode=False)


async def _proxy(request: Request, path: str, chat_mode: bool):
    raw_body = await request.body()
    try:
        body = json.loads(raw_body)
    except Exception:
        body = {}
    conv_id = _conv_id_for(body, request)
    model = body.get("model")

    _capture_user(body, conv_id, model)

    forward_headers = {
        k: v for k, v in request.headers.items()
        if k.lower() not in {"host", "content-length", "x-atlas-conversation-id"}
    }

    is_stream = body.get("stream", True)  # Ollama default is stream=True
    upstream_url = f"{DEFAULT_UPSTREAM.rstrip('/')}{path}"

    if is_stream:
        async def stream() -> AsyncIterator[bytes]:
            """Proxy the upstream SSE stream, accumulating content for capture, then write one TurnRecord on close."""
            content_parts: list[str] = []
            prompt_tokens = 0
            completion_tokens = 0
            buffer = b""
            try:
                async with _client.stream("POST", upstream_url, headers=forward_headers, content=raw_body) as resp:
                    async for chunk in resp.aiter_raw():
                        yield chunk
                        buffer += chunk
                        while b"\n" in buffer:
                            line, _, buffer = buffer.partition(b"\n")
                            if not line.strip():
                                continue
                            try:
                                obj = json.loads(line.decode("utf-8", errors="replace"))
                            except Exception:
                                continue
                            if chat_mode:
                                msg = obj.get("message") or {}
                                if "content" in msg:
                                    content_parts.append(msg.get("content") or "")
                            else:
                                if "response" in obj:
                                    content_parts.append(obj.get("response") or "")
                            if obj.get("done"):
                                prompt_tokens = int(obj.get("prompt_eval_count", prompt_tokens))
                                completion_tokens = int(obj.get("eval_count", completion_tokens))
            finally:
                _capture_assistant("".join(content_parts), conv_id, model,
                                   prompt_tokens, completion_tokens)
        return StreamingResponse(stream(), media_type="application/x-ndjson")

    # Non-streaming path.
    upstream_resp = await _client.post(upstream_url, headers=forward_headers, content=raw_body)
    try:
        out = upstream_resp.json()
    except Exception:
        out = {}
    if chat_mode:
        msg = out.get("message") or {}
        content = msg.get("content") or ""
    else:
        content = out.get("response") or ""
    _capture_assistant(
        content,
        conv_id,
        model,
        int(out.get("prompt_eval_count", 0)),
        int(out.get("eval_count", 0)),
    )
    return JSONResponse(content=out, status_code=upstream_resp.status_code)


@app.get("/api/tags")
async def api_tags(request: Request):
    """GET /api/tags -- forward model list request to upstream Ollama."""
    upstream_resp = await _client.get(f"{DEFAULT_UPSTREAM.rstrip('/')}/api/tags")
    return JSONResponse(content=upstream_resp.json(), status_code=upstream_resp.status_code)


@app.get("/v1/health")
async def health():
    """GET /v1/health -- proxy health check (no upstream ping)."""
    return {"status": "ok", "source": SOURCE, "upstream": DEFAULT_UPSTREAM}


def main() -> int:
    """Entry point: start the Ollama capture proxy on DEFAULT_PORT (localhost only)."""
    import uvicorn
    logging.basicConfig(
        level=os.environ.get("ATLAS_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    log.info("starting Ollama capture proxy on :%d -> %s", DEFAULT_PORT, DEFAULT_UPSTREAM)
    uvicorn.run(app, host="127.0.0.1", port=DEFAULT_PORT, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
