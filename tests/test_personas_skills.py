"""P5 tests — personas and skills MCP tools."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from atlas_plus.mcp.memory_server.tools.personas import personas_adopt, personas_list
from atlas_plus.mcp.memory_server.tools.skills import skills_invoke, skills_list


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def memory_root(tmp_path) -> Path:
    root = tmp_path / ".memory"
    root.mkdir()

    personas_dir = root / "personas"
    personas_dir.mkdir()

    runbooks_dir = root / "runbooks"
    runbooks_dir.mkdir()

    # template (should be excluded from list)
    (personas_dir / "_template.md").write_text("---\nid: template\n---\ntemplate body", encoding="utf-8")

    # planner persona
    (personas_dir / "planner.md").write_text(
        textwrap.dedent("""\
        ---
        id: planner
        title: Planner
        type: persona
        status: active
        updated: 2026-05-03
        allowed_namespaces:
          - global
          - persona/planner
        default_provider: ollama:llama3.1:8b
        cost_cap_usd_per_session: 0
        skill_refs:
          - version-before-edit
        ---

        # Identity

        You are the Atlas planning persona. Break work into tickets.
        """),
        encoding="utf-8",
    )

    # retired persona (should be excluded from adopt)
    (personas_dir / "old-persona.md").write_text(
        textwrap.dedent("""\
        ---
        id: old-persona
        title: Old Persona
        type: persona
        status: retired
        updated: 2026-01-01
        allowed_namespaces:
          - global
        default_provider: ollama:mistral
        cost_cap_usd_per_session: 0
        ---

        Old persona body.
        """),
        encoding="utf-8",
    )

    # runbook
    (runbooks_dir / "version-before-edit.md").write_text(
        textwrap.dedent("""\
        ---
        id: version-before-edit
        title: Version Before Edit
        type: runbook
        updated: 2026-04-30
        ---

        Copy the file to versions/ before editing.

        Step 1: copy to versions/.
        Step 2: verify copy exists.
        Step 3: edit the original.
        """),
        encoding="utf-8",
    )

    (runbooks_dir / "session-log.md").write_text(
        textwrap.dedent("""\
        ---
        id: session-log
        title: Session Log
        type: runbook
        updated: 2026-04-30
        ---

        Write session log entries to updates.txt and sessions/<date>.md.
        """),
        encoding="utf-8",
    )

    return root


# ---------------------------------------------------------------------------
# personas_list
# ---------------------------------------------------------------------------

class TestPersonasList:
    def test_lists_active_personas(self, memory_root):
        result = personas_list(memory_root)
        names = [p["name"] for p in result]
        assert "planner" in names

    def test_excludes_template(self, memory_root):
        result = personas_list(memory_root)
        names = [p["name"] for p in result]
        assert "_template" not in names

    def test_excludes_retired(self, memory_root):
        result = personas_list(memory_root)
        names = [p["name"] for p in result]
        assert "old-persona" not in names

    def test_returns_expected_fields(self, memory_root):
        result = personas_list(memory_root)
        planner = next(p for p in result if p["name"] == "planner")
        assert planner["title"] == "Planner"
        assert planner["default_provider"] == "ollama:llama3.1:8b"
        assert planner["cost_cap_usd_per_session"] == 0
        assert "version-before-edit" in planner["skill_refs"]
        assert "global" in planner["allowed_namespaces"]

    def test_empty_when_no_personas_dir(self, tmp_path):
        root = tmp_path / ".memory"
        root.mkdir()
        assert personas_list(root) == []


# ---------------------------------------------------------------------------
# personas_adopt
# ---------------------------------------------------------------------------

class TestPersonasAdopt:
    def test_returns_identity_for_known_persona(self, memory_root):
        result = personas_adopt("planner", memory_root)
        assert "error" not in result
        assert result["name"] == "planner"
        assert "planning persona" in result["identity"].lower()
        assert "instruction" in result

    def test_error_for_unknown_persona(self, memory_root):
        result = personas_adopt("nonexistent", memory_root)
        assert "error" in result
        assert "planner" in result["error"]  # shows available

    def test_error_for_retired_persona(self, memory_root):
        result = personas_adopt("old-persona", memory_root)
        assert "error" in result
        assert "retired" in result["error"].lower()

    def test_returns_all_schema_fields(self, memory_root):
        result = personas_adopt("planner", memory_root)
        for field in ("name", "title", "identity", "allowed_namespaces",
                      "default_provider", "cost_cap_usd_per_session", "skill_refs", "instruction"):
            assert field in result, f"Missing field: {field}"


# ---------------------------------------------------------------------------
# skills_list
# ---------------------------------------------------------------------------

class TestSkillsList:
    def test_lists_runbooks(self, memory_root):
        result = skills_list(memory_root)
        names = [s["name"] for s in result]
        assert "version-before-edit" in names
        assert "session-log" in names

    def test_returns_expected_fields(self, memory_root):
        result = skills_list(memory_root)
        vbe = next(s for s in result if s["name"] == "version-before-edit")
        assert vbe["title"] == "Version Before Edit"
        assert vbe["type"] == "runbook"
        assert vbe["description"]  # first sentence extracted

    def test_empty_when_no_runbooks_dir(self, tmp_path):
        root = tmp_path / ".memory"
        root.mkdir()
        assert skills_list(root) == []


# ---------------------------------------------------------------------------
# skills_invoke
# ---------------------------------------------------------------------------

class TestSkillsInvoke:
    def test_returns_body_for_known_skill(self, memory_root):
        result = skills_invoke("version-before-edit", memory_root)
        assert "error" not in result
        assert "Step 1" in result["body"]
        assert result["name"] == "version-before-edit"
        assert "instruction" in result

    def test_case_insensitive_match(self, memory_root):
        result = skills_invoke("Version-Before-Edit", memory_root)
        assert "error" not in result
        assert result["name"] == "version-before-edit"

    def test_error_for_unknown_skill(self, memory_root):
        result = skills_invoke("nonexistent-skill", memory_root)
        assert "error" in result
        assert "version-before-edit" in result["error"]  # shows available

    def test_returns_full_body(self, memory_root):
        result = skills_invoke("version-before-edit", memory_root)
        assert "Step 1" in result["body"]
        assert "Step 2" in result["body"]
        assert "Step 3" in result["body"]
