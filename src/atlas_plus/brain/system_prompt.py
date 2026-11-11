"""Build the Atlas Brain system prompt from .memory/ and house rules.

The prompt is rebuilt on each conversation start so it reflects the current
state of the resolved identity file (see atlas_plus.identity) and active
project context.

Target length: < 600 tokens to leave room for conversation + tool results.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from atlas_plus.identity import resolve_identity_path, user_display_name
from atlas_plus import paths as atlas_paths

MEMORY_ROOT = atlas_paths.memory_root()

_HOUSE_RULES = [
    "Version before edit -- always copy files to versions/ before changing them.",
    "Archive never delete -- move stale items to archive/, never rm.",
    "LLM-agnostic output -- no em-dashes, no emojis, use ISO 8601 dates.",
    "Code delivery -- multiline Python goes to a .py file, not inline in shell.",
    "Session log at end -- append a dated entry to .memory/sessions/ when the session ends.",
]

_TOOL_GUIDANCE = """\
Tool use guidance:
- Search memory before guessing. Use memory_search_tool for keyword queries.
- Use temporal_search_tool for time-sensitive questions about project state.
- Prefer read tools before write tools. Never write without a clear user request.
- memory_remember_tool and tickets_create_tool require ATLAS_AUTONOMOUS=on.
- Keep responses concise. One or two paragraphs unless more detail is requested."""


def _read_stripped(path: Path, max_chars: int = 400) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
        if text.startswith("---"):
            end = text.find("---", 3)
            if end != -1:
                text = text[end + 3:].strip()
        # ASCII-safe
        text = "".join(c if ord(c) < 128 and c >= " " else " " for c in text)
        return text.strip()[:max_chars]
    except OSError:
        return ""


def build_system_prompt(memory_root: Optional[Path] = None) -> str:
    """Build the Atlas Brain system prompt with today's date, identity, and relevant memory context."""
    root = memory_root or MEMORY_ROOT
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    user = user_display_name(root)

    parts: list[str] = [
        f"You are Atlas, {user}'s personal AI assistant. Today is {today}.",
        "",
        f"You have access to {user}'s knowledge base (.memory/) and a suite of tools "
        "for reading memory, searching facts, managing tickets, and querying the "
        "temporal knowledge graph. You maintain memory across sessions.",
        "",
    ]

    # Identity
    identity_path = resolve_identity_path(root)
    identity = _read_stripped(identity_path, max_chars=350)
    if identity:
        parts += [f"## About {user}", identity, ""]

    # House rules
    parts += ["## House rules"]
    parts += [f"- {r}" for r in _HOUSE_RULES]
    parts.append("")

    # Tool guidance
    parts += [_TOOL_GUIDANCE, ""]

    return "\n".join(parts)
