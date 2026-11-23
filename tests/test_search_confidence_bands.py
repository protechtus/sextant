"""ATL-029: the four confidence bands of memory_search_tool.

The bands live inline in server.memory_search_tool and are read by every
client (Brain CLI, Telegram, web UI) to decide whether the model answers,
hedges, or asks for clarification. The earlier regression (a lone FTS hit
kept its raw negative rank and read as "starved") was fixed in fts.py, but
the banding itself was only ever exercised through a local re-implementation
in test_fts.py. These tests drive the REAL function at every boundary.

Documented contract (server.memory_search_tool docstring):
  high    -- top score >= 0.7
  medium  -- 0.4 <= top score < 0.7
  low     -- 0.2 <= top score < 0.4
  starved -- top score < 0.2, or no results at all
"""

from contextlib import contextmanager
from unittest.mock import patch

import pytest


def _hits(*scores: float) -> list[dict]:
    return [
        {"path": f"reference/hit{i}.md", "snippet": "...", "score": s}
        for i, s in enumerate(scores)
    ]


@contextmanager
def _search_returning(results, retrieval="fts-only"):
    """Run memory_search_tool against canned hybrid-search results.

    Neutralises the two side channels (search log, temporal context) so the
    only thing under test is the band computation and the pass-through.
    """
    from atlas_plus.mcp.memory_server import server

    with patch("atlas_plus.mcp.memory_server.tools.search.memory_search_hybrid",
               return_value=(results, retrieval)) as hybrid, \
         patch("atlas_plus.mcp.memory_server.tools.search_log.log_search") as logged, \
         patch.object(server, "_temporal_context_for_query", return_value=[]):
        yield server, hybrid, logged


class TestConfidenceBands:
    """One case per band, plus every boundary. Boundaries are inclusive on
    the lower edge: a score exactly on a threshold belongs to the upper band."""

    @pytest.mark.parametrize("top_score, expected", [
        (0.0, "starved"),
        (0.19, "starved"),
        (0.199999, "starved"),
        (0.2, "low"),
        (0.3, "low"),
        (0.39, "low"),
        (0.4, "medium"),
        (0.55, "medium"),
        (0.69, "medium"),
        (0.7, "high"),
        (0.95, "high"),
        (1.0, "high"),
        (1.2, "high"),  # feedback/ and identity/ authority multiplier can exceed 1.0
    ])
    def test_band_for_top_score(self, top_score, expected):
        with _search_returning(_hits(top_score)) as (server, _, _):
            out = server.memory_search_tool("anything")
        assert out["confidence"] == expected, (
            f"top score {top_score} must band as {expected}, got {out['confidence']}")

    def test_no_results_is_starved(self):
        with _search_returning([]) as (server, _, _):
            out = server.memory_search_tool("nothing matches this")
        assert out["confidence"] == "starved"
        assert out["results"] == []

    def test_band_uses_max_score_not_first_result(self):
        """RRF ordering can put a lower-scored file first; the band must still
        reflect the best score anywhere in the list."""
        with _search_returning(_hits(0.25, 0.85, 0.1)) as (server, _, _):
            out = server.memory_search_tool("anything")
        assert out["confidence"] == "high"

    def test_single_hit_with_good_score_is_not_starved(self):
        """ATL-029 regression at the server layer: one perfect unique match
        must tell the model to answer, not to ask a clarifying question."""
        with _search_returning(_hits(0.65)) as (server, _, _):
            out = server.memory_search_tool("zygomorphic")
        assert len(out["results"]) == 1
        assert out["confidence"] != "starved"

    def test_score_missing_from_a_result_counts_as_zero(self):
        """A malformed result without a score must not crash the tool and
        must not inflate the band."""
        with _search_returning([{"path": "reference/x.md", "snippet": "..."}]) as (server, _, _):
            out = server.memory_search_tool("anything")
        assert out["confidence"] == "starved"

    def test_string_scores_are_coerced(self):
        """Scores round-tripped through JSON or sqlite may arrive as strings."""
        with _search_returning([{"path": "reference/x.md", "snippet": "...", "score": "0.75"}]) as (server, _, _):
            out = server.memory_search_tool("anything")
        assert out["confidence"] == "high"


class TestSearchToolPassThrough:
    def test_results_and_retrieval_mode_are_returned_unchanged(self):
        hits = _hits(0.5, 0.3)
        with _search_returning(hits, retrieval="hybrid") as (server, _, _):
            out = server.memory_search_tool("anything")
        assert out["results"] == hits
        assert out["retrieval"] == "hybrid"
        assert "temporal_context" not in out

    def test_query_scope_and_limit_are_forwarded(self):
        with _search_returning(_hits(0.5)) as (server, hybrid, _):
            server.memory_search_tool("dark mode", scope="feedback", limit=3)
        # the search tool asks for partial matches when no note holds every word (loose=True)
        hybrid.assert_called_once_with(query="dark mode", scope="feedback", limit=3, loose=True)

    def test_top_score_and_count_are_logged_for_miss_analysis(self):
        with _search_returning(_hits(0.42, 0.61)) as (server, _, logged):
            server.memory_search_tool("wdac policy", scope="reference")
        logged.assert_called_once()
        kwargs = logged.call_args.kwargs
        assert kwargs["query"] == "wdac policy"
        assert kwargs["scope"] == "reference"
        assert kwargs["top_score"] == pytest.approx(0.61)
        assert kwargs["result_count"] == 2

    def test_empty_search_logs_zero_top_score(self):
        with _search_returning([]) as (server, _, logged):
            server.memory_search_tool("nothing")
        assert logged.call_args.kwargs["top_score"] == 0.0
        assert logged.call_args.kwargs["result_count"] == 0


class TestBandsEndToEnd:
    """Drive the real FTS index (semantic search switched off) so the band
    reflects what fts.search actually emits, not a canned score."""

    def _world(self, tmp_path, monkeypatch, files: dict[str, str]):
        import atlas_plus.mcp.memory_server.fts as fts

        mem = tmp_path / "mem"
        for rel, body in files.items():
            p = mem / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(f"---\nt: x\n---\n{body}", encoding="utf-8")
        monkeypatch.setattr(fts, "MEMORY_ROOT", mem)
        monkeypatch.setattr(fts, "FTS_DB", tmp_path / "fts.sqlite")
        monkeypatch.setenv("ATLAS_SEMANTIC_SEARCH", "off")
        fts.rebuild(force=True)

    def _run(self, query):
        from atlas_plus.mcp.memory_server import server

        with patch("atlas_plus.mcp.memory_server.tools.search_log.log_search"), \
             patch.object(server, "_temporal_context_for_query", return_value=[]):
            return server.memory_search_tool(query)

    def test_lone_unique_match_answers(self, tmp_path, monkeypatch):
        """The original ATL-029 symptom, end to end: one file, one hit, must
        not be 'starved'."""
        self._world(tmp_path, monkeypatch,
                    {"reference/only.md": "the unique zygomorphic fact"})
        out = self._run("zygomorphic")
        assert len(out["results"]) == 1
        assert out["retrieval"] == "fts-only"
        assert out["confidence"] in ("medium", "high"), out

    def test_no_match_starves(self, tmp_path, monkeypatch):
        self._world(tmp_path, monkeypatch,
                    {"reference/a.md": "quince marmalade recipe"})
        out = self._run("zygomorphic")
        assert out["results"] == []
        assert out["confidence"] == "starved"

    def test_best_of_several_hits_sets_the_band(self, tmp_path, monkeypatch):
        """The top-ranked file normalises to BM25 1.0 and lands in high;
        weaker co-hits do not drag the band down."""
        self._world(tmp_path, monkeypatch, {
            "reference/strong.md": "quince quince quince quince marmalade",
            "reference/weak1.md": "a long note that mentions quince once among many other words here",
            "reference/weak2.md": "another long note that mentions quince once among many other words",
        })
        out = self._run("quince")
        assert len(out["results"]) == 3
        assert out["confidence"] == "high"
        assert max(float(r["score"]) for r in out["results"]) >= 0.7
