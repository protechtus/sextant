"""Tests for the RESEARCH_API_KEY skip-guard on the nightly API research batches.

When the key is absent, the paid API batches must skip (no task queued) so they do not
fail and alert every run. The local Ollama research batch is unaffected.
"""

from __future__ import annotations

import asyncio

from atlas_plus.brain import scheduler


def _run(coro):
    return asyncio.run(coro)


def test_research_api_enabled_reflects_env(monkeypatch):
    monkeypatch.delenv("RESEARCH_API_KEY", raising=False)
    assert scheduler._research_api_enabled() is False
    monkeypatch.setenv("RESEARCH_API_KEY", "x")
    assert scheduler._research_api_enabled() is True


def test_api_batches_skip_when_key_absent(monkeypatch):
    monkeypatch.delenv("RESEARCH_API_KEY", raising=False)
    calls = []

    async def fake_queue(**kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(scheduler, "_queue_worker_task", fake_queue)
    _run(scheduler._research_api_batch_1())
    _run(scheduler._research_api_batch_2())
    assert calls == []  # nothing queued -> no failure, no alert


def test_api_batches_queue_when_key_present(monkeypatch):
    monkeypatch.setenv("RESEARCH_API_KEY", "test-key")
    calls = []

    async def fake_queue(**kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(scheduler, "_queue_worker_task", fake_queue)
    _run(scheduler._research_api_batch_1())
    _run(scheduler._research_api_batch_2())
    assert len(calls) == 2
    assert all(c.get("kind") == "research_runner_api" for c in calls)


def test_local_research_batch_is_not_gated(monkeypatch):
    monkeypatch.delenv("RESEARCH_API_KEY", raising=False)
    calls = []

    async def fake_queue(**kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(scheduler, "_queue_worker_task", fake_queue)
    _run(scheduler._research_batch())  # local Ollama batch -- runs regardless of the API key
    assert len(calls) == 1
    assert calls[0].get("kind") == "research_runner"
