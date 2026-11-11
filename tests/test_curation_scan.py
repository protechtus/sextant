"""Tests for curation mirror-exclusion (the reference/research scan skip).

The reference/research subtree under MEMORY_ROOT mirrors the standalone research
library (thousands of files) and is NOT curatable memory. Walking it made an
orphan/tag/decay scan pathologically slow (a single web test took 96 minutes).
These tests pin the shared exclusion predicate and prove the orphan and decay
scanners actually skip the mirror while keeping look-alike real memory files.
"""
from __future__ import annotations

from pathlib import Path

from atlas_plus.curation.scan import is_excluded_mirror


# ---------------------------------------------------------------------------
# the predicate
# ---------------------------------------------------------------------------

class TestIsExcludedMirror:
    def test_inside_reference_research_is_excluded(self):
        assert is_excluded_mirror(("reference", "research", "bgp-fundamentals.md"))

    def test_deeply_nested_reference_research_is_excluded(self):
        assert is_excluded_mirror(("reference", "research", "benchmarks", "res-014", "final.md"))

    def test_reference_research_dir_itself(self):
        assert is_excluded_mirror(("reference", "research"))

    def test_reference_top_level_file_is_kept(self):
        # reference/ holds real curatable memory (glossary, people, machines)
        assert not is_excluded_mirror(("reference", "glossary.md"))

    def test_name_lookalike_is_kept(self):
        # prefix match by PARTS, not substring -- a real file that merely starts
        # with "research" in its name must not be excluded
        assert not is_excluded_mirror(("reference", "research-methodology.md"))

    def test_research_under_other_root_is_kept(self):
        assert not is_excluded_mirror(("projects", "research", "notes.md"))

    def test_empty_is_kept(self):
        assert not is_excluded_mirror(())

    def test_accepts_a_list(self):
        assert is_excluded_mirror(["reference", "research", "x.md"])


# ---------------------------------------------------------------------------
# orphan scanner skips the mirror (via _build_inbound_refs)
# ---------------------------------------------------------------------------

def test_build_inbound_refs_skips_reference_research(tmp_path):
    from atlas_plus.curation.orphan import _build_inbound_refs

    (tmp_path / "reference" / "research").mkdir(parents=True)
    (tmp_path / "reference").joinpath("glossary.md").write_text("# Glossary\n", encoding="utf-8")
    (tmp_path / "reference" / "research" / "mirrored-report.md").write_text(
        "# Mirrored Report\n", encoding="utf-8")

    inbound = _build_inbound_refs(tmp_path)

    assert "glossary" in inbound                 # real reference file is scanned
    assert "mirrored-report" not in inbound       # the mirror file is skipped


# ---------------------------------------------------------------------------
# decay scanner skips the mirror
# ---------------------------------------------------------------------------

def test_find_decayed_facts_skips_reference_research(tmp_path, monkeypatch):
    import atlas_plus.curation.decay as decay

    monkeypatch.setattr(decay, "MEMORY_ROOT", tmp_path)

    # A file that DEFINITELY decays: explicit short half-life + a very old updated date.
    body = "---\ntitle: t\nhalf_life: 1\nupdated: 2020-01-01\n---\nbody\n"
    (tmp_path / "feedback").mkdir(parents=True)
    (tmp_path / "feedback" / "real-decayed.md").write_text(body, encoding="utf-8")
    (tmp_path / "reference" / "research").mkdir(parents=True)
    (tmp_path / "reference" / "research" / "mirror-decayed.md").write_text(body, encoding="utf-8")

    paths = [c.path for c in decay.find_decayed_facts(threshold_days=0)]

    assert "feedback/real-decayed.md" in paths            # real memory still flagged
    assert all("reference/research" not in p for p in paths)  # mirror skipped
