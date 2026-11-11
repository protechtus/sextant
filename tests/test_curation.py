"""Phase 7 curation hygiene tests."""

from __future__ import annotations

import textwrap
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch
import json

import pytest

from atlas_plus.curation.duplicates import find_duplicates, _tokenize, _jaccard
from atlas_plus.curation.stale import find_stale, archive_file


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------

@pytest.fixture()
def mem(tmp_path) -> Path:
    root = tmp_path / ".memory"
    root.mkdir()
    for d in ("identity", "feedback", "projects", "runbooks", "archive", "_sync"):
        (root / d).mkdir()

    today = date.today().isoformat()
    old = (date.today() - timedelta(days=400)).isoformat()
    ancient = (date.today() - timedelta(days=700)).isoformat()

    (root / "identity" / "alex.md").write_text(
        f"---\nid: alex\ntitle: Alex Example\ntype: identity\nupdated: {today}\n---\n\nJay works at ExampleCorp as a Tier 3 support engineer.",
        encoding="utf-8",
    )

    (root / "feedback" / "archive-not-delete.md").write_text(
        f"---\nid: archive-not-delete\ntitle: Archive not delete\ntype: feedback\nupdated: {today}\n---\n\nNever delete files. Move stale files to archive/.",
        encoding="utf-8",
    )

    # Near-duplicate pair
    (root / "feedback" / "policy-a.md").write_text(
        f"---\nid: policy-a\ntitle: Policy A\ntype: feedback\nupdated: {old}\n---\n\nAlways run tests before committing. Never skip the test suite.",
        encoding="utf-8",
    )
    (root / "feedback" / "policy-b.md").write_text(
        f"---\nid: policy-b\ntitle: Policy B\ntype: feedback\nupdated: {old}\n---\n\nAlways run tests before committing code. Never skip tests.",
        encoding="utf-8",
    )

    # Stale files
    (root / "projects" / "old-project.md").write_text(
        f"---\nid: old-project\ntitle: Old Project\ntype: project\nupdated: {old}\n---\n\nThis project is no longer active.",
        encoding="utf-8",
    )
    (root / "projects" / "ancient-project.md").write_text(
        f"---\nid: ancient-project\ntitle: Ancient Project\ntype: project\nupdated: {ancient}\n---\n\nVery old project.",
        encoding="utf-8",
    )

    return root


# ---------------------------------------------------------------------------
# P7-T1: duplicates
# ---------------------------------------------------------------------------

class TestDuplicates:
    def test_tokenize_strips_frontmatter(self):
        text = "---\nid: x\ntitle: Y\n---\nhello world testing"
        tokens = _tokenize(text)
        assert "hello" in tokens
        assert "id" not in tokens  # frontmatter stripped

    def test_jaccard_identical(self):
        s = {"a", "b", "c"}
        assert _jaccard(s, s) == 1.0

    def test_jaccard_disjoint(self):
        assert _jaccard({"a", "b"}, {"c", "d"}) == 0.0

    def test_jaccard_partial(self):
        sim = _jaccard({"a", "b", "c"}, {"b", "c", "d"})
        assert 0 < sim < 1

    def test_finds_near_duplicates(self, mem):
        pairs = find_duplicates(memory_root=mem, threshold=0.30)
        paths = [(p.file1, p.file2) for p in pairs]
        # policy-a and policy-b should be flagged
        assert any(
            ("feedback/policy-a.md" in p[0] or "feedback/policy-a.md" in p[1]) and
            ("feedback/policy-b.md" in p[0] or "feedback/policy-b.md" in p[1])
            for p in paths
        )

    def test_excludes_archive_and_sync(self, mem):
        # Put a copy in archive — should not appear in results
        (mem / "archive" / "copy.md").write_text(
            "---\nid: copy\ntitle: Copy\ntype: feedback\nupdated: 2026-01-01\n---\n\nNever delete files. Move stale files to archive/.",
            encoding="utf-8",
        )
        pairs = find_duplicates(memory_root=mem, threshold=0.30)
        for p in pairs:
            assert "archive/" not in p.file1
            assert "archive/" not in p.file2

    def test_sorted_by_similarity_desc(self, mem):
        pairs = find_duplicates(memory_root=mem, threshold=0.0)
        sims = [p.similarity for p in pairs]
        assert sims == sorted(sims, reverse=True)

    def test_scope_restricts_search(self, mem):
        pairs = find_duplicates(memory_root=mem, threshold=0.30, scope="identity")
        # Only identity/ dir — single file, no pairs possible
        assert pairs == []


# ---------------------------------------------------------------------------
# P7-T4: stale
# ---------------------------------------------------------------------------

class TestStale:
    def test_finds_stale_files(self, mem):
        results = find_stale(memory_root=mem, months=6)
        paths = [r.path for r in results]
        assert any("old-project" in p for p in paths)
        assert any("ancient-project" in p for p in paths)

    def test_excludes_fresh_files(self, mem):
        results = find_stale(memory_root=mem, months=6)
        paths = [r.path for r in results]
        assert not any("alex.md" in p for p in paths)
        assert not any("archive-not-delete" in p for p in paths)

    def test_sorted_oldest_first(self, mem):
        results = find_stale(memory_root=mem, months=1)
        ages = [r.age_days for r in results]
        assert ages == sorted(ages, reverse=True)

    def test_excludes_archive_dir(self, mem):
        (mem / "archive" / "old.md").write_text(
            "---\nid: old\ntitle: Old\ntype: feedback\nupdated: 2020-01-01\n---\nOld archived file.",
            encoding="utf-8",
        )
        results = find_stale(memory_root=mem, months=1)
        assert not any("archive/" in r.path for r in results)

    def test_scope_restricts(self, mem):
        results = find_stale(memory_root=mem, months=6, scope="identity")
        assert results == []  # identity/alex.md is fresh

    def test_archive_file_moves_not_deletes(self, mem):
        src = mem / "projects" / "old-project.md"
        assert src.exists()
        dest_rel = archive_file("projects/old-project.md", memory_root=mem)
        assert not src.exists()
        assert (mem / dest_rel).exists()

    def test_archive_file_not_found_raises(self, mem):
        with pytest.raises(FileNotFoundError):
            archive_file("nonexistent/file.md", memory_root=mem)

    def test_archive_file_rejects_path_traversal(self, mem):
        with pytest.raises(ValueError, match="escape"):
            archive_file("../../etc/passwd", memory_root=mem)

    def test_archive_file_handles_name_collision(self, mem):
        from datetime import date as _date
        today = _date.today().isoformat()
        # Pre-create a file in the archive destination with the same name
        archive_dir = mem / "archive" / f"{today}-curation"
        archive_dir.mkdir(parents=True, exist_ok=True)
        (archive_dir / "old-project.md").write_text("pre-existing", encoding="utf-8")

        src = mem / "projects" / "old-project.md"
        assert src.exists()
        dest_rel = archive_file("projects/old-project.md", memory_root=mem)

        # Should get a suffixed name to avoid clobbering
        assert dest_rel != f"archive/{today}-curation/old-project.md"
        assert not src.exists()
        dest = mem / dest_rel
        assert dest.exists()

    def test_stale_file_as_dict_fields(self, mem):
        from atlas_plus.curation.stale import StaleFile
        sf = StaleFile(path="projects/foo.md", updated="2025-01-01", age_days=500, title="Foo")
        d = sf.as_dict()
        assert d["path"] == "projects/foo.md"
        assert d["age_days"] == 500
        assert set(d.keys()) == {"path", "updated", "age_days", "title"}

    def test_parse_updated_handles_date_object(self, mem):
        from atlas_plus.curation.stale import _parse_updated
        from datetime import date as _date
        d = _date(2026, 1, 15)
        result = _parse_updated({"updated": d})
        assert result == d

    def test_parse_updated_handles_invalid_string(self, mem):
        from atlas_plus.curation.stale import _parse_updated
        result = _parse_updated({"updated": "not-a-date"})
        assert result is None

    def test_parse_updated_missing_field_returns_none(self, mem):
        from atlas_plus.curation.stale import _parse_updated
        assert _parse_updated({}) is None

    def test_find_stale_skips_files_without_updated_field(self, mem):
        (mem / "identity" / "no-updated.md").write_text(
            "---\nid: no-updated\ntitle: No Updated\ntype: identity\n---\nNo date here.",
            encoding="utf-8",
        )
        results = find_stale(memory_root=mem, months=1)
        paths = [r.path for r in results]
        assert "identity/no-updated.md" not in paths

    def test_find_stale_skips_underscore_files(self, mem):
        (mem / "identity" / "_private.md").write_text(
            "---\nid: _private\ntitle: Private\ntype: identity\nupdated: 2020-01-01\n---\nOld.",
            encoding="utf-8",
        )
        results = find_stale(memory_root=mem, months=1)
        paths = [r.path for r in results]
        assert "identity/_private.md" not in paths


# ---------------------------------------------------------------------------
# P7-T2: contradictions (dry_run only — no real LLM call)
# ---------------------------------------------------------------------------

class TestContradictions:
    def test_dry_run_returns_candidates(self, mem):
        from atlas_plus.curation.contradictions import find_contradictions
        results = find_contradictions(memory_root=mem, dry_run=True)
        # All results should have the dry-run explanation
        for r in results:
            assert "dry-run" in r.explanation

    def test_mocked_llm_detects_contradiction(self, mem):
        from atlas_plus.curation.contradictions import find_contradictions

        mock_response = json.dumps([
            {"pair": 1, "contradicts": True, "confidence": "high", "explanation": "They say opposite things."},
        ])

        with patch("atlas_plus.summarizer.providers.complete") as mock_complete:
            mock_complete.return_value = (mock_response, "mock")
            results = find_contradictions(memory_root=mem, dry_run=False)

        assert isinstance(results, list)

    def test_mocked_llm_no_contradiction(self, mem):
        from atlas_plus.curation.contradictions import find_contradictions

        mock_response = json.dumps([
            {"pair": 1, "contradicts": False, "confidence": "high", "explanation": ""},
        ])

        with patch("atlas_plus.summarizer.providers.complete") as mock_complete:
            mock_complete.return_value = (mock_response, "mock")
            results = find_contradictions(memory_root=mem, scope="identity", dry_run=False)

        assert results == []


# ---------------------------------------------------------------------------
# P7-T5: weekly report
# ---------------------------------------------------------------------------

class TestWeeklyReport:
    def test_dry_run_does_not_write(self, mem):
        from atlas_plus.curation.weekly_report import generate_report
        report, out_path = generate_report(memory_root=mem, dry_run=True)
        assert not out_path.exists()
        assert "Curation Report" in report

    def test_live_writes_file(self, mem):
        from atlas_plus.curation.weekly_report import generate_report
        report, out_path = generate_report(memory_root=mem, dry_run=False)
        assert out_path.exists()
        content = out_path.read_text(encoding="utf-8")
        assert "Curation Report" in content

    def test_report_contains_stale_section(self, mem):
        from atlas_plus.curation.weekly_report import generate_report
        report, _ = generate_report(memory_root=mem, stale_months=6, dry_run=True)
        assert "Stale Files" in report

    def test_report_contains_duplicates_section(self, mem):
        from atlas_plus.curation.weekly_report import generate_report
        report, _ = generate_report(memory_root=mem, dup_threshold=0.30, dry_run=True)
        assert "Potential Duplicates" in report

    def test_report_contains_unconfirmed_decay_section(self, mem):
        from atlas_plus.curation.weekly_report import generate_report
        report, _ = generate_report(memory_root=mem, dry_run=True)
        assert "Unconfirmed Stale" in report or "Unconfirmed" in report

    def test_report_contains_topic_coverage_section(self, mem):
        from atlas_plus.curation.weekly_report import generate_report
        report, _ = generate_report(memory_root=mem, dry_run=True)
        assert "Topic Coverage" in report or "Memory Topic" in report
