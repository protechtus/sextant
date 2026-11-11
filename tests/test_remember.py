"""P3-T7: Smoke tests for memory_remember (opt-in remembering)."""

import textwrap
from pathlib import Path

import pytest
import yaml


@pytest.fixture(autouse=True)
def temp_memory(tmp_path, monkeypatch):
    mem = tmp_path / ".memory"
    mem.mkdir()
    (mem / "identity").mkdir()
    (mem / "feedback").mkdir()
    (mem / "projects").mkdir()
    (mem / "reference").mkdir()
    (mem / "_atlas").mkdir()

    (mem / "identity" / "alex.md").write_text(
        textwrap.dedent("""\
        ---
        id: alex
        title: Alex Example
        type: identity
        priority: critical
        updated: 2026-04-24
        ---

        Alex is a Tier 3 support engineer at ExampleCorp.
        """),
        encoding="utf-8",
    )

    (mem / "projects" / "sidecar.md").write_text(
        textwrap.dedent("""\
        ---
        id: sidecar
        title: Sidecar
        type: project
        updated: 2026-04-24
        ---

        Sidecar service notes.
        """),
        encoding="utf-8",
    )

    routing = {
        "routes": {
            "identity": {"target": "identity/alex.md", "create_if_missing": False},
            "preference": {
                "target": "feedback/{topic}.md",
                "create_if_missing": True,
                "frontmatter_template": {"type": "feedback", "status": "active", "priority": "normal"},
            },
            "project": {"target": "projects/{name}.md", "create_if_missing": False},
            "reference": {
                "target": "reference/{topic}.md",
                "create_if_missing": True,
                "frontmatter_template": {"type": "reference", "status": "active", "priority": "normal"},
            },
        }
    }
    (mem / "_atlas" / "routing.yaml").write_text(yaml.dump(routing), encoding="utf-8")

    import atlas_plus.mcp.memory_server.config as cfg
    import atlas_plus.mcp.memory_server.routing as routing_mod
    import atlas_plus.mcp.memory_server.versioning as ver_mod

    cfg.MEMORY_ROOT = mem
    routing_mod.MEMORY_ROOT = mem
    routing_mod.ROUTING_CONFIG = mem / "_atlas" / "routing.yaml"
    ver_mod.MEMORY_ROOT = mem

    monkeypatch.setenv("ATLAS_AUTONOMOUS", "on")
    yield mem


def test_remember_identity_appends_to_existing_file(temp_memory):
    from atlas_plus.mcp.memory_server.tools.remember import memory_remember
    result = memory_remember("identity", "Alex prefers direct communication, no filler.")
    assert result["ok"] is True
    content = (temp_memory / "identity" / "alex.md").read_text(encoding="utf-8")
    assert "Alex prefers direct communication" in content
    assert "Alex is a Tier 3" in content  # original content preserved


def test_remember_identity_creates_version_copy(temp_memory):
    from atlas_plus.mcp.memory_server.tools.remember import memory_remember
    result = memory_remember("identity", "Alex uses uv for all Python projects.")
    assert result["version_copy"] is not None
    assert Path(result["version_copy"]).exists()


def test_remember_preference_creates_new_file(temp_memory):
    from atlas_plus.mcp.memory_server.tools.remember import memory_remember
    result = memory_remember("preference:editor", "Alex uses VS Code. No strong IDE preferences.")
    assert result["ok"] is True
    target = temp_memory / "feedback" / "editor.md"
    assert target.exists()
    assert "VS Code" in target.read_text(encoding="utf-8")


def test_remember_project_appends_to_existing(temp_memory):
    from atlas_plus.mcp.memory_server.tools.remember import memory_remember
    result = memory_remember("project:sidecar", "First real WDK build pending.")
    assert result["ok"] is True
    content = (temp_memory / "projects" / "sidecar.md").read_text(encoding="utf-8")
    assert "First real WDK build" in content
    assert "Sidecar service notes" in content  # original preserved


def test_remember_project_missing_raises_error(temp_memory):
    from atlas_plus.mcp.memory_server.tools.remember import memory_remember
    result = memory_remember("project:nonexistent", "Some fact.")
    assert result["ok"] is False
    assert "does not exist" in result["error"]


def test_remember_unknown_scope_returns_error(temp_memory):
    from atlas_plus.mcp.memory_server.tools.remember import memory_remember
    result = memory_remember("bogus:scope", "Some fact.")
    assert result["ok"] is False
    assert "Unknown scope" in result["error"]


def test_remember_blocked_when_autonomous_off(temp_memory, monkeypatch):
    monkeypatch.setenv("ATLAS_AUTONOMOUS", "off")
    from atlas_plus.mcp.memory_server.tools.remember import memory_remember
    # match the mechanism, not the message formatting (the error now includes the actual
    # env value repr, e.g. ATLAS_AUTONOMOUS='off', so typos are visible in the message)
    with pytest.raises(RuntimeError, match="ATLAS_AUTONOMOUS"):
        memory_remember("identity", "Should not be written.")


def test_remember_idempotent_version_copy(temp_memory):
    from atlas_plus.mcp.memory_server.tools.remember import memory_remember
    r1 = memory_remember("identity", "Alex prefers tabs over spaces.")
    r2 = memory_remember("identity", "Alex also prefers dark mode.")
    # Both should succeed
    assert r1["ok"] and r2["ok"]
    # Content should have both facts
    content = (temp_memory / "identity" / "alex.md").read_text(encoding="utf-8")
    assert "tabs over spaces" in content
    assert "dark mode" in content


# ---------------------------------------------------------------------------
# Duplicate detection
# ---------------------------------------------------------------------------

def test_remember_result_includes_duplicate_fields(temp_memory):
    """memory_remember() always returns possible_duplicate and duplicate_hint keys."""
    from atlas_plus.mcp.memory_server.tools.remember import memory_remember
    from unittest.mock import patch
    # Patch FTS to return no duplicates
    with patch("atlas_plus.mcp.memory_server.tools.remember._find_duplicate", return_value=None):
        result = memory_remember("identity", "Alex uses Python 3.12 for all projects.")
    assert "possible_duplicate" in result
    assert "duplicate_hint" in result
    assert result["possible_duplicate"] is False
    assert result["duplicate_hint"] is None


def test_remember_flags_possible_duplicate_when_similar_exists(temp_memory):
    """When FTS returns a similar result, possible_duplicate=True in the response."""
    from atlas_plus.mcp.memory_server.tools.remember import memory_remember
    from unittest.mock import patch

    dup_result = {"path": "feedback/style.md", "score": 0.55}
    with patch("atlas_plus.mcp.memory_server.tools.remember._find_duplicate", return_value=dup_result):
        result = memory_remember("identity", "Alex uses Python 3.12 for all projects.")
    assert result["ok"] is True  # still wrote the fact
    assert result["possible_duplicate"] is True
    assert "feedback/style.md" in (result["duplicate_hint"] or "")


def test_jaccard_same_text_returns_one():
    from atlas_plus.mcp.memory_server.tools.remember import _jaccard
    text = "Atlas Brain worker processes queued tasks from the SQLite database"
    assert _jaccard(text, text) == 1.0


def test_jaccard_disjoint_returns_zero():
    from atlas_plus.mcp.memory_server.tools.remember import _jaccard
    assert _jaccard("atlas brain worker tasks", "telescope galaxy universe stars") == 0.0


def test_jaccard_partial_overlap():
    from atlas_plus.mcp.memory_server.tools.remember import _jaccard
    a = "Atlas Brain worker processes tasks from SQLite database"
    b = "Brain worker checks SQLite database for pending tasks every second"
    result = _jaccard(a, b)
    assert 0.0 < result < 1.0


def test_find_duplicate_skips_self(temp_memory):
    """_find_duplicate should not return the file we just wrote to."""
    from atlas_plus.mcp.memory_server.tools.remember import _find_duplicate, memory_remember
    from unittest.mock import patch

    # Write the fact first so the target file exists
    memory_remember("identity", "Alex uses Python extensively for automation tasks.")

    target = temp_memory / "identity" / "alex.md"
    # FTS returns the same file -- should be skipped
    self_result = [{"path": "identity/alex.md", "score": 0.9, "snippet": "Python extensively for automation"}]
    with patch("atlas_plus.mcp.memory_server.fts.search", return_value=self_result), \
         patch("atlas_plus.mcp.memory_server.tools.remember.MEMORY_ROOT", temp_memory):
        dup = _find_duplicate("Alex uses Python extensively for automation tasks.", target)
    assert dup is None  # self-match should be filtered out


# ---------------------------------------------------------------------------
# Git autocommit
# ---------------------------------------------------------------------------

def test_git_autocommit_skips_when_off(temp_memory, monkeypatch):
    """_git_autocommit is a no-op when ATLAS_MEMORY_AUTOCOMMIT is not set."""
    monkeypatch.delenv("ATLAS_MEMORY_AUTOCOMMIT", raising=False)
    from unittest.mock import patch
    from atlas_plus.mcp.memory_server.tools.remember import _git_autocommit

    with patch("subprocess.run") as mock_run:
        _git_autocommit(temp_memory / "identity" / "alex.md", "identity", temp_memory)
    mock_run.assert_not_called()


def test_git_autocommit_runs_when_on(temp_memory, monkeypatch):
    """_git_autocommit calls git add + git commit when ATLAS_MEMORY_AUTOCOMMIT=on."""
    monkeypatch.setenv("ATLAS_MEMORY_AUTOCOMMIT", "on")
    from unittest.mock import patch, MagicMock
    from atlas_plus.mcp.memory_server.tools.remember import _git_autocommit

    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = "1 file changed"
    mock_result.stderr = ""

    with patch("subprocess.run", return_value=mock_result) as mock_run:
        _git_autocommit(temp_memory / "identity" / "alex.md", "identity", temp_memory)

    # Should have called git at least twice (add + commit)
    assert mock_run.call_count >= 2


def test_git_autocommit_survives_exception(temp_memory, monkeypatch):
    """_git_autocommit never raises even if git fails."""
    monkeypatch.setenv("ATLAS_MEMORY_AUTOCOMMIT", "on")
    from unittest.mock import patch
    from atlas_plus.mcp.memory_server.tools.remember import _git_autocommit

    with patch("subprocess.run", side_effect=OSError("git not found")):
        # Should not raise
        _git_autocommit(temp_memory / "identity" / "alex.md", "identity", temp_memory)


# ---------------------------------------------------------------------------
# Fact provenance trail (session_id / task_id)
# ---------------------------------------------------------------------------

def test_append_fact_new_file_stores_source_session_frontmatter(temp_memory):
    """New-file path: source_session: and source_task: appear in frontmatter."""
    from atlas_plus.mcp.memory_server.routing import append_fact

    target = temp_memory / "reference" / "provtest.md"
    target.parent.mkdir(exist_ok=True)
    append_fact(
        target,
        "Provenance test fact for new file creation.",
        {"type": "reference", "status": "active"},
        source="agent",
        session_id="sess-abc-123",
        task_id="task-xyz-456",
    )
    content = target.read_text(encoding="utf-8")
    assert "source_session: sess-abc-123" in content
    assert "source_task: task-xyz-456" in content


def test_append_fact_new_file_truncates_long_ids(temp_memory):
    """session_id is capped at 60 chars and task_id at 36 chars in frontmatter."""
    from atlas_plus.mcp.memory_server.routing import append_fact

    target = temp_memory / "reference" / "trunctest.md"
    target.parent.mkdir(exist_ok=True)
    long_session = "S" * 80
    long_task = "T" * 50
    append_fact(
        target,
        "Truncation test fact.",
        {"type": "reference", "status": "active"},
        source="agent",
        session_id=long_session,
        task_id=long_task,
    )
    content = target.read_text(encoding="utf-8")
    assert "S" * 60 in content
    assert "S" * 61 not in content
    assert "T" * 36 in content
    assert "T" * 37 not in content


def test_append_fact_inline_comment_includes_session_and_task(temp_memory):
    """Inline provenance comment carries session: and task: parts."""
    from atlas_plus.mcp.memory_server.routing import append_fact

    target = temp_memory / "identity" / "alex.md"
    append_fact(
        target,
        "Alex confirmed dark mode preference.",
        {},
        source="agent",
        session_id="my-session-id",
        task_id="my-task-id",
    )
    content = target.read_text(encoding="utf-8")
    assert "session:my-session-id" in content
    assert "task:my-task-id" in content


def test_append_fact_inline_comment_omits_absent_ids(temp_memory):
    """When session_id and task_id are None, the inline comment has source: only."""
    from atlas_plus.mcp.memory_server.routing import append_fact

    target = temp_memory / "identity" / "alex.md"
    append_fact(target, "Alex prefers Python over JavaScript.", {}, source="agent")
    content = target.read_text(encoding="utf-8")
    # Verify last appended comment contains source: but not session:/task:
    last_comment = content.split("<!--")[-1].split("-->")[0]
    assert "source:agent" in last_comment
    assert "session:" not in last_comment
    assert "task:" not in last_comment


def test_memory_remember_passes_session_and_task_ids(temp_memory):
    """memory_remember() threads session_id and task_id through to the written file."""
    from atlas_plus.mcp.memory_server.tools.remember import memory_remember

    result = memory_remember(
        "preference:provenance-check",
        "Alex wants full provenance on every write.",
        source="agent",
        session_id="sess-e2e-789",
        task_id="task-e2e-012",
    )
    assert result["ok"] is True
    content = (temp_memory / "feedback" / "provenance-check.md").read_text(encoding="utf-8")
    assert "source_session: sess-e2e-789" in content
    assert "source_task: task-e2e-012" in content


def test_memory_remember_without_ids_still_succeeds(temp_memory):
    """memory_remember() works fine when no session/task IDs are provided."""
    from atlas_plus.mcp.memory_server.tools.remember import memory_remember

    result = memory_remember(
        "preference:no-ids",
        "This fact has no session or task ID.",
    )
    assert result["ok"] is True
    content = (temp_memory / "feedback" / "no-ids.md").read_text(encoding="utf-8")
    assert "This fact has no session or task ID." in content
    # No source_session/source_task fields should appear in frontmatter
    end = content.find("\n---", 3)
    fm_block = content[3:end]
    assert "source_session" not in fm_block
    assert "source_task" not in fm_block
