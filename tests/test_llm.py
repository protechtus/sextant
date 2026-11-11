"""Tests for the unified LLM provider layer (atlas_plus.llm). No real model is ever called:
HTTP transports and the Anthropic client are injected."""

from __future__ import annotations

import pytest

from atlas_plus.llm import (AnthropicProvider, AutoProvider, ChatMessage, ChatResponse,
                            ModelProvider, OllamaProvider, OpenAIProvider, ProviderError,
                            get_model, get_provider, normalize_messages)


# --- message normalization -------------------------------------------------

def test_normalize_messages_accepts_str_dict_and_obj():
    assert normalize_messages("hi") == [ChatMessage("user", "hi")]
    out = normalize_messages([{"role": "system", "content": "s"}, ChatMessage("user", "u")])
    assert out == [ChatMessage("system", "s"), ChatMessage("user", "u")]
    with pytest.raises(ProviderError):
        normalize_messages([123])


# --- Ollama ----------------------------------------------------------------

def test_ollama_chat_builds_payload_and_parses(monkeypatch):
    captured = {}

    def fake_post(url, json, timeout):
        captured["url"] = url
        captured["json"] = json
        return {"message": {"content": "  Paris  "}}

    p = OllamaProvider(base_url="http://x:11434", default_model="qwen3:8b", post=fake_post)
    r = p.generate("capital of France?", temperature=0.5)
    assert r == "Paris"
    assert captured["url"] == "http://x:11434/api/chat"
    assert captured["json"]["model"] == "qwen3:8b"
    assert captured["json"]["options"]["temperature"] == 0.5
    assert captured["json"]["messages"][-1] == {"role": "user", "content": "capital of France?"}


def test_ollama_embed(monkeypatch):
    captured = {}

    def fake_post(url, json, timeout):
        captured["url"] = url
        captured["json"] = json
        return {"embeddings": [[0.1, 0.2, 0.3]]}

    p = OllamaProvider(post=fake_post)
    assert p.embed("hi") == [0.1, 0.2, 0.3]
    # Modern endpoint pinned: legacy /api/embeddings hard-errors on
    # over-context input since an Ollama update (2026-10-04 find).
    assert captured["url"].endswith("/api/embed")
    assert captured["json"]["input"] == "hi"


# --- OpenAI-compatible -----------------------------------------------------

def test_openai_chat_and_embed():
    def fake_post(url, json, headers, timeout):
        if url.endswith("/chat/completions"):
            assert headers["Authorization"] == "Bearer k"
            return {"choices": [{"message": {"content": "hello"}}]}
        return {"data": [{"embedding": [1.0, 2.0]}]}

    p = OpenAIProvider(base_url="https://api.openai.com/v1", api_key="k", post=fake_post)
    assert p.available() is True
    assert p.generate("hi") == "hello"
    assert p.embed("x") == [1.0, 2.0]


def test_openai_local_endpoint_needs_no_key():
    p = OpenAIProvider(base_url="http://localhost:1234/v1", api_key="")
    assert p.available() is True


# --- Anthropic -------------------------------------------------------------

class _FakeBlock:
    def __init__(self, text):
        self.text = text


class _FakeAnthropicResp:
    def __init__(self, text):
        self.content = [_FakeBlock(text)]


class _FakeMessages:
    def __init__(self, text):
        self._text = text
        self.last_kwargs = None

    def create(self, **kwargs):
        self.last_kwargs = kwargs
        return _FakeAnthropicResp(self._text)


class _FakeAnthropicClient:
    def __init__(self, text="claude says hi"):
        self.messages = _FakeMessages(text)


def test_anthropic_available_requires_key_or_client():
    assert AnthropicProvider(api_key="").available() is False
    assert AnthropicProvider(api_key="sk-x").available() is True
    assert AnthropicProvider(api_key="", client=_FakeAnthropicClient()).available() is True


def test_anthropic_chat_splits_system_and_extracts_text():
    client = _FakeAnthropicClient("hi there")
    p = AnthropicProvider(client=client, default_model="claude-x")
    out = p.chat([ChatMessage("system", "be terse"), ChatMessage("user", "hello")])
    assert out.content == "hi there"
    assert client.messages.last_kwargs["system"] == "be terse"
    assert client.messages.last_kwargs["messages"] == [{"role": "user", "content": "hello"}]


# --- AutoProvider fallback -------------------------------------------------

class _Fake(ModelProvider):
    def __init__(self, name, *, avail=True, fail=False, content="ok"):
        super().__init__()
        self.name = name
        self._avail = avail
        self._fail = fail
        self._content = content

    def _chat(self, messages, **kw):
        if self._fail:
            raise RuntimeError("boom")
        return ChatResponse(content=self._content, model=self.name)

    def available(self):
        return self._avail


def test_auto_falls_through_to_first_working_provider():
    auto = AutoProvider([
        _Fake("a", avail=False),
        _Fake("b", fail=True),
        _Fake("c", content="from-c"),
    ])
    assert auto.generate("x") == "from-c"


def test_auto_raises_when_all_fail():
    auto = AutoProvider([_Fake("a", fail=True), _Fake("b", avail=False)])
    with pytest.raises(ProviderError):
        auto.generate("x")


# --- role resolution -------------------------------------------------------

def test_get_model_uses_env_then_default(monkeypatch):
    monkeypatch.delenv("ATLAS_BRAIN_MODEL", raising=False)
    assert get_model("brain") == "qwen3:8b"
    monkeypatch.setenv("ATLAS_BRAIN_MODEL", "llama3")
    assert get_model("brain") == "llama3"
    monkeypatch.delenv("ATLAS_FAST_MODEL", raising=False)
    assert get_model("fast") == "qwen3:1.7b"


def test_get_provider_resolves_by_role_and_override(monkeypatch):
    monkeypatch.delenv("ATLAS_MODEL_BRAIN_PROVIDER", raising=False)
    assert isinstance(get_provider("brain"), OllamaProvider)
    monkeypatch.setenv("ATLAS_MODEL_BRAIN_PROVIDER", "openai")
    assert isinstance(get_provider("brain"), OpenAIProvider)
    # summarizer default is auto; legacy ATLAS_SUMMARIZER_PROVIDER still honored
    monkeypatch.delenv("ATLAS_MODEL_SUMMARIZER_PROVIDER", raising=False)
    monkeypatch.delenv("ATLAS_SUMMARIZER_PROVIDER", raising=False)
    assert isinstance(get_provider("summarizer"), AutoProvider)
    monkeypatch.setenv("ATLAS_SUMMARIZER_PROVIDER", "claude")
    assert isinstance(get_provider("summarizer"), AnthropicProvider)


class TestLocalOnly:
    """ATLAS_LOCAL_ONLY must keep every role off cloud providers (the
    CONFIGURATION catalog promises "refuse cloud providers everywhere";
    before 2026-10-16 the auto chain ignored it)."""

    def test_auto_chain_is_ollama_only_when_local_only(self, monkeypatch):
        from atlas_plus import llm as llm_mod

        monkeypatch.setenv("ATLAS_LOCAL_ONLY", "on")
        monkeypatch.delenv("ATLAS_MODEL_SUMMARIZER_PROVIDER", raising=False)
        monkeypatch.delenv("ATLAS_SUMMARIZER_PROVIDER", raising=False)
        provider = llm_mod.get_provider("summarizer")
        names = [type(p).__name__ for p in provider.providers]
        assert names == ["OllamaProvider"]

    def test_auto_chain_is_local_only_by_default(self, monkeypatch):
        from atlas_plus import llm as llm_mod

        monkeypatch.delenv("ATLAS_LOCAL_ONLY", raising=False)
        monkeypatch.delenv("ATLAS_MODEL_SUMMARIZER_PROVIDER", raising=False)
        monkeypatch.delenv("ATLAS_SUMMARIZER_PROVIDER", raising=False)
        provider = llm_mod.get_provider("summarizer")
        names = [type(p).__name__ for p in provider.providers]
        assert names == ["OllamaProvider"]

    def test_auto_chain_has_cloud_fallback_when_opted_in(self, monkeypatch):
        from atlas_plus import llm as llm_mod

        monkeypatch.setenv("ATLAS_LOCAL_ONLY", "off")
        monkeypatch.delenv("ATLAS_MODEL_SUMMARIZER_PROVIDER", raising=False)
        monkeypatch.delenv("ATLAS_SUMMARIZER_PROVIDER", raising=False)
        provider = llm_mod.get_provider("summarizer")
        names = [type(p).__name__ for p in provider.providers]
        assert names == ["OllamaProvider", "AnthropicProvider", "OpenAIProvider"]

    def test_explicit_cloud_provider_refused_when_local_only(self, monkeypatch):
        import pytest as _pytest
        from atlas_plus import llm as llm_mod
        from atlas_plus.llm.base import ProviderError

        monkeypatch.setenv("ATLAS_LOCAL_ONLY", "1")
        monkeypatch.setenv("ATLAS_MODEL_SUMMARIZER_PROVIDER", "anthropic")
        with _pytest.raises(ProviderError, match="LOCAL_ONLY"):
            llm_mod.get_provider("summarizer")

    def test_explicit_ollama_still_fine_when_local_only(self, monkeypatch):
        from atlas_plus import llm as llm_mod

        monkeypatch.setenv("ATLAS_LOCAL_ONLY", "true")
        monkeypatch.setenv("ATLAS_MODEL_SUMMARIZER_PROVIDER", "ollama")
        provider = llm_mod.get_provider("summarizer")
        assert type(provider).__name__ == "OllamaProvider"
