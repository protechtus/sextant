"""P2-T5: LLM provider routing for the summarizer.

Tries local Ollama first. Falls back to Claude Haiku ONLY when the user opts in
with ATLAS_LOCAL_ONLY=off (cloud fallback is off by default, so an API key in
the environment never sends transcript text to the cloud on its own). Never silently fails -- raises ProviderError if all
providers are exhausted.

Override via env vars:
  ATLAS_SUMMARIZER_MODEL    -- ollama model name (default: mistral)
  ATLAS_SUMMARIZER_PROVIDER -- force a provider: ollama | claude | auto
  ANTHROPIC_API_KEY         -- required for Claude fallback
  ATLAS_LOCAL_ONLY=off      -- opt in to the Claude fallback in auto mode
"""

from __future__ import annotations

import logging
import os
from typing import Literal

log = logging.getLogger("atlas.summarizer.providers")

OLLAMA_URL = os.environ.get("ATLAS_OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.environ.get("ATLAS_SUMMARIZER_MODEL", "mistral")
CLAUDE_MODEL = os.environ.get("ATLAS_SUMMARIZER_CLAUDE_MODEL", "claude-haiku-4-5-20251001")
FORCED_PROVIDER = os.environ.get("ATLAS_SUMMARIZER_PROVIDER", "auto").lower()
TIMEOUT = int(os.environ.get("ATLAS_SUMMARIZER_TIMEOUT", "120"))


class ProviderError(RuntimeError):
    """Raised when an LLM provider call fails or returns an unusable response."""

    pass


def _call_ollama(prompt: str) -> str:
    # Routes through the unified LLM provider layer (one Ollama transport for the whole
    # codebase). complete() below still owns the auto -> claude fallback.
    from atlas_plus.llm import OllamaProvider

    try:
        text = OllamaProvider(base_url=OLLAMA_URL, default_model=OLLAMA_MODEL).generate(
            prompt, temperature=0.3, timeout=float(TIMEOUT)
        ).strip()
    except Exception as e:  # connect/timeout/HTTP -> a failure 'auto' can fall back from
        raise ProviderError(f"Ollama not reachable at {OLLAMA_URL}: {e}")
    if not text:
        raise ProviderError("Ollama returned empty response")
    log.info("Ollama (%s) responded (%d chars)", OLLAMA_MODEL, len(text))
    return text


def _call_claude(prompt: str) -> str:
    from atlas_plus.llm import AnthropicProvider, _local_only

    # ATLAS_LOCAL_ONLY must hold on THIS path too: the stop hook, suggest-facts,
    # dream mode, contradictions, reflexion and weekly synthesis all call
    # complete() directly, never get_provider(), so the 2026-10-16 guard in
    # atlas_plus.llm alone did not keep transcript text off cloud APIs
    # (skeptic-persona finding, 2026-10-16 batch 3).
    if _local_only():
        raise ProviderError(
            "ATLAS_LOCAL_ONLY is on: refusing cloud provider 'claude' for the summarizer"
        )
    p = AnthropicProvider(default_model=CLAUDE_MODEL)
    if not p.available():
        raise ProviderError("ANTHROPIC_API_KEY not set; cannot use Claude fallback")
    try:
        text = p.generate(prompt, temperature=0.3, max_tokens=2048).strip()
    except Exception as e:
        raise ProviderError(f"Claude call failed: {e}")
    if not text:
        raise ProviderError("Claude returned empty response")
    log.info("Claude (%s) responded (%d chars)", CLAUDE_MODEL, len(text))
    return text


def complete(prompt: str) -> tuple[str, str]:
    """Call the best available LLM with prompt. Returns (text, provider_used)."""
    if FORCED_PROVIDER == "claude":
        text = _call_claude(prompt)
        return text, "claude"

    if FORCED_PROVIDER == "ollama":
        text = _call_ollama(prompt)
        return text, "ollama"

    # auto: try ollama; the cloud fallback is OPT-IN (ATLAS_LOCAL_ONLY=off).
    from atlas_plus.llm import _auto_local_only

    chain = [("ollama", _call_ollama)]
    if not _auto_local_only():
        chain.append(("claude", _call_claude))
    errors: list[str] = []
    for provider, fn in chain:
        try:
            text = fn(prompt)
            return text, provider
        except ProviderError as e:
            log.warning("Provider %s failed: %s", provider, e)
            errors.append(f"{provider}: {e}")

    raise ProviderError("All providers failed:\n" + "\n".join(errors))
