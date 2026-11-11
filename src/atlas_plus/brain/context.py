"""Brain context packs -- bounded task-local context artifacts.

The worker stores context as files and passes short summaries between steps.
This keeps task execution durable without replaying whole chats into every
model call.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

from .tasks import Task
from atlas_plus import paths as atlas_paths

_WORKSPACE_ROOT = atlas_paths.workspace_root()
_DEFAULT_MEMORY_ROOT = Path(os.environ.get("ATLAS_MEMORY_ROOT", _WORKSPACE_ROOT / ".memory"))
_DEFAULT_RESEARCH_ROOT = Path(os.environ.get("ATLAS_RESEARCH_ROOT", _WORKSPACE_ROOT / "research"))
_DEFAULT_ARTIFACT_ROOT = Path(
    os.environ.get("ATLAS_BRAIN_ARTIFACT_DIR", _WORKSPACE_ROOT / ".atlas" / "brain" / "artifacts")
)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")


def _read_head(path: Path, max_chars: int) -> str:
    if not path.exists() or not path.is_file():
        return ""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return text[:max_chars].strip()


def _clip(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    suffix = "\n\n## Truncated\nContext pack clipped to fit the task budget."
    keep = max(0, max_chars - len(suffix))
    return text[:keep].rstrip() + suffix


def _gather_temporal_context(instruction: str, limit: int = 5) -> str:
    """Query the temporal graph for facts related to the instruction.

    Returns a compact text block (empty string if graph is empty or query fails).
    Kept under ~500 chars to stay within task context budget.
    """
    try:
        from atlas_plus.temporal_graph.file_provider import FileTemporalProvider
        provider = FileTemporalProvider()
        stats = provider.stats()
        if stats.get("facts_active", 0) == 0:
            return ""
        results = provider.search_current(instruction, limit=limit)
        if not results:
            return ""
        lines = []
        for r in results:
            fact = r.fact
            entity = r.entity
            subj = entity.name if entity else fact.subject_id
            lines.append(f"- [{fact.valid_from[:10]}] {subj}: {fact.fact_text}")
        return "\n".join(lines)
    except Exception:
        return ""


def build_context_pack(
    instruction: str,
    *,
    memory_root: Path = _DEFAULT_MEMORY_ROOT,
    research_root: Path = _DEFAULT_RESEARCH_ROOT,
    max_chars: int = 6000,
    include_temporal: bool = True,
) -> str:
    """Build a compact context packet for one Brain task.

    Includes memory index, Keel project memory, research queue, and
    (when include_temporal=True) top temporal graph facts for the instruction.
    """
    memory_index = _read_head(memory_root / "INDEX.md", 1800)
    atlas_project = _read_head(memory_root / "projects" / "atlas-memory-plus.md", 1800)
    research_plan = _read_head(research_root / "RESEARCH-PLAN.md", 1800)
    temporal_ctx = _gather_temporal_context(instruction) if include_temporal else ""

    parts = [
        "# Brain Task Context",
        "",
        "## Instruction",
        instruction.strip() or "(empty)",
    ]
    if temporal_ctx:
        parts.extend(["", "## Temporal Context (recent facts from knowledge graph)", temporal_ctx])
    if memory_index:
        parts.extend(["", "## Memory Hot Cache", memory_index])
    if atlas_project:
        parts.extend(["", "## Keel Project Memory", atlas_project])
    if research_plan:
        parts.extend(["", "## Research Queue", research_plan])

    return _clip("\n".join(parts), max_chars)


def write_context_artifact(
    task: Task,
    *,
    artifact_root: Path = _DEFAULT_ARTIFACT_ROOT,
    memory_root: Path = _DEFAULT_MEMORY_ROOT,
    research_root: Path = _DEFAULT_RESEARCH_ROOT,
    max_chars: int = 6000,
) -> Path:
    """Write a context pack artifact for a task and return its path."""
    target_dir = artifact_root / task.task_id
    target_dir.mkdir(parents=True, exist_ok=True)
    path = target_dir / "context.md"
    body = build_context_pack(
        task.instruction,
        memory_root=memory_root,
        research_root=research_root,
        max_chars=max_chars,
    )
    content = (
        "---\n"
        f"id: {task.task_id}-context\n"
        "title: Brain Task Context\n"
        "type: brain-artifact\n"
        f"status: {task.status}\n"
        f"updated: {_now()}\n"
        "---\n\n"
        f"Task: `{task.task_id}`\n\n"
        f"Kind: `{task.kind}`\n\n"
        f"Priority: `{task.priority}`\n\n"
        f"{body}\n"
    )
    path.write_text(content, encoding="utf-8")
    return path
