"""Tests for curation/ref_graph.py: build_reference_graph, compute_inbound_counts, find_hub_files."""

from __future__ import annotations

from pathlib import Path


def _write_md(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


# ---------------------------------------------------------------------------
# build_reference_graph
# ---------------------------------------------------------------------------

class TestBuildReferenceGraph:
    def test_empty_dir_returns_empty(self, tmp_path):
        from atlas_plus.curation.ref_graph import build_reference_graph
        result = build_reference_graph(tmp_path)
        assert result == {}

    def test_file_with_no_links_not_in_graph(self, tmp_path):
        from atlas_plus.curation.ref_graph import build_reference_graph
        _write_md(tmp_path / "reference" / "standalone.md", "Just plain text with no links.")
        result = build_reference_graph(tmp_path)
        assert result == {}

    def test_wiki_link_captured(self, tmp_path):
        from atlas_plus.curation.ref_graph import build_reference_graph
        _write_md(tmp_path / "reference" / "source-doc.md", "See [[target-file]] for details.")
        _write_md(tmp_path / "reference" / "target-file.md", "Target content.")
        result = build_reference_graph(tmp_path)
        assert "source-doc" in result
        assert "target-file" in result["source-doc"]

    def test_md_link_captured(self, tmp_path):
        from atlas_plus.curation.ref_graph import build_reference_graph
        _write_md(tmp_path / "reference" / "source-doc.md",
                  "See [Title](other-reference.md) for more.")
        _write_md(tmp_path / "reference" / "other-reference.md", "Content here.")
        result = build_reference_graph(tmp_path)
        assert "source-doc" in result
        assert "other-reference" in result["source-doc"]

    def test_wiki_link_with_alias(self, tmp_path):
        from atlas_plus.curation.ref_graph import build_reference_graph
        _write_md(tmp_path / "reference" / "source-doc.md",
                  "See [[atlas-brain|Brain system]] for details.")
        _write_md(tmp_path / "reference" / "atlas-brain.md", "Brain content.")
        result = build_reference_graph(tmp_path)
        assert "source-doc" in result
        assert "atlas-brain" in result["source-doc"]

    def test_self_link_not_counted(self, tmp_path):
        from atlas_plus.curation.ref_graph import build_reference_graph
        _write_md(tmp_path / "reference" / "self-link.md",
                  "This [[self-link]] is a self-reference.")
        result = build_reference_graph(tmp_path)
        assert result == {}

    def test_archive_dir_excluded(self, tmp_path):
        from atlas_plus.curation.ref_graph import build_reference_graph
        _write_md(tmp_path / "archive" / "archived-doc.md",
                  "See [[other-file]] for info.")
        _write_md(tmp_path / "reference" / "other-file.md", "Content.")
        result = build_reference_graph(tmp_path)
        assert result == {}

    def test_short_stem_targets_excluded(self, tmp_path):
        from atlas_plus.curation.ref_graph import build_reference_graph
        _write_md(tmp_path / "reference" / "source-doc.md",
                  "See [[sip]] for protocol info.")
        _write_md(tmp_path / "reference" / "sip.md", "SIP protocol.")
        result = build_reference_graph(tmp_path)
        # "sip" is 3 chars, below 4-char minimum -- should not appear as a ref target
        if "source-doc" in result:
            assert "sip" not in result["source-doc"]

    def test_refs_are_sorted(self, tmp_path):
        from atlas_plus.curation.ref_graph import build_reference_graph
        _write_md(tmp_path / "reference" / "source-doc.md",
                  "See [[brain-system]] and [[atlas-memory]] here.")
        _write_md(tmp_path / "reference" / "brain-system.md", "Brain.")
        _write_md(tmp_path / "reference" / "atlas-memory.md", "Memory.")
        result = build_reference_graph(tmp_path)
        if "source-doc" in result:
            refs = result["source-doc"]
            assert refs == sorted(refs)


# ---------------------------------------------------------------------------
# compute_inbound_counts
# ---------------------------------------------------------------------------

class TestComputeInboundCounts:
    def test_empty_graph_returns_empty(self):
        from atlas_plus.curation.ref_graph import compute_inbound_counts
        assert compute_inbound_counts({}) == {}

    def test_counts_single_reference(self):
        from atlas_plus.curation.ref_graph import compute_inbound_counts
        graph = {"source": ["target-file"]}
        counts = compute_inbound_counts(graph)
        assert counts["target-file"] == 1

    def test_counts_multiple_references_same_target(self):
        from atlas_plus.curation.ref_graph import compute_inbound_counts
        graph = {
            "source-a": ["atlas-brain"],
            "source-b": ["atlas-brain", "other-file"],
        }
        counts = compute_inbound_counts(graph)
        assert counts["atlas-brain"] == 2
        assert counts["other-file"] == 1

    def test_unreferenced_not_in_counts(self):
        from atlas_plus.curation.ref_graph import compute_inbound_counts
        graph = {"source": ["target-file"]}
        counts = compute_inbound_counts(graph)
        assert "source" not in counts


# ---------------------------------------------------------------------------
# find_hub_files
# ---------------------------------------------------------------------------

class TestFindHubFiles:
    def test_empty_graph_returns_empty(self):
        from atlas_plus.curation.ref_graph import find_hub_files
        assert find_hub_files({}) == []

    def test_returns_top_n(self):
        from atlas_plus.curation.ref_graph import find_hub_files
        graph = {
            "a": ["atlas-hub"],
            "b": ["atlas-hub"],
            "c": ["atlas-hub"],
            "d": ["other-file"],
        }
        hubs = find_hub_files(graph, top_n=1)
        assert len(hubs) == 1
        assert hubs[0]["stem"] == "atlas-hub"

    def test_sorted_by_inbound_desc(self):
        from atlas_plus.curation.ref_graph import find_hub_files
        graph = {
            "x": ["brain-core"],
            "y": ["brain-core"],
            "z": ["brain-core"],
            "q": ["side-file"],
        }
        hubs = find_hub_files(graph)
        assert hubs[0]["inbound_count"] >= hubs[-1]["inbound_count"]

    def test_result_has_expected_keys(self):
        from atlas_plus.curation.ref_graph import find_hub_files
        graph = {"source": ["atlas-core"]}
        hubs = find_hub_files(graph)
        assert len(hubs) == 1
        assert "stem" in hubs[0]
        assert "inbound_count" in hubs[0]
        assert hubs[0]["inbound_count"] == 1
