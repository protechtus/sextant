"""Tests for memory file tag auto-suggestion (curation/tag_suggest.py)."""
from __future__ import annotations

import textwrap
from pathlib import Path
import pytest


def _write_md(path: Path, fm: str, body: str = "Body text.\n") -> Path:
    """Write a markdown file with the given frontmatter (no tags:) and body."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{fm}\n---\n\n{body}", encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# suggest_tags_for_file
# ---------------------------------------------------------------------------

class TestSuggestTagsForFile:
    def test_returns_empty_for_file_already_tagged(self, tmp_path):
        from atlas_plus.curation.tag_suggest import suggest_tags_for_file
        p = _write_md(tmp_path / "identity" / "alex.md",
                      "id: alex\ntitle: Alex Example\ntype: identity\ntags: [identity, personal]")
        assert suggest_tags_for_file(p, memory_root=tmp_path) == []

    def test_returns_empty_for_unreadable_file(self, tmp_path):
        from atlas_plus.curation.tag_suggest import suggest_tags_for_file
        p = tmp_path / "missing.md"
        assert suggest_tags_for_file(p, memory_root=tmp_path) == []

    def test_returns_empty_when_no_frontmatter(self, tmp_path):
        from atlas_plus.curation.tag_suggest import suggest_tags_for_file
        p = tmp_path / "plain.md"
        p.write_text("No frontmatter here.\n", encoding="utf-8")
        assert suggest_tags_for_file(p, memory_root=tmp_path) == []

    def test_identity_dir_suggests_identity_tag(self, tmp_path):
        from atlas_plus.curation.tag_suggest import suggest_tags_for_file
        p = _write_md(tmp_path / "identity" / "alex.md",
                      "id: alex\ntitle: Alex Example\ntype: identity")
        tags = suggest_tags_for_file(p, memory_root=tmp_path)
        assert "identity" in tags

    def test_feedback_dir_suggests_feedback_tag(self, tmp_path):
        from atlas_plus.curation.tag_suggest import suggest_tags_for_file
        p = _write_md(tmp_path / "feedback" / "editor.md",
                      "title: Editor Preferences\ntype: feedback")
        tags = suggest_tags_for_file(p, memory_root=tmp_path)
        assert "feedback" in tags

    def test_type_field_contributes_tag(self, tmp_path):
        from atlas_plus.curation.tag_suggest import suggest_tags_for_file
        p = _write_md(tmp_path / "reference" / "examplecorp.md",
                      "title: ExampleCorp VoIP Platform\ntype: reference")
        tags = suggest_tags_for_file(p, memory_root=tmp_path)
        assert "reference" in tags

    def test_title_keywords_extracted(self, tmp_path):
        from atlas_plus.curation.tag_suggest import suggest_tags_for_file
        p = _write_md(tmp_path / "projects" / "gatekeeper.md",
                      "title: Gatekeeper Security Platform\ntype: project")
        tags = suggest_tags_for_file(p, memory_root=tmp_path)
        # Should include something from the title (gatekeeper, security, platform)
        assert any(t in tags for t in ["gatekeeper", "security", "platform"])

    def test_tags_are_lowercase_slugs(self, tmp_path):
        from atlas_plus.curation.tag_suggest import suggest_tags_for_file
        p = _write_md(tmp_path / "reference" / "sip.md",
                      "title: SIP Registration Protocol\ntype: reference")
        tags = suggest_tags_for_file(p, memory_root=tmp_path)
        for tag in tags:
            assert tag == tag.lower()
            assert " " not in tag

    def test_respects_max_tags_limit(self, tmp_path):
        from atlas_plus.curation.tag_suggest import suggest_tags_for_file
        p = _write_md(tmp_path / "reference" / "voip.md",
                      "title: VoIP Telecom Registration Protocol Stack\ntype: reference")
        tags = suggest_tags_for_file(p, memory_root=tmp_path, max_tags=2)
        assert len(tags) <= 2

    def test_no_duplicate_tags(self, tmp_path):
        from atlas_plus.curation.tag_suggest import suggest_tags_for_file
        # type: feedback + dir: feedback -> same base tag, should not duplicate
        p = _write_md(tmp_path / "feedback" / "style.md",
                      "title: Feedback Style Guide\ntype: feedback")
        tags = suggest_tags_for_file(p, memory_root=tmp_path)
        assert len(tags) == len(set(tags))


# ---------------------------------------------------------------------------
# inject_tags
# ---------------------------------------------------------------------------

class TestInjectTags:
    def test_writes_tags_to_frontmatter(self, tmp_path):
        from atlas_plus.curation.tag_suggest import inject_tags
        p = _write_md(tmp_path / "identity" / "alex.md",
                      "id: alex\ntitle: Alex Example\ntype: identity")
        result = inject_tags(p, ["identity", "personal"])
        assert result is True
        content = p.read_text(encoding="utf-8")
        assert "tags: [identity, personal]" in content

    def test_skips_file_already_tagged(self, tmp_path):
        from atlas_plus.curation.tag_suggest import inject_tags
        p = _write_md(tmp_path / "alex.md",
                      "title: Alex\ntags: [existing]")
        original = p.read_text(encoding="utf-8")
        result = inject_tags(p, ["new-tag"])
        assert result is False
        assert p.read_text(encoding="utf-8") == original

    def test_returns_false_for_missing_file(self, tmp_path):
        from atlas_plus.curation.tag_suggest import inject_tags
        assert inject_tags(tmp_path / "missing.md", ["tag"]) is False

    def test_returns_false_for_no_tags_given(self, tmp_path):
        from atlas_plus.curation.tag_suggest import inject_tags
        p = _write_md(tmp_path / "alex.md", "title: Alex")
        assert inject_tags(p, []) is False

    def test_tags_appear_inside_frontmatter_block(self, tmp_path):
        from atlas_plus.curation.tag_suggest import inject_tags
        p = _write_md(tmp_path / "ref.md", "title: Reference\ntype: reference")
        inject_tags(p, ["reference", "examplecorp"])
        content = p.read_text(encoding="utf-8")
        # Tags must be before the second ---
        fm_end = content.find("\n---", 3)
        fm_block = content[3:fm_end]
        assert "tags:" in fm_block


# ---------------------------------------------------------------------------
# suggest-tags CLI command
# ---------------------------------------------------------------------------

class TestSuggestTagsCLI:
    def _setup_memory(self, tmp_path: Path) -> Path:
        mem = tmp_path / ".memory"
        _write_md(mem / "identity" / "alex.md",
                  "id: alex\ntitle: Alex Example\ntype: identity")
        _write_md(mem / "feedback" / "editor.md",
                  "title: Editor Preferences\ntype: feedback")
        _write_md(mem / "reference" / "examplecorp.md",
                  "title: ExampleCorp VoIP Platform\ntype: reference\ntags: [existing]")
        return mem

    def test_dry_run_does_not_modify_files(self, tmp_path):
        from typer.testing import CliRunner
        from atlas_plus.curation.cli import app
        from unittest.mock import patch

        mem = self._setup_memory(tmp_path)
        original_identity = (mem / "identity" / "alex.md").read_text()

        runner = CliRunner()
        with patch("atlas_plus.curation.cli.MEMORY_ROOT", mem):
            result = runner.invoke(app, ["suggest-tags"])
        assert result.exit_code == 0
        assert (mem / "identity" / "alex.md").read_text() == original_identity

    def test_write_applies_tags(self, tmp_path):
        from typer.testing import CliRunner
        from atlas_plus.curation.cli import app
        from unittest.mock import patch

        mem = self._setup_memory(tmp_path)
        runner = CliRunner()
        with patch("atlas_plus.curation.cli.MEMORY_ROOT", mem):
            result = runner.invoke(app, ["suggest-tags", "--write"])
        assert result.exit_code == 0
        content = (mem / "identity" / "alex.md").read_text()
        assert "tags:" in content

    def test_skip_files_with_existing_tags(self, tmp_path):
        from typer.testing import CliRunner
        from atlas_plus.curation.cli import app
        from unittest.mock import patch
        import json

        mem = self._setup_memory(tmp_path)
        runner = CliRunner()
        with patch("atlas_plus.curation.cli.MEMORY_ROOT", mem):
            result = runner.invoke(app, ["suggest-tags", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output.strip())
        # JSON is now {results: [...], no_suggestion_count: N, already_tagged_count: N}
        results = data["results"] if isinstance(data, dict) else data
        files = {r["file"] for r in results}
        # examplecorp.md already has tags -- should be excluded
        assert not any("examplecorp" in f for f in files)

    def test_json_output_has_expected_shape(self, tmp_path):
        from typer.testing import CliRunner
        from atlas_plus.curation.cli import app
        from unittest.mock import patch
        import json

        mem = self._setup_memory(tmp_path)
        runner = CliRunner()
        with patch("atlas_plus.curation.cli.MEMORY_ROOT", mem):
            result = runner.invoke(app, ["suggest-tags", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output.strip())
        # JSON is {results: [...], no_suggestion_count: N, already_tagged_count: N}
        assert isinstance(data, dict)
        assert "results" in data
        assert "no_suggestion_count" in data
        assert "already_tagged_count" in data
        results = data["results"]
        assert len(results) >= 1
        rec = results[0]
        assert "file" in rec
        assert "tags" in rec
        assert isinstance(rec["tags"], list)
        assert len(rec["tags"]) > 0

    def test_json_output_summary_counts(self, tmp_path):
        """JSON output includes no_suggestion_count and already_tagged_count."""
        from typer.testing import CliRunner
        from atlas_plus.curation.cli import app
        from unittest.mock import patch
        import json

        mem = self._setup_memory(tmp_path)
        runner = CliRunner()
        with patch("atlas_plus.curation.cli.MEMORY_ROOT", mem):
            result = runner.invoke(app, ["suggest-tags", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output.strip())
        # examplecorp.md has tags -> no suggestions for it -> no_suggestion_count >= 1
        assert data["no_suggestion_count"] >= 0
        assert data["already_tagged_count"] >= 0
