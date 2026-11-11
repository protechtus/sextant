"""Atlas Brain agent loop -- provider-agnostic tool-calling conversation cycle.

Runs the multi-turn ReAct loop against WHICHEVER provider the "brain" role
resolves to (ATLAS_MODEL_BRAIN_PROVIDER: ollama default, anthropic, openai, or
any OpenAI-compatible endpoint -- LM Studio, vLLM, llama.cpp, ...). Tool
schemas, response parsing, and conversation-history serialization all go
through the atlas_plus.llm seam (see docs/LLM-AGNOSTIC-BRAIN-tool-calling.md,
Increment 2, 2026-07-31), so this loop never hand-builds provider wire dicts.
Dispatches tool calls via tools.dispatch() and loops until the model stops
calling tools or the circuit breaker fires (max 10 tool call rounds).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

import httpx

from atlas_plus.llm import ChatResponse, ProviderError, get_provider

from . import history as hist
from . import system_prompt as sp
from . import tools as brain_tools

log = logging.getLogger("atlas.brain.agent")

OLLAMA_BASE_URL = __import__('atlas_plus.llm.ollama_url', fromlist=['default_ollama_url']).default_ollama_url()
DEFAULT_MODEL = os.environ.get("ATLAS_BRAIN_MODEL", "qwen3:8b")

# 10 rounds covers the typical Atlas task (search -> read -> decide -> write -> verify).
# Raising this increases latency linearly; lowering it cuts off multi-step reasoning.
# In practice >6 rounds is rare -- the circuit breaker fires as a safety net, not a normal path.
_MAX_TOOL_ROUNDS = 10

# Generous timeout: qwen3:8b on RTX 3070 typically responds in 5-30s depending on context length.
# 120s accommodates a full 32k-token context window with cold-cache VRAM paging.
_REQUEST_TIMEOUT = 120.0


@dataclass
class AgentResponse:
    """Return value from a single Brain agent turn: reply text, tool call count, model name, and tool log."""

    text: str
    tool_calls_count: int
    model: str
    session_id: str
    tool_log: list[dict] = field(default_factory=list)
    # Accumulated across every provider round of the turn (spend v2).
    prompt_tokens: int = 0
    completion_tokens: int = 0


def _get_brain_provider(base_url: str = ""):
    """Resolve the brain-role provider; honor a caller-supplied Ollama URL.

    ATLAS_MODEL_BRAIN_PROVIDER selects the provider (default ollama). The
    legacy ollama_base_url parameter is respected only when the resolved
    provider IS Ollama, preserving the pre-refactor call contract.
    """
    provider = get_provider("brain")
    if getattr(provider, "name", "") == "ollama" and base_url:
        provider.base_url = base_url.rstrip("/")
    return provider


def _brain_chat(
    messages: list[dict],
    model: str,
    tools: list[dict],
    base_url: str,
) -> ChatResponse:
    """Run one chat turn through the brain-role provider.

    Args:
        messages: Full conversation history in the CURRENT provider's wire
                  form (built via the provider's own serializers below).
        model:    Model tag/name for the resolved provider.
        tools:    brain_tools.TOOL_SCHEMAS (wire-wrapped); unwrapped to the
                  neutral {name, description, parameters} form here.
        base_url: Legacy Ollama base URL (ignored for non-Ollama providers).

    Returns a provider-neutral ChatResponse (.content, .tool_calls).
    Raises httpx.HTTPError / ProviderError on failure.
    """
    provider = _get_brain_provider(base_url)
    neutral_tools = [t.get("function", t) for t in tools]
    return provider.chat(messages, model=model, tools=neutral_tools,
                         timeout=_REQUEST_TIMEOUT)


def chat(
    session_id: str,
    user_message: str,
    model: str = DEFAULT_MODEL,
    ollama_base_url: str = OLLAMA_BASE_URL,
    memory_root=None,
    history_db_path: Optional[Path] = None,
    caller_identity: Optional[str] = None,
    progress_fn: Optional[Callable[[dict], None]] = None,
) -> AgentResponse:
    """Run one user turn through the Atlas Brain agent loop.

    Conversation flow:
      1. Build system prompt (Atlas context + house rules).
      2. Load last 40 messages from SQLite history for this session_id.
      3. Loop up to _MAX_TOOL_ROUNDS times:
           a. POST messages + tool schemas to Ollama.
           b. If model returns tool_calls: dispatch each via brain_tools.dispatch(),
              append tool results to messages, and continue the loop.
           c. If model returns no tool_calls: the turn is complete -- persist and return.
      4. If _MAX_TOOL_ROUNDS exhausted (circuit breaker): return an error string so the
         caller can mark the task failed rather than hanging indefinitely.

    All messages (user, assistant, tool results) are persisted to the history DB
    so future turns in the same session_id have full context.

    Args:
        session_id:       Stable identifier for the conversation (shared across turns).
        user_message:     The instruction or question for this turn.
        model:            Ollama model tag. Defaults to ATLAS_BRAIN_MODEL env var.
        caller_identity:  Optional identity string for rate limiting and audit. If set,
                          Brain enforces per-identity rate limits and rejects cross-identity
                          session access (prevents identity spoofing).
        memory_root:      Override .memory/ path (used in tests).
        progress_fn:      Optional callable invoked before each tool dispatch with a dict
                          {"type":"tool_call","tool":<name>,"round":<int>}. Used by the
                          streaming /chat/stream endpoint to relay live progress to callers.
                          Exceptions from progress_fn are silently swallowed.

    Returns AgentResponse with .text (final assistant text), .tool_calls_count, .tool_log.
    On Ollama network failure, returns an error response rather than raising.
    """
    system = sp.build_system_prompt(memory_root)

    prior = hist.get_messages(session_id, limit=40, db_path=history_db_path or hist._DB_PATH)
    messages: list[dict] = [{"role": "system", "content": system}] + prior

    user_msg = {"role": "user", "content": user_message}
    messages.append(user_msg)
    hist.add_message(session_id, "user", user_message, model=model,
                     caller_identity=caller_identity, db_path=history_db_path or hist._DB_PATH)

    tool_calls_count = 0
    tool_log: list[dict] = []
    prompt_tokens = 0
    completion_tokens = 0

    def _record_spend() -> None:
        # Ledger write is soft-fail and covers EVERY exit path of the turn.
        from atlas_plus.brain import spend as _spend
        _spend.record_usage(session_id, model, prompt_tokens, completion_tokens,
                            db_path=None)

    for _round in range(_MAX_TOOL_ROUNDS):
        try:
            resp = _brain_chat(messages, model, brain_tools.TOOL_SCHEMAS, ollama_base_url)
            prompt_tokens += resp.prompt_tokens
            completion_tokens += resp.completion_tokens
        except (httpx.HTTPError, ProviderError, ValueError) as exc:
            # Provider failure mid-conversation: surface the error rather than
            # looping. The task will be marked failed; the caller can retry.
            error_text = f"[Brain error: model provider call failed -- {exc}]"
            hist.add_message(
                session_id, "assistant", error_text, model=model,
                db_path=history_db_path or hist._DB_PATH,
            )
            _record_spend()
            return AgentResponse(
                text=error_text,
                tool_calls_count=tool_calls_count,
                model=model,
                session_id=session_id,
                tool_log=tool_log,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
            )

        if not resp.tool_calls:
            # Model is done -- persist and return
            hist.add_message(
                session_id, "assistant", resp.content,
                model=model, tool_calls=None,
                db_path=history_db_path or hist._DB_PATH,
            )
            _record_spend()
            return AgentResponse(
                text=resp.content,
                tool_calls_count=tool_calls_count,
                model=model,
                session_id=session_id,
                tool_log=tool_log,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
            )

        # Append the assistant tool-call turn in the provider's OWN wire form
        # (Ollama/OpenAI: role+tool_calls; Anthropic: content blocks).
        provider = _get_brain_provider(ollama_base_url)
        assistant_wire = provider.format_assistant_message(resp)
        messages.append(assistant_wire)
        hist.add_message(
            session_id, "assistant", resp.content,
            model=model, tool_calls=assistant_wire.get("tool_calls"),
            db_path=history_db_path or hist._DB_PATH,
        )

        # Dispatch each tool call and add results (arguments are ALWAYS a
        # parsed dict -- JSON-string decoding happens in the provider layer).
        for call in resp.tool_calls:
            # Fire progress callback before dispatching (best-effort; never raises)
            if progress_fn is not None:
                try:
                    progress_fn({"type": "tool_call", "tool": call.name, "round": _round + 1})
                except Exception:
                    pass

            log.info("Tool call: %s(%s)", call.name, list(call.arguments.keys()))
            result_str = brain_tools.dispatch(call.name, call.arguments)
            tool_calls_count += 1
            tool_log.append({"tool": call.name, "args": call.arguments,
                             "result_preview": result_str[:120]})

            messages.extend(provider.format_tool_result(call, result_str))
            hist.add_message(session_id, "tool", result_str, db_path=history_db_path or hist._DB_PATH)

    # Circuit breaker fired
    fallback = "[Brain: maximum tool call rounds reached. Please rephrase or ask for a simpler query.]"
    hist.add_message(
        session_id, "assistant", fallback, model=model,
        db_path=history_db_path or hist._DB_PATH,
    )
    _record_spend()
    return AgentResponse(
        text=fallback,
        tool_calls_count=tool_calls_count,
        model=model,
        session_id=session_id,
        tool_log=tool_log,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
    )
