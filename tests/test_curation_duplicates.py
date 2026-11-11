"""Tests for atlas_plus.curation.duplicates -- Jaccard-based duplicate detection."""
from __future__ import annotations

from pathlib import Path


def _write_md(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

class TestTokenize:
    def test_strips_frontmatter(self):
        from atlas_plus.curation.duplicates import _tokenize
        text = "---\ntitle: Test\n---\nHello world networking."
        tokens = _tokenize(text)
        assert "hello" in tokens or "world" in tokens or "networking" in tokens
        assert "title" not in tokens or "test" not in tokens

    def test_removes_stopwords(self):
        from atlas_plus.curation.duplicates import _tokenize
        text = "the quick brown fox and the lazy dog"
        tokens = _tokenize(text)
        assert "the" not in tokens
        assert "and" not in tokens

    def test_filters_single_chars(self):
        from atlas_plus.curation.duplicates import _tokenize
        tokens = _tokenize("a b c technical reference")
        short = {t for t in tokens if len(t) <= 2}
        assert not short


class TestJaccard:
    def test_identical_sets_return_one(self):
        from atlas_plus.curation.duplicates import _jaccard
        s = {"networking", "protocol", "sip"}
        assert _jaccard(s, s) == 1.0

    def test_disjoint_sets_return_zero(self):
        from atlas_plus.curation.duplicates import _jaccard
        assert _jaccard({"abc", "def"}, {"xyz", "uvw"}) == 0.0

    def test_both_empty_returns_one(self):
        from atlas_plus.curation.duplicates import _jaccard
        assert _jaccard(set(), set()) == 1.0

    def test_one_empty_returns_zero(self):
        from atlas_plus.curation.duplicates import _jaccard
        assert _jaccard({"abc"}, set()) == 0.0
        assert _jaccard(set(), {"abc"}) == 0.0

    def test_partial_overlap(self):
        from atlas_plus.curation.duplicates import _jaccard
        a = {"networking", "protocol", "sip", "voip"}
        b = {"networking", "protocol", "rtp", "media"}
        sim = _jaccard(a, b)
        assert 0.0 < sim < 1.0


# ---------------------------------------------------------------------------
# find_duplicates
# ---------------------------------------------------------------------------

class TestFindDuplicates:
    def test_empty_directory_returns_empty(self, tmp_path):
        from atlas_plus.curation.duplicates import find_duplicates
        result = find_duplicates(memory_root=tmp_path)
        assert result == []

    def test_single_file_returns_empty(self, tmp_path):
        from atlas_plus.curation.duplicates import find_duplicates
        _write_md(tmp_path / "solo.md",
                  "---\ntitle: Solo\n---\nUnique content here.\n")
        result = find_duplicates(memory_root=tmp_path)
        assert result == []

    def test_detects_near_identical_files(self, tmp_path):
        from atlas_plus.curation.duplicates import find_duplicates
        body = "networking protocols voip sip rtp sdp registration call setup teardown"
        _write_md(tmp_path / "a.md", f"---\ntitle: A\n---\n{body}\n")
        _write_md(tmp_path / "b.md", f"---\ntitle: B\n---\n{body} extra\n")
        result = find_duplicates(memory_root=tmp_path, threshold=0.5)
        assert len(result) >= 1
        sim = result[0].similarity
        assert sim >= 0.5

    def test_skips_dissimilar_files(self, tmp_path):
        from atlas_plus.curation.duplicates import find_duplicates
        _write_md(tmp_path / "sip.md",
                  "---\ntitle: SIP\n---\nSession Initiation Protocol registration.\n")
        _write_md(tmp_path / "python.md",
                  "---\ntitle: Python\n---\nVariables functions classes decorators.\n")
        result = find_duplicates(memory_root=tmp_path, threshold=0.8)
        assert result == []

    def test_skips_archive_directory(self, tmp_path):
        from atlas_plus.curation.duplicates import find_duplicates
        body = "identical content voip networking protocol sip registration"
        _write_md(tmp_path / "archive" / "old.md", f"---\ntitle: O\n---\n{body}\n")
        _write_md(tmp_path / "current.md", f"---\ntitle: C\n---\n{body}\n")
        result = find_duplicates(memory_root=tmp_path, threshold=0.5)
        for pair in result:
            assert "archive" not in pair.file1
            assert "archive" not in pair.file2

    def test_skips_underscore_files(self, tmp_path):
        from atlas_plus.curation.duplicates import find_duplicates
        body = "identical content voip networking protocol"
        _write_md(tmp_path / "_index.md", f"---\ntitle: I\n---\n{body}\n")
        _write_md(tmp_path / "real.md", f"---\ntitle: R\n---\n{body}\n")
        result = find_duplicates(memory_root=tmp_path, threshold=0.5)
        for pair in result:
            assert not pair.file1.startswith("_")
            assert not pair.file2.startswith("_")

    def test_sorted_by_similarity_descending(self, tmp_path):
        from atlas_plus.curation.duplicates import find_duplicates
        body = "networking voip sip rtp protocol"
        _write_md(tmp_path / "a.md", f"---\ntitle: A\n---\n{body}\n")
        _write_md(tmp_path / "b.md", f"---\ntitle: B\n---\n{body}\n")
        _write_md(tmp_path / "c.md", f"---\ntitle: C\n---\n{body} codecs audio\n")
        result = find_duplicates(memory_root=tmp_path, threshold=0.3)
        for i in range(len(result) - 1):
            assert result[i].similarity >= result[i + 1].similarity

    def test_respects_scope_parameter(self, tmp_path):
        from atlas_plus.curation.duplicates import find_duplicates
        body = "networking voip sip registration protocol"
        _write_md(tmp_path / "project" / "a.md", f"---\ntitle: A\n---\n{body}\n")
        _write_md(tmp_path / "project" / "b.md", f"---\ntitle: B\n---\n{body}\n")
        _write_md(tmp_path / "feedback" / "c.md", f"---\ntitle: C\n---\n{body}\n")
        result_scoped = find_duplicates(memory_root=tmp_path, threshold=0.5, scope="project")
        result_all = find_duplicates(memory_root=tmp_path, threshold=0.5)
        assert len(result_scoped) <= len(result_all)
        for pair in result_scoped:
            assert "project" in pair.file1 or "project" in pair.file2


# ---------------------------------------------------------------------------
# DuplicatePair.as_dict
# ---------------------------------------------------------------------------

class TestDuplicatePairAsDict:
    def test_returns_all_expected_keys(self, tmp_path):
        from atlas_plus.curation.duplicates import find_duplicates
        body = "networking voip sip rtp protocol registration codecs"
        _write_md(tmp_path / "a.md", f"---\ntitle: A\n---\n{body}\n")
        _write_md(tmp_path / "b.md", f"---\ntitle: B\n---\n{body}\n")
        pairs = find_duplicates(memory_root=tmp_path, threshold=0.5)
        assert len(pairs) >= 1
        d = pairs[0].as_dict()
        for key in ("file1", "file2", "similarity", "snippet1", "snippet2"):
            assert key in d

    def test_similarity_is_rounded(self, tmp_path):
        from atlas_plus.curation.duplicates import find_duplicates
        body = "networking voip sip rtp protocol registration codecs"
        _write_md(tmp_path / "a.md", f"---\ntitle: A\n---\n{body}\n")
        _write_md(tmp_path / "b.md", f"---\ntitle: B\n---\n{body}\n")
        pairs = find_duplicates(memory_root=tmp_path, threshold=0.5)
        if pairs:
            sim_str = str(pairs[0].as_dict()["similarity"])
            assert len(sim_str.split(".")[-1]) <= 3
