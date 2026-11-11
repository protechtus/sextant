"""Atlas Brain local worker.

Consumes durable tasks from `brain_tasks`, runs bounded executors, and stores
artifacts under `.atlas/brain/artifacts/` by default.

Result digest: when a task completes with a long result (>500 chars), the worker
optionally generates a 2-3 sentence AI summary using the fast local model and
stores it in result_digest. Controlled by ATLAS_DIGEST_RESULTS=on (default off).
The digest is shown in the Web UI Tasks tab for quick review without loading the
full result.
"""

from __future__ import annotations

import argparse
import logging
import os
import socket
import threading
import time
import uuid
from pathlib import Path
from typing import Optional

from . import executors
from . import tasks as task_store
from .context import _DEFAULT_ARTIFACT_ROOT
from atlas_plus.redaction import redact

log = logging.getLogger("atlas.brain.worker")

# Heartbeat interval: update heartbeat_at every N seconds during long executor runs.
# Must be well below _DEFAULT_LEASE_SECONDS (300s) to avoid zombie reaping.
_HEARTBEAT_INTERVAL = int(os.environ.get("ATLAS_WORKER_HEARTBEAT_INTERVAL", "60"))


class _HeartbeatKeeper:
    """Background daemon thread that renews a task's heartbeat while it runs.

    Use as a context manager around run_executor() calls:

        with _HeartbeatKeeper(task_id, db_path):
            result = executors.run_executor(task, ctx)

    The thread stops automatically when the context exits (normal or exception).
    """

    def __init__(self, task_id: str, db_path: Path, interval: int = _HEARTBEAT_INTERVAL,
                 worker_id: "str | None" = None):
        self._task_id = task_id
        self._db_path = db_path
        self._interval = max(1, interval)  # minimum 1s; production uses ATLAS_WORKER_HEARTBEAT_INTERVAL (default 60s)
        self._worker_id = worker_id
        self._stop = threading.Event()
        # Set when an owner-checked renewal is refused: the lease expired and another
        # worker reclaimed the task. The keeper stops renewing (it would otherwise keep a
        # row it does not own looking alive) and run_once skips finalization
        # (round C 2026-11-10, lane B F1).
        self.lost = threading.Event()
        self._thread: threading.Thread | None = None

    def __enter__(self) -> "_HeartbeatKeeper":
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="worker-heartbeat")
        self._thread.start()
        return self

    def __exit__(self, *_) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    def _loop(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                if self._worker_id is not None:
                    if not task_store.heartbeat_task(self._task_id, self._worker_id,
                                                     db_path=self._db_path):
                        log.warning("task %s: lease lost, heartbeat stopped",
                                    self._task_id[:16])
                        self.lost.set()
                        return
                else:
                    task_store.update_heartbeat(self._task_id, db_path=self._db_path)
                log.debug("Heartbeat renewed for task %s", self._task_id[:16])
            except Exception as exc:
                log.warning("Heartbeat renewal failed for task %s: %s", self._task_id[:16], exc)


# Only generate digests for results longer than this threshold
_DIGEST_MIN_CHARS = 500
_DIGEST_MAX_INPUT = 2000  # chars of result text sent to the model


def _generate_digest(result_text: str, instruction: str) -> Optional[str]:
    """Generate a 2-3 sentence digest of a task result using the fast local model.

    Returns the digest string or None if generation fails or is not configured.
    Only runs when ATLAS_DIGEST_RESULTS=on and result exceeds _DIGEST_MIN_CHARS.
    """
    if os.environ.get("ATLAS_DIGEST_RESULTS", "off").lower() not in ("on", "1", "true"):
        return None
    if len(result_text) < _DIGEST_MIN_CHARS:
        return None
    try:
        from atlas_plus.llm import get_provider
        prompt = (
            f"Summarise the following task result in 2-3 sentences. "
            f"Be specific: mention key outputs, file paths, or metrics. "
            f"Do not repeat the instruction.\n\n"
            f"Instruction: {instruction[:200]}\n\n"
            f"Result:\n{result_text[:_DIGEST_MAX_INPUT]}"
        )
        # 'fast' role -> qwen3:1.7b by default; honors ATLAS_MODEL_FAST_PROVIDER.
        digest = get_provider("fast").generate(prompt, temperature=0.3, timeout=30.0).strip()
        return digest[:500] if digest else None
    except Exception as exc:
        log.debug("Digest generation failed: %s", exc)
    return None


def _worker_id() -> str:
    # Full UUID (32 hex chars) to make lease collisions statistically impossible.
    return f"{socket.gethostname()}-{uuid.uuid4().hex}"


def _push_completion_ntfy(task_id: str, status: str, result_text: str) -> None:
    """POST a task completion notification to ntfy when ATLAS_NTFY_URL is configured.

    Format: '[done] task-abc123 -- First 100 chars of result...'
    Silently no-ops when ATLAS_NTFY_URL is missing or the POST fails.
    Requires ATLAS_NTFY_COMPLETIONS=on to fire (set in the worker's environment).
    """
    ntfy_url = os.environ.get("ATLAS_NTFY_URL", "")
    if not ntfy_url:
        return
    short_id = task_id[:12]
    summary = (result_text or "")[:100].replace("\n", " ")
    msg = f"[{status}] {short_id} -- {summary}"
    icon = "white_check_mark" if status == "done" else "x"
    try:
        import httpx as _httpx
        headers = {
            "Title": f"Brain task {status}: {short_id}",
            "Tags": icon,
            "Priority": "default" if status == "done" else "high",
        }
        _ntfy_token = os.environ.get("ATLAS_NTFY_TOKEN", "")  # ATL-007: auth for reserved topics
        if _ntfy_token:
            headers["Authorization"] = f"Bearer {_ntfy_token}"
        _httpx.post(
            ntfy_url,
            content=msg.encode(),
            headers=headers,
            timeout=5.0,
        )
        log.debug("Completion ntfy sent for task %s (%s)", short_id, status)
    except Exception as exc:
        log.debug("Completion ntfy failed: %s", exc)


def _finish_task(
    task_id: str,
    result: executors.ExecutionResult,
    *,
    db_path: Path,
    task: "task_store.Task | None" = None,
    owner: "str | None" = None,
) -> None:
    """Finalize a task and optionally escalate failures to Claude Code.

    The optional `task` argument is provided when the caller already has the
    Task object in scope (avoids a second DB lookup for escalation checks).
    """
    artifact_path = str(result.artifact_path) if result.artifact_path else None
    instruction = task.instruction if task is not None else ""
    finished_now = task_store._now()

    # ATL-017 / audit MEDIUM-4: redact secrets before the result is PERSISTED to the task DB
    # (subprocess stdout/stderr is spliced into result.text and could echo a token). In-memory
    # heuristics below (quality gate, verify-reopen) keep the original text -- redaction only
    # affects what lands on disk.
    stored = redact(result.text) if result.text else result.text

    # Fenced on the lease this worker claimed under (owner AND attempt): a late finish
    # after the lease expired and another worker reclaimed the task changes nothing
    # (wave-2 2026-11-09; round C lane B F2).
    attempt = task.attempts if (task is not None and owner is not None) else None

    def _refused(outcome: str) -> None:
        log.warning("task %s: %s outcome refused, lease no longer ours", task_id, outcome)
        if task is not None:
            task_store.record_task_attempt(
                task_id, attempt_num=task.attempts, outcome=f"refused-{outcome}",
                agent=task.agent, started_at=task.started_at, finished_at=finished_now,
                result_summary=result.text, db_path=db_path,
            )

    if result.status == "done":
        if not task_store.mark_done(task_id, stored, db_path=db_path, artifact_path=artifact_path,
                                    owner=owner, attempt=attempt):
            _refused("done")
            return
    elif result.status == "blocked":
        if not task_store.mark_blocked(task_id, stored, db_path=db_path, artifact_path=artifact_path,
                                       owner=owner, attempt=attempt):
            _refused("blocked")
            return
    else:
        if not task_store.mark_failed(task_id, stored, db_path=db_path, artifact_path=artifact_path,
                                      owner=owner, attempt=attempt):
            _refused("failed")
            return

    # Record per-attempt telemetry FIRST, so a failure in any post-processing step below
    # (quality gate, verify, digest, ntfy, escalation) cannot lose the attempt row
    # (round C 2026-11-10, lane B F4).
    if task is not None:
        task_store.record_task_attempt(
            task_id,
            attempt_num=task.attempts,
            outcome=result.status,
            agent=task.agent,
            started_at=task.started_at,
            finished_at=finished_now,
            result_summary=result.text,
            db_path=db_path,
        )

    if result.status == "done":
        # Quality gate: if result looks like a refusal or is too short, reopen
        # the linked ticket so it gets re-dispatched on the next poll cycle.
        if task is not None:
            from .dispatcher import reopen_ticket_if_low_quality
            reopen_ticket_if_low_quality(task.session_id or "", result.text)
        # Auto-verify: queue a kind=verify task after agent ticket tasks when
        # ATLAS_VERIFY_TICKETS=on. The verify task runs pytest; failure reopens
        # the ticket via _reopen_ticket_from_verify_failure() below.
        if task is not None and os.environ.get("ATLAS_VERIFY_TICKETS", "off").lower() in (
            "on", "1", "true"
        ) and getattr(task, "kind", "") == "agent":
            from .dispatcher import create_verify_task
            create_verify_task(
                task.session_id or "", task.instruction, db_path=db_path
            )
        # Generate and store a brief digest for long results (ATLAS_DIGEST_RESULTS=on).
        # Feed the redacted text so a secret can't survive into the persisted digest.
        digest = _generate_digest(stored, instruction)
        if digest:
            task_store.set_result_digest(task_id, redact(digest), db_path=db_path)

    # Verify failure -> ticket reopen: when a kind=verify task fails, reopen
    # the original ticket so it gets re-dispatched on the next poll cycle.
    if result.status in ("failed", "blocked") and task is not None and getattr(task, "kind", "") == "verify":
        from .dispatcher import _reopen_ticket_from_verify_failure
        _reopen_ticket_from_verify_failure(task.session_id or "", result.text)

    # Task completion push notification (opt-in via ATLAS_NTFY_COMPLETIONS=on).
    # Fires for done + failed outcomes when ATLAS_NTFY_URL is set.
    # Blocked is excluded: it means the task will retry, not that it's finished.
    if result.status in ("done", "failed") and os.environ.get(
        "ATLAS_NTFY_COMPLETIONS", "off"
    ).lower() in ("on", "1", "true") and task_store.claim_task_notification(
        task_id, result.status, db_path=db_path
    ):
        _push_completion_ntfy(task_id, result.status, result.text)

    # Phase 1: Failure/block escalation to Claude Code (worker path).
    # The inline server path handles its own escalation via asyncio.create_task().
    # Worker tasks just need the new escalation task queued -- the worker's next
    # iteration (or another worker instance) will claim and execute it.
    if result.status in ("failed", "blocked") and task is not None:
        from .escalation import should_escalate_failure, create_escalation_task
        if should_escalate_failure(task.agent, task.session_id, task.kind):
            create_escalation_task(
                task_id, task.session_id, task.instruction,
                f"worker: {result.status}", result.text,
            )


def run_once(
    *,
    db_path: Optional[Path] = None,
    artifact_root: Optional[Path] = None,
    worker_id: Optional[str] = None,
) -> int:
    """Claim and execute one pending task. Returns 1 if a task ran, 0 if queue empty.

    Claim-execute-finish pattern:
      1. claim_next_task() atomically sets lease_owner + lease_expires_at via
         a single UPDATE ... WHERE status='pending'. Only one worker wins per row.
      2. run_executor() dispatches to the correct executor (agent, temporal_build,
         curation_report, etc.) and returns an ExecutionResult.
      3. _finish_task() calls mark_done/mark_blocked/mark_failed which also
         triggers _try_close_ticket() if the session_id encodes a ticket reference.

    Any unhandled exception from run_executor marks the task failed rather than
    leaving it in-flight (which would eventually be reclaimed by zombie-cleanup).
    """
    wid = worker_id or _worker_id()
    root = artifact_root or _DEFAULT_ARTIFACT_ROOT
    if db_path is None:  # resolve at call time so tests can monkeypatch task_store._DB_PATH
        db_path = task_store._DB_PATH
    task = task_store.claim_next_task(wid, db_path=db_path)
    if task is None:
        return 0

    ctx = executors.ExecutorContext(
        task=task,
        artifact_root=root,
        db_path=db_path,
        worker_id=wid,
    )
    try:
        with _HeartbeatKeeper(task.task_id, db_path=db_path, worker_id=wid) as keeper:
            result = executors.run_executor(task, ctx)
        if keeper.lost.is_set():
            # Another worker holds the lease now; our result is history, not state.
            log.warning("task %s: lease lost during execution, result discarded", task.task_id)
            task_store.record_task_attempt(
                task.task_id, attempt_num=task.attempts, outcome="lease-lost", agent=task.agent,
                started_at=task.started_at, finished_at=task_store._now(),
                result_summary=result.text, db_path=db_path,
            )
            return 1
        _finish_task(task.task_id, result, db_path=db_path, task=task, owner=wid)
    except Exception as exc:
        task_store.mark_failed(task.task_id, str(exc), db_path=db_path, owner=wid,
                               attempt=task.attempts)
    return 1


def run_loop(
    *,
    sleep_seconds: float = 5.0,
    limit: Optional[int] = None,
    db_path: Optional[Path] = None,
    artifact_root: Optional[Path] = None,
    worker_id: Optional[str] = None,
) -> int:
    """Poll and execute tasks continuously until stopped or limit reached.

    Adaptive idle backoff: when the queue is empty the sleep interval doubles
    on each consecutive idle poll (capped at 30s) to avoid burning CPU during
    quiet periods.  It resets to sleep_seconds the moment a task is found.

    This means a worker in a busy queue processes tasks back-to-back without
    sleeping, while an idle overnight worker polls at most once every 30s.

    Args:
        sleep_seconds: Base idle interval.  Also the reset value after work.
        limit:         If set, exit after processing this many tasks. Used by
                       tests and --limit CLI flag to avoid infinite loops.
    """
    processed = 0
    idle_sleep = sleep_seconds  # grows during idle stretches
    wid = worker_id or _worker_id()
    if limit == 0:
        return 0

    while True:
        ran = run_once(db_path=db_path, artifact_root=artifact_root, worker_id=wid)
        processed += ran
        if limit is not None and processed >= limit:
            return processed
        if ran == 0:
            if limit is not None:
                return processed
            time.sleep(idle_sleep)
            # Back off exponentially when queue is quiet; reset on work found.
            idle_sleep = min(idle_sleep * 2, 30.0)
        else:
            idle_sleep = sleep_seconds  # reset after successful task


def main() -> None:
    """CLI entry point for the Atlas Brain task worker process.

    Parses --once / --limit / --sleep / --worker-id flags, then delegates to
    run_loop(). Typical invocation is from a service wrapper (no --limit)
    for the always-on Windows Scheduled Task; --once / --limit used in tests.
    """
    parser = argparse.ArgumentParser(description="Atlas Brain local task worker")
    parser.add_argument("--once", action="store_true", help="Process at most one task and exit")
    parser.add_argument("--limit", type=int, default=None, help="Process at most N tasks and exit")
    parser.add_argument("--sleep", type=float, default=5.0, help="Idle sleep seconds for loop mode")
    parser.add_argument("--worker-id", default=None, help="Stable worker ID for lease ownership")
    args = parser.parse_args()

    # ATL-016: enable the executor write-gate if ATLAS_BRAIN_GATEKEEPER=on (no-op otherwise).
    executors.enable_gatekeeper_if_configured()

    limit = 1 if args.once and args.limit is None else args.limit
    processed = run_loop(
        sleep_seconds=args.sleep,
        limit=limit,
        worker_id=args.worker_id,
    )
    print(f"processed={processed}")


if __name__ == "__main__":
    main()
