"""Gate-drift regression (Codex audit 2026-09-21): garbage ATLAS_AUTONOMOUS
values must never enable a background write anywhere.

The audit PROVED brain.research._try_link_reference wrote a reference file
under ATLAS_AUTONOMOUS=yes because it used denylist semantics while the
canonical gate is an allowlist. Every predicate now routes through
atlas_plus.autonomous.autonomous_enabled(); these tests pin that.
"""
from __future__ import annotations

import pytest

from atlas_plus.autonomous import autonomous_enabled

GARBAGE_VALUES = ["yes", "onn", "of", "enabled", "TRUE ", "y", "1.0"]
# ("TRUE " strips to "true" -- allowlisted; keep it out of garbage)
GARBAGE_VALUES.remove("TRUE ")


class TestCanonicalPredicate:
    @pytest.mark.parametrize("value", GARBAGE_VALUES)
    def test_garbage_is_off(self, monkeypatch, value):
        monkeypatch.setenv("ATLAS_AUTONOMOUS", value)
        assert autonomous_enabled() is False

    def test_unset_is_off(self, monkeypatch):
        monkeypatch.delenv("ATLAS_AUTONOMOUS", raising=False)
        assert autonomous_enabled() is False

    @pytest.mark.parametrize("value", ["on", "1", "true", "ON", " true "])
    def test_allowlist_is_on(self, monkeypatch, value):
        monkeypatch.setenv("ATLAS_AUTONOMOUS", value)
        assert autonomous_enabled() is True


class TestNoDriftedCallSites:
    """Static gate: no direct denylist check of ATLAS_AUTONOMOUS may exist
    outside the canonical module (doctor's raw display excepted)."""

    def test_no_denylist_env_checks(self):
        import re
        from pathlib import Path

        src = Path(__file__).resolve().parents[1] / "src" / "atlas_plus"
        offenders = []
        for py in src.rglob("*.py"):
            if py.name == "autonomous.py":
                continue
            text = py.read_text(encoding="utf-8", errors="replace")
            for i, line in enumerate(text.splitlines(), 1):
                if 'environ.get("ATLAS_AUTONOMOUS"' in line:
                    rel = py.relative_to(src).as_posix()
                    if rel == "doctor/cli.py":  # raw value display only
                        continue
                    offenders.append(f"{rel}:{i}: {line.strip()[:80]}")
        assert not offenders, (
            "direct ATLAS_AUTONOMOUS checks outside atlas_plus.autonomous "
            f"(use autonomous_enabled()/require_write): {offenders}"
        )


class TestProvenBypassStaysClosed:
    def test_research_link_helper_refuses_on_garbage(self, tmp_path, monkeypatch):
        """The audit's exact reproduction: _try_link_reference must NOT write
        under ATLAS_AUTONOMOUS=yes."""
        mem = tmp_path / "mem"
        mem.mkdir()
        monkeypatch.setenv("ATLAS_MEMORY_ROOT", str(mem))
        monkeypatch.setenv("ATLAS_STATE_DIR", str(tmp_path / "state"))
        monkeypatch.setenv("ATLAS_AUTONOMOUS", "yes")
        from atlas_plus.brain import research

        # Call the helper the way the pipeline does; the corpus must remain
        # untouched regardless of outcome.
        report = tmp_path / "report.md"
        report.write_text("# scratch report", encoding="utf-8")
        try:
            research._try_link_reference(
                "scratch-topic", "scratch-topic", "2026-09-22", report
            )
        except Exception:
            pass
        written = list(mem.rglob("*.md"))
        assert written == [], f"background write leaked under garbage env: {written}"

    def test_brain_tools_preflight_refuses_on_garbage(self, monkeypatch):
        monkeypatch.setenv("ATLAS_AUTONOMOUS", "onn")
        from atlas_plus.brain import tools

        # the write-preflight helper (line ~25) must report disabled
        import inspect

        fn = None
        for name in ("_autonomous_enabled", "_is_autonomous", "_writes_enabled"):
            fn = getattr(tools, name, None)
            if fn:
                break
        if fn is None:
            pytest.skip("preflight helper renamed; static gate covers it")
        assert fn() is False
