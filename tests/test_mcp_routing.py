"""Tests for MCP memory server routing helpers and downgrade functions."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch


# ---------------------------------------------------------------------------
# _map_confidence
# ---------------------------------------------------------------------------

class TestMapConfidence:
    def test_human_direct_returns_high(self):
        from atlas_plus.mcp.memory_server.routing import _map_confidence
        assert _map_confidence(None, "human-direct") == "high"

    def test_high_float_returns_high(self):
        from atlas_plus.mcp.memory_server.routing import _map_confidence
        assert _map_confidence(0.9, "agent") == "high"

    def test_medium_float_returns_medium(self):
        from atlas_plus.mcp.memory_server.routing import _map_confidence
        assert _map_confidence(0.75, "agent") == "medium"

    def test_low_float_returns_low(self):
        from atlas_plus.mcp.memory_server.routing import _map_confidence
        assert _map_confidence(0.5, "agent") == "low"

    def test_none_confidence_returns_medium(self):
        from atlas_plus.mcp.memory_server.routing import _map_confidence
        assert _map_confidence(None, "agent") == "medium"

    def test_boundary_at_085_returns_high(self):
        from atlas_plus.mcp.memory_server.routing import _map_confidence
        assert _map_confidence(0.85, "agent") == "high"

    def test_boundary_at_070_returns_medium(self):
        from atlas_plus.mcp.memory_server.routing import _map_confidence
        assert _map_confidence(0.70, "agent") == "medium"


# ---------------------------------------------------------------------------
# _update_frontmatter_field
# ---------------------------------------------------------------------------

class TestUpdateFrontmatterField:
    def test_adds_new_field(self):
        from atlas_plus.mcp.memory_server.routing import _update_frontmatter_field
        raw = "---\ntitle: Test\n---\n\nbody"
        result = _update_frontmatter_field(raw, "confidence", "high")
        assert "confidence: high" in result

    def test_updates_existing_field(self):
        from atlas_plus.mcp.memory_server.routing import _update_frontmatter_field
        raw = "---\ntitle: Test\nconfidence: medium\n---\n\nbody"
        result = _update_frontmatter_field(raw, "confidence", "low")
        assert "confidence: low" in result
        assert "confidence: medium" not in result

    def test_preserves_body(self):
        from atlas_plus.mcp.memory_server.routing import _update_frontmatter_field
        raw = "---\ntitle: T\n---\n\noriginal body content"
        result = _update_frontmatter_field(raw, "updated", "2026-05-21")
        assert "original body content" in result

    def test_no_frontmatter_returns_unchanged(self):
        from atlas_plus.mcp.memory_server.routing import _update_frontmatter_field
        raw = "just plain text"
        result = _update_frontmatter_field(raw, "confidence", "high")
        assert result == raw

    def test_idempotent_on_same_value(self):
        from atlas_plus.mcp.memory_server.routing import _update_frontmatter_field
        raw = "---\ntitle: T\nconfidence: high\n---\n\nbody"
        result = _update_frontmatter_field(raw, "confidence", "high")
        assert result.count("confidence:") == 1


# ---------------------------------------------------------------------------
# _append_to_frontmatter_list
# ---------------------------------------------------------------------------

class TestAppendToFrontmatterList:
    def test_creates_new_list_field(self):
        from atlas_plus.mcp.memory_server.routing import _append_to_frontmatter_list
        raw = "---\ntitle: T\n---\n\nbody"
        result = _append_to_frontmatter_list(raw, "tags", "sip")
        assert "tags: [sip]" in result

    def test_appends_to_existing_list(self):
        from atlas_plus.mcp.memory_server.routing import _append_to_frontmatter_list
        raw = "---\ntitle: T\ntags: [sip]\n---\n\nbody"
        result = _append_to_frontmatter_list(raw, "tags", "voip")
        assert "sip" in result
        assert "voip" in result

    def test_no_duplicate_added(self):
        from atlas_plus.mcp.memory_server.routing import _append_to_frontmatter_list
        raw = "---\ntitle: T\ntags: [sip]\n---\n\nbody"
        result = _append_to_frontmatter_list(raw, "tags", "sip")
        assert result.count("sip") == 1

    def test_no_frontmatter_returns_unchanged(self):
        from atlas_plus.mcp.memory_server.routing import _append_to_frontmatter_list
        raw = "plain text"
        result = _append_to_frontmatter_list(raw, "tags", "sip")
        assert result == raw


# ---------------------------------------------------------------------------
# downgrade_stale_confirmations
# ---------------------------------------------------------------------------

class TestDowngradeStaleConfirmations:
    def _write_mem(self, path: Path, last_confirmed: str, confidence: str = "high") -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            f"---\ntitle: Test\nconfidence: {confidence}\nlast_confirmed: {last_confirmed}\n---\n\nbody",
            encoding="utf-8",
        )

    def test_empty_root_returns_empty(self, tmp_path):
        from atlas_plus.mcp.memory_server.routing import downgrade_stale_confirmations
        (tmp_path / "empty").mkdir()
        result = downgrade_stale_confirmations(threshold_days=90, memory_root=tmp_path / "empty")
        assert result == []

    def test_fresh_file_not_downgraded(self, tmp_path):
        from atlas_plus.mcp.memory_server.routing import downgrade_stale_confirmations
        from datetime import date, timedelta
        recent = (date.today() - timedelta(days=10)).isoformat()
        self._write_mem(tmp_path / "reference" / "fresh.md", recent)
        result = downgrade_stale_confirmations(threshold_days=90, memory_root=tmp_path)
        assert result == []

    def test_stale_file_flagged_in_dry_run(self, tmp_path):
        from atlas_plus.mcp.memory_server.routing import downgrade_stale_confirmations
        self._write_mem(tmp_path / "reference" / "stale.md", "2025-01-01")
        result = downgrade_stale_confirmations(threshold_days=90, memory_root=tmp_path, dry_run=True)
        assert len(result) == 1
        assert result[0]["old_confidence"] == "high"
        assert result[0]["age_days"] > 90

    def test_dry_run_does_not_write(self, tmp_path):
        from atlas_plus.mcp.memory_server.routing import downgrade_stale_confirmations
        f = tmp_path / "reference" / "stale.md"
        self._write_mem(f, "2025-01-01")
        original = f.read_text(encoding="utf-8")
        downgrade_stale_confirmations(threshold_days=90, memory_root=tmp_path, dry_run=True)
        assert f.read_text(encoding="utf-8") == original

    def test_write_mode_lowers_confidence(self, tmp_path):
        from atlas_plus.mcp.memory_server.routing import downgrade_stale_confirmations
        f = tmp_path / "feedback" / "stale.md"
        self._write_mem(f, "2025-01-01", confidence="high")
        downgrade_stale_confirmations(threshold_days=90, memory_root=tmp_path, dry_run=False)
        assert "confidence: low" in f.read_text(encoding="utf-8")

    def test_already_low_skipped(self, tmp_path):
        from atlas_plus.mcp.memory_server.routing import downgrade_stale_confirmations
        self._write_mem(tmp_path / "reference" / "already.md", "2025-01-01", confidence="low")
        result = downgrade_stale_confirmations(threshold_days=90, memory_root=tmp_path, dry_run=True)
        assert result == []

    def test_no_last_confirmed_skipped(self, tmp_path):
        from atlas_plus.mcp.memory_server.routing import downgrade_stale_confirmations
        f = tmp_path / "reference" / "no-date.md"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("---\ntitle: No date\nconfidence: high\n---\n\nbody", encoding="utf-8")
        result = downgrade_stale_confirmations(threshold_days=90, memory_root=tmp_path, dry_run=True)
        assert result == []

    def test_archive_dir_skipped(self, tmp_path):
        from atlas_plus.mcp.memory_server.routing import downgrade_stale_confirmations
        self._write_mem(tmp_path / "archive" / "old.md", "2025-01-01")
        result = downgrade_stale_confirmations(threshold_days=90, memory_root=tmp_path, dry_run=True)
        assert result == []


# ---------------------------------------------------------------------------
# _slug helper from decision.py
# ---------------------------------------------------------------------------

class TestDecisionSlug:
    def test_basic_slug(self):
        from atlas_plus.mcp.memory_server.tools.decision import _slug
        assert _slug("Use FAISS for VoIP corpus") == "use-faiss-for-voip-corpus"

    def test_special_chars_stripped(self):
        from atlas_plus.mcp.memory_server.tools.decision import _slug
        result = _slug("what is SIP? (version 2)")
        assert "?" not in result
        assert "(" not in result

    def test_truncated_to_60(self):
        from atlas_plus.mcp.memory_server.tools.decision import _slug
        long_title = "a " * 40
        assert len(_slug(long_title)) <= 60

    def test_empty_returns_empty(self):
        from atlas_plus.mcp.memory_server.tools.decision import _slug
        assert _slug("") == ""


# ---------------------------------------------------------------------------
# memory_remember_decision (decision.py) -- gate + validation tests
# ---------------------------------------------------------------------------

class TestMemoryRememberDecision:
    def test_blocked_without_autonomous(self, tmp_path, monkeypatch):
        import pytest
        monkeypatch.setenv("ATLAS_AUTONOMOUS", "off")
        import atlas_plus.mcp.memory_server.tools.decision as mod
        with patch.object(mod, "MEMORY_ROOT", tmp_path), \
             pytest.raises(RuntimeError, match="ATLAS_AUTONOMOUS"):
            mod.memory_remember_decision(title="Use FAISS", decision="Because it is fast.")

    def test_empty_title_returns_error(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ATLAS_AUTONOMOUS", "on")
        import atlas_plus.mcp.memory_server.tools.decision as mod
        with patch.object(mod, "MEMORY_ROOT", tmp_path), \
             patch("atlas_plus.mcp.memory_server.versioning.version_before_write", return_value=None):
            result = mod.memory_remember_decision(title="", decision="Some decision.")
        assert result["ok"] is False
        assert "title" in result["error"]

    def test_empty_decision_returns_error(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ATLAS_AUTONOMOUS", "on")
        import atlas_plus.mcp.memory_server.tools.decision as mod
        with patch.object(mod, "MEMORY_ROOT", tmp_path), \
             patch("atlas_plus.mcp.memory_server.versioning.version_before_write", return_value=None):
            result = mod.memory_remember_decision(title="Use FAISS", decision="")
        assert result["ok"] is False
        assert "decision" in result["error"]

    def test_creates_decision_file(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ATLAS_AUTONOMOUS", "on")
        import atlas_plus.mcp.memory_server.tools.decision as mod
        with patch.object(mod, "MEMORY_ROOT", tmp_path), \
             patch("atlas_plus.mcp.memory_server.versioning.version_before_write", return_value=None):
            result = mod.memory_remember_decision(
                title="Use FAISS",
                decision="FAISS provides sub-millisecond ANN search.",
                revisit_if=["If corpus exceeds 10M vectors"],
            )
        assert result["ok"] is True
        assert (tmp_path / "decisions" / "use-faiss.md").exists()


# ---------------------------------------------------------------------------
# version_before_write (versioning.py)
# ---------------------------------------------------------------------------

class TestVersionBeforeWrite:
    def test_returns_none_when_file_missing(self, tmp_path):
        import atlas_plus.mcp.memory_server.versioning as mod
        with patch.object(mod, "MEMORY_ROOT", tmp_path):
            result = mod.version_before_write(tmp_path / "feedback" / "new.md")
        assert result is None

    def test_creates_version_copy(self, tmp_path):
        import atlas_plus.mcp.memory_server.versioning as mod
        f = tmp_path / "feedback" / "pref.md"
        f.parent.mkdir(parents=True)
        f.write_text("original content", encoding="utf-8")
        with patch.object(mod, "MEMORY_ROOT", tmp_path):
            version = mod.version_before_write(f)
        assert version is not None
        assert version.exists()
        assert version.read_text(encoding="utf-8") == "original content"

    def test_version_in_versions_subdir(self, tmp_path):
        import atlas_plus.mcp.memory_server.versioning as mod
        f = tmp_path / "reference" / "sip.md"
        f.parent.mkdir(parents=True)
        f.write_text("SIP content", encoding="utf-8")
        with patch.object(mod, "MEMORY_ROOT", tmp_path):
            version = mod.version_before_write(f)
        assert "versions" in str(version)

    def test_idempotent_same_content(self, tmp_path):
        import atlas_plus.mcp.memory_server.versioning as mod
        f = tmp_path / "feedback" / "x.md"
        f.parent.mkdir(parents=True)
        f.write_text("same content", encoding="utf-8")
        with patch.object(mod, "MEMORY_ROOT", tmp_path):
            v1 = mod.version_before_write(f)
            v2 = mod.version_before_write(f)
        assert v1 == v2

    def test_increments_version_number_on_change(self, tmp_path):
        import atlas_plus.mcp.memory_server.versioning as mod
        f = tmp_path / "feedback" / "y.md"
        f.parent.mkdir(parents=True)
        f.write_text("content v1", encoding="utf-8")
        with patch.object(mod, "MEMORY_ROOT", tmp_path):
            v1 = mod.version_before_write(f)
            f.write_text("content v2", encoding="utf-8")
            v2 = mod.version_before_write(f)
        assert v1 != v2
        assert "_v1" in str(v1)
        assert "_v2" in str(v2)


# ---------------------------------------------------------------------------
# confirm_fact (routing.py)
# ---------------------------------------------------------------------------

class TestConfirmFact:
    def test_returns_false_for_missing_file(self, tmp_path):
        from atlas_plus.mcp.memory_server.routing import confirm_fact
        result = confirm_fact(tmp_path / "feedback" / "missing.md")
        assert result is False

    def test_updates_last_confirmed(self, tmp_path):
        from atlas_plus.mcp.memory_server.routing import confirm_fact
        from datetime import date
        f = tmp_path / "feedback" / "pref.md"
        f.parent.mkdir(parents=True)
        f.write_text("---\ntitle: T\nconfidence: high\n---\n\nbody", encoding="utf-8")
        result = confirm_fact(f)
        assert result is True
        content = f.read_text(encoding="utf-8")
        assert f"last_confirmed: {date.today().isoformat()}" in content
        assert f"updated: {date.today().isoformat()}" in content

    def test_returns_true_on_success(self, tmp_path):
        from atlas_plus.mcp.memory_server.routing import confirm_fact
        f = tmp_path / "reference" / "doc.md"
        f.parent.mkdir(parents=True)
        f.write_text("---\ntitle: Doc\n---\n\nbody", encoding="utf-8")
        assert confirm_fact(f) is True


# ---------------------------------------------------------------------------
# mark_contradiction (routing.py)
# ---------------------------------------------------------------------------

class TestMarkContradiction:
    def test_both_missing_returns_false_false(self, tmp_path):
        import atlas_plus.mcp.memory_server.routing as mod
        with patch.object(mod, "MEMORY_ROOT", tmp_path):
            a, b = mod.mark_contradiction("reference/a.md", "reference/b.md")
        assert a is False
        assert b is False

    def test_updates_both_files(self, tmp_path):
        import atlas_plus.mcp.memory_server.routing as mod
        ref = tmp_path / "reference"
        ref.mkdir()
        (ref / "a.md").write_text("---\ntitle: A\n---\n\nbody", encoding="utf-8")
        (ref / "b.md").write_text("---\ntitle: B\n---\n\nbody", encoding="utf-8")
        with patch.object(mod, "MEMORY_ROOT", tmp_path):
            ua, ub = mod.mark_contradiction("reference/a.md", "reference/b.md")
        assert ua is True
        assert ub is True
        assert "reference/b.md" in (ref / "a.md").read_text(encoding="utf-8")
        assert "reference/a.md" in (ref / "b.md").read_text(encoding="utf-8")

    def test_only_existing_file_updated(self, tmp_path):
        import atlas_plus.mcp.memory_server.routing as mod
        ref = tmp_path / "reference"
        ref.mkdir()
        (ref / "a.md").write_text("---\ntitle: A\n---\n\nbody", encoding="utf-8")
        with patch.object(mod, "MEMORY_ROOT", tmp_path):
            ua, ub = mod.mark_contradiction("reference/a.md", "reference/missing.md")
        assert ua is True
        assert ub is False


class TestAtomicWriteResidue:
    """Codex audit item 9: every mutation of a memory file must go through
    _atomic_write_text (tmp + os.replace) -- a plain .write_text() truncates
    the file first, so a crash mid-write corrupts the corpus. Static gate so
    residue cannot creep back in."""

    def test_no_direct_write_text_in_routing_mutations(self):
        import inspect

        import atlas_plus.mcp.memory_server.routing as mod
        src = inspect.getsource(mod)
        offenders = []
        for i, line in enumerate(src.splitlines(), 1):
            code = line.split("#", 1)[0]
            if ".write_text(" not in code:
                continue
            if "_atomic_write_text" in code or "tmp.write_text" in code:
                continue
            offenders.append(f"routing.py:{i}: {line.strip()}")
        assert not offenders, (
            "direct .write_text() on a memory file (use _atomic_write_text): "
            + "; ".join(offenders)
        )

    def test_downgrade_write_is_atomic_and_survives_partial(self, tmp_path, monkeypatch):
        """Behavior check: the downgrade rewrite preserves full content."""
        monkeypatch.setenv("ATLAS_AUTONOMOUS", "on")
        md = tmp_path / "reference" / "old.md"
        md.parent.mkdir(parents=True)
        md.write_text(
            "---\ntitle: Old\nconfidence: high\nlast_confirmed: 2020-01-01\n---\n\nbody line\n",
            encoding="utf-8",
        )
        from atlas_plus.mcp.memory_server.routing import downgrade_stale_confirmations
        result = downgrade_stale_confirmations(threshold_days=90, memory_root=tmp_path)
        assert [r["path"] for r in result] == ["reference/old.md"]
        text = md.read_text(encoding="utf-8")
        assert "confidence: low" in text
        assert "body line" in text  # content intact, not truncated
