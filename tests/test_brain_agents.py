"""Tests for atlas_plus.brain.agents -- routing, keyword patterns, caching."""
from __future__ import annotations

import importlib
from unittest.mock import MagicMock, patch

import pytest

import atlas_plus.brain.agents as agents_mod


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _online(name: str) -> None:
    """Mark an agent online in the registry (test helper)."""
    a = agents_mod._registry_by_name.get(name)
    if a:
        a.online = True


def _offline_all() -> None:
    """Mark every agent offline."""
    for a in agents_mod.REGISTRY:
        a.online = False


def _online_set(*names: str) -> None:
    _offline_all()
    for n in names:
        _online(n)


# ---------------------------------------------------------------------------
# route() -- override takes precedence
# ---------------------------------------------------------------------------

class TestRouteOverride:
    def test_override_returns_named_agent_when_online(self):
        _online_set("fast")
        with patch.object(agents_mod, "refresh_status", lambda: None):
            agent = agents_mod.route("do something", override="fast")
        assert agent.name == "fast"

    def test_override_ignores_offline_agent_falls_back(self):
        _online_set("brain")
        # fast is offline
        with patch.object(agents_mod, "refresh_status", lambda: None):
            agent = agents_mod.route("do something", override="fast")
        # falls through to default brain
        assert agent.name == "brain"

    def test_override_unknown_name_falls_back(self):
        _online_set("brain")
        with patch.object(agents_mod, "refresh_status", lambda: None):
            agent = agents_mod.route("do something", override="nonexistent")
        assert agent.name == "brain"


# ---------------------------------------------------------------------------
# route() -- keyword routing
# ---------------------------------------------------------------------------

class TestRouteKeyword:
    def setup_method(self):
        # reset smart routing off for deterministic keyword tests
        agents_mod._SMART_ROUTING = False

    def test_coding_keyword_routes_to_coder(self):
        _online_set("coder", "brain")
        with patch.object(agents_mod, "refresh_status", lambda: None):
            agent = agents_mod.route("implement a login function")
        assert agent.name == "coder"

    def test_coding_complex_routes_to_claude_code(self):
        _online_set("claude-code", "brain")
        with patch.object(agents_mod, "refresh_status", lambda: None):
            # Must match a coding pattern AND a complex keyword
            agent = agents_mod.route("implement a multi-file authentication system")
        assert agent.name == "claude-code"

    def test_quick_pattern_routes_to_fast(self):
        _online_set("fast", "brain")
        with patch.object(agents_mod, "refresh_status", lambda: None):
            agent = agents_mod.route("what is the capital of France")
        assert agent.name == "fast"

    def test_research_pattern_routes_to_research(self):
        _online_set("research", "brain")
        with patch.object(agents_mod, "refresh_status", lambda: None):
            agent = agents_mod.route("research the history of SIP protocol")
        assert agent.name == "research"

    def test_default_routes_to_brain(self):
        _online_set("brain")
        with patch.object(agents_mod, "refresh_status", lambda: None):
            agent = agents_mod.route("do some general task")
        assert agent.name == "brain"

    def test_fallback_to_any_online_when_brain_offline(self):
        _online_set("fast")
        with patch.object(agents_mod, "refresh_status", lambda: None):
            agent = agents_mod.route("do some general task")
        # brain is offline, so falls back to last-resort any-online
        assert agent.online

    def test_all_offline_returns_brain_anyway(self):
        _offline_all()
        with patch.object(agents_mod, "refresh_status", lambda: None):
            agent = agents_mod.route("do something")
        assert agent.name == "brain"


# ---------------------------------------------------------------------------
# route() -- smart routing integration (mocked LLM classifier)
# ---------------------------------------------------------------------------

class TestSmartRouting:
    def setup_method(self):
        agents_mod._SMART_ROUTING = True
        agents_mod._ROUTING_CACHE.clear()

    def teardown_method(self):
        agents_mod._SMART_ROUTING = False
        agents_mod._ROUTING_CACHE.clear()

    def test_smart_routing_simple_goes_to_fast(self):
        _online_set("fast", "brain")
        with patch.object(agents_mod, "refresh_status", lambda: None), \
             patch.object(agents_mod, "_classify_instruction", return_value="simple"):
            agent = agents_mod.route("what time is it")
        assert agent.name == "fast"

    def test_smart_routing_coding_goes_to_coder(self):
        _online_set("coder", "brain")
        with patch.object(agents_mod, "refresh_status", lambda: None), \
             patch.object(agents_mod, "_classify_instruction", return_value="coding"):
            agent = agents_mod.route("write a function")
        assert agent.name == "coder"

    def test_smart_routing_offline_preferred_falls_to_keyword(self):
        # classifier says "simple" but fast is offline -> falls through to keyword routing -> brain
        _online_set("brain")
        with patch.object(agents_mod, "refresh_status", lambda: None), \
             patch.object(agents_mod, "_classify_instruction", return_value="simple"):
            agent = agents_mod.route("what is 2+2")
        # fast is offline, keyword "what is" would go to fast too but it's offline
        # final fallback is brain
        assert agent.name == "brain"

    def test_smart_routing_unknown_category_falls_to_brain(self):
        _online_set("brain")
        with patch.object(agents_mod, "refresh_status", lambda: None), \
             patch.object(agents_mod, "_classify_instruction", return_value="unknown-cat"):
            agent = agents_mod.route("do something unusual")
        assert agent.name == "brain"


# ---------------------------------------------------------------------------
# _classify_instruction -- cache behaviour
# ---------------------------------------------------------------------------

class TestClassifyInstruction:
    def setup_method(self):
        agents_mod._ROUTING_CACHE.clear()

    def teardown_method(self):
        agents_mod._ROUTING_CACHE.clear()

    def test_result_is_cached(self):
        call_count = [0]

        def _fake_post(*a, **kw):
            call_count[0] += 1
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = {"message": {"content": "coding"}}
            return resp

        with patch("httpx.post", side_effect=_fake_post):
            r1 = agents_mod._classify_instruction("write a function please")
            r2 = agents_mod._classify_instruction("write a function please")

        assert r1 == "coding"
        assert r2 == "coding"
        assert call_count[0] == 1  # second call used cache

    def test_network_error_returns_analysis(self):
        import httpx as _httpx
        with patch("httpx.post", side_effect=_httpx.ConnectError("offline")):
            result = agents_mod._classify_instruction("anything")
        assert result == "analysis"

    def test_unknown_response_word_normalises_to_analysis(self):
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"message": {"content": "garbage-word"}}
        with patch("httpx.post", return_value=resp):
            result = agents_mod._classify_instruction("do something")
        assert result == "analysis"

    def test_cache_evicts_when_full(self):
        # fill cache beyond 500
        for i in range(501):
            agents_mod._ROUTING_CACHE[f"key{i}"] = "simple"
        assert len(agents_mod._ROUTING_CACHE) <= 500 + 1  # eviction fires on the add
        # calling classify triggers cache write + eviction
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"message": {"content": "simple"}}
        with patch("httpx.post", return_value=resp):
            agents_mod._classify_instruction("a brand new instruction")
        # Cache was at 501 pre-fill + 1 new entry = 502, eviction removes 1 -> 501
        assert len(agents_mod._ROUTING_CACHE) <= 501


# ---------------------------------------------------------------------------
# get_agent() and get_agents()
# ---------------------------------------------------------------------------

class TestRegistryLookup:
    def test_get_agent_known_name(self):
        a = agents_mod.get_agent("brain")
        assert a is not None
        assert a.name == "brain"

    def test_get_agent_unknown_returns_none(self):
        assert agents_mod.get_agent("phantom") is None

    def test_all_agents_have_required_fields(self):
        for a in agents_mod.REGISTRY:
            assert a.name
            assert a.model
            assert a.agent_type
            assert isinstance(a.capabilities, list)
            assert a.description

    def test_as_dict_contains_keys(self):
        d = agents_mod.get_agent("fast").as_dict()
        for k in ("name", "model", "type", "capabilities", "description", "online", "current_task"):
            assert k in d
