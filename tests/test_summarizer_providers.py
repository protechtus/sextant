"""Tests for atlas_plus.summarizer.providers -- LLM provider routing."""
from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest

import atlas_plus.summarizer.providers as prov


# ---------------------------------------------------------------------------
# complete() -- provider selection and fallback
# ---------------------------------------------------------------------------

class TestComplete:
    """complete() routes to the correct provider based on env vars."""

    # Patch the module-level FORCED_PROVIDER constant directly -- the variable is
    # evaluated at import time, so patching os.environ after import has no effect.

    def test_forced_ollama_calls_ollama(self):
        with patch.object(prov, "FORCED_PROVIDER", "ollama"), \
             patch.object(prov, "_call_ollama", return_value="ollama result") as mock:
            text, provider = prov.complete("prompt")
        assert text == "ollama result"
        assert provider == "ollama"
        mock.assert_called_once_with("prompt")

    def test_forced_claude_calls_claude(self):
        with patch.object(prov, "FORCED_PROVIDER", "claude"), \
             patch.object(prov, "_call_claude", return_value="claude result") as mock:
            text, provider = prov.complete("prompt")
        assert text == "claude result"
        assert provider == "claude"
        mock.assert_called_once_with("prompt")

    def test_auto_tries_ollama_first(self):
        with patch.object(prov, "FORCED_PROVIDER", "auto"), \
             patch.object(prov, "_call_ollama", return_value="local answer") as mock_o, \
             patch.object(prov, "_call_claude") as mock_c:
            text, provider = prov.complete("prompt")
        assert text == "local answer"
        assert provider == "ollama"
        mock_c.assert_not_called()

    def test_auto_falls_back_to_claude_when_ollama_fails(self, monkeypatch):
        monkeypatch.setenv("ATLAS_LOCAL_ONLY", "off")  # cloud fallback is opt-in
        with patch.object(prov, "FORCED_PROVIDER", "auto"), \
             patch.object(prov, "_call_ollama",
                          side_effect=prov.ProviderError("Ollama not reachable")), \
             patch.object(prov, "_call_claude", return_value="claude fallback"):
            text, provider = prov.complete("prompt")
        assert text == "claude fallback"
        assert provider == "claude"

    def test_raises_provider_error_when_all_fail(self):
        with patch.object(prov, "FORCED_PROVIDER", "auto"), \
             patch.object(prov, "_call_ollama",
                          side_effect=prov.ProviderError("no ollama")), \
             patch.object(prov, "_call_claude",
                          side_effect=prov.ProviderError("no claude")):
            with pytest.raises(prov.ProviderError, match="All providers failed"):
                prov.complete("prompt")

    def test_forced_ollama_error_propagates(self):
        with patch.object(prov, "FORCED_PROVIDER", "ollama"), \
             patch.object(prov, "_call_ollama",
                          side_effect=prov.ProviderError("timeout")):
            with pytest.raises(prov.ProviderError, match="timeout"):
                prov.complete("prompt")


# ---------------------------------------------------------------------------
# ATLAS_LOCAL_ONLY on the summarizer's own path
# ---------------------------------------------------------------------------

class TestLocalOnly:
    """The 2026-10-16 guard in atlas_plus.llm.get_provider does not cover this
    module: the stop hook, suggest-facts, dream mode, contradictions, reflexion
    and weekly synthesis all call complete() directly. ATLAS_LOCAL_ONLY must be
    enforced inside _call_claude itself (skeptic-persona finding, batch 3)."""

    def test_forced_claude_refused_when_local_only(self, monkeypatch):
        monkeypatch.setenv("ATLAS_LOCAL_ONLY", "on")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-not-real")
        with patch.object(prov, "FORCED_PROVIDER", "claude"):
            with pytest.raises(prov.ProviderError, match="ATLAS_LOCAL_ONLY"):
                prov.complete("prompt")

    def test_auto_fallback_never_reaches_anthropic_when_local_only(self, monkeypatch):
        monkeypatch.setenv("ATLAS_LOCAL_ONLY", "on")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-not-real")
        import atlas_plus.llm as llm

        with patch.object(prov, "FORCED_PROVIDER", "auto"), \
             patch.object(prov, "_call_ollama",
                          side_effect=prov.ProviderError("Ollama not reachable")), \
             patch.object(llm.AnthropicProvider, "generate") as mock_gen:
            with pytest.raises(prov.ProviderError, match="All providers failed"):
                prov.complete("prompt")
        mock_gen.assert_not_called()

    def test_auto_fallback_still_works_when_local_only_off(self, monkeypatch):
        monkeypatch.setenv("ATLAS_LOCAL_ONLY", "off")
        with patch.object(prov, "FORCED_PROVIDER", "auto"), \
             patch.object(prov, "_call_ollama",
                          side_effect=prov.ProviderError("no ollama")), \
             patch.object(prov, "_call_claude", return_value="claude fallback"):
            text, provider = prov.complete("prompt")
        assert text == "claude fallback"
        assert provider == "claude"


    def test_default_never_attempts_cloud_with_api_key_and_ollama_down(self, monkeypatch):
        """Data-safety audit row 19: ANTHROPIC_API_KEY in the environment plus
        an unreachable Ollama must NOT send the prompt to the cloud by default."""
        monkeypatch.delenv("ATLAS_LOCAL_ONLY", raising=False)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-not-real")
        with patch.object(prov, "FORCED_PROVIDER", "auto"), \
             patch.object(prov, "_call_ollama",
                          side_effect=prov.ProviderError("no ollama")), \
             patch("atlas_plus.llm.AnthropicProvider.generate") as cloud:
            with pytest.raises(prov.ProviderError, match="All providers failed"):
                prov.complete("prompt")
        cloud.assert_not_called()


# ---------------------------------------------------------------------------
# ProviderError
# ---------------------------------------------------------------------------

class TestProviderError:
    def test_is_runtime_error(self):
        err = prov.ProviderError("something went wrong")
        assert isinstance(err, RuntimeError)
        assert "something went wrong" in str(err)
