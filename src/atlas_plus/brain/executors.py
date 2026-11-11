"""Brain executor registry.

Executors are small, bounded adapters that turn a queued task into a stored
result. The default executor still uses the existing Brain agent router.

Write-policy gate (optional)
----------------------------

Executors that touch the file system go through `_check_gate()` before the
write. `_check_gate` consults the module-level `_GK_CLIENT`; when that is
None (the default, including in the test suite) gating is a no-op so the
existing executor surface keeps working. Production startup wires a real
client via `enable_gatekeeper_gating(client)`.

Why an inline check and not a decorator: the existing helpers
(`_write_text_artifact`) take positional args, and we want the gate to fire
for ALL callers of those helpers without rewriting every call site. An
inline `_check_gate` at the lowest write point keeps the change small and
the audit trail correct (the audit captures the real target path, not a
wrapping path).

The gate client is an OPTIONAL extension package (`atlas_plus.gatekeeper`).
It is not part of the public distribution; when it is absent the import
below fails softly and gating stays off. The MUST/SHOULD/NEVER write
contract the gate enforces is documented with that extension.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional
from atlas_plus import paths as atlas_paths

log = logging.getLogger("atlas.brain.executors")

from . import agents as agent_registry
from . import context as brain_context
from . import tasks as task_store
from .tasks import Task
from atlas_plus.redaction import redact
from atlas_plus.temporal_graph.build import build_temporal_graph

# Optional extension import. The gate client ships separately from the public
# distribution, so this must fail softly: the executor module loads and gating
# stays off when the extension is not installed.
try:
    from atlas_plus.gatekeeper.client import GatekeeperClient, PolicyDecision
    from atlas_plus.gatekeeper.decorator import (
        GatekeeperDenied,
        GatekeeperUnavailable,
    )
    _GK_IMPORT_ERROR: Exception | None = None
except Exception as _exc:  # noqa: BLE001
    GatekeeperClient = None  # type: ignore[assignment, misc]
    PolicyDecision = None  # type: ignore[assignment, misc]
    GatekeeperDenied = RuntimeError  # type: ignore[assignment, misc]
    GatekeeperUnavailable = RuntimeError  # type: ignore[assignment, misc]
    _GK_IMPORT_ERROR = _exc


# Module-level gatekeeper client. None = gating disabled (the default
# state at import time). Production startup calls
# `enable_gatekeeper_gating(client)` to turn the gate on. Tests can
# enable it with a fake client whose subprocess runner has been swapped
# for canned responses.
_GK_CLIENT: "GatekeeperClient | None" = None


def enable_gatekeeper_gating(client: "GatekeeperClient | None") -> None:
    """Install (or remove) the module-level gatekeeper client.

    Pass a `GatekeeperClient` instance to turn gating on. Pass `None` to
    turn it off (useful in tests or for emergency disable). Idempotent:
    calling with the same client twice is a no-op.

    Thread-safety: the assignment is atomic in CPython, but concurrent
    writes from multiple modules are not expected by design. The Brain
    worker process owns this slot for its lifetime.
    """
    global _GK_CLIENT
    _GK_CLIENT = client


def gatekeeper_gating_enabled() -> bool:
    """True when the module-level client is set. Used by tests and by
    introspection tools to verify state."""
    return _GK_CLIENT is not None


def enable_gatekeeper_if_configured() -> bool:
    """Enable the executor write-gate when ATLAS_BRAIN_GATEKEEPER is on (ATL-016 / audit HIGH-4).

    Without this call `_GK_CLIENT` stays None and `_check_gate` is a no-op, so the documented
    fail-closed write contract is unenforced in the running
    Brain. Bootstrap entrypoints (worker.main, server startup) call this so production gating is
    explicit and observable. Returns True if gating was turned on. Off by default so behaviour is
    opt-in and tests are unaffected.
    """
    enabled = os.environ.get("ATLAS_BRAIN_GATEKEEPER", "off").lower() in ("on", "1", "true")
    if not enabled:
        log.info("Brain gatekeeper gating: OFF (set ATLAS_BRAIN_GATEKEEPER=on to enforce write policy)")
        return False
    if GatekeeperClient is None:
        log.warning("ATLAS_BRAIN_GATEKEEPER=on but the gatekeeper client is unavailable: %s", _GK_IMPORT_ERROR)
        return False
    enable_gatekeeper_gating(GatekeeperClient())
    log.info("Brain gatekeeper gating: ON — executor writes are policy-checked.")
    return True


def _check_gate(
    action: str,
    target: str,
    *,
    context: dict[str, Any] | None = None,
    correlation_id: str | None = None,
) -> None:
    """Consult gatekeeper before a side-effectful operation.

    Returns silently on `allowed=True` (the wrapped op may proceed) or
    when gating is disabled (`_GK_CLIENT is None`). Raises
    `GatekeeperDenied` on `allowed=False` and `GatekeeperUnavailable` on
    `allowed=None`. Both exceptions are the typed ones from
    `atlas_plus.gatekeeper.decorator`.

    Fail-CLOSED: an unreachable gatekeeper does NOT pass through. Brain
    runs unattended; the wrapped function must not run unless the gate
    affirmatively allowed it. The contract is documented in
    the gatekeeper contract notes.

    No-op fast path: when gating is disabled the function returns
    immediately without touching gatekeeper. This keeps the existing
    test suite green (no subprocess to gatekeeper) without per-test
    monkey-patching.
    """
    if _GK_CLIENT is None:
        return
    decision = _GK_CLIENT.policy_check_sync(
        action=action,
        target=target,
        context=context,
        correlation_id=correlation_id,
    )
    if decision.allowed is True:
        return
    if decision.allowed is None:
        raise GatekeeperUnavailable(decision)
    raise GatekeeperDenied(decision)

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_PYTEST_TIMEOUT_SECONDS = 1800  # full suite (3400+ tests) exceeds 900s; raised 2026-06-04
_DEFAULT_PYTEST_ARGS = [
    "tests/test_brain_tasks.py",
    "tests/test_brain_context.py",
    "tests/test_brain_worker.py",
    "tests/test_brain_worker_launcher.py",
    "tests/test_brain_task_execution.py",
    "tests/test_brain_agent.py",
    "tests/test_brain_history.py",
    "tests/test_brain_improvement.py",
    "tests/test_brain_tools.py",
    "tests/test_brain_scheduler.py",
    "-q",
    "-p",
    "no:cacheprovider",
]


# ---------------------------------------------------------------------------
# Claude auto-promote system
#
# Local Qwen3 models handle short Q&A, summaries, and classification well.
# For these instruction shapes, Claude (via claude_task) is measurably better:
#   - Code review / audit        -- multi-file context + reasoning depth
#   - Code analysis / explanation -- tracing bugs across call chains
#   - Complex refactoring         -- architecture-level changes
#   - SIP/VoIP protocol work      -- RFC knowledge, trace reading, header parsing
#   - Research synthesis          -- cross-document reasoning, comparison tables
#   - Doc/code extraction         -- structured extraction from long technical text
#
# When ATLAS_CLAUDE_AUTO_PROMOTE=on: matching agent tasks are silently promoted to
# claude_task before execution (zero extra queue entry, same task record).
# When the env var is absent: the result text includes a suggestion note so the
# operator can re-queue manually or enable the flag.
# ---------------------------------------------------------------------------
_CLAUDE_STRENGTH_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (
        "code_review",
        re.compile(
            r"\b(code\s+review|review\s+((this|the|my|an?)\s+)?(code|implementation|function|module|class)"
            r"|audit\s+((the|this|an?|my)\s+)?(code|implementation|security))\b",
            re.IGNORECASE,
        ),
    ),
    (
        "code_analysis",
        re.compile(
            r"\b(analyze|analyse|explain|understand|trace)\s+"
            r"((this|the|an?|my)\s+)?(code|function|module|class|method|error|exception|traceback|stack\s+trace)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "complex_coding",
        re.compile(
            r"\b(refactor|redesign|architect|rewrite\s+(this|the)|migrate\s+(from|the|this))\b",
            re.IGNORECASE,
        ),
    ),
    (
        "sip_protocol",
        re.compile(
            r"\b(sip\s+(header|rfc|trace|ladder|packet|message|flow|call)"
            r"|pcap(\s+(analysis|review|file))?"
            r"|rfc\s*\d{4}"
            r"|stir.{0,3}shaken"
            r"|srtp\s+(key|session|audit)"
            r"|(rtp|jitter|mos)\s+(analysis|review|audit))\b",
            re.IGNORECASE,
        ),
    ),
    (
        "research_synthesis",
        re.compile(
            r"\b(synthesize|comprehensive\s+analysis"
            r"|compare\s+(these\s+)?(approaches|implementations|protocols|options|methods)"
            r"|evaluate\s+(these\s+)?(approaches|strategies|options|tradeoffs)"
            r"|cross.reference)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "doc_extraction",
        re.compile(
            r"\b(read\s+(through|this\s+code|this\s+file)"
            r"|extract\s+(key|important|critical)\s+(points|facts|findings|information)\s+from"
            r"|summarize\s+(this\s+)?(code|file|document|report)"
            r"|what\s+does\s+this\s+(code|function|class|file)\s+do)\b",
            re.IGNORECASE,
        ),
    ),
]


def _detect_claude_strength(instruction: str) -> str | None:
    """Return the matched pattern group name if the instruction matches Claude's strengths.

    Returns None when local Qwen models are appropriate. Only consulted for
    agent-kind tasks; explicit kinds (claude_task, verify, pytest, ...) bypass this.
    """
    for group_name, pattern in _CLAUDE_STRENGTH_PATTERNS:
        if pattern.search(instruction):
            return group_name
    return None


@dataclass
class ExecutionResult:
    """Return value from an executor: terminal status string, text output, and optional artifact path."""

    status: str
    text: str
    artifact_path: Optional[Path] = None


@dataclass
class ExecutorContext:
    """Runtime context injected into every executor: task record, artifact directory, DB path, worker ID."""

    task: Task
    artifact_root: Path
    db_path: Path
    worker_id: str


Executor = Callable[[ExecutorContext], ExecutionResult]


def _write_text_artifact(ctx: ExecutorContext, name: str, text: str) -> Path:
    """Write a UTF-8 text artifact under the task's artifact root.

    Goes through `_check_gate` before the write so that production
    deployments with gating enabled (`enable_gatekeeper_gating(client)`)
    can deny writes that violate the policy. When gating is disabled
    (the default, including during the existing test suite) this remains
    a plain file write. The directory mkdir is intentionally NOT gated;
    creating an empty directory does not match the destructive-write
    semantics gatekeeper protects against.
    """
    target_dir = ctx.artifact_root / ctx.task.task_id
    target_dir.mkdir(parents=True, exist_ok=True)
    path = target_dir / name
    _check_gate(
        action="write_file",
        target=str(path),
        context={
            "process": "atlas-brain",
            "task_kind": ctx.task.kind,
            "worker_id": ctx.worker_id,
        },
        correlation_id=ctx.task.task_id,
    )
    # ATL-017: redact secrets before persisting artifacts (executor reports splice raw subprocess
    # stdout/stderr; a token echoed by a subprocess would otherwise land in cleartext on disk).
    path.write_text(redact(text), encoding="utf-8")
    return path


def _noop(ctx: ExecutorContext) -> ExecutionResult:
    artifact = brain_context.write_context_artifact(
        ctx.task,
        artifact_root=ctx.artifact_root,
    )
    return ExecutionResult(status="done", text=ctx.task.instruction, artifact_path=artifact)


def _fail(ctx: ExecutorContext) -> ExecutionResult:
    raise RuntimeError("intentional test failure")


def _context_pack(ctx: ExecutorContext) -> ExecutionResult:
    artifact = brain_context.write_context_artifact(
        ctx.task,
        artifact_root=ctx.artifact_root,
    )
    return ExecutionResult(
        status="done",
        text=f"Context pack written to {artifact}",
        artifact_path=artifact,
    )


def _research_digest(ctx: ExecutorContext) -> ExecutionResult:
    research_root = brain_context._DEFAULT_RESEARCH_ROOT
    plan = research_root / "RESEARCH-PLAN.md"
    if not plan.exists():
        text = f"No research plan found at {plan}"
        artifact = _write_text_artifact(ctx, "research-digest.md", text)
        return ExecutionResult(status="blocked", text=text, artifact_path=artifact)

    lines = plan.read_text(encoding="utf-8", errors="replace").splitlines()
    queued = [line for line in lines if "| RES-" in line and "| queued |" in line][:12]
    done = [line for line in lines if "| RES-" in line and "| done |" in line][:12]
    text = "\n".join(
        [
            "---",
            f"id: {ctx.task.task_id}-research-digest",
            "title: Research Digest",
            "type: brain-artifact",
            "status: active",
            "---",
            "",
            "# Research Digest",
            "",
            "## Queued",
            *(queued or ["No queued research rows found."]),
            "",
            "## Done",
            *(done or ["No completed research rows found."]),
            "",
        ]
    )
    artifact = _write_text_artifact(ctx, "research-digest.md", text)
    return ExecutionResult(status="done", text=f"Research digest written to {artifact}", artifact_path=artifact)


def _pytest_python(project_root: Path) -> Path | str:
    venv311 = project_root / ".venv311" / "Scripts" / "python.exe"
    if venv311.exists():
        return venv311
    return sys.executable


# Non-ATLAS live-deployment keys that change src/ behavior when set (the same
# failure class as ATLAS_WEB_API_KEY: a verify run must look like clean CI).
_LIVE_ENV_KEYS = frozenset({"RESEARCH_API_KEY", "TELEGRAM_BOT_TOKEN"})


def _clean_pytest_env() -> dict[str, str]:
    """Environment for pytest/verify subprocesses: the worker's own ATLAS_*
    context must not leak into the suite.

    The Brain worker runs with the live deployment environment loaded
    (ATLAS_STATE_DIR, ATLAS_WEB_API_KEY, ...). Inherited by pytest, a live
    API key alone failed ~260 web tests on every verify run, and a live
    state dir pointed test fixtures at real operator state (2026-10-12,
    61 failed verify tasks in 14 days). Verify must reproduce the clean
    CI-like run, so every ATLAS_* variable is dropped, plus the handful of
    other live keys in _LIVE_ENV_KEYS. Matching is case-insensitive so a
    lowercase atlas_* cannot slip through on POSIX.
    """
    return {
        k: v
        for k, v in os.environ.items()
        if not k.upper().startswith("ATLAS_") and k.upper() not in _LIVE_ENV_KEYS
    }


def _pytest(ctx: ExecutorContext) -> ExecutionResult:
    python = _pytest_python(_PROJECT_ROOT)
    cmd = [str(python), "-m", "pytest", *_DEFAULT_PYTEST_ARGS]
    completed = subprocess.run(
        cmd,
        cwd=str(_PROJECT_ROOT),
        capture_output=True,
        text=True,
        timeout=_PYTEST_TIMEOUT_SECONDS,
        encoding="utf-8",
        errors="replace",
        env=_clean_pytest_env(),
    )
    status = "done" if completed.returncode == 0 else "failed"
    summary = "pytest passed" if completed.returncode == 0 else "pytest failed"
    report = "\n".join(
        [
            "---",
            f"id: {ctx.task.task_id}-pytest",
            "title: Brain Pytest Run",
            "type: brain-artifact",
            f"status: {status}",
            "---",
            "",
            "# Brain Pytest Run",
            "",
            f"Task: `{ctx.task.task_id}`",
            "",
            f"Return code: `{completed.returncode}`",
            "",
            "## Command",
            "",
            "```powershell",
            " ".join(cmd),
            "```",
            "",
            "## Stdout",
            "",
            "```text",
            completed.stdout.strip(),
            "```",
            "",
            "## Stderr",
            "",
            "```text",
            completed.stderr.strip(),
            "```",
            "",
        ]
    )
    artifact = _write_text_artifact(ctx, "pytest.md", report)
    return ExecutionResult(status=status, text=f"{summary}: {artifact}", artifact_path=artifact)


def _temporal_build(ctx: ExecutorContext) -> ExecutionResult:
    # source="all" includes memory, sessions, transcripts (metadata), and mining (tool patterns)
    # Manifest-based incremental -- unchanged files are skipped, so "all" is only slightly
    # slower than "atlas" on subsequent runs.
    result = build_temporal_graph(source="all")
    if "error" in result:
        text = str(result["error"])
        artifact = _write_text_artifact(ctx, "temporal-build.md", text)
        return ExecutionResult(status="failed", text=text, artifact_path=artifact)

    index = result["index"]
    report = "\n".join(
        [
            "---",
            f"id: {ctx.task.task_id}-temporal-build",
            "title: Temporal Build",
            "type: brain-artifact",
            "status: done",
            "---",
            "",
            "# Temporal Build",
            "",
            f"Task: `{ctx.task.task_id}`",
            "",
            "## Result",
            "",
            f"- source: `{result['source']}`",
            f"- files_indexed: `{result['files_indexed']}`",
            f"- files_skipped: `{result['files_skipped']}`",
            f"- facts_added: `{result['facts_added']}`",
            f"- episodes: `{index['episodes']}`",
            f"- entities: `{index['entities']}`",
            f"- facts_total: `{index['facts_total']}`",
            f"- facts_active: `{index['facts_active']}`",
            "",
            "## Warnings",
            "",
            *(f"- {warning}" for warning in (result.get("warnings") or ["None."])),
            "",
        ]
    )
    artifact = _write_text_artifact(ctx, "temporal-build.md", report)
    text = (
        "Temporal build complete: "
        f"{result['files_indexed']} indexed, {result['files_skipped']} skipped, "
        f"{result['facts_added']} facts added, {index['entities']} entities, "
        f"{index['facts_active']} active facts. Artifact: {artifact}"
    )
    return ExecutionResult(status="done", text=text, artifact_path=artifact)


def _gaps_report(ctx: ExecutorContext) -> ExecutionResult:
    """Run the weekly memory gap report with auto-stub and auto-ticket filing.

    Writes a gaps-YYYY-Www.md to .atlas/reports/, creates .memory/gaps/ stubs
    for uncovered topics, and files ATL dispatch tickets for gaps with no
    existing research report (--autoticket). Returns done/failed ExecutionResult.
    """
    python = _pytest_python(_PROJECT_ROOT)
    cmd = [
        str(python),
        "-m",
        "atlas_plus.curation.cli",
        "gaps",
        "--since",
        "7d",
        "--write",
        "--autofile",
        "--autoticket",
    ]
    completed = subprocess.run(
        cmd,
        cwd=str(_PROJECT_ROOT),
        capture_output=True,
        text=True,
        timeout=120,
        encoding="utf-8",
        errors="replace",
    )
    status = "done" if completed.returncode == 0 else "failed"
    summary = "Gaps report complete" if completed.returncode == 0 else "Gaps report failed"
    report = "\n".join(
        [
            "---",
            f"id: {ctx.task.task_id}-gaps-report",
            "title: Memory Gaps Report",
            "type: brain-artifact",
            f"status: {status}",
            "---",
            "",
            "# Memory Gaps Report",
            "",
            f"Task: `{ctx.task.task_id}`",
            "",
            f"Return code: `{completed.returncode}`",
            "",
            "## Command",
            "",
            "```powershell",
            " ".join(cmd),
            "```",
            "",
            "## Stdout",
            "",
            "```text",
            completed.stdout.strip(),
            "```",
            "",
            "## Stderr",
            "",
            "```text",
            completed.stderr.strip(),
            "```",
            "",
        ]
    )
    artifact = _write_text_artifact(ctx, "gaps-report.md", report)
    return ExecutionResult(status=status, text=f"{summary}: {artifact}", artifact_path=artifact)


def _convergence_report(ctx: ExecutorContext) -> ExecutionResult:
    """Write memory convergence report -- scan .memory/ for unresolved contradiction flags.

    Runs `atlas-review-memory convergence --write` to produce
    .atlas/reports/convergence-YYYY-MM-DD.md. No LLM required.
    """
    python = _pytest_python(_PROJECT_ROOT)
    cmd = [
        str(python),
        "-m",
        "atlas_plus.curation.cli",
        "convergence",
        "--write",
    ]
    completed = subprocess.run(
        cmd,
        cwd=str(_PROJECT_ROOT),
        capture_output=True,
        text=True,
        timeout=60,
        encoding="utf-8",
        errors="replace",
    )
    status = "done" if completed.returncode == 0 else "failed"
    summary = "Convergence report complete" if completed.returncode == 0 else "Convergence report failed"
    report = "\n".join(
        [
            "---",
            f"id: {ctx.task.task_id}-convergence-report",
            "title: Memory Convergence Report",
            "type: brain-artifact",
            f"status: {status}",
            "---",
            "",
            f"## {summary}",
            "",
            "## stdout",
            "```text",
            completed.stdout.strip()[:2000],
            "```",
            "",
            "## stderr",
            "```text",
            completed.stderr.strip()[:500],
            "```",
            "",
        ]
    )
    artifact = _write_text_artifact(ctx, "convergence-report.md", report)
    return ExecutionResult(status=status, text=f"{summary}: {artifact}", artifact_path=artifact)


def _curation_report(ctx: ExecutorContext) -> ExecutionResult:
    python = _pytest_python(_PROJECT_ROOT)
    cmd = [
        str(python),
        "-m",
        "atlas_plus.curation.cli",
        "report",
    ]
    try:
        completed = subprocess.run(
            cmd,
            cwd=str(_PROJECT_ROOT),
            capture_output=True,
            text=True,
            timeout=900,  # full curation report over the whole memory; 300 still timed out twice in Oct 2026 nightly runs (growing corpus), raised 2026-10-12
            encoding="utf-8",
            errors="replace",
        )
    except subprocess.TimeoutExpired:
        # Without this, the timeout escaped as a raw exception and the task
        # failed with no artifact; leave a legible trace like every other path.
        msg = "Curation report timed out after 900s"
        artifact = _write_text_artifact(ctx, "curation-report.md", msg)
        return ExecutionResult(status="failed", text=msg, artifact_path=artifact)
    status = "done" if completed.returncode == 0 else "failed"
    summary = "Curation report complete" if completed.returncode == 0 else "Curation report failed"
    report = "\n".join(
        [
            "---",
            f"id: {ctx.task.task_id}-curation-report",
            "title: Memory Curation Report",
            "type: brain-artifact",
            f"status: {status}",
            "---",
            "",
            "# Memory Curation Report",
            "",
            f"Task: `{ctx.task.task_id}`",
            "",
            f"Return code: `{completed.returncode}`",
            "",
            "## Command",
            "",
            "```powershell",
            " ".join(cmd),
            "```",
            "",
            "## Stdout",
            "",
            "```text",
            completed.stdout.strip(),
            "```",
            "",
            "## Stderr",
            "",
            "```text",
            completed.stderr.strip(),
            "```",
            "",
        ]
    )
    artifact = _write_text_artifact(ctx, "curation-report.md", report)
    return ExecutionResult(status=status, text=f"{summary}: {artifact}", artifact_path=artifact)


def _suggest_facts_review(ctx: ExecutorContext) -> ExecutionResult:
    python = _pytest_python(_PROJECT_ROOT)
    cmd = [
        str(python),
        "-m",
        "atlas_plus.curation.suggest_facts",
        "--since",
        "today",
        "--source",
        "claude-code",
        "--dry-run",
    ]
    completed = subprocess.run(
        cmd,
        cwd=str(_PROJECT_ROOT),
        capture_output=True,
        text=True,
        timeout=240,
        encoding="utf-8",
        errors="replace",
    )
    status = "done" if completed.returncode == 0 else "failed"
    summary = "Suggest-facts review complete" if completed.returncode == 0 else "Suggest-facts review failed"
    report = "\n".join(
        [
            "---",
            f"id: {ctx.task.task_id}-suggest-facts-review",
            "title: Suggest Facts Review",
            "type: brain-artifact",
            f"status: {status}",
            "---",
            "",
            "# Suggest Facts Review",
            "",
            f"Task: `{ctx.task.task_id}`",
            "",
            f"Return code: `{completed.returncode}`",
            "",
            "## Command",
            "",
            "```powershell",
            " ".join(cmd),
            "```",
            "",
            "## Stdout",
            "",
            "```text",
            completed.stdout.strip(),
            "```",
            "",
            "## Stderr",
            "",
            "```text",
            completed.stderr.strip(),
            "```",
            "",
        ]
    )
    artifact = _write_text_artifact(ctx, "suggest-facts-review.md", report)
    return ExecutionResult(status=status, text=f"{summary}: {artifact}", artifact_path=artifact)


def _run_reporting_subprocess(
    ctx: ExecutorContext,
    cmd: list[str],
    artifact_name: str,
    title: str,
    timeout: int = 300,
) -> ExecutionResult:
    """Run a curation subprocess and capture stdout/stderr into a brain artifact.

    Shared shape for the deterministic-report executors (curation, suggest-facts
    auto, dream, reflexion): run the command, wrap its output in a markdown
    artifact, and map the return code to done/failed.
    """
    try:
        completed = subprocess.run(
            cmd,
            cwd=str(_PROJECT_ROOT),
            capture_output=True,
            text=True,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
        )
    except subprocess.TimeoutExpired as exc:
        # Fail gracefully with a legible artifact instead of crashing the worker
        # with an unhandled exception. The shared helper previously did not catch
        # this, so a slow reflexion / suggest-facts-auto run killed the task
        # outright (never a "failed" record). 124 is the conventional timeout code.
        def _as_text(value: object) -> str:
            if value is None:
                return ""
            return value.decode("utf-8", "replace") if isinstance(value, bytes) else str(value)

        completed = subprocess.CompletedProcess(
            cmd,
            returncode=124,
            stdout=_as_text(exc.stdout),
            stderr=f"TimeoutExpired after {timeout}s\n{_as_text(exc.stderr)}",
        )
    status = "done" if completed.returncode == 0 else "failed"
    report = "\n".join(
        [
            "---",
            f"id: {ctx.task.task_id}-{artifact_name.replace('.md', '')}",
            f"title: {title}",
            "type: brain-artifact",
            f"status: {status}",
            "---",
            "",
            f"# {title}",
            "",
            f"Task: `{ctx.task.task_id}`",
            "",
            f"Return code: `{completed.returncode}`",
            "",
            "## Command",
            "",
            "```powershell",
            " ".join(cmd),
            "```",
            "",
            "## Stdout",
            "",
            "```text",
            completed.stdout.strip(),
            "```",
            "",
            "## Stderr",
            "",
            "```text",
            completed.stderr.strip(),
            "```",
            "",
        ]
    )
    artifact = _write_text_artifact(ctx, artifact_name, report)
    summary = f"{title} complete" if status == "done" else f"{title} failed"
    return ExecutionResult(status=status, text=f"{summary}: {artifact}", artifact_path=artifact)


def _suggest_facts_auto(ctx: ExecutorContext) -> ExecutionResult:
    """Nightly suggest-facts in AUTO mode: writes facts with confidence >= 0.95,
    skipping near-duplicates of existing memory (Jaccard >= 0.6). Unlike the
    dry-run review executor, this persists high-confidence facts to .memory/."""
    python = _pytest_python(_PROJECT_ROOT)
    cmd = [
        str(python), "-m", "atlas_plus.curation.suggest_facts",
        "--since", "today", "--source", "claude-code",
        "--auto", "--skip-similar", "0.6",
    ]
    # LLM extraction over the day's transcript can run long on the local model.
    # 600 -> 1200 (2026-07-30): rc124 timeouts 5 of 14 nights -- the per-candidate
    # local-LLM review exceeds 600s when Ollama is cold. Runs at 03:10 with no
    # contention; same remedy as the reflexion 900s raise (2026-07-18).
    return _run_reporting_subprocess(ctx, cmd, "suggest-facts-auto.md", "Suggest Facts (auto)", timeout=1200)


def _dream(ctx: ExecutorContext) -> ExecutionResult:
    """Nightly meta-cognitive dream over the .memory/ corpus. With ATLAS_AUTONOMOUS
    off it writes to .atlas/ (no .memory/ writes); with it on, to sessions/."""
    python = _pytest_python(_PROJECT_ROOT)
    cmd = [str(python), "-m", "atlas_plus.curation.dream_mode"]
    return _run_reporting_subprocess(ctx, cmd, "dream.md", "Dream")


def _reflexion_loop(ctx: ExecutorContext) -> ExecutionResult:
    """Weekly reflexion over facts ingested in the last 7 days. With ATLAS_AUTONOMOUS
    off, every REVISE is downgraded to a review ticket (no temporal-graph writes)."""
    python = _pytest_python(_PROJECT_ROOT)
    cmd = [str(python), "-m", "atlas_plus.curation.reflexion_loop", "--days", "7"]
    # Reflexion critiques a week of ingested facts in batches of 5 LLM calls on the
    # local model; 300s was never enough (it timed out every run). Give it room.
    return _run_reporting_subprocess(ctx, cmd, "reflexion-loop.md", "Reflexion Loop", timeout=900)


def _agent(ctx: ExecutorContext) -> ExecutionResult:
    artifact = brain_context.write_context_artifact(
        ctx.task,
        artifact_root=ctx.artifact_root,
    )
    agent = agent_registry.route(ctx.task.instruction, ctx.task.agent)
    result_text = agent_registry.dispatch(agent, ctx.task.instruction, ctx.task.session_id)
    status = task_store.classify_result(result_text)
    return ExecutionResult(status=status, text=result_text, artifact_path=artifact)


def _research_runner(ctx: ExecutorContext) -> ExecutionResult:
    """Execute a research job via the research_runner.py pipeline.

    Supports two modes:
      - instruction = "run-one RES-XXX" -- run a specific job
      - instruction = "run --batch N --priority P3" -- run a batch of P3 jobs

    Falls back to a status check if no instruction is provided.
    """
    runner_path = atlas_paths.workspace_root() / "research" / "research_runner.py"
    if not runner_path.exists():
        text = f"research_runner.py not found at {runner_path}"
        artifact = _write_text_artifact(ctx, "research-runner.md", text)
        return ExecutionResult(status="blocked", text=text, artifact_path=artifact)

    # Parse instruction into runner args
    instruction = ctx.task.instruction.strip()
    if instruction.startswith("run"):
        runner_args = instruction.split()
    elif instruction.startswith("RES-"):
        runner_args = ["run-one", instruction]
    else:
        runner_args = ["status"]

    python = _pytest_python(_PROJECT_ROOT)
    cmd = [str(python), str(runner_path)] + runner_args

    completed = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=_PYTEST_TIMEOUT_SECONDS,
        encoding="utf-8",
        errors="replace",
    )

    if completed.returncode == 0 and not completed.stdout.strip():
        # Script exited 0 but produced no output -- likely a no-op batch run or silent error.
        status = "blocked"
    else:
        status = "done" if completed.returncode == 0 else "failed"
    summary = f"Research runner ({' '.join(runner_args)}): {'ok' if status == 'done' else status}"

    report = "\n".join(
        [
            "---",
            f"id: {ctx.task.task_id}-research-runner",
            "title: Research Runner Execution",
            "type: brain-artifact",
            f"status: {status}",
            "---",
            "",
            "# Research Runner Execution",
            "",
            f"Task: `{ctx.task.task_id}`",
            f"Args: `{' '.join(runner_args)}`",
            "",
            f"Return code: `{completed.returncode}`",
            "",
            "## Stdout",
            "",
            "```text",
            completed.stdout.strip(),
            "```",
            "",
            "## Stderr",
            "",
            "```text",
            completed.stderr.strip(),
            "```",
            "",
        ]
    )
    artifact = _write_text_artifact(ctx, "research-runner.md", report)
    return ExecutionResult(status=status, text=f"{summary}: {artifact}", artifact_path=artifact)


def _research_runner_api(ctx: ExecutorContext) -> ExecutionResult:
    """Execute research jobs via Anthropic API (Sonnet 4 + optional Opus review).

    Supports:
      - instruction = "run --batch 30 --tier P2 --max-spend 15.00"
      - instruction = "run-one RES-206 --tier P2"
      - instruction = "status"
      - instruction = "cost-report"
    """
    runner_path = atlas_paths.workspace_root() / "research" / "research_runner_api.py"
    if not runner_path.exists():
        text = f"research_runner_api.py not found at {runner_path}"
        artifact = _write_text_artifact(ctx, "research-runner-api.md", text)
        return ExecutionResult(status="blocked", text=text, artifact_path=artifact)

    instruction = ctx.task.instruction.strip()
    if instruction.startswith(("run", "status", "cost")):
        runner_args = instruction.split()
    else:
        runner_args = ["status"]

    python = _pytest_python(_PROJECT_ROOT)
    cmd = [str(python), str(runner_path)] + runner_args

    # API runs can take a while (30+ reports x 60s each)
    api_timeout = max(_PYTEST_TIMEOUT_SECONDS, 7200)

    completed = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=api_timeout,
        encoding="utf-8",
        errors="replace",
    )

    if completed.returncode == 0 and not completed.stdout.strip():
        status = "blocked"
    else:
        status = "done" if completed.returncode == 0 else "failed"
    summary = f"Research runner API ({' '.join(runner_args)}): {'ok' if status == 'done' else status}"

    report = "\n".join(
        [
            "---",
            f"id: {ctx.task.task_id}-research-runner-api",
            "title: Research Runner API Execution",
            "type: brain-artifact",
            f"status: {status}",
            "---",
            "",
            "# Research Runner API Execution",
            "",
            f"Task: `{ctx.task.task_id}`",
            f"Args: `{' '.join(runner_args)}`",
            "",
            f"Return code: `{completed.returncode}`",
            "",
            "## Stdout",
            "",
            "```text",
            completed.stdout.strip(),
            "```",
            "",
            "## Stderr",
            "",
            "```text",
            completed.stderr.strip(),
            "```",
            "",
        ]
    )
    artifact = _write_text_artifact(ctx, "research-runner-api.md", report)
    return ExecutionResult(status=status, text=f"{summary}: {artifact}", artifact_path=artifact)


def _narrative_weekly(ctx: ExecutorContext) -> ExecutionResult:
    """Build cross-session narrative report and write to .memory/sessions/."""
    try:
        from atlas_plus.session_narrative import run_narrative
        result = run_narrative(since_days=14)
    except Exception as exc:
        return ExecutionResult(status="failed", text=f"Narrative build failed: {exc}")
    if not result.get("ok"):
        reason = result.get("reason", "unknown")
        return ExecutionResult(status="done", text=f"No narrative written: {reason}")
    summary = (
        f"Narrative written: {result['week']} | "
        f"sessions={result['sessions']} energy={result['energy']} "
        f"open_loops={result['open_loops']} direction={result['direction_signals']}"
    )
    artifact = _write_text_artifact(ctx, "narrative.md", summary)
    return ExecutionResult(status="done", text=summary, artifact_path=artifact)


def _memory_agent(ctx: ExecutorContext) -> ExecutionResult:
    """Run weekly memory maintenance scan (stale projects, decay, decisions)."""
    try:
        from atlas_plus.memory_agent import run_weekly_agent
        report = run_weekly_agent(create_tickets=True, push_ntfy=True, run_narrative=False)
    except Exception as exc:
        return ExecutionResult(status="failed", text=f"Memory agent failed: {exc}")
    summary = (
        f"Memory agent: stale_projects={len(report.stale_projects)} "
        f"decayed={len(report.decayed_facts)} "
        f"decisions_to_revisit={len(report.decisions_to_revisit)} "
        f"tickets_created={len(report.tickets_created)}"
    )
    artifact = _write_text_artifact(ctx, "memory-agent.md", summary)
    return ExecutionResult(status="done", text=summary, artifact_path=artifact)


def _patterns_rebuild(ctx: ExecutorContext) -> ExecutionResult:
    """Rebuild session co-occurrence patterns model."""
    try:
        from atlas_plus.session_patterns import save_patterns
        result = save_patterns(since_days=120)
    except Exception as exc:
        return ExecutionResult(status="failed", text=f"Patterns rebuild failed: {exc}")
    projects = len(result.get("project_count", {}))
    # project_count maps project -> session count; max value = total unique sessions with that project
    max_sessions = max(result.get("project_count", {}).values(), default=0)
    summary = f"Patterns rebuilt: {projects} projects tracked, up to {max_sessions} sessions analysed"
    artifact = _write_text_artifact(ctx, "patterns.md", summary)
    return ExecutionResult(status="done", text=summary, artifact_path=artifact)


def _decay_report(ctx: ExecutorContext) -> ExecutionResult:
    """Run memory half-life decay check and write artifact.

    Only flags files with an explicit `half_life:` frontmatter field (threshold_days=0),
    so results are always intentional -- no false positives from implicit defaults.
    """
    try:
        from atlas_plus.curation.decay import find_decayed_facts
        candidates = find_decayed_facts(threshold_days=0)
    except Exception as exc:
        return ExecutionResult(status="failed", text=f"Decay report failed: {exc}")

    summary_lines = [
        "---",
        f"id: {ctx.task.task_id}-decay-report",
        "title: Memory Decay Report",
        "type: brain-artifact",
        f"status: {'done' if candidates else 'done'}",
        "---",
        "",
        f"# Memory Decay Report",
        "",
        f"Files past their `half_life:` threshold: **{len(candidates)}**",
        "",
    ]
    if candidates:
        summary_lines += ["| File | Type | Half-life | Overdue |", "|------|------|-----------|---------|"]
        for c in candidates[:20]:
            summary_lines.append(f"| `{c.path}` | {c.memory_type} | {c.half_life_days}d | {c.days_overdue}d |")
        if len(candidates) > 20:
            summary_lines.append(f"| *(+{len(candidates) - 20} more)* | | | |")
        summary_lines += ["", "Run `atlas-review-memory decay` to review each file."]
    else:
        summary_lines.append("*No files past their half-life threshold.*")

    report = "\n".join(summary_lines)
    artifact = _write_text_artifact(ctx, "decay-report.md", report)
    text = (
        f"Decay report: {len(candidates)} file(s) overdue. "
        f"Artifact: {artifact}"
    )
    return ExecutionResult(status="done", text=text, artifact_path=artifact)


def _reverify_scan(ctx: ExecutorContext) -> ExecutionResult:
    """Decay-as-behavior: re-verify the memories the half-life scan flagged as stale/expired.

    Drains the decay reverify_queue, asks the model (provider-layer role 'brain', so
    LLM-agnostic) whether each fact still holds, and acts: still-valid -> reset the decay
    clock; outdated -> queued for quarantine; else -> human review. The clock-reset
    mutation is OFF by default (ATLAS_REVERIFY_RESET) so an autonomous pass never silently
    edits memory; the assessment + report always run. ATLAS_REVERIFY_LIMIT caps per-run model
    calls (default 10).
    """
    import os
    try:
        from atlas_plus.curation.decay import find_decayed_facts, reverify_queue
        from atlas_plus.curation.reverify import run_reverification
        queue = reverify_queue(find_decayed_facts(threshold_days=0))
    except Exception as exc:
        return ExecutionResult(status="failed", text=f"Re-verification scan failed: {exc}")

    autonomous = os.environ.get("ATLAS_REVERIFY_RESET", "off").lower() in ("on", "1", "true")
    try:
        limit = int(os.environ.get("ATLAS_REVERIFY_LIMIT", "10"))
    except ValueError:
        limit = 10

    if not queue:
        report = "# Decay re-verification run\n\nNo stale or expired memories to re-verify."
        artifact = _write_text_artifact(ctx, "reverify.md", report)
        return ExecutionResult(status="done", text="Re-verification: nothing queued.",
                               artifact_path=artifact)

    run = run_reverification(queue, autonomous=autonomous, limit=limit)
    artifact = _write_text_artifact(ctx, "reverify.md", run.summary_md())
    text = (
        f"Re-verification: checked {run.checked}, reset {len(run.reset)}, "
        f"would-reset {len(run.would_reset)}, quarantine {len(run.quarantine)}, "
        f"human {len(run.human)}. Artifact: {artifact}"
    )
    return ExecutionResult(status="done", text=text, artifact_path=artifact)


def _claude_supervisor(ctx: ExecutorContext) -> ExecutionResult:
    """Weekly supervisor pass: Claude Code reviews failed/blocked tasks and low-quality results.

    Builds a structured context pack from the past 7 days of Brain activity and
    dispatches to claude-code with a supervisor prompt. Claude's analysis is written
    as a .atlas/reports/supervisor-YYYY-MM-DD.md artifact.

    This is Phase 4 of the Claude escalation layer -- the weekly supervisory review
    that catches systemic issues the per-task escalation might miss.
    """
    from datetime import datetime, timezone, timedelta
    import json as _json

    now = datetime.now(timezone.utc)
    week_start = (now - timedelta(days=7)).isoformat()

    # Gather failed/blocked tasks from the last 7 days
    failed_tasks = task_store.list_tasks(status="failed", limit=50)
    blocked_tasks = task_store.list_tasks(status="blocked", limit=50)
    recent_failed = [t for t in failed_tasks if (t.finished_at or "") >= week_start]
    recent_blocked = [t for t in blocked_tasks if (t.finished_at or "") >= week_start]

    # Get pending facts awaiting review
    pending_count = 0
    try:
        from .task_facts import list_pending
        pending_count = len(list_pending())
    except Exception:
        pass

    # Build the supervisor prompt
    lines = [
        "You are acting as a supervisor reviewing the Atlas Brain AI system's activity.",
        "The Brain uses local Qwen3 models for most tasks. You are the senior reviewer.",
        "",
        f"Review period: last 7 days (since {week_start[:10]})",
        "",
        f"FAILED TASKS ({len(recent_failed)}):",
    ]
    for t in recent_failed[:15]:
        lines.append(f"  - [{t.agent or 'auto'}] {t.instruction[:80]}")
        if t.error:
            lines.append(f"    Error: {t.error[:120]}")
    if len(recent_failed) > 15:
        lines.append(f"  (+{len(recent_failed) - 15} more)")

    lines += ["", f"BLOCKED TASKS ({len(recent_blocked)}):"]
    for t in recent_blocked[:10]:
        lines.append(f"  - [{t.agent or 'auto'}] {t.instruction[:80]}")
    if len(recent_blocked) > 10:
        lines.append(f"  (+{len(recent_blocked) - 10} more)")

    lines += [
        "",
        f"PENDING FACTS AWAITING REVIEW: {pending_count}",
        "",
        "Please provide:",
        "1. PATTERNS: What recurring failure patterns do you see? (2-4 bullets)",
        "2. ROOT CAUSES: What are the likely root causes? (local model capability limits, ambiguous instructions, missing context?)",
        "3. ACTIONS: Specific recommended actions (improve prompts, add memory, rotate a model, etc.)",
        "4. FACT REVIEW NOTE: Is the pending fact count concerning? Recommend action if >10.",
        "5. HEALTH RATING: Rate the Brain system health this week: Excellent / Good / Fair / Poor",
        "",
        "Be specific and actionable. Reference task instructions by name where relevant.",
    ]

    supervisor_prompt = "\n".join(lines)

    # Dispatch to Claude Code
    try:
        from .agents import route as agent_route, dispatch as agent_dispatch
        agent = agent_route(supervisor_prompt, "claude-code")
        result_text = agent_dispatch(agent, supervisor_prompt, ctx.task.session_id)
    except Exception as exc:
        return ExecutionResult(status="failed", text=f"Supervisor dispatch failed: {exc}")

    # Write report artifact
    from datetime import date
    today = date.today().isoformat()
    report_lines = [
        "---",
        f"id: supervisor-{today}",
        f"title: Weekly Claude Supervisor Report -- {today}",
        "type: brain-artifact",
        "status: done",
        f"updated: {today}",
        "---",
        "",
        f"# Weekly Claude Supervisor Report: {today}",
        "",
        f"Generated by: claude-code  |  Task: {ctx.task.task_id}",
        f"Failed tasks reviewed: {len(recent_failed)}  |  Blocked: {len(recent_blocked)}  |  Pending facts: {pending_count}",
        "",
        "---",
        "",
        result_text,
        "",
    ]
    report = "\n".join(report_lines)

    # Write to .atlas/reports/ as well
    try:
        import os as _os
        from pathlib import Path as _Path
        reports_dir = atlas_paths.state_dir() / "reports"
        reports_dir.mkdir(parents=True, exist_ok=True)
        (reports_dir / f"supervisor-{today}.md").write_text(report, encoding="utf-8")
    except Exception:
        pass

    artifact = _write_text_artifact(ctx, f"supervisor-{today}.md", report)
    text = (
        f"Supervisor report complete. "
        f"Reviewed {len(recent_failed)} failed + {len(recent_blocked)} blocked tasks. "
        f"Artifact: {artifact}"
    )
    return ExecutionResult(status="done", text=text, artifact_path=artifact)


def _ticket_sync(ctx: ExecutorContext) -> ExecutionResult:
    """Scan recent git commits and auto-close tickets referenced in commit messages.

    Reads the last ATLAS_TICKET_SYNC_DAYS (default 30) days of git log from each
    repo in ATLAS_TICKET_SYNC_REPOS (semicolon-separated paths; defaults to workspace
    + atlas-memory+).

    Auto-close logic:
    - ``closes <ID>`` or ``fixes <ID>`` anywhere in the commit message -> done
    - commit title starts with feat:/fix:/docs:/refactor:/test: AND contains <ID> -> done

    Any ticket ID mentioned without a close/fix signal is logged as "referenced".
    Skips tickets already done or archived.
    """
    import os
    import re
    from datetime import date

    try:
        from atlas_plus.tickets.store import get_ticket, update_ticket_status
    except Exception as exc:
        return ExecutionResult(status="failed", text=f"ticket_sync: import failed: {exc}")

    days = int(os.environ.get("ATLAS_TICKET_SYNC_DAYS", "30"))
    default_repos = f"{atlas_paths.workspace_root()};{atlas_paths.projects_root() / 'atlas-memory+'}"
    repos_raw = os.environ.get("ATLAS_TICKET_SYNC_REPOS", default_repos)
    # ";"-separated. Do NOT split on ":" -- it shreds Windows drive letters
    # (the old replace(":", ";") turned C:\Tools into ["C", "\Tools"]).
    repo_paths = [p.strip() for p in repos_raw.split(";") if p.strip()]

    _ID_RE = re.compile(r"\b([A-Z]{2,8}-\d{1,4})\b")
    _CLOSE_RE = re.compile(r"\b(?:closes?|fixes?)\s+([A-Z]{2,8}-\d{1,4})\b", re.IGNORECASE)
    _SHIP_PREFIX = re.compile(r"^(?:feat|fix|docs|refactor|test|chore)(?:\([^)]+\))?:", re.IGNORECASE)

    closed: list[str] = []
    referenced: list[str] = []
    errors: list[str] = []
    seen_closed: set[str] = set()
    seen_ref: set[str] = set()

    for repo in repo_paths:
        try:
            from atlas_plus import gitsafe
            result = gitsafe.run(
                ["log", f"--since={days} days ago", "--pretty=format:%s %b"], repo, timeout=15,
            )
            if result.returncode != 0:
                errors.append(f"{repo}: git log failed")
                continue
        except Exception as exc:
            errors.append(f"{repo}: {exc}")
            continue

        for line in result.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            close_ids = {m.group(1).upper() for m in _CLOSE_RE.finditer(line)}
            ship_ids: set[str] = set()
            if _SHIP_PREFIX.match(line):
                ship_ids = {m.group(1).upper() for m in _ID_RE.finditer(line)}
            all_ids = {m.group(1).upper() for m in _ID_RE.finditer(line)}

            for tid in close_ids | ship_ids:
                if tid in seen_closed:
                    continue
                ticket = get_ticket(tid)
                if ticket is None or ticket.status in ("done", "archived"):
                    continue
                try:
                    update_ticket_status(tid, "done")
                    closed.append(tid)
                    seen_closed.add(tid)
                except Exception as exc:
                    errors.append(f"{tid}: {exc}")

            for tid in all_ids - close_ids - ship_ids:
                if tid not in seen_closed and tid not in seen_ref and get_ticket(tid) is not None:
                    referenced.append(tid)
                    seen_ref.add(tid)

    today = date.today().isoformat()
    report_lines = [
        "---",
        f"id: {ctx.task.task_id}-ticket-sync",
        "title: Ticket Sync Report",
        "type: brain-artifact",
        "status: done",
        f"updated: '{today}'",
        "---",
        "",
        "# Ticket Sync Report",
        "",
        f"Repos scanned: {len(repo_paths)} | Days back: {days}",
        "",
        f"## Auto-closed ({len(closed)})",
        "",
    ]
    report_lines += ([f"- {t}" for t in sorted(closed)] if closed else ["*None*"])
    report_lines += [
        "",
        f"## Referenced in commits (not yet closed) ({len(referenced)})",
        "",
    ]
    report_lines += ([f"- {t}" for t in sorted(set(referenced))] if referenced else ["*None*"])
    if errors:
        report_lines += ["", "## Errors", ""] + [f"- {e}" for e in errors]

    report = "\n".join(report_lines)
    artifact = _write_text_artifact(ctx, "ticket-sync.md", report)
    return ExecutionResult(
        status="done",
        text=(
            f"ticket_sync: closed {len(closed)}, "
            f"referenced {len(set(referenced))}, errors {len(errors)}"
        ),
        artifact_path=artifact,
    )


def _ticket_health_check(ctx: ExecutorContext) -> ExecutionResult:
    """Scan all open tickets for staleness and auto-reset orphaned in-progress tickets.

    Staleness thresholds (configurable via env vars):
    - ATLAS_TICKET_STALE_IP_DAYS (default 7): in-progress tickets not updated within
      this many days are flagged as stale.
    - ATLAS_TICKET_STALE_BL_DAYS (default 3): blocked tickets not updated within
      this many days are flagged as needing attention.
    - ATLAS_TICKET_ORPHAN_DAYS (default 14): in-progress tickets with non-human
      assignees not updated for this long are automatically reset to 'todo' so the
      dispatcher re-picks them up.

    Writes a markdown report artifact summarising all findings.
    """
    import os
    from datetime import date

    try:
        from atlas_plus.tickets.store import list_tickets, update_ticket_status
    except Exception as exc:
        return ExecutionResult(status="failed", text=f"ticket_health_check: import failed: {exc}")

    stale_ip_days = int(os.environ.get("ATLAS_TICKET_STALE_IP_DAYS", "7"))
    stale_bl_days = int(os.environ.get("ATLAS_TICKET_STALE_BL_DAYS", "3"))
    orphan_days = int(os.environ.get("ATLAS_TICKET_ORPHAN_DAYS", "14"))

    today = date.today()

    def _age(t) -> int:
        try:
            return (today - date.fromisoformat(t.updated)).days
        except Exception:
            return 0

    try:
        tickets = [t for t in list_tickets() if t.status in ("todo", "in-progress", "blocked")]
    except Exception as exc:
        return ExecutionResult(status="failed", text=f"ticket_health_check: list_tickets failed: {exc}")

    ages = {t.id: _age(t) for t in tickets}

    stale_ip = [(t, ages[t.id]) for t in tickets if t.status == "in-progress" and ages[t.id] > stale_ip_days]
    stale_bl = [(t, ages[t.id]) for t in tickets if t.status == "blocked" and ages[t.id] > stale_bl_days]
    # Auto-reset candidates: in-progress, non-human, past orphan threshold
    orphans = [(t, ages[t.id]) for t in tickets
               if t.status == "in-progress" and t.assignee != "human" and ages[t.id] > orphan_days]

    reset_ids: list[str] = []
    reset_errors: list[str] = []
    for t, d in orphans:
        try:
            update_ticket_status(t.id, "todo")
            reset_ids.append(f"{t.id} ({d}d)")
        except Exception as exc:
            reset_errors.append(f"{t.id}: {exc}")

    # Build report
    report_lines = [
        f"# Ticket Health Check -- {today.isoformat()}",
        "",
        f"Open tickets scanned: {len(tickets)} "
        f"({sum(1 for t in tickets if t.status == 'in-progress')} in-progress, "
        f"{sum(1 for t in tickets if t.status == 'blocked')} blocked, "
        f"{sum(1 for t in tickets if t.status == 'todo')} todo)",
        "",
    ]

    if stale_ip:
        report_lines += [f"## Stale in-progress (>{stale_ip_days}d)", ""]
        for t, d in sorted(stale_ip, key=lambda x: x[1], reverse=True):
            report_lines.append(f"- `{t.id}` {t.title} [{d}d, {t.assignee}, {t.priority}]")
        report_lines.append("")

    if stale_bl:
        report_lines += [f"## Needs attention: blocked (>{stale_bl_days}d)", ""]
        for t, d in sorted(stale_bl, key=lambda x: x[1], reverse=True):
            report_lines.append(f"- `{t.id}` {t.title} [{d}d, {t.assignee}, {t.priority}]")
        report_lines.append("")

    if reset_ids:
        report_lines += [f"## Auto-reset to todo (>{orphan_days}d orphans)", ""]
        for rid in reset_ids:
            report_lines.append(f"- {rid}")
        report_lines.append("")

    if reset_errors:
        report_lines += ["## Reset errors", ""]
        for err in reset_errors:
            report_lines.append(f"- {err}")
        report_lines.append("")

    if not stale_ip and not stale_bl and not orphans:
        report_lines.append("All tickets healthy -- no stale or orphaned work found.")

    report = "\n".join(report_lines)
    artifact = _write_text_artifact(ctx, "ticket-health.md", report)

    summary = (
        f"ticket_health_check: {len(stale_ip)} stale in-progress, "
        f"{len(stale_bl)} blocked tickets need attention, "
        f"{len(reset_ids)} orphaned tickets auto-reset to todo"
    )
    return ExecutionResult(status="done", text=summary, artifact_path=artifact)


def _research_gap(ctx: ExecutorContext) -> ExecutionResult:
    """Run a research investigation for an auto-filed gap ticket.

    Extracts the topic from the task instruction ('Research gap: <topic>').
    Calls run_research() to produce a .atlas/reports/ entry, then marks
    the linked ticket done via session_id='ticket-<ID>' convention.

    Returns done with the report path, or failed with an error explanation.
    Deduplication: if a similar recent report already exists, marks the
    ticket done without re-running research (gap already closed).
    """
    instruction = ctx.task.instruction or ""
    # Extract topic from "Research gap: <topic>" instruction format
    prefix = "Research gap: "
    if instruction.startswith(prefix):
        topic = instruction[len(prefix):].strip()
    else:
        # Fall back to the full instruction as topic
        topic = instruction.strip()

    if not topic:
        return ExecutionResult(
            status="failed",
            text="research_gap executor: could not extract topic from instruction",
        )

    session_id = ctx.task.session_id or f"research-gap-{ctx.task.task_id[:8]}"

    try:
        from atlas_plus.brain.research import run_research
        _body, report_path = run_research(topic=topic, session_id=session_id)

        # Read quality_score from the report frontmatter so the result text
        # and ntfy notification carry the score without re-running scoring.
        quality_score: float | None = None
        try:
            import yaml as _yaml
            raw = Path(report_path).read_text(encoding="utf-8", errors="replace")
            if raw.startswith("---"):
                end = raw.find("\n---", 3)
                if end != -1:
                    fm = _yaml.safe_load(raw[3:end]) or {}
                    qs = fm.get("quality_score")
                    if qs is not None:
                        quality_score = float(qs)
        except Exception:
            pass

        score_suffix = f" (quality: {quality_score:.2f})" if quality_score is not None else ""
        text = f"Research complete for '{topic}': {report_path}{score_suffix}"
        artifact = _write_text_artifact(ctx, "research-gap.md",
                                        f"# Research Gap Report\n\nTopic: {topic}\nReport: {report_path}\nQuality: {quality_score}\n")
        return ExecutionResult(status="done", text=text, artifact_path=artifact)

    except ValueError as exc:
        # ValueError = similar report already exists (deduplication hit)
        text = f"Gap already covered (dedup): {exc}"
        return ExecutionResult(status="done", text=text)

    except Exception as exc:
        return ExecutionResult(
            status="failed",
            text=f"Research failed for '{topic}': {exc}",
        )


def _verify(ctx: ExecutorContext) -> ExecutionResult:
    """Run the project test suite to verify a completed ticket task.

    Triggered automatically when ATLAS_VERIFY_TICKETS=on and an agent task
    with a ticket-{ID} session_id completes with status=done.  The verify
    task session_id is "verify-ticket-{ID}" so _try_close_ticket() ignores it;
    worker._finish_task() handles the verify-failure -> ticket-reopen path.

    Runs pytest --tb=short -q on the full test suite (not just the brain subset)
    so regressions in any module are caught before the ticket is marked verified.
    """
    python = _pytest_python(_PROJECT_ROOT)
    cmd = [str(python), "-m", "pytest", "--tb=short", "-q", "-p", "no:cacheprovider"]
    completed = subprocess.run(
        cmd,
        cwd=str(_PROJECT_ROOT),
        capture_output=True,
        text=True,
        timeout=_PYTEST_TIMEOUT_SECONDS,
        encoding="utf-8",
        errors="replace",
        env=_clean_pytest_env(),
    )
    status = "done" if completed.returncode == 0 else "failed"
    summary = "verify passed" if completed.returncode == 0 else "verify failed"

    # Extract N passed / N failed line from pytest output
    last_line = ""
    for line in reversed(completed.stdout.splitlines()):
        stripped = line.strip()
        if stripped and ("passed" in stripped or "failed" in stripped or "error" in stripped):
            last_line = stripped
            break

    report = "\n".join([
        "---",
        f"id: {ctx.task.task_id}-verify",
        "title: Ticket Verify Run",
        "type: brain-artifact",
        f"status: {status}",
        "---",
        "",
        "# Ticket Verify Run",
        "",
        f"Task: `{ctx.task.task_id}`",
        f"Session: `{ctx.task.session_id or '(none)'}`",
        "",
        "## Pytest result",
        "",
        f"```\n{last_line or completed.stdout.strip()[-500:]}\n```",
        "",
        "## Stdout",
        "",
        "```text",
        completed.stdout.strip()[-2000:],
        "```",
    ])
    artifact = _write_text_artifact(ctx, "verify.md", report)
    result_text = f"{summary}: {last_line}" if last_line else summary
    return ExecutionResult(status=status, text=result_text, artifact_path=artifact)


_CLAUDE_TASK_TIMEOUT = 600  # seconds -- `claude --print` sessions are short-lived by design


def _claude_task(ctx: ExecutorContext) -> ExecutionResult:
    """Run a headless Claude Code session via `claude --print`.

    Instruction format (two supported forms):

    JSON (recommended):
        {"prompt": "...", "working_dir": "C:/path/to/project", "max_turns": 20}

    Plain text:
        Any string -- treated as the prompt; working_dir defaults to _PROJECT_ROOT.

    Each `claude --print` invocation starts fresh, keeping token cost minimal.
    Silent failure on timeout or subprocess error -- always returns ExecutionResult.
    """
    import json as _json
    import shutil as _shutil

    claude_bin = _shutil.which("claude")
    if claude_bin is None:
        msg = "claude CLI not found in PATH -- install Claude Code CLI first"
        artifact = _write_text_artifact(ctx, "claude-task.md", msg)
        return ExecutionResult(status="blocked", text=msg, artifact_path=artifact)

    # Budget preflight: a headless claude session is the most token-expensive task kind, and the
    # Brain runs it unattended -- a mid-flight rate-limit kill leaves corrupt work. Opt-in via
    # ATLAS_BUDGET_PREFLIGHT (off [default] | warn | block); per-task gross estimate via
    # ATLAS_CLAUDE_TASK_ESTIMATE (default 1.2M). 'block' defers an insufficient task instead of
    # starting a doomed run; 'warn' just records the verdict in the artifact and proceeds. Default
    # off so the Brain's behavior only changes when the operator deliberately enables it.
    import os as _os
    bp_note = "n/a"
    bp_mode = _os.environ.get("ATLAS_BUDGET_PREFLIGHT", "off").lower()
    if bp_mode in ("warn", "block"):
        try:
            from atlas_plus.budget import preflight as _preflight
            _est = int(_os.environ.get("ATLAS_CLAUDE_TASK_ESTIMATE", "1200000"))
            _v = _preflight(_est)
            bp_note = f"{_v.status} (headroom {_v.headroom} / est {_est}): {_v.recommendation}"
            if bp_mode == "block" and _v.status == "insufficient":
                artifact = _write_text_artifact(
                    ctx, "claude-task.md",
                    "Deferred -- insufficient token budget to finish this safely.\n\n" + bp_note)
                return ExecutionResult(
                    status="blocked",
                    text=f"claude_task deferred (budget {_v.status}): {_v.recommendation}",
                    artifact_path=artifact)
        except Exception as _exc:  # never let the preflight itself block a task
            bp_note = f"preflight skipped: {_exc}"

    instruction = ctx.task.instruction.strip()
    working_dir = _PROJECT_ROOT
    max_turns = 20
    prompt = instruction

    try:
        parsed = _json.loads(instruction)
        if isinstance(parsed, dict):
            prompt = parsed.get("prompt", instruction)
            if "working_dir" in parsed:
                working_dir = Path(parsed["working_dir"])
            max_turns = int(parsed.get("max_turns", 20))
    except (_json.JSONDecodeError, ValueError):
        pass  # plain text prompt -- use defaults

    if not isinstance(prompt, str) or not prompt.strip():
        # `claude --print` with no prompt exits 1 ("Input must be provided
        # either through stdin or as a prompt argument"); fail fast with a
        # message that names the real problem instead of burning an attempt.
        # Non-string covers JSON like {"prompt": 123} -- there is nothing
        # sensible to send, so block rather than raise AttributeError.
        msg = "claude task rejected: empty instruction/prompt (or non-string prompt) -- nothing to send to `claude --print`"
        artifact = _write_text_artifact(ctx, "claude-task.md", msg)
        return ExecutionResult(status="blocked", text=msg, artifact_path=artifact)

    cmd = [
        claude_bin,
        "--print",
        f"--max-turns={max_turns}",
        prompt,
    ]

    try:
        completed = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=_CLAUDE_TASK_TIMEOUT,
            encoding="utf-8",
            errors="replace",
            cwd=str(working_dir),
        )
    except subprocess.TimeoutExpired:
        msg = f"claude --print timed out after {_CLAUDE_TASK_TIMEOUT}s"
        artifact = _write_text_artifact(ctx, "claude-task.md", msg)
        return ExecutionResult(status="failed", text=msg, artifact_path=artifact)
    except Exception as exc:  # noqa: BLE001
        msg = f"claude --print failed to launch: {exc}"
        artifact = _write_text_artifact(ctx, "claude-task.md", msg)
        return ExecutionResult(status="failed", text=msg, artifact_path=artifact)

    status = "done" if completed.returncode == 0 else "failed"
    output_text = (completed.stdout or "").strip()
    stderr_text = (completed.stderr or "").strip()
    summary_line = output_text.splitlines()[-1][:200] if output_text else "(no output)"

    report = "\n".join([
        "---",
        f"id: {ctx.task.task_id}-claude-task",
        "title: Claude Task Execution",
        "type: brain-artifact",
        f"status: {status}",
        "---",
        "",
        "# Claude Task Execution",
        "",
        f"Task: `{ctx.task.task_id}`",
        f"Working dir: `{working_dir}`",
        f"Max turns: `{max_turns}`",
        f"Budget preflight: {bp_note}",
        f"Return code: `{completed.returncode}`",
        "",
        "## Prompt",
        "",
        "```text",
        prompt[:500],
        "```",
        "",
        "## Output",
        "",
        "```text",
        output_text[-4000:] if len(output_text) > 4000 else output_text,
        "```",
        "",
        "## Stderr",
        "",
        "```text",
        stderr_text[-1000:] if stderr_text else "(none)",
        "```",
        "",
    ])
    artifact = _write_text_artifact(ctx, "claude-task.md", report)
    return ExecutionResult(status=status, text=f"claude-task {status}: {summary_line}", artifact_path=artifact)


def _inspire(ctx: ExecutorContext) -> ExecutionResult:
    """Inspiration Engine entry (task kind="inspire").

    Generate N divergent samples for the task instruction, score self-consistency,
    and persist the captured ideas as UNVERIFIED to a quarantine store under
    .atlas/brain/ideas/<task_id>/ -- deliberately NOT the .memory/ fact corpus, so
    temporal_build never ingests an unverified idea as a fact. Promotion to a real
    memory fact happens later through a verifier (confirm / contradiction-free check).
    """
    import os
    from . import inspiration
    from atlas_plus.llm import get_provider

    prompt = re.sub(r"^\s*/?inspire\s*[:\-]?\s*", "", (ctx.task.instruction or "").strip(),
                    flags=re.IGNORECASE)
    if not prompt:
        return ExecutionResult(status="failed", text="inspire: empty prompt")

    n = int(os.environ.get("ATLAS_INSPIRE_SAMPLES", "6"))
    temperature = float(os.environ.get("ATLAS_INSPIRE_TEMPERATURE", "1.0"))

    def _gen(p: str) -> str:
        # Routed through the unified provider layer: the Inspiration Engine runs on any
        # model (Ollama/Anthropic/OpenAI-compatible) via ATLAS_MODEL_INSPIRE_PROVIDER.
        return get_provider("inspire").generate(p, temperature=temperature)

    try:
        run = inspiration.inspire(_gen, prompt, n=n, source=f"brain/inspire/{ctx.task.task_id}")
    except Exception as exc:  # noqa: BLE001
        return ExecutionResult(status="failed", text=f"inspire: generation failed: {exc}")

    state_dir = atlas_paths.state_dir()
    ideas_dir = state_dir / "brain" / "ideas" / ctx.task.task_id
    _check_gate("write", str(ideas_dir), context={"kind": "inspire", "task_id": ctx.task.task_id})
    inspiration.persist_ideas(run.ideas, ideas_dir, created=task_store._now())

    report = inspiration.run_to_markdown(run, run_id=ctx.task.task_id, created=task_store._now())
    artifact = _write_text_artifact(ctx, "inspire.md", report)

    r = run.result
    text = (
        f"inspire: {r.verdict} (top {r.top_fraction}, entropy {r.entropy}); "
        f"{len(run.ideas)} unverified idea(s) from {r.n} samples. Quarantine: {ideas_dir}"
    )
    return ExecutionResult(status="done", text=text, artifact_path=artifact)


def _triage(ctx: ExecutorContext) -> ExecutionResult:
    """Classify a work item ROUTINE vs HARD via the local model, then enqueue a follow-up.

    This is the hostless form of `route-iteration.ps1` (LOO-001): the triage runs as a Brain
    task itself, so the escalation router needs no external PowerShell host. Routine work routes
    to a free local `agent` task; hard work escalates to a `claude_task`. An ambiguous reply
    escalates to HARD -- a wasted local attempt costs more than a few Claude tokens.

    The follow-up is enqueued in the SAME db (`ctx.db_path`) the worker drains, so the next
    poll picks it up. The classifier reply is `<think>`-stripped (qwen3) before token matching.
    """
    from atlas_plus.llm import get_provider

    instruction = ctx.task.instruction.strip()
    prompt = (
        "You are a task router. Classify the work item below as exactly one word: "
        "ROUTINE or HARD.\n"
        "ROUTINE = high-volume, low-judgment, mechanical work (indexing, triage, scaffolding, "
        "doc stubs, gap-finding, data transforms) that a small local model can do.\n"
        "HARD = needs real judgment (novel design, subtle debugging, cross-cutting refactors, "
        "correctness guarantees, deep reasoning).\n"
        "Reply with ONLY the single word ROUTINE or HARD.\n"
        f"Work item: {instruction} /no_think"
    )

    try:
        raw = get_provider("brain").generate(prompt, temperature=0.1, timeout=60.0)
    except Exception as exc:  # noqa: BLE001 -- any model/transport error fails the task cleanly
        msg = f"triage classification failed: {exc}"
        artifact = _write_text_artifact(ctx, "triage.md", msg)
        return ExecutionResult(status="failed", text=msg, artifact_path=artifact)

    cleaned = re.sub(r"(?s)<think>.*?</think>", "", raw).strip()
    match = re.search(r"\b(ROUTINE|HARD)\b", cleaned.upper()) or re.search(
        r"\b(ROUTINE|HARD)\b", raw.upper()
    )
    if match is not None:
        verdict = match.group(1)
        note = f"classified {verdict}"
    else:
        verdict = "HARD"  # tie-break: escalate rather than let the local model flub it
        note = "ambiguous -> defaulted HARD"

    follow_kind = "agent" if verdict == "ROUTINE" else "claude_task"
    follow_up = task_store.create_task(
        instruction=instruction,
        session_id=ctx.task.session_id,
        db_path=ctx.db_path,
        kind=follow_kind,
        priority=5,
    )

    report = "\n".join(
        [
            "---",
            f"id: {ctx.task.task_id}-triage",
            "title: Triage routing",
            "type: brain-artifact",
            "status: done",
            "---",
            "",
            "# Triage routing",
            "",
            f"Work item: {instruction[:300]}",
            f"Decision: {verdict} ({note})",
            f"Follow-up task: {follow_up.task_id} (kind={follow_kind}, priority=5)",
            "",
            "## Raw classifier reply",
            "",
            "```text",
            raw[:1000],
            "```",
            "",
        ]
    )
    artifact = _write_text_artifact(ctx, "triage.md", report)
    return ExecutionResult(
        status="done",
        text=f"triage: {verdict} -> {follow_kind} ({follow_up.task_id})",
        artifact_path=artifact,
    )


def _inbox_summarize(ctx: ExecutorContext) -> ExecutionResult:
    """ATL-026: summarize an inbox capture by ID via the local model; persist summary + tags.

    Instruction carries the item id, either as JSON {"item_id": "INB-0001"} or a bare id string.
    Delegates to inbox.summarize.summarize_item (which is autonomous-gated and self-contained).
    """
    import json

    from atlas_plus.inbox.summarize import summarize_item

    raw = (ctx.task.instruction or "").strip()
    item_id = ""
    try:
        item_id = str(json.loads(raw).get("item_id", "")).strip()
    except Exception:
        item_id = raw  # allow a bare item id as the instruction
    if not item_id:
        return ExecutionResult(status="failed", text="inbox_summarize: no item_id in instruction")
    try:
        it = summarize_item(item_id)
        return ExecutionResult(status="done", text=it.summary or "(summarized)")
    except FileNotFoundError:
        return ExecutionResult(status="failed", text=f"inbox_summarize: item {item_id} not found")
    except Exception as exc:  # noqa: BLE001
        return ExecutionResult(status="failed", text=f"inbox_summarize error: {exc}")


_EXECUTORS: dict[str, Executor] = {
    "noop": _noop,
    "inbox_summarize": _inbox_summarize,  # ATL-026: summarize a captured link/blurb locally
    "inspire": _inspire,
    "fail": _fail,
    "context_pack": _context_pack,
    "research_digest": _research_digest,
    "research_runner": _research_runner,
    "research_runner_api": _research_runner_api,
    "research_gap": _research_gap,
    "pytest": _pytest,
    "verify": _verify,
    "temporal_build": _temporal_build,
    "gaps_report": _gaps_report,
    "convergence_report": _convergence_report,
    "curation_report": _curation_report,
    "suggest_facts_review": _suggest_facts_review,
    "suggest_facts_auto": _suggest_facts_auto,
    "dream": _dream,
    "reflexion_loop": _reflexion_loop,
    "narrative_weekly": _narrative_weekly,
    "memory_agent": _memory_agent,
    "patterns_rebuild": _patterns_rebuild,
    "decay_report": _decay_report,
    "reverify": _reverify_scan,           # decay-as-behavior: re-verify stale memories + act
    "claude_supervisor": _claude_supervisor,
    "ticket_sync": _ticket_sync,          # auto-close tickets from git commits
    "ticket_health_check": _ticket_health_check,  # staleness audit + orphan reset
    "claude_task": _claude_task,          # headless claude --print session (zero loop context)
    "triage": _triage,                    # classify routine/hard -> enqueue agent/claude_task (LOO-001)
    "agent": _agent,
}

# Operator-machine executors (private integrations) register themselves when
# their module exists; the public build excludes it, so public installs have
# exactly the registry above (stranger-test batch 9).
try:
    from atlas_plus.brain.executors_local import LOCAL_EXECUTORS as _LOCAL_EXECUTORS
    _EXECUTORS.update(_LOCAL_EXECUTORS)
except ImportError:
    pass


def run_executor(task: Task, ctx: ExecutorContext) -> ExecutionResult:
    """Dispatch a task to the correct executor by kind, falling back to _agent.

    Looks up task.kind in _EXECUTORS; any unknown kind runs as a plain agent task.
    Always returns ExecutionResult -- never raises. Callers (worker._finish_task)
    persist the result and handle escalation/verify queueing.

    Claude auto-promote:
    When task.kind is 'agent' and the instruction matches _CLAUDE_STRENGTH_PATTERNS,
    this function promotes the effective dispatcher to claude_task:
      - ATLAS_CLAUDE_AUTO_PROMOTE=on  : silent promotion, claude_task runs automatically
      - env var absent/off            : local model runs, suggestion prepended to result text

    Explicit kinds (verify, pytest, ticket_sync, etc.) bypass the strength check.
    """
    import os as _os

    effective_kind = task.kind
    matched_group: str | None = None

    if task.kind == "agent":
        matched_group = _detect_claude_strength(task.instruction)
        if matched_group and _os.environ.get("ATLAS_CLAUDE_AUTO_PROMOTE", "").lower() == "on":
            effective_kind = "claude_task"

    executor = _EXECUTORS.get(effective_kind, _agent)
    result = executor(ctx)

    # Surface suggestion when pattern matched but auto-promote was not active
    if matched_group and effective_kind == "agent":
        suggestion = (
            f"[claude-suggest pattern={matched_group!r}] "
            f"This instruction matches Claude's strengths (code/SIP/synthesis). "
            f"Re-queue with kind=claude_task or set ATLAS_CLAUDE_AUTO_PROMOTE=on "
            f"to promote automatically."
        )
        return ExecutionResult(
            status=result.status,
            text=f"{suggestion}\n\n{result.text}",
            artifact_path=result.artifact_path,
        )

    return result
