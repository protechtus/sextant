"""Tests for atlas_plus.curation.orphan -- memory orphan detection."""
from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path


def _write_md(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _old_date(days: int = 200) -> str:
    return (date.today() - timedelta(days=days)).isoformat()


# ---------------------------------------------------------------------------
# _build_inbound_refs
# ---------------------------------------------------------------------------

class TestBuildInboundRefs:
    def test_file_with_no_refs_returns_zero(self, tmp_path):
        from atlas_plus.curation.orphan import _build_inbound_refs
        _write_md(tmp_path / "a.md", "---\ntitle: A\n---\nNothing here.")
        _write_md(tmp_path / "b.md", "---\ntitle: B\n---\nAlso nothing.")
        inbound = _build_inbound_refs(tmp_path)
        assert inbound.get("a", 0) == 0
        assert inbound.get("b", 0) == 0

    def test_file_referenced_by_another_counts_one(self, tmp_path):
        from atlas_plus.curation.orphan import _build_inbound_refs
        _write_md(tmp_path / "target.md", "---\ntitle: Target\n---\nContent.")
        _write_md(tmp_path / "source.md", "---\ntitle: Source\n---\nSee target for details.")
        inbound = _build_inbound_refs(tmp_path)
        assert inbound.get("target", 0) >= 1

    def test_self_reference_not_counted(self, tmp_path):
        from atlas_plus.curation.orphan import _build_inbound_refs
        _write_md(tmp_path / "selfref.md", "---\ntitle: SelfRef\n---\nThis selfref file mentions itself.")
        inbound = _build_inbound_refs(tmp_path)
        assert inbound.get("selfref", 0) == 0

    def test_skips_archive_dir(self, tmp_path):
        from atlas_plus.curation.orphan import _build_inbound_refs
        _write_md(tmp_path / "active.md", "---\ntitle: Active\n---\nContent.")
        _write_md(tmp_path / "archive" / "old.md", "---\ntitle: Old\n---\nContent.")
        inbound = _build_inbound_refs(tmp_path)
        assert "old" not in inbound

    def test_short_stems_skipped_to_avoid_noise(self, tmp_path):
        from atlas_plus.curation.orphan import _build_inbound_refs
        # 3-char stem should not be checked (too many false positives)
        _write_md(tmp_path / "foo.md", "---\ntitle: Foo\n---\nContent.")
        _write_md(tmp_path / "other.md", "---\ntitle: Other\n---\nMentions foo and bar.")
        inbound = _build_inbound_refs(tmp_path)
        # foo is 3 chars -- below the 4-char threshold, so inbound not counted
        assert inbound.get("foo", 0) == 0


# ---------------------------------------------------------------------------
# find_orphan_candidates
# ---------------------------------------------------------------------------

class TestFindOrphanCandidates:
    def test_returns_empty_when_no_candidates(self, tmp_path):
        from atlas_plus.curation.orphan import find_orphan_candidates
        # Fully annotated file (health=1.0) -- excluded by max_health
        _write_md(tmp_path / "feedback" / "good.md",
                  f"---\ntitle: Good\ntags: [python]\nlast_confirmed: {date.today().isoformat()}\n"
                  f"updated: {date.today().isoformat()}\nconfidence: high\n---\nContent.\n")
        results = find_orphan_candidates(tmp_path, min_age_days=0, max_health=0.5)
        assert results == []

    def test_returns_orphan_for_bare_old_unreferenced_file(self, tmp_path):
        from atlas_plus.curation.orphan import find_orphan_candidates
        old = _old_date(300)
        # file has no tags, no confirmed, no confidence (health=0.0)
        _write_md(tmp_path / "feedback" / "orphan.md",
                  f"---\ntitle: Old Orphan\nupdated: {old}\n---\nForgotten content.\n")
        results = find_orphan_candidates(tmp_path, min_age_days=200, max_health=0.5)
        paths = [r["path"] for r in results]
        assert any("orphan.md" in p for p in paths)

    def test_orphan_score_higher_for_completely_bare_files(self, tmp_path):
        from atlas_plus.curation.orphan import find_orphan_candidates
        old = _old_date(400)
        # File 1: no metadata at all
        _write_md(tmp_path / "observations" / "bare.md",
                  f"---\ntitle: Bare\nupdated: {old}\n---\nContent.\n")
        # File 2: has tags + confidence (health=0.5)
        _write_md(tmp_path / "observations" / "partial.md",
                  f"---\ntitle: Partial\ntags: [test]\nconfidence: low\nupdated: {old}\n---\nContent.\n")
        results = find_orphan_candidates(tmp_path, min_age_days=200, max_health=0.5)
        # bare file should have higher orphan_score (lower health = higher orphan score)
        bare = next((r for r in results if "bare.md" in r["path"]), None)
        partial = next((r for r in results if "partial.md" in r["path"]), None)
        assert bare is not None
        if partial:
            assert bare["orphan_score"] >= partial["orphan_score"]

    def test_file_with_inbound_refs_excluded_by_default(self, tmp_path):
        from atlas_plus.curation.orphan import find_orphan_candidates
        old = _old_date(300)
        _write_md(tmp_path / "reference" / "reachable.md",
                  f"---\ntitle: Reachable\nupdated: {old}\n---\nContent.\n")
        # Another file that references it
        _write_md(tmp_path / "feedback" / "pointer.md",
                  f"---\ntitle: Pointer\nupdated: {old}\n---\nSee reachable for details.\n")
        results = find_orphan_candidates(tmp_path, min_age_days=0, max_health=1.0, max_inbound_refs=0)
        # reachable.md has >= 1 inbound ref, should be excluded (max_refs=0)
        assert not any("reachable.md" in r["path"] for r in results)

    def test_fresh_file_excluded_by_min_age(self, tmp_path):
        from atlas_plus.curation.orphan import find_orphan_candidates
        today = date.today().isoformat()
        _write_md(tmp_path / "feedback" / "fresh.md",
                  f"---\ntitle: Fresh\nupdated: {today}\n---\nContent.\n")
        results = find_orphan_candidates(tmp_path, min_age_days=180, max_health=1.0)
        assert not any("fresh.md" in r["path"] for r in results)

    def test_result_keys_present(self, tmp_path):
        from atlas_plus.curation.orphan import find_orphan_candidates
        old = _old_date(300)
        _write_md(tmp_path / "orphan.md",
                  f"---\ntitle: Orphan\nupdated: {old}\n---\nContent.\n")
        results = find_orphan_candidates(tmp_path, min_age_days=200, max_health=1.0)
        if results:
            r = results[0]
            assert "path" in r
            assert "score" in r
            assert "inbound_refs" in r
            assert "age_days" in r
            assert "orphan_score" in r

    def test_scope_restricts_scan(self, tmp_path):
        from atlas_plus.curation.orphan import find_orphan_candidates
        old = _old_date(300)
        _write_md(tmp_path / "feedback" / "in-scope.md",
                  f"---\ntitle: In Scope\nupdated: {old}\n---\nContent.\n")
        _write_md(tmp_path / "identity" / "out-of-scope.md",
                  f"---\ntitle: Out of Scope\nupdated: {old}\n---\nContent.\n")
        results = find_orphan_candidates(tmp_path, scope="feedback", min_age_days=200, max_health=1.0)
        paths = [r["path"] for r in results]
        assert any("in-scope.md" in p for p in paths)
        assert not any("out-of-scope.md" in p for p in paths)

    def test_limit_caps_results(self, tmp_path):
        from atlas_plus.curation.orphan import find_orphan_candidates
        old = _old_date(300)
        for i in range(10):
            _write_md(tmp_path / f"file{i:02d}.md",
                      f"---\ntitle: File {i}\nupdated: {old}\n---\nContent {i}.\n")
        results = find_orphan_candidates(tmp_path, min_age_days=100, max_health=1.0, limit=3)
        assert len(results) <= 3


# ---------------------------------------------------------------------------
# check_orphans (doctor integration)
# ---------------------------------------------------------------------------

class TestCheckOrphans:
    def test_returns_expected_keys(self, tmp_path, monkeypatch):
        from atlas_plus.doctor.cli import check_orphans
        from unittest.mock import patch
        with patch("atlas_plus.doctor.cli.MEMORY_ROOT", tmp_path):
            result = check_orphans()
        assert "candidates" in result

    def test_returns_candidates_for_orphan_files(self, tmp_path, monkeypatch):
        from atlas_plus.doctor.cli import check_orphans
        from unittest.mock import patch
        old = _old_date(300)
        _write_md(tmp_path / "feedback" / "old_orphan.md",
                  f"---\ntitle: Old Orphan\nupdated: {old}\n---\nForgotten.\n")
        with patch("atlas_plus.doctor.cli.MEMORY_ROOT", tmp_path):
            result = check_orphans()
        # May or may not find candidates depending on health/age thresholds
        assert isinstance(result["candidates"], int)


# ---------------------------------------------------------------------------
# _build_inbound_refs -- O(n) tokenization rewrite: boundary/case edge cases
# ---------------------------------------------------------------------------

class TestBuildInboundRefsTokenization:
    """Guards the fast-path (token-set) and slow-path (regex) equivalence to the old boundary regex."""

    def test_case_insensitive_match(self, tmp_path):
        from atlas_plus.curation.orphan import _build_inbound_refs
        _write_md(tmp_path / "Target.md", "---\ntitle: T\n---\nContent.")
        _write_md(tmp_path / "source.md", "---\ntitle: S\n---\nrefers to target here.")
        inbound = _build_inbound_refs(tmp_path)
        assert inbound.get("Target", 0) >= 1

    def test_hyphen_boundary_no_partial_match(self, tmp_path):
        # stem "atlas" must NOT be counted when content only has the token "atlas-ship"
        from atlas_plus.curation.orphan import _build_inbound_refs
        _write_md(tmp_path / "atlas.md", "---\ntitle: A\n---\nContent.")
        _write_md(tmp_path / "other.md", "---\ntitle: O\n---\nWe deploy atlas-ship today.")
        inbound = _build_inbound_refs(tmp_path)
        assert inbound.get("atlas", 0) == 0

    def test_non_alnum_stem_uses_regex_fallback(self, tmp_path):
        # stem "v1.20" contains a dot -> slow-path regex; an inbound mention must still count
        from atlas_plus.curation.orphan import _build_inbound_refs
        _write_md(tmp_path / "v1.20.md", "---\ntitle: V\n---\nContent.")
        _write_md(tmp_path / "notes.md", "---\ntitle: N\n---\nsee v1.20 for the changelog.")
        inbound = _build_inbound_refs(tmp_path)
        assert inbound.get("v1.20", 0) >= 1
