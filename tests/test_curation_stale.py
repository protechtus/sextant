"""Tests for atlas_plus.curation.stale -- stale file detection and archival."""
from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path


def _write_md(path: Path, frontmatter: str, body: str = "Content.") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{frontmatter}\n---\n\n{body}\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# find_stale
# ---------------------------------------------------------------------------

class TestFindStale:
    def test_empty_directory_returns_empty(self, tmp_path):
        from atlas_plus.curation.stale import find_stale
        result = find_stale(memory_root=tmp_path)
        assert result == []

    def test_detects_old_file(self, tmp_path):
        from atlas_plus.curation.stale import find_stale
        old_date = (date.today() - timedelta(days=200)).isoformat()
        _write_md(tmp_path / "old.md", f"title: Old\nupdated: {old_date}")
        results = find_stale(memory_root=tmp_path, months=1)
        assert len(results) == 1
        assert results[0].path == "old.md"
        assert results[0].age_days >= 200

    def test_skips_recent_file(self, tmp_path):
        from atlas_plus.curation.stale import find_stale
        recent = date.today().isoformat()
        _write_md(tmp_path / "fresh.md", f"title: Fresh\nupdated: {recent}")
        results = find_stale(memory_root=tmp_path, months=6)
        assert results == []

    def test_skips_file_without_updated_field(self, tmp_path):
        from atlas_plus.curation.stale import find_stale
        _write_md(tmp_path / "noupdate.md", "title: NoUpdate")
        results = find_stale(memory_root=tmp_path, months=1)
        assert results == []

    def test_skips_underscore_files(self, tmp_path):
        from atlas_plus.curation.stale import find_stale
        old_date = (date.today() - timedelta(days=300)).isoformat()
        _write_md(tmp_path / "_skip.md", f"title: Skip\nupdated: {old_date}")
        results = find_stale(memory_root=tmp_path, months=1)
        assert results == []

    def test_skips_archive_directory(self, tmp_path):
        from atlas_plus.curation.stale import find_stale
        old_date = (date.today() - timedelta(days=300)).isoformat()
        _write_md(tmp_path / "archive" / "archived.md", f"title: A\nupdated: {old_date}")
        _write_md(tmp_path / "normal" / "current.md", f"title: C\nupdated: {old_date}")
        results = find_stale(memory_root=tmp_path, months=1)
        paths = [r.path for r in results]
        assert not any("archive" in p for p in paths)
        assert any("current" in p for p in paths)

    def test_sorted_by_age_descending(self, tmp_path):
        from atlas_plus.curation.stale import find_stale
        old_date = (date.today() - timedelta(days=400)).isoformat()
        newer_date = (date.today() - timedelta(days=200)).isoformat()
        _write_md(tmp_path / "newer.md", f"title: A\nupdated: {newer_date}")
        _write_md(tmp_path / "older.md", f"title: B\nupdated: {old_date}")
        results = find_stale(memory_root=tmp_path, months=1)
        assert len(results) == 2
        assert results[0].age_days >= results[1].age_days

    def test_scope_restricts_search(self, tmp_path):
        from atlas_plus.curation.stale import find_stale
        old_date = (date.today() - timedelta(days=300)).isoformat()
        _write_md(tmp_path / "project" / "in_scope.md", f"title: A\nupdated: {old_date}")
        _write_md(tmp_path / "other" / "out_scope.md", f"title: B\nupdated: {old_date}")
        results = find_stale(memory_root=tmp_path, months=1, scope="project")
        paths = [r.path for r in results]
        assert any("in_scope" in p for p in paths)
        assert not any("out_scope" in p for p in paths)


# ---------------------------------------------------------------------------
# StaleFile.as_dict
# ---------------------------------------------------------------------------

class TestStaleFileAsDict:
    def test_returns_all_expected_keys(self, tmp_path):
        from atlas_plus.curation.stale import find_stale
        old_date = (date.today() - timedelta(days=300)).isoformat()
        _write_md(tmp_path / "s.md", f"title: S\nupdated: {old_date}")
        results = find_stale(memory_root=tmp_path, months=1)
        assert len(results) == 1
        d = results[0].as_dict()
        for key in ("path", "updated", "age_days", "title"):
            assert key in d

    def test_path_uses_forward_slashes(self, tmp_path):
        from atlas_plus.curation.stale import find_stale
        old_date = (date.today() - timedelta(days=300)).isoformat()
        sub = tmp_path / "sub"
        sub.mkdir()
        _write_md(sub / "file.md", f"title: F\nupdated: {old_date}")
        results = find_stale(memory_root=tmp_path, months=1)
        assert "\\" not in results[0].as_dict()["path"]


# ---------------------------------------------------------------------------
# archive_file
# ---------------------------------------------------------------------------

class TestArchiveFile:
    def test_moves_file_to_archive(self, tmp_path):
        from atlas_plus.curation.stale import archive_file
        f = tmp_path / "identity" / "role.md"
        f.parent.mkdir()
        f.write_text("---\ntitle: Role\n---\n\nContent.\n", encoding="utf-8")
        dest = archive_file("identity/role.md", memory_root=tmp_path)
        assert not f.exists()
        assert "archive" in dest

    def test_raises_on_path_traversal(self, tmp_path):
        from atlas_plus.curation.stale import archive_file
        import pytest
        with pytest.raises((ValueError, Exception)):
            archive_file("../../etc/passwd", memory_root=tmp_path)

    def test_raises_on_missing_file(self, tmp_path):
        from atlas_plus.curation.stale import archive_file
        import pytest
        with pytest.raises(FileNotFoundError):
            archive_file("nonexistent.md", memory_root=tmp_path)
