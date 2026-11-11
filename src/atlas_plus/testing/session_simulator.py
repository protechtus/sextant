"""Session simulator -- builds synthetic transcripts and runs the Atlas pipeline.

Used by integration tests to simulate a full session lifecycle without needing
a real LLM session. Patches load_turns so the pipeline sees synthetic turns,
then runs suggest_facts and captures session_start output.
"""

from __future__ import annotations

import io
import os
import sys
import textwrap
from pathlib import Path
from typing import Any
from unittest.mock import patch

from atlas_plus.transcripts.store import TurnRecord


def build_routing_yaml(memory_root: Path) -> None:
    """Write a minimal routing.yaml sufficient for APP tests."""
    atlas_dir = memory_root / "_atlas"
    atlas_dir.mkdir(exist_ok=True)
    routing = textwrap.dedent("""\
        routes:
          identity:
            target: "identity/user.md"
            create_if_missing: false
          preference:
            target: "feedback/{topic}.md"
            create_if_missing: true
            frontmatter_template:
              type: feedback
              status: active
              priority: normal
          project:
            target: "projects/{name}.md"
            create_if_missing: true
            frontmatter_template:
              type: project
              status: active
              priority: normal
          reference:
            target: "reference/{topic}.md"
            create_if_missing: true
            frontmatter_template:
              type: reference
              status: active
              priority: normal
        """)
    (atlas_dir / "routing.yaml").write_text(routing, encoding="utf-8")


def build_identity_file(memory_root: Path) -> None:
    """Write a minimal identity file so session_start does not error."""
    identity_dir = memory_root / "identity"
    identity_dir.mkdir(exist_ok=True)
    (identity_dir / "user.md").write_text(
        "---\nid: user\ntitle: Test User\ntype: identity\nupdated: 2026-05-11\n---\n"
        "Test User, Tier 3 Support Engineer at ExampleCorp.\n",
        encoding="utf-8",
    )


def setup_memory_root(tmp_path: Path) -> Path:
    """Create a minimal .memory/ structure for APP tests. Returns the memory root."""
    root = tmp_path / ".memory"
    for subdir in ("identity", "feedback", "projects", "reference", "_atlas"):
        (root / subdir).mkdir(parents=True, exist_ok=True)
    build_identity_file(root)
    build_routing_yaml(root)
    return root


def patch_memory_root(monkeypatch, memory_root: Path) -> None:
    """Patch all Atlas modules to use the given memory root."""
    monkeypatch.setenv("ATLAS_MEMORY_ROOT", str(memory_root))
    monkeypatch.setenv("ATLAS_AUTONOMOUS", "on")

    import importlib

    import atlas_plus.mcp.memory_server.config as cfg
    cfg.MEMORY_ROOT = memory_root

    import atlas_plus.mcp.memory_server.routing as routing_mod
    routing_mod.MEMORY_ROOT = memory_root
    routing_mod.ROUTING_CONFIG = memory_root / "_atlas" / "routing.yaml"

    import atlas_plus.mcp.memory_server.tools.read as read_mod
    read_mod.MEMORY_ROOT = memory_root

    import atlas_plus.mcp.memory_server.tools.list as list_mod
    list_mod.MEMORY_ROOT = memory_root

    import atlas_plus.mcp.memory_server.versioning as versioning_mod
    versioning_mod.MEMORY_ROOT = memory_root

    import atlas_plus.curation.suggest_facts as sf_mod
    sf_mod.MEMORY_ROOT = memory_root


def run_suggest_facts_with_turns(
    turns: list[TurnRecord],
    memory_root: Path,
    llm_response: str | None = None,
    auto: bool = True,
    dry_run: bool = False,
) -> dict:
    """Run suggest_facts pipeline against synthetic turns.

    If llm_response is provided, the LLM call is mocked with that JSON string.
    Otherwise, a real LLM call is made (requires Ollama or ANTHROPIC_API_KEY).

    In test contexts, any facts below the auto-approve threshold (0.85) that
    would normally prompt for input are silently skipped via EOFError injection.
    """
    import builtins
    from atlas_plus.curation.suggest_facts import suggest_facts

    def fake_load_transcript(since: str = "today", source: str = "claude-code") -> str:
        """Return the injected turns as a flat ROLE: content string for use in suggest_facts tests."""
        parts = []
        for t in turns:
            role = t.role.upper()
            content = t.content if isinstance(t.content, str) else str(t.content)
            parts.append(f"{role}: {content.strip()[:400]}")
        return "\n".join(parts)

    active_patches = [
        patch("atlas_plus.curation.suggest_facts._load_transcript_text", side_effect=fake_load_transcript),
        patch("atlas_plus.curation.suggest_facts.MEMORY_ROOT", memory_root),
        # Silence interactive prompt in tests -- EOFError is handled by _review_candidates
        patch.object(builtins, "input", side_effect=EOFError("non-interactive test")),
    ]

    if llm_response is not None:
        active_patches.append(
            patch("atlas_plus.summarizer.providers.complete", return_value=(llm_response, "mock"))
        )

    for p in active_patches:
        p.__enter__()
    try:
        result = suggest_facts(auto=auto, dry_run=dry_run, memory_root=memory_root)
    finally:
        for p in reversed(active_patches):
            p.__exit__(None, None, None)

    return result


def capture_session_start_output(memory_root: Path, project_dir: Path | None = None) -> str:
    """Run session_start.main() with the given memory root and capture stdout."""
    buf = io.BytesIO()

    import atlas_plus.hooks.session_start as ss_mod
    original_memory_root = ss_mod.MEMORY_ROOT

    ss_mod.MEMORY_ROOT = memory_root
    old_stdout_buffer = sys.stdout.buffer if hasattr(sys.stdout, "buffer") else None

    class FakeStdout:
        """Minimal bytes-mode stdout stub that redirects writes to an in-memory buffer."""

        def write(self, data: bytes) -> None:
            """Write bytes to the capture buffer."""
            buf.write(data)

        def flush(self) -> None:
            """No-op flush (buffer is in memory)."""
            pass

    sys.stdout = type("FakeOut", (), {
        "buffer": FakeStdout(),
        "write": lambda self, s: buf.write(s.encode("utf-8")),
        "flush": lambda self: None,
    })()

    try:
        with patch.object(ss_mod, "MEMORY_ROOT", memory_root):
            if project_dir:
                with patch.object(ss_mod, "_detect_project", return_value=None):
                    ss_mod.main()
            else:
                with patch.object(ss_mod, "_detect_project", return_value=None):
                    ss_mod.main()
    except SystemExit:
        pass
    finally:
        sys.stdout = sys.__stdout__
        ss_mod.MEMORY_ROOT = original_memory_root

    return buf.getvalue().decode("utf-8", errors="replace")


def memory_contains_phrase(memory_root: Path, phrase: str) -> bool:
    """Search all .memory/ files for a phrase. Case-insensitive."""
    phrase_lower = phrase.lower()
    for f in memory_root.rglob("*.md"):
        try:
            if phrase_lower in f.read_text(encoding="utf-8", errors="replace").lower():
                return True
        except OSError:
            pass
    return False


def find_fact_in_memory(memory_root: Path, key_phrase: str) -> list[Path]:
    """Return all .memory/ files that contain the key_phrase."""
    phrase_lower = key_phrase.lower()
    found = []
    for f in memory_root.rglob("*.md"):
        try:
            if phrase_lower in f.read_text(encoding="utf-8", errors="replace").lower():
                found.append(f)
        except OSError:
            pass
    return found
