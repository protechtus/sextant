"""Tests for Brain task execution status semantics."""

from __future__ import annotations

import pytest

from atlas_plus.brain import server as srv


@pytest.mark.asyncio
async def test_execute_task_marks_text_error_as_failed(monkeypatch):
    events: list[tuple[str, str, str | None]] = []

    class Agent:
        name = "brain"
        model = "qwen3:8b"

    monkeypatch.setattr(
        srv.task_store,
        "mark_running",
        lambda task_id: events.append(("running", task_id, None)),
    )
    monkeypatch.setattr(
        srv.task_store,
        "mark_done",
        lambda task_id, result: events.append(("done", task_id, result)),
    )
    monkeypatch.setattr(
        srv.task_store,
        "mark_failed",
        lambda task_id, error: events.append(("failed", task_id, error)),
    )
    monkeypatch.setattr(srv.task_store, "update_task_agent", lambda task_id, agent_name: None)
    monkeypatch.setattr(srv.agent_registry, "route", lambda instruction, agent_name=None: Agent())
    monkeypatch.setattr(
        srv.agent_registry,
        "dispatch",
        lambda agent, instruction, session_id: "[Brain error: could not reach Ollama -- 404]",
    )

    await srv._execute_task("task-x", "session-x", "run temporal graph")

    assert ("failed", "task-x", "[Brain error: could not reach Ollama -- 404]") in events
    assert not any(event[0] == "done" for event in events)


@pytest.mark.asyncio
async def test_execute_task_marks_normal_result_done(monkeypatch):
    events: list[tuple[str, str, str | None]] = []

    class Agent:
        name = "brain"
        model = "qwen3:8b"

    monkeypatch.setattr(
        srv.task_store,
        "mark_running",
        lambda task_id: events.append(("running", task_id, None)),
    )
    monkeypatch.setattr(
        srv.task_store,
        "mark_done",
        lambda task_id, result: events.append(("done", task_id, result)),
    )
    monkeypatch.setattr(
        srv.task_store,
        "mark_failed",
        lambda task_id, error: events.append(("failed", task_id, error)),
    )
    monkeypatch.setattr(srv.task_store, "update_task_agent", lambda task_id, agent_name: None)
    monkeypatch.setattr(srv.agent_registry, "route", lambda instruction, agent_name=None: Agent())
    monkeypatch.setattr(
        srv.agent_registry,
        "dispatch",
        lambda agent, instruction, session_id: "Created the plan.",
    )

    await srv._execute_task("task-y", "session-y", "make a plan")

    assert ("done", "task-y", "Created the plan.") in events
    assert not any(event[0] == "failed" for event in events)


@pytest.mark.asyncio
async def test_execute_task_marks_followup_prompt_as_blocked(monkeypatch):
    events: list[tuple[str, str, str | None]] = []

    class Agent:
        name = "brain"
        model = "qwen3:8b"

    monkeypatch.setattr(
        srv.task_store,
        "mark_running",
        lambda task_id: events.append(("running", task_id, None)),
    )
    monkeypatch.setattr(
        srv.task_store,
        "mark_done",
        lambda task_id, result: events.append(("done", task_id, result)),
    )
    monkeypatch.setattr(
        srv.task_store,
        "mark_failed",
        lambda task_id, error: events.append(("failed", task_id, error)),
    )
    monkeypatch.setattr(
        srv.task_store,
        "mark_blocked",
        lambda task_id, result: events.append(("blocked", task_id, result)),
        raising=False,
    )
    monkeypatch.setattr(srv.task_store, "update_task_agent", lambda task_id, agent_name: None)
    monkeypatch.setattr(srv.agent_registry, "route", lambda instruction, agent_name=None: Agent())
    monkeypatch.setattr(
        srv.agent_registry,
        "dispatch",
        lambda agent, instruction, session_id: "Would you like me to create a ticket?",
    )

    await srv._execute_task("task-z", "session-z", "improve workflow")

    assert ("blocked", "task-z", "Would you like me to create a ticket?") in events
    assert not any(event[0] == "done" for event in events)


@pytest.mark.asyncio
async def test_submit_task_persists_worker_metadata(monkeypatch):
    captured: dict[str, object] = {}

    def fake_create_task(**kwargs):
        captured.update(kwargs)
        return srv.task_store.Task(
            task_id="task-meta",
            session_id=kwargs.get("session_id") or "brain-task-meta",
            instruction=kwargs["instruction"],
            kind=kwargs["kind"],
            priority=kwargs["priority"],
            agent=kwargs["agent"],
            status="pending",
            created_at="2026-05-19T00:00:00+00:00",
            max_attempts=kwargs["max_attempts"],
        )

    def fake_create_background_task(coro):
        coro.close()
        return None

    monkeypatch.setattr(srv.task_store, "create_task", fake_create_task)
    monkeypatch.setattr(srv.asyncio, "create_task", fake_create_background_task)

    req = srv.TaskSubmitRequest(
        instruction="build context",
        kind="context_pack",
        priority=2,
        agent="fast",
        max_attempts=4,
    )
    resp = await srv.submit_task(req)

    assert resp.task_id == "task-meta"
    assert captured["kind"] == "context_pack"
    assert captured["priority"] == 2
    assert captured["agent"] == "fast"
    assert captured["max_attempts"] == 4


@pytest.mark.asyncio
async def test_submit_task_leaves_non_agent_kind_for_worker(monkeypatch):
    background_calls: list[object] = []

    def fake_create_task(**kwargs):
        return srv.task_store.Task(
            task_id="task-worker",
            session_id="brain-task-worker",
            instruction=kwargs["instruction"],
            kind=kwargs["kind"],
            priority=kwargs["priority"],
            agent=kwargs["agent"],
            status="pending",
            created_at="2026-05-19T00:00:00+00:00",
            max_attempts=kwargs["max_attempts"],
        )

    def fake_create_background_task(coro):
        background_calls.append(coro)
        coro.close()
        return None

    monkeypatch.setattr(srv.task_store, "create_task", fake_create_task)
    monkeypatch.setattr(srv.asyncio, "create_task", fake_create_background_task)

    req = srv.TaskSubmitRequest(
        instruction="write context artifact",
        kind="context_pack",
    )
    resp = await srv.submit_task(req)

    assert resp.status == "pending"
    assert resp.kind == "context_pack"
    assert background_calls == []


# ---------------------------------------------------------------------------
# Deduplication in /task endpoint
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_submit_task_dedup_returns_existing_task(monkeypatch):
    """When an identical pending task exists within 5 min, submit_task returns it."""
    existing_task = srv.task_store.Task(
        task_id="task-existing-abc",
        session_id="brain-task-existing-abc",
        instruction="do the thing",
        kind="agent",
        priority=5,
        agent="brain",
        status="pending",
        created_at="2026-05-20T03:00:00+00:00",
        max_attempts=3,
    )

    monkeypatch.setattr(
        srv.task_store, "find_duplicate_pending",
        lambda instruction, agent, **kwargs: existing_task,
    )
    create_calls = []
    monkeypatch.setattr(srv.task_store, "create_task", lambda **kwargs: create_calls.append(kwargs))

    req = srv.TaskSubmitRequest(instruction="do the thing", agent="brain")
    resp = await srv.submit_task(req)

    # Should return the existing task
    assert resp.task_id == "task-existing-abc"
    # Flag must be set so clients know this was a duplicate
    assert resp.deduplicated is True
    # create_task should NOT have been called
    assert create_calls == []


@pytest.mark.asyncio
async def test_submit_task_no_dedup_when_no_duplicate(monkeypatch):
    """When no duplicate exists, a new task is created normally."""
    monkeypatch.setattr(
        srv.task_store, "find_duplicate_pending",
        lambda instruction, agent, **kwargs: None,
    )

    new_task = srv.task_store.Task(
        task_id="task-new-xyz",
        session_id="brain-task-new-xyz",
        instruction="fresh instruction",
        kind="agent",
        priority=5,
        agent=None,
        status="pending",
        created_at="2026-05-20T03:00:00+00:00",
        max_attempts=3,
    )
    monkeypatch.setattr(srv.task_store, "create_task", lambda **kwargs: new_task)
    # Prevent inline execution from firing
    monkeypatch.setattr(srv.asyncio, "create_task", lambda coro: coro.close())

    req = srv.TaskSubmitRequest(instruction="fresh instruction")
    resp = await srv.submit_task(req)

    assert resp.task_id == "task-new-xyz"
    assert resp.deduplicated is False


@pytest.mark.asyncio
async def test_submit_task_dedup_false_by_default_on_new_response(monkeypatch):
    """New (non-deduped) TaskResponse has deduplicated=False by default."""
    monkeypatch.setattr(
        srv.task_store, "find_duplicate_pending",
        lambda instruction, agent, **kwargs: None,
    )
    new_task = srv.task_store.Task(
        task_id="task-default-flag",
        session_id="brain-task-default-flag",
        instruction="some instruction",
        kind="agent",
        priority=5,
        agent=None,
        status="pending",
        created_at="2026-05-20T03:00:00+00:00",
        max_attempts=3,
    )
    monkeypatch.setattr(srv.task_store, "create_task", lambda **kwargs: new_task)
    monkeypatch.setattr(srv.asyncio, "create_task", lambda coro: coro.close())

    req = srv.TaskSubmitRequest(instruction="some instruction")
    resp = await srv.submit_task(req)
    assert resp.deduplicated is False
