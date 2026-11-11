"""Tests for decision memory type: routing, tool, and listing."""
from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest


class TestDecisionTool:
    def test_creates_decision_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            with patch("atlas_plus.mcp.memory_server.tools.decision.MEMORY_ROOT", tmp_path), \
                 patch("atlas_plus.mcp.memory_server.tools.decision.require_autonomous_writes", lambda _: None), \
                 patch("atlas_plus.mcp.memory_server.tools.decision.version_before_write", return_value=None):
                from atlas_plus.mcp.memory_server.tools.decision import memory_remember_decision
                result = memory_remember_decision(
                    title="Use FAISS for VoIP",
                    decision="FAISS is faster for static corpora.",
                    conditions=["Corpus is static"],
                    revisit_if=["Corpus updates weekly"],
                    project="harbor-app",
                )
        assert result["ok"] is True
        assert result["slug"] == "use-faiss-for-voip"

    def test_slug_from_title(self):
        from atlas_plus.mcp.memory_server.tools.decision import _slug
        assert _slug("Use FAISS for VoIP") == "use-faiss-for-voip"
        assert _slug("Deploy  on  AWS") == "deploy-on-aws"

    def test_empty_title_returns_error(self):
        with patch("atlas_plus.mcp.memory_server.tools.decision.require_autonomous_writes", lambda _: None):
            from atlas_plus.mcp.memory_server.tools.decision import memory_remember_decision
            result = memory_remember_decision(title="", decision="something")
        assert result["ok"] is False
        assert "title" in result["error"]

    def test_empty_decision_returns_error(self):
        with patch("atlas_plus.mcp.memory_server.tools.decision.require_autonomous_writes", lambda _: None):
            from atlas_plus.mcp.memory_server.tools.decision import memory_remember_decision
            result = memory_remember_decision(title="A decision", decision="")
        assert result["ok"] is False

    def test_decision_file_contains_revisit_if(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            with patch("atlas_plus.mcp.memory_server.tools.decision.MEMORY_ROOT", tmp_path), \
                 patch("atlas_plus.mcp.memory_server.tools.decision.require_autonomous_writes", lambda _: None), \
                 patch("atlas_plus.mcp.memory_server.tools.decision.version_before_write", return_value=None):
                from atlas_plus.mcp.memory_server.tools.decision import memory_remember_decision
                memory_remember_decision(
                    title="Pick database",
                    decision="Use SQLite.",
                    revisit_if=["Team grows beyond 3", "Need concurrent writes"],
                )
            content = (tmp_path / "decisions" / "pick-database.md").read_text()
        assert "Team grows beyond 3" in content
        assert "revisit_if" in content

    def test_list_decisions_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch("atlas_plus.mcp.memory_server.tools.decision.MEMORY_ROOT", Path(tmp)):
                from atlas_plus.mcp.memory_server.tools.decision import list_decisions
                assert list_decisions() == []

    def test_list_decisions_after_create(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            with patch("atlas_plus.mcp.memory_server.tools.decision.MEMORY_ROOT", tmp_path), \
                 patch("atlas_plus.mcp.memory_server.tools.decision.require_autonomous_writes", lambda _: None), \
                 patch("atlas_plus.mcp.memory_server.tools.decision.version_before_write", return_value=None):
                from atlas_plus.mcp.memory_server.tools.decision import list_decisions, memory_remember_decision
                memory_remember_decision(title="Alpha", decision="Alpha decision.", project="p1")
                memory_remember_decision(title="Beta", decision="Beta decision.", project="p2")
                all_d = list_decisions()
                p1_d = list_decisions(project="p1")

        assert len(all_d) == 2
        assert len(p1_d) == 1
        assert p1_d[0]["project"] == "p1"


    def test_empty_revisit_if_produces_valid_yaml_list(self):
        """When revisit_if is None, the frontmatter YAML must parse revisit_if as a list."""
        import yaml
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            with patch("atlas_plus.mcp.memory_server.tools.decision.MEMORY_ROOT", tmp_path), \
                 patch("atlas_plus.mcp.memory_server.tools.decision.require_autonomous_writes", lambda _: None), \
                 patch("atlas_plus.mcp.memory_server.tools.decision.version_before_write", return_value=None):
                from atlas_plus.mcp.memory_server.tools.decision import memory_remember_decision
                memory_remember_decision(
                    title="No Conditions Decision",
                    decision="Some choice.",
                    # revisit_if=None (default)
                )
            content = (tmp_path / "decisions" / "no-conditions-decision.md").read_text()
        # Parse the frontmatter
        end = content.find("\n---", 3)
        fm = yaml.safe_load(content[3:end]) or {}
        # revisit_if must be a list, not a bare string
        assert isinstance(fm.get("revisit_if"), list), \
            f"revisit_if should be a list, got {type(fm.get('revisit_if'))}: {fm.get('revisit_if')!r}"


class TestProvenanceTag:
    def test_append_fact_includes_source_comment(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "test.md"
            target.write_text("---\nid: test\n---\n\n## 2020-01-01\n\nOld fact\n")
            from atlas_plus.mcp.memory_server.routing import append_fact
            append_fact(target, "New fact", {"type": "reference"}, source="telegram")
            content = target.read_text()
        assert "<!-- source:telegram -->" in content

    def test_new_file_includes_source_comment(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "new.md"
            from atlas_plus.mcp.memory_server.routing import append_fact
            append_fact(target, "Brand new fact", {"type": "feedback"}, source="suggest-facts")
            content = target.read_text()
        assert "<!-- source:suggest-facts -->" in content

    def test_default_source_is_agent(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "default.md"
            from atlas_plus.mcp.memory_server.routing import append_fact
            append_fact(target, "A fact with default source", {"type": "reference"})
            content = target.read_text()
        assert "<!-- source:agent -->" in content
