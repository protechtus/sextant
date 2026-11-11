"""ATL-018 / audit HIGH-3 completion: curation .memory writers honor the autonomous kill switch."""
from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from atlas_plus.curation import cli, tag_suggest


class TestGapStubs:
    def test_blocked_when_off(self, tmp_path):
        with patch.dict(os.environ, {"ATLAS_AUTONOMOUS": "off"}):
            with pytest.raises(RuntimeError, match="blocked"):
                cli._create_gap_stubs([], tmp_path / "gaps", "2026-06-05")
        assert not (tmp_path / "gaps").exists()   # nothing created

    def test_allowed_when_on(self, tmp_path):
        with patch.dict(os.environ, {"ATLAS_AUTONOMOUS": "on"}):
            assert cli._create_gap_stubs([], tmp_path / "gaps", "2026-06-05") == []


class TestInjectTags:
    def _md(self, tmp_path):
        f = tmp_path / "note.md"
        f.write_text("---\ntitle: note\n---\nbody text", encoding="utf-8")
        return f

    def test_blocked_when_off(self, tmp_path):
        f = self._md(tmp_path)
        with patch.dict(os.environ, {"ATLAS_AUTONOMOUS": "off"}):
            with pytest.raises(RuntimeError, match="blocked"):
                tag_suggest.inject_tags(f, ["sip", "voip"])
        assert "sip" not in f.read_text(encoding="utf-8")   # unmodified

    def test_allowed_when_on(self, tmp_path):
        f = self._md(tmp_path)
        with patch.dict(os.environ, {"ATLAS_AUTONOMOUS": "on"}):
            assert tag_suggest.inject_tags(f, ["sip"]) is True
        assert "sip" in f.read_text(encoding="utf-8")

    def test_empty_tags_no_gate(self, tmp_path):
        # No tags -> returns False before the gate (no write attempted), even when off.
        f = self._md(tmp_path)
        with patch.dict(os.environ, {"ATLAS_AUTONOMOUS": "off"}):
            assert tag_suggest.inject_tags(f, []) is False
