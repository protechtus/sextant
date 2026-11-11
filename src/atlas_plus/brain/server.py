"""Atlas Brain FastAPI service -- port 8767.

Endpoints:
  POST /chat                  -- send a message, get a response (buffered)
  POST /chat/stream           -- SSE streaming chat with live tool-call progress events
  GET  /health                -- liveness + Ollama reachability check
  GET  /tasks                 -- list tasks (status/limit filters)
  GET  /tasks/stats           -- aggregate counts by status/agent (since_days)
  GET  /tasks/export          -- download task list as CSV or JSON
  GET  /session/{id}/history  -- last N messages for a session
  DELETE /session/{id}        -- clear session history

Run:
  uv run atlas-brain                # fail-closed: requires ATLAS_BRAIN_API_KEY
  uv run atlas-brain --no-auth      # local-only dev mode

Bind:
  Always 127.0.0.1 by default. To expose, route via Cloudflare tunnel,
  not by re-binding to 0.0.0.0. See ADR-015 (Brain threat model).

Environment variables:
  ATLAS_BRAIN_PORT       -- listen port (default 8767)
  ATLAS_BRAIN_MODEL      -- Ollama model (default qwen3:8b)
  ATLAS_BRAIN_OLLAMA_URL -- Ollama base URL (default http://localhost:11434)
  ATLAS_BRAIN_API_KEY    -- REQUIRED Bearer token (unless --no-auth)
  ATLAS_BRAIN_HOST       -- bind host (default 127.0.0.1; never set 0.0.0.0)
  ATLAS_AUTONOMOUS       -- on/off write gate (default on)
"""

from __future__ import annotations

import argparse
import asyncio
import sqlite3
import hmac
import json
import logging
import re
import os
import sys
import time
from collections import deque
from contextlib import asynccontextmanager
from typing import AsyncIterator, Optional

import httpx
from fastapi import FastAPI, HTTPException, Request, WebSocket
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field, field_validator

from . import agents as agent_registry
from . import history as hist
from . import improvement
from . import scheduler as brain_scheduler
from . import tasks as task_store
from .agent import DEFAULT_MODEL, OLLAMA_BASE_URL, AgentResponse, chat
from atlas_plus.autonomous import autonomous_enabled

log = logging.getLogger("atlas.brain.server")

from atlas_plus import paths as _atlas_paths
_PORT = _atlas_paths.env_int("ATLAS_BRAIN_PORT", 8767)
_HOST = os.environ.get("ATLAS_BRAIN_HOST", "127.0.0.1")
_API_KEY = os.environ.get("ATLAS_BRAIN_API_KEY", "")
# Never read from env: an inherited env var must not silently disable auth on deployed services.
_ALLOW_NO_AUTH = False  # flipped only by --no-auth CLI flag at startup
_NTFY_URL = os.environ.get("ATLAS_NTFY_URL", "")  # e.g. https://ntfy.sh/my-topic
_NTFY_TOKEN = os.environ.get("ATLAS_NTFY_TOKEN", "")  # ATL-007: auth for reserved/ACL'd topics


async def _ntfy(title: str, message: str, priority: str = "default") -> None:
    """Fire-and-forget push notification via ntfy.sh. Silently swallows errors."""
    if not _NTFY_URL:
        return
    try:
        headers = {
            "Title": title[:60],
            "Priority": priority,
            "Tags": "brain",
        }
        if _NTFY_TOKEN:
            headers["Authorization"] = f"Bearer {_NTFY_TOKEN}"
        async with httpx.AsyncClient(timeout=3.0) as client:
            await client.post(
                _NTFY_URL,
                content=message[:400].encode(),
                headers=headers,
            )
    except Exception:
        # Best-effort only: a failed push must never affect task processing.
        pass

# ---------------------------------------------------------------------------
# Per-identity rate limiter (ATL-B03)
# ---------------------------------------------------------------------------
_RATE_LIMIT_RPM = int(os.environ.get("ATLAS_BRAIN_RATE_LIMIT_RPM", "120"))
# Shared primitive (atlas_plus.auth, ROADMAP item 6); behavior identical to
# the previous inline bucket implementation.
from atlas_plus.auth import SlidingWindowRateLimiter as _SlidingWindowRateLimiter
from atlas_plus.auth import AuthFailureLog as _AuthFailureLog

# Failed-auth accounting shared by the HTTP middleware and the WebSocket handshake
# (wave-2 2026-11-09, SEC-04). Same primitive as the dashboard's AUTH_FAILURES.
AUTH_FAILURES = _AuthFailureLog()
_LOOPBACK = ("127.0.0.1", "::1", "localhost")


def _source_ips(conn) -> tuple[str, str]:
    """(socket peer, forwarded source). The forwarded header is trusted only when the
    socket peer is loopback (a tunnel/proxy in front of us); a direct LAN client could
    otherwise rotate it to dodge the throttle or lock someone else out."""
    from atlas_plus.auth import forwarded_ip
    peer = conn.client.host if conn.client else ""
    return peer, forwarded_ip(conn.headers, peer)

_rate_limiter = _SlidingWindowRateLimiter(_RATE_LIMIT_RPM, 60.0)
# Test/compat hooks: existing tests clear the buckets directly and mutate
# _RATE_LIMIT_RPM at module level, so the limiter re-reads it per call.
_rate_buckets = _rate_limiter._buckets


def _check_rate_limit(identity: str) -> tuple[bool, float]:
    """Return (allowed, retry_after_secs). 0 RPM = unlimited."""
    _rate_limiter.limit = _RATE_LIMIT_RPM
    return _rate_limiter.allow(identity)


def _extract_caller_identity(request: Request) -> str:
    """Return a stable caller identity string from the request.

    Priority:
      1. X-Atlas-Caller-Identity header (set by Telegram bot, etc.)
      2. Cloudflare Access JWT email claim (CF-Access-Authenticated-User-Email)
      3. Last 12 chars of the bearer token (single-identity fallback)
    """
    explicit = request.headers.get("X-Atlas-Caller-Identity", "").strip()
    if explicit:
        return explicit
    cf_email = request.headers.get("CF-Access-Authenticated-User-Email", "").strip()
    if cf_email:
        return cf_email
    from atlas_plus.auth import bearer_token
    token = bearer_token(request.headers.get("Authorization", ""))
    return f"token:{token[-12:]}" if token else "unknown"


@asynccontextmanager
async def lifespan(app: FastAPI):
    """FastAPI lifespan: enable the write-gate (ATL-016), start APScheduler on startup, stop on shutdown."""
    from . import executors
    executors.enable_gatekeeper_if_configured()  # no-op unless ATLAS_BRAIN_GATEKEEPER=on
    brain_scheduler.start_scheduler()
    yield
    brain_scheduler.stop_scheduler()


app = FastAPI(title="Atlas Brain", version="0.1.0", lifespan=lifespan)


# ---------------------------------------------------------------------------
# Auth middleware -- fail closed by default
# ---------------------------------------------------------------------------

@app.middleware("http")
async def _auth_middleware(request: Request, call_next):
    # Health is the only unauthenticated endpoint, and it returns a minimal
    # liveness signal only -- no model identity, no autonomous state.
    if request.url.path == "/health":
        return await call_next(request)
    # Local-only dev mode opt-in
    if _ALLOW_NO_AUTH and not _API_KEY:
        return await call_next(request)
    if not _API_KEY:
        # Fail closed: missing token is configuration error, not "open mode."
        return JSONResponse(
            status_code=503,
            content={"detail": "Brain not configured for auth. Set ATLAS_BRAIN_API_KEY."},
        )
    # Failed attempts are counted per real source and a source over the threshold is
    # refused BEFORE the token is even compared -- the same accounting the dashboard and
    # the MCP server keep; the Brain's limiter only ever counted authenticated calls
    # (wave-2 2026-11-09, SEC-04). Loopback with no forwarded source is never locked out.
    peer_ip, fwd_ip = _source_ips(request)
    throttle_ip = fwd_ip or peer_ip
    if fwd_ip or peer_ip not in _LOOPBACK:
        if AUTH_FAILURES.is_rate_limited(throttle_ip):
            return JSONResponse(
                status_code=429,
                content={"detail": "Too many failed authentication attempts; slow down."},
                headers={"Retry-After": str(int(AUTH_FAILURES.window_seconds))},
            )
    from atlas_plus.auth import bearer_token, constant_time_equals
    token = bearer_token(request.headers.get("Authorization", ""))
    if not token:
        log.warning(
            "401 missing Bearer: %s %s from %s",
            request.method, request.url.path,
            request.client.host if request.client else "unknown",
        )
        AUTH_FAILURES.record(path=request.url.path, client_ip=peer_ip, cf_ip=fwd_ip,
                             reason="missing_key")
        return JSONResponse(
            status_code=401,
            content={"detail": "Missing Bearer token."},
            headers={"WWW-Authenticate": "Bearer"},
        )
    if not constant_time_equals(token, _API_KEY):
        log.warning(
            "401 invalid Bearer: %s %s from %s",
            request.method, request.url.path,
            request.client.host if request.client else "unknown",
        )
        AUTH_FAILURES.record(path=request.url.path, client_ip=peer_ip, cf_ip=fwd_ip,
                             reason="bad_key")
        return JSONResponse(
            status_code=401,
            content={"detail": "Invalid Bearer token."},
            headers={"WWW-Authenticate": "Bearer"},
        )
    # Rate limit (ATL-B03): check after auth so identity is known
    identity = _extract_caller_identity(request)
    allowed, retry_after = _check_rate_limit(identity)
    if not allowed:
        log.warning("429 rate limit: identity=%s retry_after=%.1fs", identity, retry_after)
        return JSONResponse(
            status_code=429,
            content={"detail": f"Rate limit exceeded ({_RATE_LIMIT_RPM} rpm). Retry after {int(retry_after)+1}s."},
            headers={"Retry-After": str(min(int(retry_after) + 1, 3600))},
        )
    return await call_next(request)


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class ChatRequest(BaseModel):
    """POST /chat request body: session ID, message text, and optional model override."""

    session_id: str = Field(..., max_length=200)
    message: str = Field(..., max_length=100_000)  # ~75K tokens; prevents memory spike in agent loop
    model: Optional[str] = Field(None, max_length=100)


class ChatResponse(BaseModel):
    """POST /chat response: model reply, tool call count, model name, and per-call tool log."""

    session_id: str
    response: str
    tool_calls_count: int
    model: str
    tool_log: list[dict] = []  # {tool, args, result_preview} for each tool call


class TaskSubmitRequest(BaseModel):
    """POST /task body: instruction, routing hints (agent, kind), priority, and retry limit."""

    instruction: str = Field(..., min_length=1, max_length=10_000)

    @field_validator("instruction")
    @classmethod
    def _instruction_not_blank(cls, v: str) -> str:
        # min_length catches "", not "   "; a whitespace-only instruction
        # reaches the LLM as an empty prompt and burns a run for nothing.
        if not v.strip():
            raise ValueError("instruction must not be blank")
        return v

    session_id: Optional[str] = Field(None, max_length=200)
    task_id: Optional[str] = Field(None, max_length=100)
    agent: Optional[str] = Field(None, max_length=50)  # routing hint; unknown names fall back to default
    kind: str = Field("agent", max_length=50)
    priority: int = Field(5, ge=1, le=100)  # 1=urgent, 100=background; clamped to prevent queue starvation
    max_attempts: int = Field(3, ge=1, le=10)


class TaskResponse(BaseModel):
    """Full task state returned by GET/POST /tasks endpoints; includes lease and artifact metadata."""

    task_id: str
    session_id: str
    status: str
    kind: str = "agent"
    priority: int = 5
    agent: Optional[str] = None
    created_at: str
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    result: Optional[str] = None
    error: Optional[str] = None
    attempts: int = 0
    max_attempts: int = 3
    lease_owner: Optional[str] = None
    lease_expires_at: Optional[str] = None
    heartbeat_at: Optional[str] = None
    artifact_path: Optional[str] = None
    result_digest: Optional[str] = None
    deduplicated: bool = False


# ---------------------------------------------------------------------------
# Task execution
# ---------------------------------------------------------------------------

# Heuristic strings that indicate an API quota/billing hard stop rather than a task
# failure.  These are checked case-insensitively against the raw result text.
# Fragility note: these strings come from claude CLI, codex CLI, and OpenAI API error
# messages -- they may change with CLI version updates.  A false positive (e.g. a task
# that legitimately mentions "billing") just triggers a local fallback, which is safe.
# A false negative (unrecognised credit error message) falls through and marks the task
# failed, which the user can retry after topping up.
_CREDIT_EXHAUSTED_MARKERS = (
    "credit balance is too low",
    "insufficient_quota",
    "exceeded your current quota",
    "rate limit exceeded",
    "billing",
)

# In-process set: intentionally ephemeral. Restarting Brain clears it, which is
# the natural recovery path after topping up API credits.
_credit_exhausted_agents: set[str] = set()


def _is_credit_error(text: str) -> bool:
    low = text.lower()
    return any(m in low for m in _CREDIT_EXHAUSTED_MARKERS)


async def _execute_task(
    task_id: str,
    session_id: str,
    instruction: str,
    agent_name: Optional[str] = None,
) -> None:
    """Route and run a task through the best available agent.

    If the chosen agent fails with a credit/quota error, automatically
    re-routes to the best available local agent (brain or coder).
    """
    task_store.mark_running(task_id)
    agent = None  # ensure always bound before except block
    try:
        agent = await asyncio.to_thread(agent_registry.route, instruction, agent_name)
        task_store.update_task_agent(task_id, agent.name)
        log.info("Task %s -> agent=%s model=%s", task_id, agent.name, agent.model)
        result_text = await asyncio.to_thread(
            agent_registry.dispatch, agent, instruction, session_id
        )

        # Credit exhaustion fallback -- re-route to local model
        if _is_credit_error(result_text):
            _credit_exhausted_agents.add(agent.name)
            log.warning(
                "Task %s: %s credit exhausted -- falling back to local agent",
                task_id, agent.name,
            )
            # Prefer coder first: most failed tasks are coding requests.
            # brain is the universal fallback. fast/mistral are last resorts.
            local_agents = ["coder", "brain", "fast", "mistral"]
            fallback = agent  # default: same agent (will re-fail gracefully)
            for local_name in local_agents:
                if local_name not in _credit_exhausted_agents:
                    from .agents import get_agent
                    local = get_agent(local_name)
                    if local and local.online:
                        fallback = local
                        break
            log.info("Task %s -> fallback agent=%s", task_id, fallback.name)
            task_store.update_task_agent(task_id, fallback.name)
            result_text = await asyncio.to_thread(
                agent_registry.dispatch, fallback, instruction, session_id
            )
            agent = fallback

        result_status = task_store.classify_result(result_text)
        short_instr = instruction[:60] + ("..." if len(instruction) > 60 else "")

        # Phase 1: Failure/block escalation to Claude Code
        # Runs before marking the task terminal so the escalation task is created
        # before any ntfy push (keeps observer causality correct).
        from .escalation import (
            should_escalate_failure, should_escalate_quality,
            create_escalation_task, ESCALATION_SESSION_PREFIX,
        )
        _task_kind = "agent"  # inline tasks are always kind=agent
        inline_enabled = os.environ.get("ATLAS_BRAIN_INLINE_TASKS", "on").lower() not in ("off", "0", "false")

        if result_status == "failed":
            task_store.mark_failed(task_id, result_text)
            log.warning("Task %s failed via %s: returned failure text", task_id, agent.name)
            if task_store.claim_task_notification(task_id, "failed"):
                asyncio.create_task(_ntfy(
                    f"Task failed [{agent.name}]", f"{short_instr}\n{result_text[:200]}", "high"
                ))
            if should_escalate_failure(agent.name, session_id, _task_kind):
                esc_id = create_escalation_task(task_id, session_id, instruction,
                                                "local agent failed", result_text)
                if esc_id and inline_enabled:
                    esc_instr = f"[ESCALATED from {task_id} -- local agent failed]\n\n{instruction}"
                    asyncio.create_task(
                        _execute_task(esc_id, f"{ESCALATION_SESSION_PREFIX}{session_id}",
                                      esc_instr, "claude-code")
                    )
                    log.info("Task %s: failure-escalated to claude-code as %s", task_id, esc_id)
        elif result_status == "blocked":
            task_store.mark_blocked(task_id, result_text)
            log.info("Task %s blocked via %s", task_id, agent.name)
            if task_store.claim_task_notification(task_id, "blocked"):
                asyncio.create_task(_ntfy(
                    f"Task blocked [{agent.name}]", short_instr, "default"
                ))
            if should_escalate_failure(agent.name, session_id, _task_kind):
                esc_id = create_escalation_task(task_id, session_id, instruction,
                                                "local agent blocked", result_text)
                if esc_id and inline_enabled:
                    esc_instr = f"[ESCALATED from {task_id} -- local agent blocked]\n\n{instruction}"
                    asyncio.create_task(
                        _execute_task(esc_id, f"{ESCALATION_SESSION_PREFIX}{session_id}",
                                      esc_instr, "claude-code")
                    )
                    log.info("Task %s: block-escalated to claude-code as %s", task_id, esc_id)
        else:
            task_store.mark_done(task_id, result_text)
            log.info("Task %s done via %s", task_id, agent.name)
            if task_store.claim_task_notification(task_id, "done"):
                asyncio.create_task(_ntfy(
                    f"Task done [{agent.name}]", f"{short_instr}\n{result_text[:200]}", "low"
                ))

        try:
            obs = improvement.record(
                task_id=task_id, agent_name=agent.name, model=agent.model,
                result_text=result_text, task_status=result_status,
                instruction=instruction,
            )
            log.debug("Task %s quality=%.1f", task_id, obs.quality)

            # Phase 3: Quality gate escalation to Claude Code
            # Only triggers on completed tasks where quality is below threshold.
            if result_status == "done" and should_escalate_quality(
                agent.name, session_id, _task_kind, obs.quality
            ):
                esc_id = create_escalation_task(
                    task_id, session_id, instruction,
                    f"quality too low ({obs.quality:.1f}/10)", result_text,
                )
                if esc_id and inline_enabled:
                    esc_instr = (
                        f"[ESCALATED from {task_id} -- quality={obs.quality:.1f}/10 below threshold]\n\n"
                        f"{instruction}"
                    )
                    asyncio.create_task(
                        _execute_task(esc_id, f"{ESCALATION_SESSION_PREFIX}{session_id}",
                                      esc_instr, "claude-code")
                    )
                    log.info("Task %s: quality-escalated (%.1f) to claude-code as %s",
                             task_id, obs.quality, esc_id)
        except Exception as _imp_exc:
            # Quality scoring is best-effort -- never block task completion.
            log.warning("Task %s: quality record failed: %s", task_id, _imp_exc)

        # Extract fact candidates from substantial successful results.
        # Candidates are stored as 'pending' for human review in the Web UI.
        if result_status == "done" and len(result_text) > 300:
            try:
                from .task_facts import extract_candidates
                candidates = extract_candidates(task_id, instruction, result_text)
                if candidates:
                    log.debug("Task %s: %d pending fact candidate(s) extracted", task_id, len(candidates))
            except Exception as _fe_exc:
                # Fact extraction failure must not mark the task failed.
                log.warning("Task %s: fact extraction failed: %s", task_id, _fe_exc)

    except Exception as exc:
        task_store.mark_failed(task_id, str(exc))
        log.exception("Task %s failed", task_id)
        used_name = agent.name if agent is not None else (agent_name or "brain")
        try:
            improvement.record(
                task_id=task_id, agent_name=used_name, model="unknown",
                result_text=str(exc), task_status="failed", instruction=instruction,
            )
        except Exception as _imp_exc2:
            log.warning("Task %s: quality record (failure path) failed: %s", task_id, _imp_exc2)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/")
async def root():
    """Service identity endpoint. Authenticated; returns name + version."""
    return {"service": "Atlas Brain", "version": "0.1.0"}


# /health backend probe: cached so frequent polling stays cheap, short
# timeout so a dead backend can never hang a liveness check.
_BACKEND_PROBE_TTL = 30.0
_backend_probe: dict = {"at": 0.0, "ok": True}


def _probe_backend() -> bool:
    """One live reachability check against the model backend (patchable in tests)."""
    try:
        r = httpx.get(f"{OLLAMA_BASE_URL}/api/tags", timeout=1.5)
        return r.status_code == 200
    except Exception:
        return False


async def _backend_ok() -> bool:
    now = time.monotonic()
    if now - _backend_probe["at"] > _BACKEND_PROBE_TTL:
        _backend_probe["ok"] = await asyncio.to_thread(_probe_backend)
        _backend_probe["at"] = now
    return _backend_probe["ok"]


@app.get("/health")
async def health():
    """Liveness + backend reachability. Unauthenticated so load balancers
    and watchdogs can use it.

    Found live 2026-10-09 (quality loop run 47): Ollama had been killed and
    this endpoint kept answering {"status": "ok"} while semantic indexing,
    re-verification, and the summarizer were all silently broken. The HTTP
    code intentionally STAYS 200 while the process is alive -- every
    watchdog restarts on non-200, and restarting the Brain cannot fix its
    backend -- but the body now tells the truth. No backend details leak:
    just ok/unreachable.
    """
    if await _backend_ok():
        return {"status": "ok", "backend": "ok"}
    return {"status": "degraded", "backend": "unreachable"}


@app.get("/workers")
async def workers_status(request: Request):
    """Return worker health: pending/running/stale counts + assessment.

    Assessment values: ok | idle | warning (pending tasks, no claims) | stale | unknown.
    """
    try:
        from atlas_plus.doctor.cli import check_worker_status
        return await asyncio.to_thread(check_worker_status)
    except Exception as exc:
        return {"assessment": "unknown", "reason": str(exc)}


@app.get("/worker/health")
async def worker_health(request: Request):
    """Per-worker granular stats: active worker IDs, running task lists, queue depth, zombies.

    More detailed than /workers -- shows individual lease_owner entries so you can
    see exactly which worker instances are alive and what they are running.
    Returns {active_workers, pending_by_agent, zombie_count, queue_depth}.
    """
    try:
        return await asyncio.to_thread(task_store.worker_detail)
    except Exception as exc:
        return {"active_workers": [], "pending_by_agent": {}, "zombie_count": 0, "queue_depth": 0, "error": str(exc)}


@app.get("/diagnostic")
async def diagnostic():
    """Detailed operator status: Ollama reachability, model, autonomous gate,
    credit-exhausted agents, and task queue snapshot (pending/running/stale counts).
    """
    ollama_ok = False
    try:
        r = httpx.get(f"{OLLAMA_BASE_URL}/api/tags", timeout=3.0)
        ollama_ok = r.status_code == 200
    except Exception:
        pass

    # Task queue snapshot -- lightweight aggregation for observability dashboards.
    queue: dict = {}
    try:
        from datetime import datetime as _dt, timezone as _tz, timedelta
        cutoff = (_dt.now(_tz.utc) - timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%S")
        import sqlite3 as _sqlite3
        conn = _sqlite3.connect(str(task_store._DB_PATH))
        conn.row_factory = _sqlite3.Row
        row = conn.execute(
            "SELECT "
            "  COUNT(*) FILTER (WHERE status='pending') AS pending,"
            "  COUNT(*) FILTER (WHERE status='running') AS running,"
            "  COUNT(*) FILTER (WHERE status='running' AND heartbeat_at IS NOT NULL AND heartbeat_at < ?) AS stale"
            " FROM brain_tasks",
            (cutoff,),
        ).fetchone()
        conn.close()
        queue = {"pending": row["pending"] or 0, "running": row["running"] or 0, "stale_running": row["stale"] or 0}
    except Exception:
        pass

    # Routing configuration snapshot for observability.
    routing: dict = {}
    try:
        from . import agents as _agents
        routing = {
            "smart_routing": _agents._SMART_ROUTING,
            "local_only": _agents._LOCAL_ONLY,
            "cache_size": len(_agents._ROUTING_CACHE),
        }
    except Exception:
        pass

    return {
        "status": "ok",
        "ollama_reachable": ollama_ok,
        "model": DEFAULT_MODEL,
        "autonomous": autonomous_enabled(),
        "credit_exhausted_agents": sorted(_credit_exhausted_agents),
        "queue": queue,
        "routing": routing,
    }


@app.post("/chat", response_model=ChatResponse)
async def chat_endpoint(req: ChatRequest, request: Request):
    """Send a message to the Brain agent and get a response.

    Runs the full Qwen3 conversation loop including MCP tool calls.
    Uses session_id to maintain per-session conversation history.
    Per-identity rate limiting enforced (120 rpm sliding window).
    """
    model = req.model or DEFAULT_MODEL
    caller = _extract_caller_identity(request)
    try:
        result: AgentResponse = chat(
            session_id=req.session_id,
            user_message=req.message,
            model=model,
            caller_identity=caller,
        )
    except Exception as exc:
        log.exception("Agent loop failed for session %s", req.session_id)
        raise HTTPException(status_code=500, detail=str(exc))

    return ChatResponse(
        session_id=result.session_id,
        response=result.text,
        tool_calls_count=result.tool_calls_count,
        model=result.model,
        tool_log=result.tool_log,
    )


@app.post("/chat/stream")
async def chat_stream_endpoint(req: ChatRequest, request: Request):
    """POST /chat/stream -- SSE streaming chat with live tool-call progress events.

    Starts the Brain agent in a background thread; for each tool dispatched it emits
    a Server-Sent Event so callers see progress in real time. The final event (type=done
    or type=error) carries the full response.

    SSE frame format:
      data: {"type":"tool_call","tool":"<name>","round":<int>}
      data: {"type":"done","text":"...","tool_calls_count":<int>,"model":"...","tool_log":[...]}
      data: {"type":"error","detail":"..."}

    The web/app.py ws_chat endpoint uses this to relay live progress to browser WebSockets.
    """
    model = req.model or DEFAULT_MODEL
    caller = _extract_caller_identity(request)
    loop = asyncio.get_event_loop()

    # Thread-safe queue: progress_fn (called from worker thread) puts events here;
    # the async generator reads and yields them as SSE frames.
    queue: asyncio.Queue = asyncio.Queue(maxsize=200)

    def progress_fn(event: dict) -> None:
        """Fire-and-forget: schedule queue.put from thread. Exceptions are silent."""
        try:
            asyncio.run_coroutine_threadsafe(queue.put(event), loop)
        except Exception:
            pass

    async def event_stream() -> AsyncIterator[str]:
        """Yield SSE frames from the queue until a terminal (done/error) event arrives."""
        async def run_chat() -> None:
            """Run chat() in a thread and post the result (or error) to the shared queue."""
            try:
                result: AgentResponse = await asyncio.to_thread(
                    chat,
                    session_id=req.session_id,
                    user_message=req.message,
                    model=model,
                    caller_identity=caller,
                    progress_fn=progress_fn,
                )
                await queue.put({
                    "type": "done",
                    "text": result.text,
                    "tool_calls_count": result.tool_calls_count,
                    "model": result.model,
                    "tool_log": result.tool_log,
                })
            except Exception as exc:
                log.exception("chat/stream failed for session %s", req.session_id)
                await queue.put({"type": "error", "detail": str(exc)})

        asyncio.create_task(run_chat())

        # Yield SSE frames until the terminal event arrives.
        # Timeout matches the Brain chat timeout plus a small buffer.
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=135.0)
            except asyncio.TimeoutError:
                event = {"type": "error", "detail": "Brain chat timed out"}

            yield f"data: {json.dumps(event)}\n\n"

            if event.get("type") in ("done", "error"):
                break

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/task", response_model=TaskResponse, status_code=202)
async def submit_task(req: TaskSubmitRequest):
    """Submit a task to the Brain task queue and return the task record immediately.

    For kind='agent' tasks with ATLAS_BRAIN_INLINE_TASKS=on (default), execution
    starts asynchronously in this process via asyncio.create_task. All other kinds
    are left pending for the atlas-brain-worker process to claim.

    Deduplication: if a pending task with the same instruction+agent was created
    within the last 5 minutes, the existing task is returned (deduplicated=true)
    and no new task is created. This prevents accidental double-submission.

    Returns 202 Accepted with the task_id. Poll GET /task/{task_id} for status.
    """
    # Check for an existing pending duplicate before creating a new task.
    existing = None if req.task_id else await asyncio.to_thread(
        task_store.find_duplicate_pending,
        req.instruction,
        req.agent,
        kind=req.kind,
    )
    if existing:
        resp = TaskResponse(**existing.as_dict())
        resp.deduplicated = True
        return resp

    # dedup=True: the lookup above is advisory; the store re-checks inside its write
    # transaction so two simultaneous submissions share one row (wave-2 2026-11-09).
    # Off the event loop: BEGIN IMMEDIATE waits out the sqlite busy timeout, which must
    # not stall /health (round C 2026-11-10, lane B F3).
    try:
        task = await asyncio.to_thread(
            task_store.create_task,
            instruction=req.instruction,
            session_id=req.session_id,
            task_id=req.task_id,
            kind=req.kind,
            priority=req.priority,
            agent=req.agent,
            max_attempts=req.max_attempts,
            dedup=True,
        )
    except sqlite3.OperationalError as exc:
        log.warning("/task: task store busy: %s", exc)
        raise HTTPException(status_code=503, detail="task store busy; retry")
    if getattr(task, "deduplicated", False):
        resp = TaskResponse(**task.as_dict())
        resp.deduplicated = True
        return resp
    inline_enabled = os.environ.get("ATLAS_BRAIN_INLINE_TASKS", "on").lower() not in (
        "off", "0", "false"
    )
    if inline_enabled and req.kind == "agent":
        asyncio.create_task(_execute_task(task.task_id, task.session_id, task.instruction, req.agent))
    return TaskResponse(**task.as_dict())


class InboxCaptureRequest(BaseModel):
    """POST /inbox body (ATL-026): a link and/or text dropped from a phone for later triage."""

    url: Optional[str] = Field(None, max_length=2000)
    title: Optional[str] = Field(None, max_length=500)
    text: str = Field("", max_length=20_000)
    source: str = Field("api", max_length=20)
    tags: list[str] = Field(default_factory=list)
    research: bool = False


class InboxCaptureResponse(BaseModel):
    """POST /inbox response: the new inbox item id and the queued summarize task id."""

    item_id: str
    status: str
    task_id: Optional[str] = None


@app.post("/inbox", response_model=InboxCaptureResponse, status_code=202)
async def capture_inbox(req: InboxCaptureRequest):
    """ATL-026: capture a link/blurb into the inbox queue and enqueue a local summarize task.

    Auth is enforced by the bearer-token middleware. The worker fetches and summarizes the item
    locally (free); items flagged with research=true can later be deep-researched. Review and
    promote-to-ticket happen in Helm.
    """
    import json as _json

    from fastapi import HTTPException

    from atlas_plus.inbox import store as inbox_store

    try:
        item = await asyncio.to_thread(
            inbox_store.create_item,
            source=req.source,
            url=req.url,
            title=req.title,
            text=req.text,
            tags=req.tags,
            flagged_research=req.research,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except RuntimeError as exc:  # autonomous kill switch is off
        raise HTTPException(status_code=503, detail=str(exc))

    task = task_store.create_task(
        instruction=_json.dumps({"item_id": item.id}),
        kind="inbox_summarize",
        priority=6,
    )
    return InboxCaptureResponse(item_id=item.id, status=item.status, task_id=task.task_id)


@app.get("/agents")
async def list_agents():
    """Return the full agent registry with online status refreshed from Ollama."""
    agents = await asyncio.to_thread(agent_registry.get_agents)
    return {"agents": [a.as_dict() for a in agents]}


@app.get("/improvement")
async def improvement_stats():
    """Return quality statistics and low-scoring tasks for the self-improvement loop."""
    stats = await asyncio.to_thread(improvement.all_agent_stats)
    low = await asyncio.to_thread(improvement.low_quality_tasks)
    return {"agent_stats": stats, "low_quality_tasks": low}


@app.get("/task/{task_id}", response_model=TaskResponse)
async def get_task(task_id: str):
    """Return the current state of a task by ID. 404 if not found."""
    task = task_store.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail=f"Task {task_id!r} not found")
    return TaskResponse(**task.as_dict())


@app.get("/task/{task_id}/attempts")
async def get_task_attempts(task_id: str):
    """Return per-attempt execution history for a task.

    Each attempt row contains attempt_num, outcome (done/failed/blocked),
    agent, started_at, finished_at, and a 300-char result_summary. Returns
    an empty list when no attempt rows have been recorded yet.
    """
    task = task_store.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail=f"Task {task_id!r} not found")
    attempts = task_store.list_task_attempts(task_id)
    return {"task_id": task_id, "attempts": attempts, "count": len(attempts)}


@app.get("/tasks")
async def list_tasks(status: Optional[str] = None, limit: int = 20):
    """List tasks, optionally filtered by status. Capped at 100 per request."""
    tasks = task_store.list_tasks(status=status, limit=min(limit, 100))
    return {"tasks": [t.as_dict() for t in tasks], "count": len(tasks)}


@app.get("/tasks/stats")
async def tasks_stats(since_days: int = 7):
    """Return aggregate task counts grouped by status and agent for the last N days."""
    return task_store.task_stats(since_days=since_days)


@app.get("/spend")
async def spend_endpoint(days: int = 7):
    """Token-spend summary from the provider-seam ledger (spend v2, ROADMAP 5)."""
    from atlas_plus.brain import spend as _spend
    return _spend.summary(days=min(max(days, 1), 90))


@app.get("/tasks/export")
async def tasks_export(fmt: str = "csv", limit: int = 200, status: Optional[str] = None):
    """Export task list as CSV or JSON for archival/analysis.

    Query params:
      fmt    -- 'csv' (default) or 'json'
      limit  -- max tasks returned (capped at 500)
      status -- optional status filter (pending/running/done/failed/blocked)

    Returns a downloadable file (Content-Disposition: attachment).
    """
    import csv
    import io
    limit = min(limit, 500)
    tasks = task_store.list_tasks(status=status, limit=limit)
    if fmt.lower() == "json":
        payload = json.dumps([t.as_dict() for t in tasks], indent=2, default=str)
        return StreamingResponse(
            iter([payload]),
            media_type="application/json",
            headers={"Content-Disposition": "attachment; filename=atlas-tasks.json"},
        )
    buf = io.StringIO()
    fieldnames = ["task_id", "status", "agent", "kind", "priority",
                  "session_id", "created_at", "started_at", "finished_at",
                  "instruction", "result", "error"]
    writer = csv.DictWriter(buf, fieldnames=fieldnames, extrasaction="ignore",
                            lineterminator="\n")
    writer.writeheader()
    for t in tasks:
        row = t.as_dict()
        row["instruction"] = (row.get("instruction") or "")[:120]
        row["result"] = (row.get("result") or "")[:120]
        writer.writerow(row)
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=atlas-tasks.csv"},
    )


@app.patch("/task/{task_id}/priority")
async def bump_task_priority(task_id: str, request: Request):
    """PATCH /task/{id}/priority -- update priority for a pending task (1=highest, 100=lowest).

    Body: {"priority": N}. Returns 404 when task not found or not pending.
    Only pending tasks can be re-prioritized; running/done tasks are unchanged.
    """
    body = await request.json()
    try:
        priority = max(1, min(100, int(body.get("priority", 1))))
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail="priority must be an integer between 1 and 100")
    updated = task_store.set_task_priority(task_id, priority)
    if not updated:
        raise HTTPException(
            status_code=404,
            detail=f"Task {task_id!r} not found or not in pending state."
        )
    task = task_store.get_task(task_id)
    return TaskResponse(**task.as_dict())


@app.get("/session/{session_id}/history")
async def session_history(session_id: str, request: Request, limit: int = 40):
    """Return conversation history for a session. Cross-identity access returns 403."""
    caller = _extract_caller_identity(request)
    recorded = hist.get_session_identity(session_id)
    if recorded and recorded != "unknown" and caller != recorded:
        log.warning("403 history cross-identity: session=%s caller=%s recorded=%s",
                    session_id, caller, recorded)
        raise HTTPException(status_code=403, detail="Session belongs to a different identity.")
    messages = hist.get_messages(session_id, limit=limit)
    return {"session_id": session_id, "messages": messages, "count": len(messages)}


@app.get("/session/{session_id}/export")
async def export_session(session_id: str, request: Request, limit: int = 500):
    """Export a Brain session as a markdown document.

    Returns the full conversation rendered as markdown with USER/ASSISTANT headers.
    Same identity restrictions as /session/{id}/history.
    Returns 404 if session has no messages; 406 if rendered content is empty.
    """
    caller = _extract_caller_identity(request)
    recorded = hist.get_session_identity(session_id)
    if recorded and recorded != "unknown" and caller != recorded:
        raise HTTPException(status_code=403, detail="Session belongs to a different identity.")
    md = await asyncio.to_thread(hist.session_export_markdown, session_id, limit)
    if not md:
        raise HTTPException(status_code=404, detail=f"No messages found for session {session_id!r}")
    from fastapi.responses import PlainTextResponse
    return PlainTextResponse(md, media_type="text/markdown")


@app.delete("/session/{session_id}")
async def clear_session(session_id: str, request: Request):
    """Delete all conversation history for a session. Cross-identity access returns 403."""
    caller = _extract_caller_identity(request)
    recorded = hist.get_session_identity(session_id)
    if recorded and recorded != "unknown" and caller != recorded:
        log.warning("403 delete cross-identity: session=%s caller=%s recorded=%s",
                    session_id, caller, recorded)
        raise HTTPException(status_code=403, detail="Session belongs to a different identity.")
    deleted = hist.clear_session(session_id)
    return {"session_id": session_id, "deleted": deleted}


# ---------------------------------------------------------------------------
# WebSocket (streaming-compatible entry point for future use)
# ---------------------------------------------------------------------------

_SCHEDULER_JOB_MAP = {
    "temporal-build":       lambda: brain_scheduler._temporal_build(),
    "suggest-facts":        lambda: brain_scheduler._suggest_facts(),
    "curation":             lambda: brain_scheduler._curation(),
    "research-batch":       lambda: brain_scheduler._research_batch(),
    "patterns-rebuild":     lambda: brain_scheduler._patterns_rebuild(),
    "gaps-report":          lambda: brain_scheduler._gaps_report(),
    "memory-agent":         lambda: brain_scheduler._memory_agent_weekly(),
    "narrative-weekly":     lambda: brain_scheduler._narrative_weekly(),
    "dream":                lambda: brain_scheduler._dream_nightly(),
    "reflexion-loop":       lambda: brain_scheduler._reflexion_weekly(),
    "research-api-batch-1": lambda: brain_scheduler._research_api_batch_1(),
    "research-api-batch-2": lambda: brain_scheduler._research_api_batch_2(),
    "zombie-cleanup":       lambda: brain_scheduler._zombie_cleanup(),
}


@app.get("/pending-facts")
async def list_pending_facts(status: str = "pending", limit: int = 50):
    """List pending fact candidates awaiting human review."""
    from .task_facts import list_pending_facts as _list
    facts = await asyncio.to_thread(_list, status=status, limit=limit)
    return {"facts": [f.as_dict() for f in facts], "count": len(facts)}


@app.post("/pending-facts/{fact_id}/approve")
async def approve_pending_fact(fact_id: str, request: Request):
    """Approve a pending fact and write it to .memory/."""
    from .task_facts import approve_fact
    body = {}
    try:
        body = await request.json()
    except Exception:
        pass
    scope = body.get("scope")
    result = await asyncio.to_thread(approve_fact, fact_id=fact_id, scope=scope)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error", "approval failed"))
    return result


@app.post("/pending-facts/{fact_id}/reject")
async def reject_pending_fact(fact_id: str):
    """Reject a pending fact candidate."""
    from .task_facts import reject_fact
    result = await asyncio.to_thread(reject_fact, fact_id=fact_id)
    return result


@app.get("/pending-facts/count")
async def pending_facts_count():
    """Return count of pending fact candidates."""
    from .task_facts import pending_count
    return {"pending": await asyncio.to_thread(pending_count)}


# ---------------------------------------------------------------------------
# Memory proxy endpoints (for TUI clients and external tools)
# ---------------------------------------------------------------------------

@app.get("/memory/search")
async def memory_search_endpoint(q: str, limit: int = 5, scope: str = ""):
    """FTS5 search over the Atlas memory corpus.

    Returns {query, results: [{title, path, score, snippet}]}.
    Lets TUI apps search memory via a single authenticated Brain request
    without running a full MCP client.
    """
    if not q or not q.strip():
        raise HTTPException(status_code=400, detail="q is required")
    limit = max(1, min(limit, 20))
    try:
        from atlas_plus.mcp.memory_server.fts import search
        results = await asyncio.to_thread(search, q.strip(), scope=scope, limit=limit)
        return {"query": q, "results": results}
    except Exception as exc:
        log.error("GET /memory/search failed: %s", exc)
        raise HTTPException(status_code=503, detail=str(exc))


@app.get("/research/search")
async def research_search_endpoint(
    q: str, k: int = 5, verified_only: bool = True, min_tier: str = "B"
):
    """Search the local research RAG (proxies the research service on :8769).

    Lets Brain answer from the operator's vetted research library. Source-of-truth by
    default (verified_only). Returns {query, count, results: [...]} or
    {error, results: []} if the research service is not running.
    """
    if not q or not q.strip():
        raise HTTPException(status_code=400, detail="q is required")
    url = os.environ.get("ATLAS_RESEARCH_API_URL", "http://127.0.0.1:8769").rstrip("/")
    key = os.environ.get("RESEARCH_API_KEY", "")
    if not key:
        return {"error": "RESEARCH_API_KEY not set; research service unavailable.", "results": []}
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                f"{url}/search",
                params={
                    "q": q.strip(),
                    "k": max(1, min(k, 50)),
                    "verified_only": str(bool(verified_only)).lower(),
                    "min_tier": min_tier,
                },
                headers={"Authorization": f"Bearer {key}"},
            )
        if resp.status_code == 401:
            return {"error": "research service auth failed.", "results": []}
        resp.raise_for_status()
        return resp.json()
    except httpx.ConnectError:
        return {"error": "optional research service not reachable (ATLAS_RESEARCH_API_URL).", "results": []}
    except Exception as exc:  # noqa: BLE001
        log.error("GET /research/search failed: %s", exc)
        return {"error": f"research search failed: {exc}", "results": []}


@app.post("/memory/remember")
async def memory_remember_endpoint(request: Request):
    """Write a fact to the Atlas memory corpus.

    Body: {scope: str, fact: str, confidence: float 0-1, source: str}
    Lets TUI apps and external tools record facts without a full MCP session.
    """
    body = await request.json()
    scope = body.get("scope", "preference:quick-capture")
    fact = (body.get("fact") or "").strip()
    if not fact:
        raise HTTPException(status_code=400, detail="fact is required")
    confidence = float(body.get("confidence", 0.75))
    source = body.get("source", "external")
    try:
        from atlas_plus.mcp.memory_server.tools.remember import memory_remember
        result = await asyncio.to_thread(
            memory_remember,
            scope=scope,
            fact=fact,
            confidence=confidence,
            source=source,
        )
        return result
    except Exception as exc:
        log.error("POST /memory/remember failed: %s", exc)
        raise HTTPException(status_code=503, detail=str(exc))


@app.post("/scheduler/trigger/{job_id}")
async def scheduler_trigger(job_id: str):
    """Immediately fire a named scheduler job outside its normal schedule.

    Valid job IDs: temporal-build, suggest-facts, curation, research-batch,
    patterns-rebuild, gaps-report, memory-agent, narrative-weekly, dream,
    reflexion-loop, research-api-batch-1, research-api-batch-2, zombie-cleanup.
    Returns 404 with the full valid list if an unknown ID is given.
    """
    if job_id not in _SCHEDULER_JOB_MAP:
        raise HTTPException(
            status_code=404,
            detail=f"Unknown job {job_id!r}. Valid: {sorted(_SCHEDULER_JOB_MAP)}",
        )
    await _SCHEDULER_JOB_MAP[job_id]()
    log.info("Manual scheduler trigger: %s", job_id)
    return {"triggered": job_id, "message": "Job queued as task."}


@app.get("/routing/stats")
async def routing_stats():
    """Return smart routing configuration and cache statistics.

    Useful for diagnosing why a task went to an unexpected agent or verifying
    that smart routing is active. Cache size grows up to 500 entries (FIFO eviction).
    """
    from . import agents as _agents
    from collections import Counter
    category_counts = Counter(_agents._ROUTING_CACHE.values())
    return {
        "smart_routing": _agents._SMART_ROUTING,
        "local_only": _agents._LOCAL_ONLY,
        "cache_size": len(_agents._ROUTING_CACHE),
        "cache_capacity": 500,
        "category_distribution": dict(category_counts.most_common()),
    }


@app.get("/scheduler/jobs")
async def scheduler_jobs():
    """List registered scheduler jobs with their next fire time."""
    sched = brain_scheduler._scheduler
    if not sched or not sched.running:
        return {"running": False, "jobs": []}
    jobs = []
    for job in sched.get_jobs():
        next_run = job.next_run_time
        jobs.append({
            "id": job.id,
            "next_run": next_run.isoformat() if next_run else None,
        })
    return {"running": True, "jobs": jobs}


@app.post("/credit/reset")
async def credit_reset():
    """Clear the in-memory credit-exhausted agent set.

    Call after topping up API credits to allow those agents to be dispatched
    again without restarting Brain.
    """
    cleared = sorted(_credit_exhausted_agents)
    _credit_exhausted_agents.clear()
    log.info("Credit exhaustion state cleared (was: %s)", cleared)
    return {"cleared": cleared, "message": "Credit exhaustion state reset."}


@app.get("/dispatch-stats")
async def dispatch_stats():
    """Return ticket dispatch activity and current queue depth.

    Useful for monitoring the automation loop and for Web UI / atlas-doctor
    dispatch health panels.

    Response keys:
      enabled              -- bool: is ATLAS_TICKET_DISPATCH active?
      last_run_at          -- ISO-8601 UTC str or None (None if no run yet)
      last_dispatched_count-- tickets dispatched on the most recent run
      total_dispatched     -- cumulative since process start
      in_flight            -- tickets currently in-progress (dispatched but not done)
    """
    from .dispatcher import get_dispatch_stats
    return get_dispatch_stats()


@app.websocket("/ws/{session_id}")
async def websocket_chat(websocket: WebSocket, session_id: str):
    """WebSocket streaming chat endpoint. Used by the Web UI Brain Chat tab.

    Messages are sent as plain text; responses stream back as text chunks.

    AUTH (audit 2026-06-04 HIGH-1): the HTTP bearer middleware does NOT run on the
    WebSocket scope, so authenticate HERE, before accept(). Browsers cannot set WS
    headers, so the token arrives via the ?key= query param (same ATLAS_BRAIN_API_KEY
    as the HTTP endpoints). Fail closed: reject unless the key matches, or we are
    explicitly in --no-auth dev mode. Mirrors web/app.py ws_chat.
    """
    # Prefer the Authorization header (scripted clients and the web proxy can set it); the
    # ?key= form stays for browsers, and the uvicorn access line that would print it is
    # redacted by _redact_ws_keys (round-B 2026-11-09, row 13 residual / N-G).
    from atlas_plus.auth import bearer_token
    token = bearer_token(websocket.headers.get("authorization", "")) or websocket.query_params.get("key", "")
    if _API_KEY:
        from atlas_plus.auth import constant_time_equals
        peer_ip, fwd_ip = _source_ips(websocket)
        if (fwd_ip or peer_ip not in _LOOPBACK) and AUTH_FAILURES.is_rate_limited(fwd_ip or peer_ip):
            await websocket.close(code=4429)
            return
        if not token or not constant_time_equals(token, _API_KEY):
            AUTH_FAILURES.record(path=websocket.url.path, client_ip=peer_ip, cf_ip=fwd_ip,
                                 reason="ws_bad_key")
            await websocket.close(code=4401)
            return
    elif not _ALLOW_NO_AUTH:
        # No key configured and not explicitly in no-auth dev mode -- reject.
        await websocket.close(code=4401)
        return
    await websocket.accept()
    model = DEFAULT_MODEL
    try:
        while True:
            text = await websocket.receive_text()
            result: AgentResponse = chat(
                session_id=session_id,
                user_message=text,
                model=model,
            )
            await websocket.send_json({
                "response": result.text,
                "tool_calls_count": result.tool_calls_count,
                "model": result.model,
            })
    except Exception:
        await websocket.close()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    """Entry point for `python -m atlas_plus.brain.server`.

    Starts the FastAPI Brain server on :8767. Flags:
      --no-auth  Allow unauthenticated access (loopback-only bind enforced).
    Auth is controlled by ATLAS_MCP_TOKEN (bearer token) and ATLAS_ALLOW_ORIGINS.
    The APScheduler is started via the FastAPI lifespan hook.
    """
    global _ALLOW_NO_AUTH
    import uvicorn

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
    )

    from atlas_plus.branding import product
    parser = argparse.ArgumentParser(description=f"{product()} Brain -- local task queue API")
    parser.add_argument(
        "--no-auth",
        action="store_true",
        help="Allow unauthenticated access (local development only; refuses non-loopback bind)",
    )
    args = parser.parse_args()

    if _API_KEY and len(_API_KEY) < 16:
        log.error("ATLAS_BRAIN_API_KEY is only %d characters; refusing to start. Generate one: "
                  "python -c \"import secrets; print(secrets.token_urlsafe(32))\"", len(_API_KEY))
        sys.exit(1)
    # Fail closed: refuse to start without a token unless --no-auth is explicit.
    if not _API_KEY and not args.no_auth:
        log.error(
            "ATLAS_BRAIN_API_KEY is not set. Refusing to start. "
            "Generate a token: python -c \"import secrets; print(secrets.token_urlsafe(32))\" "
            "Set ATLAS_BRAIN_API_KEY, or use --no-auth for local development only."
        )
        sys.exit(1)

    # Hard guard: --no-auth can ONLY bind to loopback. Refuse any other host.
    host = _HOST
    if args.no_auth:
        if host not in ("127.0.0.1", "localhost", "::1"):
            log.error(
                "--no-auth requires loopback bind. Got host=%r. "
                "Either set ATLAS_BRAIN_API_KEY or set ATLAS_BRAIN_HOST=127.0.0.1.",
                host,
            )
            sys.exit(1)
        _ALLOW_NO_AUTH = True
        from starlette.middleware.trustedhost import TrustedHostMiddleware
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "[::1]", "::1"])
        log.warning("--no-auth: running without bearer token. Loopback only.")

    log.info(
        "Starting Atlas Brain on %s:%d (model: %s, auth: %s)",
        host, _PORT, DEFAULT_MODEL,
        "bearer" if _API_KEY else "NONE (loopback only)",
    )
    _install_key_redaction()
    logging.getLogger("apscheduler").setLevel(logging.WARNING)  # 11 "Adding job tentatively" lines per start
    # The app OBJECT, not an import string: `python -m atlas_plus.brain.server --no-auth`
    # imports a second module instance whose _ALLOW_NO_AUTH is still False (wave-2
    # 2026-11-09, SEC-02); the console script and this path now serve the same instance.
    uvicorn.run(app, host=host, port=_PORT, reload=False, proxy_headers=False)


class _RedactKeys(logging.Filter):
    """uvicorn.error logs the WebSocket handshake path at INFO, query string included;
    never let an API key through to a log line."""
    _pat = re.compile(r"(key=)[^&\s\"']+")

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            if record.args:
                if isinstance(record.args, tuple):
                    record.args = tuple(self._pat.sub(r"\1REDACTED", a) if isinstance(a, str) else a
                                        for a in record.args)
                elif isinstance(record.args, dict):
                    record.args = {k: (self._pat.sub(r"\1REDACTED", v) if isinstance(v, str) else v)
                                   for k, v in record.args.items()}
            if isinstance(record.msg, str) and "key=" in record.msg:
                record.msg = self._pat.sub(r"\1REDACTED", record.msg)
        except Exception:  # noqa: BLE001
            pass
        return True


def _install_key_redaction() -> None:
    for name in ("uvicorn.error", "uvicorn.access", "uvicorn"):
        logging.getLogger(name).addFilter(_RedactKeys())


if __name__ == "__main__":
    main()
