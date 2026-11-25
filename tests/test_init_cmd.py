"""Tests for atlas-init / sextant-init first-run scaffolding (X1, 2026-07-30)."""

from __future__ import annotations

from pathlib import Path

import pytest

from atlas_plus.init_cmd import run_init, _SCOPE_DIRS


@pytest.fixture()
def fresh_env(tmp_path, monkeypatch):
    monkeypatch.setenv("ATLAS_MEMORY_ROOT", str(tmp_path / "mem"))
    monkeypatch.setenv("ATLAS_STATE_DIR", str(tmp_path / "state"))
    return tmp_path


class TestRunInit:
    def test_creates_full_skeleton(self, fresh_env):
        result = run_init()
        mem = Path(result["memory_root"])
        for scope in _SCOPE_DIRS:
            assert (mem / scope).is_dir()
        assert (mem / "identity" / "user.md").is_file()
        assert "Your Name" in (mem / "identity" / "user.md").read_text(encoding="utf-8")
        assert Path(result["state_dir"]).is_dir()
        assert result["created"] and not result["dry_run"]

    def test_idempotent_second_run_creates_nothing(self, fresh_env):
        run_init()
        second = run_init()
        assert second["created"] == []
        assert second["existed"]

    def test_never_overwrites_existing_identity(self, fresh_env):
        first = run_init()
        user = Path(first["memory_root"]) / "identity" / "user.md"
        user.write_text("---\ntitle: Alex Example\n---\nmy actual identity", encoding="utf-8")
        run_init()
        assert "my actual identity" in user.read_text(encoding="utf-8")

    def test_dry_run_touches_nothing(self, fresh_env):
        result = run_init(dry_run=True)
        assert result["created"]
        assert not Path(result["memory_root"]).exists()
        assert not Path(result["state_dir"]).exists()

    def test_partial_skeleton_fills_gaps_only(self, fresh_env, monkeypatch):
        mem = fresh_env / "mem"
        (mem / "feedback").mkdir(parents=True)
        keep = mem / "feedback" / "rule.md"
        keep.write_text("existing rule", encoding="utf-8")
        result = run_init()
        assert keep.read_text(encoding="utf-8") == "existing rule"
        assert str(mem / "identity") in result["created"]
        assert str(mem / "feedback") in result["existed"]


class TestFirstRunWording:
    """SEX-043 (laptop install from PyPI, 2026-11-23): what the first run tells a newcomer."""

    def _main(self, monkeypatch, capsys, *argv):
        import sys
        from atlas_plus import init_cmd
        monkeypatch.setattr(sys, "argv", ["sextant-init", *argv])
        with pytest.raises(SystemExit) as exc:
            init_cmd.main()
        assert exc.value.code == 0
        return capsys.readouterr().out

    def test_dry_run_on_an_existing_corpus_says_it_is_found_and_reused(self, fresh_env, monkeypatch, capsys):
        run_init()
        out = self._main(monkeypatch, capsys, "--dry-run")
        assert "Found an existing corpus" in out and str(fresh_env / "mem") in out
        assert "kept and reused" in out

    def test_first_run_does_not_claim_an_existing_corpus(self, fresh_env, monkeypatch, capsys):
        out = self._main(monkeypatch, capsys)
        assert "Found an existing corpus" not in out
        assert "outside this install" in out

    def test_next_steps_are_numbered_plainly_and_name_the_demo_fact(self, fresh_env, monkeypatch, capsys):
        out = self._main(monkeypatch, capsys)
        steps = [ln.strip().split(".")[0] for ln in out.splitlines() if ln.startswith("  ") and ln.strip()[:2] in ("1.", "2.", "3.", "4.")]
        assert steps == ["1", "2", "3", "4"]
        assert "2b" not in out and "atlas-doctor" not in out
        assert "projects/demo.md" in out


def test_setup_says_which_part_is_needed_and_names_the_short_verify():
    from atlas_plus import setup_cmd
    text = setup_cmd.render(setup_cmd.describe())
    assert "ONE of the numbered sections" in text
    from atlas_plus import _next_step  # one form, the one this install types (AG107, SEX-043 f)
    assert f"Verify: {_next_step.command('verify')}" in text
