"""Tests for MCP memory server personas and skills tools."""

from __future__ import annotations

from pathlib import Path


def _write_persona(path: Path, title: str, status: str = "active",
                   provider: str = "claude", namespaces: list | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    ns = namespaces or ["global"]
    ns_yaml = ", ".join(ns)
    path.write_text(
        f"---\ntitle: {title}\nstatus: {status}\n"
        f"default_provider: {provider}\nallowed_namespaces: [{ns_yaml}]\n---\n\n"
        f"You are {title}. Be helpful.",
        encoding="utf-8",
    )


def _write_runbook(path: Path, title: str, description: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fm = f"---\ntitle: {title}\n"
    if description:
        fm += f"description: {description}\n"
    fm += "---\n\n"
    path.write_text(fm + "Step 1: Do this.\nStep 2: Do that.", encoding="utf-8")


# ---------------------------------------------------------------------------
# personas_list
# ---------------------------------------------------------------------------

class TestPersonasList:
    def test_empty_dir_returns_empty(self, tmp_path):
        from atlas_plus.mcp.memory_server.tools.personas import personas_list
        result = personas_list(memory_root=tmp_path)
        assert result == []

    def test_lists_active_persona(self, tmp_path):
        from atlas_plus.mcp.memory_server.tools.personas import personas_list
        _write_persona(tmp_path / "personas" / "sre.md", "SRE Assistant")
        result = personas_list(memory_root=tmp_path)
        assert len(result) == 1
        assert result[0]["name"] == "sre"
        assert result[0]["title"] == "SRE Assistant"

    def test_skips_retired_persona(self, tmp_path):
        from atlas_plus.mcp.memory_server.tools.personas import personas_list
        _write_persona(tmp_path / "personas" / "old.md", "Old Persona", status="retired")
        result = personas_list(memory_root=tmp_path)
        assert result == []

    def test_skips_underscore_files(self, tmp_path):
        from atlas_plus.mcp.memory_server.tools.personas import personas_list
        (tmp_path / "personas").mkdir(parents=True)
        (tmp_path / "personas" / "_index.md").write_text("private", encoding="utf-8")
        result = personas_list(memory_root=tmp_path)
        assert result == []

    def test_returns_allowed_namespaces(self, tmp_path):
        from atlas_plus.mcp.memory_server.tools.personas import personas_list
        _write_persona(
            tmp_path / "personas" / "sec.md", "Security",
            namespaces=["global", "security"]
        )
        result = personas_list(memory_root=tmp_path)
        assert len(result) == 1
        assert "security" in result[0]["allowed_namespaces"]


# ---------------------------------------------------------------------------
# personas_adopt
# ---------------------------------------------------------------------------

class TestPersonasAdopt:
    def test_invalid_name_returns_error(self, tmp_path):
        from atlas_plus.mcp.memory_server.tools.personas import personas_adopt
        result = personas_adopt("../../etc/passwd", memory_root=tmp_path)
        assert "error" in result

    def test_not_found_returns_error(self, tmp_path):
        from atlas_plus.mcp.memory_server.tools.personas import personas_adopt
        (tmp_path / "personas").mkdir(parents=True)
        result = personas_adopt("nonexistent", memory_root=tmp_path)
        assert "error" in result
        assert "not found" in result["error"].lower()

    def test_returns_identity_and_instruction(self, tmp_path):
        from atlas_plus.mcp.memory_server.tools.personas import personas_adopt
        _write_persona(tmp_path / "personas" / "researcher.md", "Research Assistant")
        result = personas_adopt("researcher", memory_root=tmp_path)
        assert "error" not in result
        assert result["name"] == "researcher"
        assert result["title"] == "Research Assistant"
        assert "instruction" in result
        assert len(result["identity"]) > 0

    def test_retired_persona_rejected(self, tmp_path):
        from atlas_plus.mcp.memory_server.tools.personas import personas_adopt
        _write_persona(tmp_path / "personas" / "legacy.md", "Legacy", status="retired")
        result = personas_adopt("legacy", memory_root=tmp_path)
        assert "error" in result
        assert "retired" in result["error"].lower()


# ---------------------------------------------------------------------------
# skills_list
# ---------------------------------------------------------------------------

class TestSkillsList:
    def test_empty_runbooks_dir_returns_empty(self, tmp_path):
        from atlas_plus.mcp.memory_server.tools.skills import skills_list
        result = skills_list(memory_root=tmp_path)
        assert result == []

    def test_lists_runbooks(self, tmp_path):
        from atlas_plus.mcp.memory_server.tools.skills import skills_list
        _write_runbook(tmp_path / "runbooks" / "version-before-edit.md", "Version Before Edit")
        result = skills_list(memory_root=tmp_path)
        assert len(result) == 1
        assert result[0]["name"] == "version-before-edit"
        assert result[0]["title"] == "Version Before Edit"

    def test_skips_underscore_files(self, tmp_path):
        from atlas_plus.mcp.memory_server.tools.skills import skills_list
        (tmp_path / "runbooks").mkdir(parents=True)
        (tmp_path / "runbooks" / "_private.md").write_text("internal", encoding="utf-8")
        result = skills_list(memory_root=tmp_path)
        assert result == []

    def test_description_from_frontmatter(self, tmp_path):
        from atlas_plus.mcp.memory_server.tools.skills import skills_list
        _write_runbook(
            tmp_path / "runbooks" / "crash.md", "Crash Recovery",
            description="Recover from a critical system failure."
        )
        result = skills_list(memory_root=tmp_path)
        assert result[0]["description"] == "Recover from a critical system failure."

    def test_description_fallback_to_body(self, tmp_path):
        from atlas_plus.mcp.memory_server.tools.skills import skills_list
        (tmp_path / "runbooks").mkdir(parents=True)
        (tmp_path / "runbooks" / "plain.md").write_text(
            "---\ntitle: Plain\n---\n\nFirst line of body text.", encoding="utf-8"
        )
        result = skills_list(memory_root=tmp_path)
        assert "First line" in result[0]["description"]


# ---------------------------------------------------------------------------
# skills_invoke
# ---------------------------------------------------------------------------

class TestSkillsInvoke:
    def test_not_found_returns_error_with_available(self, tmp_path):
        from atlas_plus.mcp.memory_server.tools.skills import skills_invoke
        _write_runbook(tmp_path / "runbooks" / "existing.md", "Existing Runbook")
        result = skills_invoke("nonexistent", memory_root=tmp_path)
        assert "error" in result
        assert "existing" in result["error"].lower()

    def test_returns_body_and_instruction(self, tmp_path):
        from atlas_plus.mcp.memory_server.tools.skills import skills_invoke
        _write_runbook(tmp_path / "runbooks" / "deploy.md", "Deploy Runbook")
        result = skills_invoke("deploy", memory_root=tmp_path)
        assert "error" not in result
        assert result["name"] == "deploy"
        assert "Step 1" in result["body"]
        assert "instruction" in result

    def test_case_insensitive_lookup(self, tmp_path):
        from atlas_plus.mcp.memory_server.tools.skills import skills_invoke
        _write_runbook(tmp_path / "runbooks" / "MyRunbook.md", "My Runbook")
        result = skills_invoke("myrunbook", memory_root=tmp_path)
        assert "error" not in result
        assert "Step 1" in result["body"]

    def test_empty_runbooks_dir_returns_error(self, tmp_path):
        from atlas_plus.mcp.memory_server.tools.skills import skills_invoke
        (tmp_path / "runbooks").mkdir(parents=True)
        result = skills_invoke("anything", memory_root=tmp_path)
        assert "error" in result
