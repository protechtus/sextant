"""Atlas LLM provider layer - one way to call any model, chosen by ROLE from env.

    from atlas_plus.llm import get_provider, get_model
    text = get_provider("inspire").generate(prompt, temperature=1.0)
    vec  = get_provider("embed").embed("hello")

Roles map to a default provider + a model env var (with legacy-var fallbacks), so existing
config keeps working. Override a role's provider with ATLAS_MODEL_<ROLE>_PROVIDER
(ollama | anthropic | openai | auto). 'auto' tries local Ollama, then a configured cloud
provider (Anthropic, then OpenAI).
"""

from __future__ import annotations

import os

from .anthropic import AnthropicProvider
from .base import (AutoProvider, ChatMessage, ChatResponse, ModelProvider, ProviderError,
                   ProviderUnavailable, ToolCall, normalize_messages)
from .ollama import OllamaProvider
from .openai import OpenAIProvider

__all__ = [
    "get_provider", "get_model", "ModelProvider", "ChatMessage", "ChatResponse",
    "ProviderError", "ProviderUnavailable", "OllamaProvider", "AnthropicProvider",
    "OpenAIProvider", "AutoProvider", "ToolCall", "normalize_messages",
]

_REGISTRY = {
    "ollama": OllamaProvider,
    "anthropic": AnthropicProvider,
    "claude": AnthropicProvider,
    "openai": OpenAIProvider,
}

# role -> (default provider, model env var, default model)
_ROLES = {
    "brain":      ("ollama", "ATLAS_BRAIN_MODEL", "qwen3:8b"),
    "inspire":    ("ollama", "ATLAS_BRAIN_MODEL", "qwen3:8b"),
    "fast":       ("ollama", "ATLAS_FAST_MODEL", "qwen3:1.7b"),
    "summarizer": ("auto", "ATLAS_SUMMARIZER_MODEL", "mistral"),
    "embed":      ("ollama", "ATLAS_EMBED_MODEL", "nomic-embed-text"),
}


def get_model(role: str = "brain") -> str:
    _, model_env, default = _ROLES.get(role, _ROLES["brain"])
    return os.environ.get(model_env) or default


def _provider_name_for(role: str) -> str:
    default_provider = _ROLES.get(role, _ROLES["brain"])[0]
    override = os.environ.get(f"ATLAS_MODEL_{role.upper()}_PROVIDER")
    if role == "summarizer" and not override:
        override = os.environ.get("ATLAS_SUMMARIZER_PROVIDER")  # legacy var
    return (override or default_provider).lower()


def _auto_local_only() -> bool:
    """The 'auto' provider chain is LOCAL-ONLY unless the user explicitly sets
    ATLAS_LOCAL_ONLY=off. Cloud fallback when Ollama is down is opt-in: an
    ANTHROPIC_/OPENAI_API_KEY lying in the environment must not, by itself,
    send transcript text to a cloud API (data-safety audit 2026-11-09)."""
    return os.environ.get("ATLAS_LOCAL_ONLY", "").lower() not in ("off", "0", "false", "no")


def _local_only() -> bool:
    """ATLAS_LOCAL_ONLY promises 'refuse cloud providers everywhere' (the
    CONFIGURATION catalog's words). Until 2026-10-16 the auto chain ignored
    it: with an ANTHROPIC_/OPENAI_API_KEY in the environment, a summarizer
    run could silently send transcript text to a cloud API when Ollama was
    down (SRE persona finding). Resolved at call time so tests and hooks
    can flip it per-run."""
    return os.environ.get("ATLAS_LOCAL_ONLY", "off").lower() in ("on", "1", "true")


def get_provider(role: str = "brain") -> ModelProvider:
    """Resolve the provider for a role from env. 'auto' = local Ollama then cloud fallback."""
    name = _provider_name_for(role)
    if name == "auto":
        chain: list[ModelProvider] = [OllamaProvider(default_model=get_model(role))]
        if not _auto_local_only():
            chain += [AnthropicProvider(), OpenAIProvider()]
        return AutoProvider(chain)
    if _local_only() and name in ("anthropic", "claude", "openai"):
        raise ProviderError(
            f"ATLAS_LOCAL_ONLY is on: refusing cloud provider '{name}' for role '{role}'"
        )
    cls = _REGISTRY.get(name)
    if cls is None:
        raise ProviderError(f"unknown provider '{name}' for role '{role}'")
    if name in ("ollama", "openai"):
        return cls(default_model=get_model(role))
    return cls()  # anthropic uses its own claude default model
