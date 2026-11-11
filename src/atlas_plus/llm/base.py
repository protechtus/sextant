"""Unified LLM provider layer: one way to call any model (chat + embeddings).

Every Atlas component can route model calls through a ModelProvider so the system is
LLM-agnostic - Ollama (local), Anthropic, OpenAI, or any OpenAI-compatible endpoint - chosen
by env per ROLE (brain / fast / inspire / summarizer / embed). Adapters live in sibling
modules; this file holds the message/response types, the base class, the auto/fallback
wrapper, and the errors. The underlying HTTP/SDK call is injectable so the core is
unit-testable without ever touching a model.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Optional, Union


class ProviderError(RuntimeError):
    """A model call failed."""


class ProviderUnavailable(ProviderError):
    """The provider is not configured (missing key/url) and cannot be used."""


@dataclass
class ChatMessage:
    role: str  # "system" | "user" | "assistant" | "tool"
    content: object  # str normally; providers may carry structured blocks
    # Provider-specific fields that must survive a round trip through the
    # normalizer (tool_calls, tool_call_id, ...). Added for cross-provider
    # tool-calling (Increment 1, 2026-07-31).
    extra: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {"role": self.role, "content": self.content, **self.extra}


@dataclass
class ToolCall:
    """One tool invocation requested by the model, provider-neutral.

    arguments is ALWAYS a parsed dict (providers decode JSON-string args).
    id correlates results for providers that require it; providers without
    ids (Ollama) get positional synthetic ids.
    """

    id: str
    name: str
    arguments: dict = field(default_factory=dict)


@dataclass
class ChatResponse:
    content: str
    model: str = ""
    finish_reason: str = ""
    tool_calls: list = field(default_factory=list)  # list[ToolCall]
    raw: dict = field(default_factory=dict)
    # Usage at the provider seam (spend v2): every provider response carries
    # token counts here; 0 = the provider did not report.
    prompt_tokens: int = 0
    completion_tokens: int = 0


Messages = Union[str, Iterable[Union["ChatMessage", dict]]]


def normalize_messages(messages: Messages) -> list[ChatMessage]:
    """Accept a bare string, dicts, or ChatMessages -> list[ChatMessage].

    Dict messages keep every key beyond role/content in .extra, and content
    passes through UNCOERCED, so provider-specific history turns (assistant
    tool_calls, tool results, Anthropic content blocks) survive intact.
    """
    if isinstance(messages, str):
        return [ChatMessage("user", messages)]
    out: list[ChatMessage] = []
    for m in messages:
        if isinstance(m, ChatMessage):
            out.append(m)
        elif isinstance(m, dict):
            extra = {k: v for k, v in m.items() if k not in ("role", "content")}
            out.append(ChatMessage(str(m.get("role", "user")), m.get("content", ""), extra))
        else:
            raise ProviderError(f"unsupported message: {m!r}")
    return out


class ModelProvider:
    """Base provider. Concrete providers override `_chat` (and optionally `embed`); the
    auto wrapper overrides `chat` directly. `generate` is a convenience over `chat`."""

    name = "base"

    def __init__(self, *, default_model: str = "", default_embed_model: str = "") -> None:
        self.default_model = default_model
        self.default_embed_model = default_embed_model

    # --- implement in subclasses ---
    def _chat(self, messages: list[ChatMessage], *, model: str, temperature: float,
              max_tokens: int, timeout: float, **kw) -> ChatResponse:
        raise NotImplementedError

    def embed(self, text: str, *, model: Optional[str] = None, timeout: float = 30.0) -> list[float]:
        raise ProviderError(f"{self.name} does not support embeddings")

    def available(self) -> bool:
        return True

    # --- cross-provider tool-calling seam (Increment 1, 2026-07-31) ---
    # `tools` is the provider-NEUTRAL Atlas schema: [{name, description,
    # parameters}]. Each provider translates to its wire schema, parses its
    # response into ChatResponse.tool_calls, and serializes history turns so
    # callers never hand-build provider-specific dicts.

    def format_assistant_message(self, response: ChatResponse) -> dict:
        """Wire-form assistant turn (incl. tool calls) for conversation history."""
        raise ProviderError(f"{self.name} does not support tool-calling")

    def format_tool_result(self, call: "ToolCall", output: str) -> list[dict]:
        """Wire-form message(s) carrying one tool result back to the model."""
        raise ProviderError(f"{self.name} does not support tool-calling")

    # --- public API ---
    def chat(self, messages: Messages, *, model: Optional[str] = None, temperature: float = 0.7,
             max_tokens: int = 2048, timeout: float = 120.0,
             tools: Optional[list] = None, **kw) -> ChatResponse:
        msgs = normalize_messages(messages)
        if tools is not None:
            kw["tools"] = tools
        return self._chat(msgs, model=model or self.default_model, temperature=temperature,
                          max_tokens=max_tokens, timeout=timeout, **kw)

    def generate(self, prompt: str, *, model: Optional[str] = None, system: Optional[str] = None,
                 temperature: float = 1.0, **kw) -> str:
        msgs: list[ChatMessage] = []
        if system:
            msgs.append(ChatMessage("system", system))
        msgs.append(ChatMessage("user", prompt))
        return self.chat(msgs, model=model, temperature=temperature, **kw).content


class AutoProvider(ModelProvider):
    """Try providers in priority order; fall back on unavailability/failure. Each provider
    uses its OWN default model, so a local-then-cloud fallback works without a model clash."""

    name = "auto"

    def __init__(self, providers: list[ModelProvider]) -> None:
        super().__init__()
        self.providers = list(providers)

    def chat(self, messages: Messages, *, model: Optional[str] = None, **kw) -> ChatResponse:
        errors: list[str] = []
        for p in self.providers:
            if not p.available():
                continue
            try:
                return p.chat(messages, model=model, **kw)
            except Exception as e:  # noqa: BLE001 - record and try the next provider
                errors.append(f"{p.name}: {type(e).__name__}: {e}")
        raise ProviderError("all providers failed (" + " | ".join(errors) + ")"
                            if errors else "no provider available")

    def embed(self, text: str, *, model: Optional[str] = None, timeout: float = 30.0) -> list[float]:
        errors: list[str] = []
        for p in self.providers:
            if not p.available():
                continue
            try:
                return p.embed(text, model=model, timeout=timeout)
            except Exception as e:  # noqa: BLE001
                errors.append(f"{p.name}: {e}")
        raise ProviderError("no embedding provider available (" + " | ".join(errors) + ")")

    def available(self) -> bool:
        return any(p.available() for p in self.providers)
