"""Tests for spend v2 (ROADMAP item 5): provider-seam usage capture,
ledger, price estimation, aggregation, and the Brain endpoint."""
from __future__ import annotations

from pathlib import Path

from atlas_plus.brain import spend


class TestPriceEstimation:
    def test_local_models_are_free(self):
        assert spend.estimate_cost_usd("qwen3:8b", 1_000_000, 1_000_000) == 0.0

    def test_known_cloud_model_priced(self):
        cost = spend.estimate_cost_usd("claude-sonnet-4", 1_000_000, 1_000_000)
        assert cost == 3.00 + 15.00

    def test_longest_prefix_wins(self):
        # claude-3-5-haiku must match its own row, not the shorter claude-haiku.
        cost = spend.estimate_cost_usd("claude-3-5-haiku-20241022", 1_000_000, 0)
        assert cost == 0.80

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("ATLAS_MODEL_PRICES", '{"mymodel": [10.0, 20.0]}')
        assert spend.estimate_cost_usd("mymodel-v1", 1_000_000, 500_000) == 10.0 + 10.0

    def test_bad_env_json_ignored(self, monkeypatch):
        monkeypatch.setenv("ATLAS_MODEL_PRICES", "{not json")
        assert spend.estimate_cost_usd("claude-sonnet-x", 1_000_000, 0) == 3.00


class TestLedger:
    def test_record_and_summarize(self, tmp_path):
        db = tmp_path / "brain.sqlite"
        spend.record_usage("s1", "qwen3:8b", 100, 50, db_path=db)
        spend.record_usage("s1", "qwen3:8b", 200, 80, db_path=db)
        spend.record_usage("s2", "claude-sonnet-4", 1000, 400, db_path=db)
        s = spend.summary(days=7, db_path=db)
        assert s["available"] is True
        assert s["total_prompt_tokens"] == 1300
        assert s["total_completion_tokens"] == 530
        assert s["total_turns"] == 3
        assert s["by_model"]["qwen3:8b"]["turns"] == 2
        assert s["est_cost_usd"] > 0  # the claude row prices

    def test_zero_usage_not_recorded(self, tmp_path):
        db = tmp_path / "brain.sqlite"
        spend.record_usage("s1", "m", 0, 0, db_path=db)
        s = spend.summary(days=7, db_path=db)
        assert s["total_turns"] == 0

    def test_record_is_soft_fail(self, tmp_path):
        # A directory where the DB file should be -> sqlite cannot open; must not raise.
        bad = tmp_path / "brain.sqlite"
        bad.mkdir()
        spend.record_usage("s1", "m", 10, 10, db_path=bad)  # no exception = pass


class TestProviderSeam:
    def test_ollama_response_carries_usage(self):
        from atlas_plus.llm.ollama import OllamaProvider
        p = OllamaProvider.__new__(OllamaProvider)
        p.base_url = "http://x"
        captured = {"message": {"content": "hi"}, "prompt_eval_count": 12, "eval_count": 7}
        p._post = lambda url, payload, timeout: captured
        resp = p.chat([{"role": "user", "content": "q"}], model="m")
        assert resp.prompt_tokens == 12
        assert resp.completion_tokens == 7

    def test_agent_response_accumulates(self, tmp_path, monkeypatch):
        # Two provider rounds (tool call then answer) must sum usage and land
        # ONE ledger row via _record_spend.
        import atlas_plus.brain.agent as agent_mod
        from atlas_plus.llm.base import ChatResponse, ToolCall

        rounds = [
            ChatResponse(content="", tool_calls=[ToolCall(id="1", name="t", arguments={})],
                         prompt_tokens=100, completion_tokens=10),
            ChatResponse(content="done", prompt_tokens=150, completion_tokens=20),
        ]
        calls = iter(rounds)
        monkeypatch.setattr(agent_mod, "_brain_chat", lambda *a, **k: next(calls))

        class _P:
            name = "ollama"
            def format_assistant_message(self, r):
                return {"role": "assistant", "content": r.content, "tool_calls": []}
            def format_tool_result(self, call, result):
                return [{"role": "tool", "content": result}]
        monkeypatch.setattr(agent_mod, "_get_brain_provider", lambda *a, **k: _P())
        monkeypatch.setattr(agent_mod.brain_tools, "dispatch", lambda n, a: "ok")
        monkeypatch.setattr(agent_mod.sp, "build_system_prompt", lambda mr: "sys")

        ledger = {}
        monkeypatch.setattr(
            "atlas_plus.brain.spend.record_usage",
            lambda sid, model, pt, ct, db_path=None: ledger.update(pt=pt, ct=ct),
        )
        resp = agent_mod.chat("sess", "do it", history_db_path=tmp_path / "h.sqlite")
        assert resp.prompt_tokens == 250
        assert resp.completion_tokens == 30
        assert ledger == {"pt": 250, "ct": 30}
