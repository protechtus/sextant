"""Tests for atlas_plus.identity -- identity file resolution and display name."""
from __future__ import annotations

from pathlib import Path

from atlas_plus.identity import (
    GENERIC_USER_NAME,
    resolve_identity_path,
    user_display_name,
)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


class TestResolveIdentityPath:
    def test_env_var_absolute(self, tmp_path, monkeypatch):
        target = tmp_path / "elsewhere" / "me.md"
        _write(target, "hi")
        monkeypatch.setenv("ATLAS_IDENTITY_PATH", str(target))
        assert resolve_identity_path(tmp_path) == target

    def test_env_var_relative_to_memory_root(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ATLAS_IDENTITY_PATH", "identity/me.md")
        assert resolve_identity_path(tmp_path) == tmp_path / "identity" / "me.md"

    def test_env_var_wins_even_over_existing_default(self, tmp_path, monkeypatch):
        _write(tmp_path / "identity" / "user.md", "default")
        monkeypatch.setenv("ATLAS_IDENTITY_PATH", "identity/me.md")
        assert resolve_identity_path(tmp_path) == tmp_path / "identity" / "me.md"

    def test_default_user_md(self, tmp_path, monkeypatch):
        monkeypatch.delenv("ATLAS_IDENTITY_PATH", raising=False)
        _write(tmp_path / "identity" / "user.md", "default")
        assert resolve_identity_path(tmp_path) == tmp_path / "identity" / "user.md"

    def test_legacy_fallback_first_md_sorted(self, tmp_path, monkeypatch):
        monkeypatch.delenv("ATLAS_IDENTITY_PATH", raising=False)
        _write(tmp_path / "identity" / "zeta.md", "z")
        _write(tmp_path / "identity" / "alpha.md", "a")
        assert resolve_identity_path(tmp_path) == tmp_path / "identity" / "alpha.md"

    def test_legacy_fallback_prefers_priority_critical(self, tmp_path, monkeypatch):
        """Multi-facet identity dirs: the person file carries priority: critical."""
        monkeypatch.delenv("ATLAS_IDENTITY_PATH", raising=False)
        _write(
            tmp_path / "identity" / "communication.md",
            "---\ntitle: Communication Style\ntype: identity\npriority: high\n---\nBe brief.\n",
        )
        _write(
            tmp_path / "identity" / "person.md",
            "---\ntitle: Alex Example\ntype: identity\npriority: critical\n---\nAlex.\n",
        )
        _write(
            tmp_path / "identity" / "working-style.md",
            "---\ntitle: Working Style\ntype: identity\npriority: high\n---\nShip.\n",
        )
        assert resolve_identity_path(tmp_path) == tmp_path / "identity" / "person.md"

    def test_empty_identity_dir_returns_default(self, tmp_path, monkeypatch):
        monkeypatch.delenv("ATLAS_IDENTITY_PATH", raising=False)
        (tmp_path / "identity").mkdir()
        result = resolve_identity_path(tmp_path)
        assert result == tmp_path / "identity" / "user.md"
        assert not result.exists()

    def test_missing_identity_dir_returns_default(self, tmp_path, monkeypatch):
        monkeypatch.delenv("ATLAS_IDENTITY_PATH", raising=False)
        result = resolve_identity_path(tmp_path)
        assert result == tmp_path / "identity" / "user.md"


class TestUserDisplayName:
    def test_env_var_wins(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ATLAS_USER_NAME", "Morgan")
        assert user_display_name(tmp_path) == "Morgan"

    def test_frontmatter_title(self, tmp_path, monkeypatch):
        monkeypatch.delenv("ATLAS_USER_NAME", raising=False)
        monkeypatch.delenv("ATLAS_IDENTITY_PATH", raising=False)
        _write(
            tmp_path / "identity" / "user.md",
            "---\nid: user\ntitle: Alex Example\ntype: identity\n---\nBody.\n",
        )
        assert user_display_name(tmp_path) == "Alex Example"

    def test_quoted_title_is_stripped(self, tmp_path, monkeypatch):
        monkeypatch.delenv("ATLAS_USER_NAME", raising=False)
        monkeypatch.delenv("ATLAS_IDENTITY_PATH", raising=False)
        _write(
            tmp_path / "identity" / "user.md",
            '---\ntitle: "Alex Example"\n---\nBody.\n',
        )
        assert user_display_name(tmp_path) == "Alex Example"

    def test_descriptive_title_is_trimmed_to_name(self, tmp_path, monkeypatch):
        """Titles like 'A. Person -- Identity & Profile' must yield just the name."""
        monkeypatch.delenv("ATLAS_USER_NAME", raising=False)
        monkeypatch.delenv("ATLAS_IDENTITY_PATH", raising=False)
        _write(
            tmp_path / "identity" / "user.md",
            "---\ntitle: Alex Example — Identity & Profile\n---\nBody.\n",
        )
        assert user_display_name(tmp_path) == "Alex Example"

    def test_no_frontmatter_falls_back_generic(self, tmp_path, monkeypatch):
        monkeypatch.delenv("ATLAS_USER_NAME", raising=False)
        monkeypatch.delenv("ATLAS_IDENTITY_PATH", raising=False)
        _write(tmp_path / "identity" / "user.md", "Just a body, no frontmatter.\n")
        assert user_display_name(tmp_path) == GENERIC_USER_NAME

    def test_frontmatter_without_title_falls_back_generic(self, tmp_path, monkeypatch):
        monkeypatch.delenv("ATLAS_USER_NAME", raising=False)
        monkeypatch.delenv("ATLAS_IDENTITY_PATH", raising=False)
        _write(tmp_path / "identity" / "user.md", "---\ntype: identity\n---\nBody.\n")
        assert user_display_name(tmp_path) == GENERIC_USER_NAME

    def test_no_identity_file_falls_back_generic(self, tmp_path, monkeypatch):
        monkeypatch.delenv("ATLAS_USER_NAME", raising=False)
        monkeypatch.delenv("ATLAS_IDENTITY_PATH", raising=False)
        assert user_display_name(tmp_path) == GENERIC_USER_NAME


class TestSystemPromptIntegration:
    def test_prompt_uses_resolved_identity_and_title(self, tmp_path, monkeypatch):
        monkeypatch.delenv("ATLAS_USER_NAME", raising=False)
        monkeypatch.delenv("ATLAS_IDENTITY_PATH", raising=False)
        from atlas_plus.brain.system_prompt import build_system_prompt
        _write(
            tmp_path / "identity" / "user.md",
            "---\ntitle: Alex Example\ntype: identity\n---\nAlex builds VoIP tooling.\n",
        )
        result = build_system_prompt(memory_root=tmp_path)
        assert "Alex Example's personal AI assistant" in result
        assert "About Alex Example" in result
        assert "Alex builds VoIP tooling." in result

    def test_prompt_generic_without_identity(self, tmp_path, monkeypatch):
        monkeypatch.delenv("ATLAS_USER_NAME", raising=False)
        monkeypatch.delenv("ATLAS_IDENTITY_PATH", raising=False)
        from atlas_plus.brain.system_prompt import build_system_prompt
        result = build_system_prompt(memory_root=tmp_path)
        assert f"{GENERIC_USER_NAME}'s personal AI assistant" in result
