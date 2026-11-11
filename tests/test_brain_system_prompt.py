"""Tests for Brain system prompt builder (system_prompt.py)."""
from __future__ import annotations

from pathlib import Path


def _write_identity(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\nid: alex\ntitle: Alex\ntype: identity\n---\n\n{body}",
        encoding="utf-8",
    )


class TestBuildSystemPrompt:
    def test_returns_nonempty_string(self, tmp_path):
        from atlas_plus.brain.system_prompt import build_system_prompt
        result = build_system_prompt(memory_root=tmp_path)
        assert isinstance(result, str)
        assert len(result) > 50

    def test_includes_atlas_persona(self, tmp_path):
        from atlas_plus.brain.system_prompt import build_system_prompt
        result = build_system_prompt(memory_root=tmp_path)
        assert "Atlas" in result

    def test_includes_today_date(self, tmp_path):
        from datetime import datetime, timezone
        from atlas_plus.brain.system_prompt import build_system_prompt
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        result = build_system_prompt(memory_root=tmp_path)
        assert today in result

    def test_includes_house_rules(self, tmp_path):
        from atlas_plus.brain.system_prompt import build_system_prompt
        result = build_system_prompt(memory_root=tmp_path)
        assert "Version before edit" in result

    def test_includes_tool_guidance(self, tmp_path):
        from atlas_plus.brain.system_prompt import build_system_prompt
        result = build_system_prompt(memory_root=tmp_path)
        assert "memory_search_tool" in result

    def test_includes_identity_when_present(self, tmp_path):
        from atlas_plus.brain.system_prompt import build_system_prompt
        _write_identity(tmp_path / "identity" / "alex.md", "Alex is a Tier 3 Support Engineer at ExampleCorp.")
        result = build_system_prompt(memory_root=tmp_path)
        assert "Alex" in result
        assert "ExampleCorp" in result

    def test_graceful_without_identity_file(self, tmp_path):
        from atlas_plus.brain.system_prompt import build_system_prompt
        # No identity file -- should not raise
        result = build_system_prompt(memory_root=tmp_path)
        assert "Atlas" in result

    def test_strips_frontmatter_from_identity(self, tmp_path):
        from atlas_plus.brain.system_prompt import build_system_prompt
        _write_identity(tmp_path / "identity" / "alex.md", "Alex works in VoIP telecom support.")
        result = build_system_prompt(memory_root=tmp_path)
        # Frontmatter keys should not appear in the final prompt
        assert "type: identity" not in result
