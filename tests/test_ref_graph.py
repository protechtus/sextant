"""Tests for atlas_plus.curation.ref_graph -- memory reference graph builder."""
from __future__ import annotations

from pathlib import Path


def _write_md(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\ntitle: Test\n---\n\n{content}\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# build_reference_graph
# ---------------------------------------------------------------------------

class TestBuildReferenceGraph:
    def test_empty_memory_returns_empty_graph(self, tmp_path):
        from atlas_plus.curation.ref_graph import build_reference_graph
        graph = build_reference_graph(tmp_path)
        assert graph == {}

    def test_wiki_link_detected(self, tmp_path):
        from atlas_plus.curation.ref_graph import build_reference_graph
        _write_md(tmp_path / "source.md", "See also [[target-file]] for details.")
        _write_md(tmp_path / "target-file.md", "Target content.")
        graph = build_reference_graph(tmp_path)
        assert "target-file" in graph.get("source", [])

    def test_markdown_link_detected(self, tmp_path):
        from atlas_plus.curation.ref_graph import build_reference_graph
        _write_md(tmp_path / "feedback" / "source.md", "Read [the guide](../reference/guide.md).")
        _write_md(tmp_path / "reference" / "guide.md", "Guide content.")
        graph = build_reference_graph(tmp_path)
        assert "guide" in graph.get("source", [])

    def test_self_reference_not_included(self, tmp_path):
        from atlas_plus.curation.ref_graph import build_reference_graph
        _write_md(tmp_path / "self-ref.md", "This self-ref file mentions [[self-ref]].")
        graph = build_reference_graph(tmp_path)
        assert "self-ref" not in graph.get("self-ref", [])

    def test_short_stems_not_included(self, tmp_path):
        from atlas_plus.curation.ref_graph import build_reference_graph
        # 3-char stem should be ignored
        _write_md(tmp_path / "foo.md", "Content.")
        _write_md(tmp_path / "source.md", "See [[foo]] for more.")
        graph = build_reference_graph(tmp_path)
        # 'foo' is 3 chars < _MIN_STEM_LEN=4, so it's not in the all_stems set
        assert "foo" not in graph.get("source", [])

    def test_skips_archive_dir(self, tmp_path):
        from atlas_plus.curation.ref_graph import build_reference_graph
        _write_md(tmp_path / "archive" / "old.md", "Old content [[target-file]].")
        _write_md(tmp_path / "target-file.md", "Target.")
        graph = build_reference_graph(tmp_path)
        # archive files are skipped -- old should not appear as a node
        assert "old" not in graph

    def test_file_with_no_links_not_in_graph(self, tmp_path):
        from atlas_plus.curation.ref_graph import build_reference_graph
        _write_md(tmp_path / "standalone.md", "This file has no links at all.")
        graph = build_reference_graph(tmp_path)
        assert "standalone" not in graph

    def test_multiple_links_in_one_file(self, tmp_path):
        from atlas_plus.curation.ref_graph import build_reference_graph
        _write_md(tmp_path / "alpha.md", "See [[beta-file]] and [[gamma-file]].")
        _write_md(tmp_path / "beta-file.md", "Beta.")
        _write_md(tmp_path / "gamma-file.md", "Gamma.")
        graph = build_reference_graph(tmp_path)
        refs = graph.get("alpha", [])
        assert "beta-file" in refs
        assert "gamma-file" in refs


# ---------------------------------------------------------------------------
# compute_inbound_counts
# ---------------------------------------------------------------------------

class TestComputeInboundCounts:
    def test_counts_inbound_refs(self):
        from atlas_plus.curation.ref_graph import compute_inbound_counts
        graph = {
            "a": ["target-x", "target-y"],
            "b": ["target-x"],
        }
        counts = compute_inbound_counts(graph)
        assert counts["target-x"] == 2
        assert counts["target-y"] == 1

    def test_empty_graph_returns_empty_counts(self):
        from atlas_plus.curation.ref_graph import compute_inbound_counts
        assert compute_inbound_counts({}) == {}


# ---------------------------------------------------------------------------
# find_hub_files
# ---------------------------------------------------------------------------

class TestFindHubFiles:
    def test_returns_top_n_sorted(self):
        from atlas_plus.curation.ref_graph import find_hub_files
        graph = {
            "a": ["hub-file", "other-file"],
            "b": ["hub-file"],
            "c": ["hub-file", "other-file"],
        }
        hubs = find_hub_files(graph, top_n=2)
        assert len(hubs) == 2
        assert hubs[0]["stem"] == "hub-file"
        assert hubs[0]["inbound_count"] == 3

    def test_returns_empty_for_empty_graph(self):
        from atlas_plus.curation.ref_graph import find_hub_files
        assert find_hub_files({}) == []


# ---------------------------------------------------------------------------
# ref-graph CLI command (CliRunner)
# ---------------------------------------------------------------------------

class TestRefGraphCLI:
    def test_json_output_returns_list(self):
        import json
        from typer.testing import CliRunner
        from atlas_plus.curation.cli import app
        from unittest.mock import patch

        fake_hubs = [{"stem": "hub-file", "inbound_count": 3}]
        with patch("atlas_plus.curation.ref_graph.build_reference_graph",
                   return_value={"source": ["hub-file"], "other": ["hub-file"]}), \
             patch("atlas_plus.curation.ref_graph.find_hub_files", return_value=fake_hubs):
            runner = CliRunner()
            result = runner.invoke(app, ["ref-graph", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert isinstance(data, list)

    def test_empty_graph_shows_no_links_message(self):
        from typer.testing import CliRunner
        from atlas_plus.curation.cli import app
        from unittest.mock import patch

        with patch("atlas_plus.curation.ref_graph.build_reference_graph", return_value={}), \
             patch("atlas_plus.curation.ref_graph.find_hub_files", return_value=[]):
            runner = CliRunner()
            result = runner.invoke(app, ["ref-graph"])
        assert result.exit_code == 0
        assert "No reference links" in result.output

    def test_hub_appears_in_table_output(self):
        from typer.testing import CliRunner
        from atlas_plus.curation.cli import app
        from unittest.mock import patch

        fake_hubs = [{"stem": "hub-file", "inbound_count": 5}]
        with patch("atlas_plus.curation.ref_graph.build_reference_graph",
                   return_value={"a": ["hub-file"]}), \
             patch("atlas_plus.curation.ref_graph.find_hub_files", return_value=fake_hubs):
            runner = CliRunner()
            result = runner.invoke(app, ["ref-graph", "--top", "5"])
        assert result.exit_code == 0
        assert "hub-file" in result.output


# ---------------------------------------------------------------------------
# cross-links CLI command (CliRunner)
# ---------------------------------------------------------------------------

class TestCrossLinksCLI:
    def _write_report(self, d: Path, name: str, url: str) -> None:
        d.mkdir(parents=True, exist_ok=True)
        (d / name).write_text(
            f"---\ntitle: Report\ndate: 2026-05-20\nreferences:\n  - {url}\n---\n\nbody\n",
            encoding="utf-8",
        )

    def test_no_dir_returns_gracefully(self, tmp_path):
        from typer.testing import CliRunner
        from atlas_plus.curation.cli import app

        runner = CliRunner()
        result = runner.invoke(app, ["cross-links", "--dir", str(tmp_path / "nosuchdir")])
        assert result.exit_code == 0
        assert "not found" in result.output.lower()

    def test_json_output_returns_list(self, tmp_path):
        import json
        from typer.testing import CliRunner
        from atlas_plus.curation.cli import app
        from unittest.mock import patch

        rdir = tmp_path / "reports"
        self._write_report(rdir, "r.md", "https://example.com/tool")
        runner = CliRunner()
        with patch("atlas_plus.brain.research.inject_cross_refs", return_value=2):
            result = runner.invoke(
                app, ["cross-links", "--dir", str(rdir), "--json"],
            )
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert isinstance(data, list)
