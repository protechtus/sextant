"""Atlas Brain scheduler -- APScheduler job registry.

Runs inside the Brain process. Owns all recurring Atlas nightly jobs so
they execute through Qwen3 instead of scattered Task Scheduler entries.

Scheduled jobs (UTC):
  02:00  db-backup         -- WAL-safe snapshot of state.sqlite (in-process, no worker)
  03:05  temporal-build    -- ingest recent sessions into temporal graph
  03:10  suggest-facts     -- dry-run fact candidate review
  03:15  curation          -- write duplicate/stale cleanup report
  03:20  research-batch    -- run queued P3 research jobs (local Ollama)
  03:25  patterns-rebuild  -- rebuild session co-occurrence model
  Sunday 03:30 gaps-report           -- write weekly memory miss report
  Sunday 03:35 convergence-report    -- write weekly contradiction divergence report
  Sunday 03:40 unconfirmed-decay     -- downgrade confidence on stale last_confirmed facts (in-process)
  Sunday 03:45 decay-report          -- memory files past their half_life: threshold
  Monday 04:00 claude-supervisor -- weekly Claude Code supervisor pass over failed/blocked tasks
  Sunday 03:50 memory-agent      -- stale projects + decisions to revisit + decay scan
  Sunday 03:55 narrative-weekly  -- cross-session open loops + direction signals
  04:05  memory-trends     -- snapshot total_files/confirmed_pct/tagged_pct to .atlas/memory-trends.jsonl (in-process)
  04:10  research-quality  -- inject quality_score into unscored .atlas/reports/ files (in-process)
  04:15  suggest-tags      -- auto-apply tags to untagged .memory/ files (in-process)
  04:40  embed-index       -- refresh the semantic embedding index (in-process, needs Ollama)
  Monday 09:00 weekly-digest   -- ntfy push with weekly Atlas stats summary (in-process)
  */10   ticket-dispatch  -- auto-create Brain tasks from eligible todo tickets (in-process)
  05:00  research-api-batch-1 -- 20 P2 API research reports (nightly)
  09:00  research-api-batch-2 -- 20 P2 API research reports (second wave)
  */10   sla-check         -- ntfy push when a task runs past ATLAS_SLA_MINUTES (default 15)
  *:45   zombie-cleanup    -- mark stale running tasks as failed (every hour)

Each job creates a task in brain_tasks. Deterministic worker jobs are left
pending for atlas-brain-worker. The db-backup and unconfirmed-decay jobs run
in-process (no task queued) for low latency.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from datetime import datetime, timedelta, timezone

from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_EXECUTED
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from atlas_plus import paths as atlas_paths

log = logging.getLogger("atlas.brain.scheduler")

_scheduler: AsyncIOScheduler | None = None


def _job_event_listener(event) -> None:
    """Log scheduler job outcomes and record a per-job heartbeat.

    APScheduler swallows job exceptions by default -- they appear in the
    apscheduler.executors logger but not at our level.  This listener
    re-emits them as WARNING so they surface in the Brain log stream, and
    upserts a heartbeat into scheduler_runs so Helm can show "last
    maintenance run" health cross-process (see brain.maintenance). This is
    the one chokepoint that fires for every job, in-process or worker-queued.
    """
    if event.exception:
        log.warning(
            "Scheduled job %r failed: %s",
            event.job_id,
            event.exception,
            exc_info=event.traceback,
        )
    else:
        log.debug("Scheduled job %r completed successfully", event.job_id)

    # Heartbeat ledger. Wrapped so a bookkeeping failure can never stop the
    # scheduler from running jobs -- the worst case is a stale Helm panel.
    try:
        from . import tasks as task_store

        # Catch-up runs must satisfy the CANONICAL job's heartbeat: recording
        # under "<job>-catchup" left the real id perpetually stale, so every
        # Brain restart replayed the whole weekly fleet (repeated digest /
        # decay / reverify pushes -- operator-reported, 2026-09-27).
        job_id = event.job_id
        if isinstance(job_id, str) and job_id.endswith("-catchup"):
            job_id = job_id[: -len("-catchup")]
        if event.exception:
            task_store.record_job_run(job_id, "error", error=str(event.exception))
        else:
            task_store.record_job_run(job_id, "ok")
    except Exception:  # pragma: no cover - defensive bookkeeping guard
        log.debug("Failed to record heartbeat for job %r", event.job_id, exc_info=True)


async def _dispatch(instruction: str, session_id: str) -> None:
    """Create a task and run it through the Brain agent."""
    from . import tasks as task_store
    from .agent import chat

    task = task_store.create_task(instruction=instruction, session_id=session_id)
    log.info("Scheduler dispatching task %s (%s)", task.task_id, session_id)
    task_store.mark_running(task.task_id)
    try:
        result = await asyncio.to_thread(
            chat,
            session_id=task.session_id,
            user_message=task.instruction,
        )
        result_status = task_store.classify_result(result.text)
        if result_status == "failed":
            task_store.mark_failed(task.task_id, result.text)
            log.warning(
                "Scheduler task %s failed: returned failure text",
                task.task_id,
            )
        elif result_status == "blocked":
            task_store.mark_blocked(task.task_id, result.text)
            log.info("Scheduler task %s blocked", task.task_id)
        else:
            task_store.mark_done(task.task_id, result.text)
            log.info("Scheduler task %s done (%d tool calls)", task.task_id, result.tool_calls_count)
    except Exception as exc:
        task_store.mark_failed(task.task_id, str(exc))
        log.exception("Scheduler task %s failed", task.task_id)


async def _queue_worker_task(instruction: str, session_id: str, kind: str, priority: int = 5) -> None:
    """Create a worker-owned task without running agent chat inline."""
    from . import tasks as task_store

    task = task_store.create_task(
        instruction=instruction,
        session_id=session_id,
        kind=kind,
        priority=priority,
    )
    log.info("Scheduler queued worker task %s (%s, kind=%s)", task.task_id, session_id, kind)


async def _temporal_build() -> None:
    await _queue_worker_task(
        instruction=(
            "Run the temporal graph build: call temporal_build to ingest recent session logs "
            "and update the temporal knowledge graph. Report how many entities and facts "
            "were updated or created."
        ),
        session_id="scheduler-temporal-build",
        kind="temporal_build",
        priority=2,
    )


async def _suggest_facts() -> None:
    await _queue_worker_task(
        instruction=(
            "Run suggest-facts in auto mode over today's claude-code session transcript. "
            "Write facts with confidence >= 0.95 to .memory/, skipping near-duplicates of "
            "existing memory (Jaccard >= 0.6). Report how many were written vs skipped."
        ),
        session_id="scheduler-suggest-facts",
        kind="suggest_facts_auto",
        priority=3,
    )


async def _dream_nightly() -> None:
    await _queue_worker_task(
        instruction=(
            "Run the nightly dream: a meta-cognitive synthesis over the .memory/ corpus "
            "(what is known, uncertain, contradictory, missing). With ATLAS_AUTONOMOUS off "
            "this writes to .atlas/ only, not .memory/."
        ),
        session_id="scheduler-dream",
        kind="dream",
        priority=6,
    )


async def _reflexion_weekly() -> None:
    await _queue_worker_task(
        instruction=(
            "Run the weekly reflexion loop over facts ingested in the last 7 days. "
            "With ATLAS_AUTONOMOUS off, every proposed revision is downgraded to a review "
            "ticket rather than editing the temporal graph."
        ),
        session_id="scheduler-reflexion",
        kind="reflexion_loop",
        priority=6,
    )


async def _curation() -> None:
    await _queue_worker_task(
        instruction=(
            "Write the weekly memory curation cleanup report. Check .memory/ for "
            "duplicate entries and stale records using the deterministic curation_report "
            "executor. Do not archive or rewrite memory files."
        ),
        session_id="scheduler-curation",
        kind="curation_report",
        priority=4,
    )


async def _zombie_cleanup() -> None:
    """Mark stale running tasks as failed (zombie sweep, runs every hour)."""
    from . import tasks as task_store
    cleaned = task_store.cleanup_zombie_tasks(max_running_minutes=30)
    if cleaned:
        log.warning("Zombie cleanup: recovered %d stale running task(s) (requeued while attempts remain, else failed)", cleaned)
    else:
        log.debug("Zombie cleanup: no stale tasks found")


# Per-process set of task IDs that have already fired an SLA alert.
# Resets on Brain restart -- acceptable; alerting again after a restart is fine.
_sla_alerted: set[str] = set()


async def _sla_check() -> None:
    """Push ntfy notification for tasks that exceed ATLAS_SLA_MINUTES without completing.

    Only fires once per task per Brain process lifetime to avoid repeat spam.
    Controlled by ATLAS_SLA_MINUTES env var (default 15). Set to 0 to disable.
    """
    import os
    from datetime import datetime, timedelta, timezone
    from . import tasks as task_store

    sla_minutes_str = os.environ.get("ATLAS_SLA_MINUTES", "15")
    try:
        sla_minutes = int(sla_minutes_str)
    except ValueError:
        sla_minutes = 15
    if sla_minutes <= 0:
        return

    cutoff = datetime.now(timezone.utc) - timedelta(minutes=sla_minutes)
    cutoff_iso = cutoff.strftime("%Y-%m-%dT%H:%M:%S")

    # Pull all running tasks and filter by started_at
    running = [t for t in task_store.list_tasks(status="running", limit=50)
               if t.started_at and t.started_at[:19] <= cutoff_iso
               and t.task_id not in _sla_alerted]

    for task in running:
        # Parse the start time defensively. A malformed/truncated started_at (e.g. left by a
        # crashed worker) can slip past the lexical pre-filter; parsing it must SKIP the task,
        # not raise -- and we must not mark it alerted on a failed parse (previously the
        # _sla_alerted.add ran first, so a bad row crashed the loop AND was stuck forever).
        try:
            started = datetime.fromisoformat(task.started_at[:19] + "+00:00")
        except (ValueError, TypeError):
            log.debug("SLA check: skipping task %s with unparseable started_at=%r",
                      task.task_id, task.started_at)
            continue
        _sla_alerted.add(task.task_id)
        elapsed = int((datetime.now(timezone.utc) - started).total_seconds() // 60)
        log.warning(
            "SLA breach: task %s (agent=%s) has been running %d min (threshold=%d min)",
            task.task_id, task.agent, elapsed, sla_minutes,
        )
        # Import here to avoid circular import; _ntfy lives in server
        try:
            from .server import _ntfy
            import asyncio
            asyncio.create_task(_ntfy(
                f"SLA breach: task running {elapsed}min",
                f"Task {task.task_id[:12]} (agent: {task.agent}) has been running "
                f"{elapsed} minutes (threshold: {sla_minutes}min).\n"
                f"Instruction: {task.instruction[:120]}",
                priority="high",
            ))
        except Exception as e:
            log.debug("SLA ntfy push failed: %s", e)


async def _research_batch() -> None:
    # The research runner is a separate pipeline; without it the nightly task blocked and
    # retried every night for a stranger (AG21, sweep 12).
    from atlas_plus import paths as _paths
    root = Path(os.environ.get("ATLAS_WORKSPACE_ROOT") or getattr(_paths, "workspace_root", lambda: Path.cwd())())
    if not (Path(root) / "research" / "research_runner.py").exists():
        log.info("research_runner.py not present under %s; skipping the research batch", root)
        return
    await _queue_worker_task(
        instruction="run --batch 3 --priority P3",
        session_id="scheduler-research-batch",
        kind="research_runner",
        priority=5,
    )


def _research_api_enabled() -> bool:
    """True when the paid research API is usable (RESEARCH_API_KEY set).

    When the key is absent the nightly API research batches skip (logging once per run)
    instead of queuing a research_runner_api task that would fail and alert every cycle.
    """
    return bool(os.environ.get("RESEARCH_API_KEY"))


async def _research_api_batch_1() -> None:
    """Nightly API research batch 1 (05:00 UTC / 1 AM ET) -- 20 P2 reports."""
    if not _research_api_enabled():
        log.info("RESEARCH_API_KEY not set; skipping API research batch 1.")
        return
    await _queue_worker_task(
        instruction="run --batch 20 --tier P2 --max-spend 15.00",
        session_id="scheduler-research-api-1",
        kind="research_runner_api",
        priority=5,
    )


async def _research_api_batch_2() -> None:
    """Nightly API research batch 2 (09:00 UTC / 5 AM ET) -- 20 P2 reports."""
    if not _research_api_enabled():
        log.info("RESEARCH_API_KEY not set; skipping API research batch 2.")
        return
    await _queue_worker_task(
        instruction="run --batch 20 --tier P2 --max-spend 15.00",
        session_id="scheduler-research-api-2",
        kind="research_runner_api",
        priority=5,
    )


async def _memory_agent_weekly() -> None:
    """Weekly memory maintenance scan (Sunday 03:50 UTC)."""
    await _queue_worker_task(
        instruction=(
            "Run the weekly memory agent scan: check for stale project files, "
            "decisions that may need revisiting, and facts past their half-life. "
            "Create tickets for actionable items and send a push summary."
        ),
        session_id="scheduler-memory-agent",
        kind="memory_agent",
        priority=5,
    )


async def _narrative_weekly() -> None:
    """Build cross-session narrative report (Sunday 03:55 UTC)."""
    await _queue_worker_task(
        instruction=(
            "Build the weekly cross-session narrative report: analyse sessions from "
            "the last 14 days for open loops, direction signals, and momentum. "
            "Write the narrative to .memory/sessions/ as the weekly narrative file."
        ),
        session_id="scheduler-narrative-weekly",
        kind="narrative_weekly",
        priority=6,
    )


async def _patterns_rebuild() -> None:
    """Rebuild session co-occurrence patterns model (daily at 03:25)."""
    await _queue_worker_task(
        instruction="Rebuild the session co-occurrence patterns model from recent session history.",
        session_id="scheduler-patterns-rebuild",
        kind="patterns_rebuild",
        priority=6,
    )


async def _gaps_report() -> None:
    await _queue_worker_task(
        instruction=(
            "Write the weekly memory gap report from the MCP search miss log. "
            "Use the deterministic gaps_report executor so the result is tracked "
            "as a durable Brain task and artifact."
        ),
        session_id="scheduler-gaps-report",
        kind="gaps_report",
        priority=4,
    )


async def _decay_report() -> None:
    """Weekly memory half-life decay report (Sunday 03:45 UTC)."""
    await _queue_worker_task(
        instruction=(
            "Write the weekly memory decay report: find .memory/ files with explicit "
            "half_life: frontmatter that have not been updated within their decay window. "
            "Report the overdue count and top files. No LLM required."
        ),
        session_id="scheduler-decay-report",
        kind="decay_report",
        priority=4,
    )


async def _reverify_weekly() -> None:
    """Weekly decay re-verification (Sunday 04:20 UTC) -- the action half of decay-as-behavior.

    Runs after the decay report: re-verifies stale/expired memories via the model and acts
    (reset clock / quarantine / human review). The clock-reset mutation stays OFF unless
    ATLAS_REVERIFY_RESET is enabled, so the default weekly pass only assesses and reports.
    """
    await _queue_worker_task(
        instruction=(
            "Re-verify memories the half-life scan flagged as stale or expired: for each, "
            "judge whether it still holds and act (reset the decay clock, quarantine, or "
            "flag for human review). Writes a reverify artifact."
        ),
        session_id="scheduler-reverify",
        kind="reverify",
        priority=4,
    )


async def _claude_supervisor_weekly() -> None:
    """Weekly Claude Code supervisor pass -- Monday 04:00 UTC.

    Dispatches a claude_supervisor executor task that reviews the past 7 days of
    failed/blocked tasks and low-quality results, producing a structured diagnosis
    and recommendations written to .atlas/reports/supervisor-YYYY-MM-DD.md.
    """
    await _queue_worker_task(
        instruction=(
            "Run the weekly Claude supervisor pass: review all failed and blocked Brain tasks "
            "from the last 7 days, identify failure patterns, root causes, and recommended actions. "
            "Check pending fact count. Write supervisor report to .atlas/reports/."
        ),
        session_id="scheduler-claude-supervisor",
        kind="claude_supervisor",
        priority=2,
    )


async def _unconfirmed_decay_weekly() -> None:
    """Weekly in-process job: downgrade confidence on stale last_confirmed facts (Sun 03:40 UTC).

    Runs directly in the scheduler coroutine (no worker task) since it is deterministic
    and fast. Downgrades confidence to 'low' for files not confirmed in >90 days.
    Controlled by ATLAS_CONFIRM_DECAY_DAYS (default 90).
    """
    import os
    threshold = int(os.environ.get("ATLAS_CONFIRM_DECAY_DAYS", "90"))
    try:
        from atlas_plus.mcp.memory_server.routing import downgrade_stale_confirmations
        downgraded = await asyncio.to_thread(
            downgrade_stale_confirmations, threshold, None, False
        )
        if downgraded:
            log.info("Unconfirmed decay: downgraded %d file(s) to confidence:low (threshold %dd)",
                     len(downgraded), threshold)
        else:
            log.debug("Unconfirmed decay: no stale confirmed facts found (threshold %dd)", threshold)
    except Exception as exc:
        log.warning("Unconfirmed decay job failed: %s", exc)


async def _memory_trends_daily() -> None:
    """Daily in-process snapshot of memory quality metrics (04:05 UTC).

    Appends {ts, total_files, confirmed_pct, tagged_pct} as a JSON line to
    .atlas/memory-trends.jsonl (rolling 30-day window). No worker task -- fast
    deterministic rglob scan that runs directly in the scheduler coroutine.
    The atlas-doctor dashboard reads this file to show 7-day trend sparklines.
    """
    try:
        from atlas_plus.doctor.cli import record_trends_snapshot
        snapshot = await asyncio.to_thread(record_trends_snapshot)
        log.info(
            "Memory trends snapshot: %d files  confirmed: %.1f%%  tagged: %.1f%%",
            snapshot.get("total_files", 0),
            snapshot.get("confirmed_pct", 0.0),
            snapshot.get("tagged_pct", 0.0),
        )
    except Exception as exc:
        log.warning("Memory trends snapshot failed: %s", exc)


async def _research_quality_daily() -> None:
    """Daily in-process quality backfill for research reports (04:10 UTC).

    Scans .atlas/reports/ for markdown files without quality_score: frontmatter
    and injects deterministic structural quality scores.  In-process (no worker
    task needed) since score_research_report() is pure text analysis.
    """
    import os
    from pathlib import Path as _Path
    atlas_dir = atlas_paths.state_dir()
    reports_dir = atlas_dir / "reports"
    if not reports_dir.exists():
        log.debug("Research quality backfill: reports dir not found, skipping")
        return

    try:
        from atlas_plus.brain.research import inject_quality_score
        scored = 0
        skipped = 0
        import yaml as _yaml
        for md_file in reports_dir.rglob("*.md"):
            if md_file.name.startswith("_"):
                continue
            try:
                raw = md_file.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if not raw.startswith("---"):
                continue
            end = raw.find("\n---", 3)
            if end == -1:
                continue
            try:
                fm = _yaml.safe_load(raw[3:end]) or {}
            except Exception:
                continue
            if fm.get("quality_score") is not None:
                skipped += 1
                continue
            result = await asyncio.to_thread(inject_quality_score, md_file)
            if result:
                scored += 1
        log.info("Research quality backfill: scored %d report(s), %d already scored", scored, skipped)
        # Post-score pass: count quality gate failures and push ntfy when any found
        try:
            gate_failed = []
            for md_file in reports_dir.rglob("*.md"):
                if md_file.name.startswith("_"):
                    continue
                raw = md_file.read_text(encoding="utf-8", errors="replace")
                if "quality_gate_passed: false" in raw:
                    gate_failed.append(md_file.stem)
            if gate_failed:
                import os as _os
                ntfy_url = _os.environ.get("ATLAS_NTFY_URL", "")
                if ntfy_url:
                    import httpx as _httpx
                    msg = (
                        f"Research quality gate: {len(gate_failed)} report(s) failed\n"
                        + "\n".join(gate_failed[:5])
                    )
                    try:
                        _httpx.post(ntfy_url, content=msg.encode(),
                                   headers={"Title": "Atlas research quality", "Priority": "low"},
                                   timeout=5.0)
                    except Exception:
                        pass
                log.warning("Research quality gate: %d report(s) have quality_gate_passed=false",
                            len(gate_failed))
        except Exception as exc:
            log.debug("Research gate check failed: %s", exc)
    except Exception as exc:
        log.warning("Research quality backfill failed: %s", exc)


def _count_untagged_by_scope() -> dict[str, int]:
    """Wrapper around doctor.cli.check_untagged_by_scope for scheduler use.

    See atlas_plus.doctor.cli.check_untagged_by_scope for full documentation.
    Kept here so tests can patch scheduler._count_untagged_by_scope independently.
    """
    from atlas_plus.doctor.cli import check_untagged_by_scope
    return check_untagged_by_scope()


def _build_weekly_digest() -> str:
    """Build a concise weekly digest text from Atlas system metrics.

    Gathers: task stats (7d), memory quality, research quality, pending facts,
    open tickets. Returns a plain-text summary suitable for ntfy or Telegram.
    Runs synchronously (called in asyncio.to_thread).
    """
    lines = ["Atlas Weekly Digest"]
    lines.append("=" * 20)

    # Task stats
    try:
        from . import tasks as task_store
        stats = task_store.task_stats(since_days=7)
        done = stats.get("done", 0)
        failed = stats.get("failed", 0)
        total = stats.get("total", 0)
        lines.append(f"Tasks (7d): {total} total | {done} done | {failed} failed")
    except Exception:
        pass

    # Memory quality + tag coverage by scope
    try:
        from atlas_plus.doctor.cli import check_memory_quality
        mq = check_memory_quality()
        conf = mq.get("confirmed_pct", 0)
        tagged = mq.get("tagged_pct", 0)
        files = mq.get("total_files", 0)
        untagged = mq.get("untagged_files", 0)
        lines.append(f"Memory: {files} files | confirmed {conf}% | tagged {tagged}%")
        if untagged > 10:
            lines.append(f"  Untagged: {untagged} files (run: atlas-review-memory suggest-tags --write)")
            # Top-3 untagged scopes breakdown so the worst areas are immediately actionable
            try:
                scope_counts = _count_untagged_by_scope()
                if scope_counts:
                    top3 = sorted(scope_counts.items(), key=lambda x: -x[1])[:3]
                    scope_str = ", ".join(f"{s} ({n})" for s, n in top3)
                    lines.append(f"  Top untagged scopes: {scope_str}")
            except Exception:
                pass
    except Exception:
        pass

    # Research quality
    try:
        from atlas_plus.doctor.cli import check_research_quality
        rq = check_research_quality()
        total_r = rq.get("total_reports", 0)
        avg_score = rq.get("avg_score", 0.0)
        low = rq.get("low_quality_count", 0)
        if total_r > 0:
            lines.append(f"Research: {total_r} reports | avg quality {avg_score:.2f}" +
                         (f" | {low} low-quality" if low else ""))
    except Exception:
        pass

    # Pending facts
    try:
        from .task_facts import pending_count
        n = pending_count()
        if n > 0:
            lines.append(f"Pending facts: {n} awaiting review")
    except Exception:
        pass

    # Open tickets + automation loop health
    try:
        from atlas_plus.tickets.store import list_tickets
        tickets = list_tickets()
        todo_n = sum(1 for t in tickets if t.status == "todo")
        in_prog = sum(1 for t in tickets if t.status == "in-progress")
        blocked_n = sum(1 for t in tickets if t.status == "blocked")
        done_n = sum(1 for t in tickets if t.status == "done")
        lines.append(
            f"Tickets: {todo_n} todo | {in_prog} in-progress | {blocked_n} blocked | {done_n} done"
        )
    except Exception:
        pass

    # Automation loop stats (dispatch + verify counts from task history)
    try:
        from . import tasks as task_store
        all_tasks_7d = task_store.task_stats(since_days=7)
        # Count verify tasks that passed/failed in the last 7 days
        verify_tasks = task_store.list_tasks(limit=200)
        week_ago_str = None
        from datetime import datetime, timezone, timedelta
        week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
        verify_7d = [
            t for t in verify_tasks
            if t.kind == "verify" and t.session_id and "ticket-" in t.session_id
            and (t.finished_at or "") >= week_ago[:10]
        ]
        verify_passed = sum(1 for t in verify_7d if t.status == "done")
        verify_failed = sum(1 for t in verify_7d if t.status == "failed")
        from .dispatcher import get_dispatch_stats
        disp = get_dispatch_stats()
        total_dispatched = disp.get("total_dispatched", 0)
        if total_dispatched > 0 or verify_7d:
            loop_line = f"Loop (session): dispatched {total_dispatched}"
            if verify_7d:
                loop_line += f" | verified {verify_passed} pass / {verify_failed} fail"
            lines.append(loop_line)
    except Exception:
        pass

    # Exhausted tasks: failed/blocked with no retries left
    try:
        from . import tasks as _task_store_ex
        exhausted = _task_store_ex.list_exhausted_tasks(limit=5)
        if exhausted:
            lines.append(f"Exhausted tasks: {len(exhausted)} need attention")
            for t in exhausted[:3]:
                lines.append(f"  [{t.status}] {t.task_id[:16]} -- {(t.instruction or '')[:50]}")
    except Exception:
        pass

    from datetime import datetime, timezone
    lines.append(f"Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    return "\n".join(lines)


async def _auto_session_log() -> None:
    """Daily 23:30 UTC: auto-write a session log entry from today's git commits.

    Calls sprint_summary.write_session_log(since='today') to build a brief
    sprint paragraph from any commits made today, then prepends it to
    .memory/sessions/_history.md and <workspace>/updates.txt.

    No-op when ATLAS_AUTO_SESSION_LOG=off (default on when any commits found).
    Fire-and-forget: errors are logged but never raise.
    """
    import os
    if os.environ.get("ATLAS_AUTO_SESSION_LOG", "on").lower() in ("off", "0", "false"):
        return
    try:
        from atlas_plus.autonomous import require_autonomous_writes
        try:
            require_autonomous_writes("auto_session_log")
        except RuntimeError as gate_exc:
            log.debug("Auto session log skipped: %s", gate_exc)
            return
        from atlas_plus.sprint_summary import write_session_log
        result = await asyncio.to_thread(write_session_log, since="today")
        if result.get("ok") and result.get("paths_written"):
            log.info("Auto session log: wrote %d file(s)", len(result["paths_written"]))
        elif not result.get("entry"):
            log.debug("Auto session log: no commits today, skipped")
    except Exception as exc:
        log.warning("Auto session log failed: %s", exc)


async def _weekly_digest_push() -> None:
    """Monday 09:00 UTC: send weekly Atlas stats digest via ntfy.

    Fires only when ATLAS_NTFY_URL is configured. Summary includes task stats,
    memory quality, research quality, pending facts, and open tickets.
    In-process (no worker task) -- fast stat queries only.
    """
    try:
        digest = await asyncio.to_thread(_build_weekly_digest)
        try:
            from .server import _ntfy
            asyncio.create_task(_ntfy(
                "Atlas Weekly Digest",
                digest,
                priority="default",
            ))
            log.info("Weekly digest sent via ntfy (%d chars)", len(digest))
        except Exception as e:
            log.debug("Weekly digest ntfy push failed: %s", e)
    except Exception as exc:
        log.warning("Weekly digest build failed: %s", exc)


async def _ticket_dispatch() -> None:
    """Every 10 minutes: auto-dispatch eligible todo tickets to Brain tasks.

    Scans all tickets with status=todo and assignee in {claude-code, codex, local, any}.
    For each eligible ticket with no active task, creates a Brain task and marks
    the ticket in-progress. Task completion automatically closes the ticket via
    _try_close_ticket() (existing mechanism in tasks.py).

    Runs in-process: no worker task queued. Failure-safe -- any ticket or task
    error is logged and skipped without blocking the rest of the batch.
    """
    try:
        from .dispatcher import dispatch_all_eligible, is_dispatch_enabled
        if not is_dispatch_enabled():
            log.debug("Ticket dispatch disabled (ATLAS_TICKET_DISPATCH != on)")
            return
        dispatched = await asyncio.to_thread(dispatch_all_eligible)
        if dispatched:
            ids = ", ".join(d["ticket_id"] for d in dispatched)
            log.info("Ticket dispatch: %d ticket(s) queued: %s", len(dispatched), ids)
            # ntfy push so the phone shows which tickets entered the queue
            try:
                from .server import _ntfy
                asyncio.create_task(_ntfy(
                    f"Atlas: {len(dispatched)} ticket(s) dispatched",
                    ids,
                    priority="low",
                ))
            except Exception as e:
                log.debug("Ticket dispatch ntfy push failed: %s", e)
    except Exception as exc:
        log.warning("Ticket dispatch job failed: %s", exc)


async def _suggest_tags_daily() -> None:
    """Daily in-process tag suggestion for untagged .memory/ files (04:15 UTC).

    Scans MEMORY_ROOT for files without tags: frontmatter and applies deterministic
    tag suggestions based on directory, type field, and title keywords.
    In-process (no worker task) -- fast rglob, pure text analysis.
    """
    try:
        from pathlib import Path as _Path
        from atlas_plus.mcp.memory_server.config import MEMORY_ROOT
        from atlas_plus.curation.tag_suggest import suggest_tags_for_file, inject_tags

        skip_dirs = {"archive", "_sync", "versions", "_atlas", "anti"}
        tagged = 0
        skipped = 0

        for md_file in MEMORY_ROOT.rglob("*.md"):
            rel_parts = md_file.relative_to(MEMORY_ROOT).parts
            if any(p in skip_dirs for p in rel_parts):
                continue
            if md_file.name.startswith("_"):
                continue
            suggestions = await asyncio.to_thread(
                suggest_tags_for_file, md_file, MEMORY_ROOT, 4
            )
            if not suggestions:
                skipped += 1
                continue
            result = await asyncio.to_thread(inject_tags, md_file, suggestions)
            if result:
                tagged += 1
            else:
                skipped += 1

        if tagged > 0:
            log.info("Tag suggestion: tagged %d file(s), %d already tagged or skipped", tagged, skipped)
        else:
            log.debug("Tag suggestion: no untagged files found (%d already tagged)", skipped)
    except Exception as exc:
        log.warning("Tag suggestion job failed: %s", exc)


async def _db_backup_daily() -> None:
    """Daily WAL-safe snapshot of state.sqlite (02:00 UTC, in-process).

    Runs directly in the scheduler coroutine (no worker task) since it is fast
    (VACUUM INTO is a single SQL call) and must not be deferred to the worker queue.
    After taking the snapshot it prunes to keep only ATLAS_DB_BACKUP_KEEP files.
    """
    import os
    from atlas_plus.db_backup.core import snapshot as _snapshot, prune as _prune
    keep = int(os.environ.get("ATLAS_DB_BACKUP_KEEP", "14"))
    try:
        dest = await asyncio.to_thread(_snapshot)
        pruned = await asyncio.to_thread(_prune, keep)
        log.info("DB backup: %s  (pruned %d old)", dest, pruned)
    except FileNotFoundError:
        log.debug("DB backup skipped: state.sqlite not found yet")
    except Exception as exc:
        log.warning("DB backup failed: %s", exc)


async def _convergence_report() -> None:
    """Weekly contradiction divergence scan (Sunday 03:35 UTC)."""
    await _queue_worker_task(
        instruction=(
            "Write the weekly memory convergence report: scan .memory/ for files "
            "with unresolved contradiction flags and write .atlas/reports/convergence-*.md. "
            "No LLM required -- deterministic frontmatter scan."
        ),
        session_id="scheduler-convergence-report",
        kind="convergence_report",
        priority=4,
    )


async def _embed_index_nightly() -> None:
    """Nightly refresh of the semantic embedding index (04:40 UTC).

    The index must eat its own cooking: without a scheduled refresh it
    silently drifts stale as the corpus changes -- the exact decay this
    system exists to prevent. Skips cleanly when semantic search is
    disabled; per-file embed failures (Ollama down) are counted, logged,
    and retried the next night.
    """
    import asyncio

    def _run() -> dict:
        from atlas_plus.mcp.memory_server import embeddings

        if not embeddings.enabled():
            return {"skipped": "ATLAS_SEMANTIC_SEARCH=off"}
        return embeddings.rebuild_index()

    try:
        result = await asyncio.to_thread(_run)
        log.info("embed-index nightly: %s", result)
    except Exception:
        log.warning("embed-index nightly failed", exc_info=True)


# --- Outage catch-up (P-1, OPTIMIZATION-PASS-2026-08-05.md) -------------------
# A machine that was off during the nightly window silently loses the whole
# hygiene run (observed 2026-08-05: down 02:00-09:00 UTC, every 03:05-04:30
# job skipped -- APScheduler's in-memory jobstore cannot misfire jobs that
# never existed). On scheduler start, any catch-up-eligible job whose LAST
# RECORDED run is older than its cadence + grace is queued once, staggered.
# Jobs that have NEVER run are left for their natural first cron: catch-up
# heals outages, it does not initialize fresh installs (a stranger's first
# boot should not stampede LLM-dependent jobs before Ollama exists).
# Disable entirely with ATLAS_SCHEDULER_CATCHUP=off.

_DAY = 24.0
_WEEK = 7 * 24.0
_CATCHUP_GRACE_HOURS = 2.0
_CATCHUP_STAGGER_SECONDS = 90

# (job_id, cadence_hours). High-frequency jobs self-heal and the 23:30
# auto-session-log is time-sensitive, so none of those appear here.
_CATCHUP_CADENCE_HOURS: dict[str, float] = {
    "db-backup": _DAY,
    "temporal-build": _DAY,
    "suggest-facts": _DAY,
    "curation": _DAY,
    "research-batch": _DAY,
    "patterns-rebuild": _DAY,
    "dream": _DAY,
    "memory-trends": _DAY,
    "embed-index": _DAY,
    "research-quality": _DAY,
    "suggest-tags": _DAY,
    "gaps-report": _WEEK,
    "convergence-report": _WEEK,
    "unconfirmed-decay": _WEEK,
    "decay-report": _WEEK,
    "reverify": _WEEK,
    "memory-agent": _WEEK,
    "narrative-weekly": _WEEK,
    "claude-supervisor": _WEEK,
    "reflexion-loop": _WEEK,
    "weekly-digest": _WEEK,
}


def catchup_plan(last_runs: dict[str, str | None], now: datetime) -> list[str]:
    """Pure decision: which registered jobs are overdue enough to catch up.

    last_runs maps job_id -> ISO timestamp of the last recorded run (from the
    scheduler_runs heartbeat ledger). Jobs absent from the map (never ran)
    are NOT caught up, by design. Returns job_ids in registration order.
    """
    overdue: list[str] = []
    for job_id, cadence_hours in _CATCHUP_CADENCE_HOURS.items():
        # Self-healing for ledgers written before 2026-09-27: heartbeats
        # from catch-up runs landed under "<job>-catchup", so the freshest
        # of the two ids is the truth.
        candidates = [
            s for s in (last_runs.get(job_id), last_runs.get(f"{job_id}-catchup")) if s
        ]
        # String max() is safe ONLY because tasks._now() is the sole writer
        # and emits one fixed-width UTC ISO format; a second writer with a
        # different shape would silently misorder (expert review note).
        stamp = max(candidates) if candidates else None
        if not stamp:
            continue
        try:
            last = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
            if last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)
        except ValueError:
            continue  # unparseable heartbeat: do not guess
        age_hours = (now - last).total_seconds() / 3600.0
        if age_hours > cadence_hours + _CATCHUP_GRACE_HOURS:
            overdue.append(job_id)
    return overdue


def _schedule_catchup(scheduler, job_funcs: dict[str, object]) -> list[str]:
    """Queue one-shot catch-up runs for overdue jobs, staggered. Never raises."""
    if os.environ.get("ATLAS_SCHEDULER_CATCHUP", "").strip().lower() == "off":
        log.info("Scheduler catch-up disabled via ATLAS_SCHEDULER_CATCHUP=off")
        return []
    try:
        from . import tasks as task_store

        last_runs = {
            r["job_id"]: r["last_run_at"] for r in task_store.list_job_runs()
        }
    except Exception:
        log.warning("Catch-up skipped: could not read scheduler_runs", exc_info=True)
        return []
    now = datetime.now(timezone.utc)
    overdue = catchup_plan(last_runs, now)
    for i, job_id in enumerate(overdue):
        func = job_funcs.get(job_id)
        if func is None:
            continue
        run_at = now + timedelta(seconds=_CATCHUP_STAGGER_SECONDS * (i + 1))
        try:
            scheduler.add_job(
                func, "date", run_date=run_at,
                id=f"{job_id}-catchup", replace_existing=True,
                misfire_grace_time=3600,
            )
            log.warning(
                "Catch-up: %r last ran %s; queued for %s",
                job_id, last_runs.get(job_id), run_at.isoformat(timespec="seconds"),
            )
        except Exception:
            log.warning("Failed to queue catch-up for %r", job_id, exc_info=True)
    return overdue


def start_scheduler() -> AsyncIOScheduler:
    """Create and start the APScheduler instance with all registered Brain jobs. Idempotent."""
    global _scheduler
    if _scheduler and _scheduler.running:
        return _scheduler

    _scheduler = AsyncIOScheduler(timezone="UTC")
    _scheduler.add_job(_db_backup_daily,  "cron", hour=2, minute=0,  id="db-backup")
    _scheduler.add_job(_temporal_build, "cron", hour=3, minute=5,  id="temporal-build")
    _scheduler.add_job(_suggest_facts,  "cron", hour=3, minute=10, id="suggest-facts")
    _scheduler.add_job(_curation,       "cron", hour=3, minute=15, id="curation")
    _scheduler.add_job(_research_batch,      "cron", hour=3, minute=20,                    id="research-batch")
    _scheduler.add_job(_patterns_rebuild,    "cron", hour=3, minute=25,                    id="patterns-rebuild")
    _scheduler.add_job(_gaps_report,          "cron", day_of_week="sun", hour=3, minute=30, id="gaps-report")
    _scheduler.add_job(_convergence_report,   "cron", day_of_week="sun", hour=3, minute=35, id="convergence-report")
    _scheduler.add_job(_unconfirmed_decay_weekly, "cron", day_of_week="sun", hour=3, minute=40, id="unconfirmed-decay")
    _scheduler.add_job(_decay_report,             "cron", day_of_week="sun", hour=3, minute=45, id="decay-report")
    _scheduler.add_job(_reverify_weekly,          "cron", day_of_week="sun", hour=4, minute=20, id="reverify")
    _scheduler.add_job(_memory_agent_weekly,      "cron", day_of_week="sun", hour=3, minute=50, id="memory-agent")
    _scheduler.add_job(_claude_supervisor_weekly, "cron", day_of_week="mon", hour=4, minute=0,  id="claude-supervisor")
    _scheduler.add_job(_narrative_weekly,    "cron", day_of_week="sun", hour=3, minute=55, id="narrative-weekly")
    _scheduler.add_job(_dream_nightly,       "cron", hour=4, minute=30,                    id="dream")
    _scheduler.add_job(_reflexion_weekly,    "cron", day_of_week="sun", hour=4, minute=35, id="reflexion-loop")
    # DISABLED 2026-05-19 -- manual invocation preferred for now
    # _scheduler.add_job(_research_api_batch_1, "cron", hour=5, minute=0,                    id="research-api-batch-1")
    # _scheduler.add_job(_research_api_batch_2, "cron", hour=9, minute=0,                    id="research-api-batch-2")
    _scheduler.add_job(_memory_trends_daily,    "cron", hour=4, minute=5,  id="memory-trends")
    _scheduler.add_job(_embed_index_nightly,    "cron", hour=4, minute=40, id="embed-index")
    _scheduler.add_job(_research_quality_daily, "cron", hour=4, minute=10, id="research-quality")
    _scheduler.add_job(_suggest_tags_daily,     "cron", hour=4, minute=15, id="suggest-tags")
    _scheduler.add_job(_weekly_digest_push,     "cron", day_of_week="mon", hour=9, minute=0, id="weekly-digest")
    _scheduler.add_job(_auto_session_log,        "cron", hour=23, minute=30,                   id="auto-session-log")
    _scheduler.add_job(_ticket_dispatch,         "cron", minute="*/10",                        id="ticket-dispatch")
    _scheduler.add_job(_sla_check,           "cron", minute="*/10",                        id="sla-check")
    _scheduler.add_job(_zombie_cleanup,      "cron", minute=45,                            id="zombie-cleanup")

    _scheduler.add_listener(_job_event_listener, EVENT_JOB_ERROR | EVENT_JOB_EXECUTED)
    _scheduler.start()
    log.info(
        "Brain scheduler started -- %d jobs registered",
        len(_scheduler.get_jobs()),
    )

    # Outage catch-up: heal any nightly/weekly run the downtime swallowed.
    _schedule_catchup(_scheduler, {
        "db-backup": _db_backup_daily,
        "temporal-build": _temporal_build,
        "suggest-facts": _suggest_facts,
        "curation": _curation,
        "research-batch": _research_batch,
        "patterns-rebuild": _patterns_rebuild,
        "dream": _dream_nightly,
        "memory-trends": _memory_trends_daily,
        "embed-index": _embed_index_nightly,
        "research-quality": _research_quality_daily,
        "suggest-tags": _suggest_tags_daily,
        "gaps-report": _gaps_report,
        "convergence-report": _convergence_report,
        "unconfirmed-decay": _unconfirmed_decay_weekly,
        "decay-report": _decay_report,
        "reverify": _reverify_weekly,
        "memory-agent": _memory_agent_weekly,
        "narrative-weekly": _narrative_weekly,
        "claude-supervisor": _claude_supervisor_weekly,
        "reflexion-loop": _reflexion_weekly,
        "weekly-digest": _weekly_digest_push,
    })
    return _scheduler


def stop_scheduler() -> None:
    """Gracefully shut down the running scheduler (no-wait). Safe to call when scheduler is not running."""
    global _scheduler
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
        log.info("Brain scheduler stopped")
