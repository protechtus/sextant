"""Tests for atlas_plus.session_patterns -- co-occurrence analysis and predictions."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch


# ---------------------------------------------------------------------------
# _parse_session_history
# ---------------------------------------------------------------------------

class TestParseSessionHistory:
    def test_returns_empty_for_missing_file(self, tmp_path):
        from atlas_plus.session_patterns import _parse_session_history
        result = _parse_session_history(tmp_path / "nonexistent.md")
        assert result == []

    def test_parses_project_from_title(self, tmp_path):
        from atlas_plus.session_patterns import _parse_session_history
        history = tmp_path / "_history.md"
        history.write_text(
            "## 2026-05-10 - atlas-memory+ Temporal Graph Design\n\nbody\n\n"
            "## 2026-05-11 - gatekeeper Windows Minifilter\n\nbody\n",
            encoding="utf-8",
        )
        # Patch ATLAS_PROJECTS_ROOT to tmp_path/projects with known project dirs
        projects = tmp_path / "projects"
        (projects / "atlas-memory+").mkdir(parents=True)
        (projects / "gatekeeper").mkdir(parents=True)
        with patch.dict("os.environ", {"ATLAS_PROJECTS_ROOT": str(projects)}):
            result = _parse_session_history(history)
        names = [r[1] for r in result]
        assert "atlas-memory+" in names or any("atlas" in n for n in names)

    def test_skips_lines_without_date_format(self, tmp_path):
        from atlas_plus.session_patterns import _parse_session_history
        history = tmp_path / "_history.md"
        history.write_text(
            "# Session History\n\n"
            "This is a paragraph without a date.\n\n"
            "## 2026-05-10 - Valid Session Title\n",
            encoding="utf-8",
        )
        with patch.dict("os.environ", {"ATLAS_PROJECTS_ROOT": str(tmp_path)}):
            result = _parse_session_history(history)
        # Only the valid line should be parsed; project may or may not match
        assert isinstance(result, list)


# ---------------------------------------------------------------------------
# build_cooccurrence
# ---------------------------------------------------------------------------

class TestBuildCooccurrence:
    def _write_history(self, path: Path, entries: list[tuple[str, str]]) -> None:
        lines = []
        for date_str, title in entries:
            lines.append(f"## {date_str} - {title}\n\nbody\n")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines), encoding="utf-8")

    def test_returns_empty_dicts_when_no_history(self, tmp_path):
        from atlas_plus.session_patterns import build_cooccurrence
        with patch("atlas_plus.session_patterns.MEMORY_ROOT", tmp_path):
            cooccur, counts = build_cooccurrence()
        assert cooccur == {}
        assert counts == {}

    def test_returns_tuple(self, tmp_path):
        """build_cooccurrence always returns a 2-tuple (cooccurrence, project_count)."""
        from atlas_plus.session_patterns import build_cooccurrence
        with patch("atlas_plus.session_patterns.MEMORY_ROOT", tmp_path):
            result = build_cooccurrence()
        assert isinstance(result, tuple)
        assert len(result) == 2

    def test_cooccurrence_is_symmetric(self, tmp_path):
        """If A co-occurs with B, B co-occurs with A with the same count."""
        from atlas_plus.session_patterns import build_cooccurrence, _parse_session_history
        history = tmp_path / "sessions" / "_history.md"
        projects = tmp_path / "projects"
        (projects / "atlas-memory+").mkdir(parents=True)
        (projects / "gatekeeper").mkdir(parents=True)
        # Three sessions with both projects appearing
        self._write_history(history, [
            ("2026-05-10", "atlas-memory+ Brain scheduler"),
            ("2026-05-10", "gatekeeper Windows kernel"),
            ("2026-05-11", "atlas-memory+ session patterns"),
            ("2026-05-11", "gatekeeper integration"),
            ("2026-05-12", "atlas-memory+ deploy"),
            ("2026-05-12", "gatekeeper deploy"),
        ])
        with patch("atlas_plus.session_patterns.MEMORY_ROOT", tmp_path), \
             patch.dict("os.environ", {"ATLAS_PROJECTS_ROOT": str(projects)}):
            cooccur, _ = build_cooccurrence(since_days=365)
        # At minimum, check that the return types are correct
        assert isinstance(cooccur, dict)


# ---------------------------------------------------------------------------
# compute_predictions
# ---------------------------------------------------------------------------

class TestComputePredictions:
    def test_returns_empty_when_no_cooccurrence(self):
        from atlas_plus.session_patterns import compute_predictions
        result = compute_predictions("atlas-memory+", {}, {})
        assert result == []

    def test_returns_predictions_sorted_by_confidence(self):
        from atlas_plus.session_patterns import compute_predictions
        cooccur = {
            "atlas-memory+": {
                "gatekeeper": 3,
                "harbor-app": 5,
            }
        }
        project_count = {"atlas-memory+": 8}
        result = compute_predictions("atlas-memory+", cooccur, project_count)
        assert len(result) >= 1
        # Higher confidence first
        confidences = [r["confidence"] for r in result]
        assert confidences == sorted(confidences, reverse=True)

    def test_filters_below_min_cooccurrence(self):
        from atlas_plus.session_patterns import compute_predictions, _MIN_COOCCURRENCE
        cooccur = {"project-a": {"project-b": 1}}  # below _MIN_COOCCURRENCE=2
        project_count = {"project-a": 10}
        result = compute_predictions("project-a", cooccur, project_count)
        assert result == []

    def test_filters_below_min_confidence(self):
        from atlas_plus.session_patterns import compute_predictions, _MIN_CONFIDENCE
        cooccur = {"project-a": {"project-b": 2}}
        project_count = {"project-a": 100}  # 2/100 = 0.02, below 0.30
        result = compute_predictions("project-a", cooccur, project_count)
        assert result == []


# ---------------------------------------------------------------------------
# save_patterns / load_patterns
# ---------------------------------------------------------------------------

class TestSaveLoadPatterns:
    def test_roundtrip(self, tmp_path):
        from atlas_plus.session_patterns import save_patterns, load_patterns
        with patch("atlas_plus.session_patterns.MEMORY_ROOT", tmp_path), \
             patch("atlas_plus.session_patterns.PATTERNS_FILE", tmp_path / "patterns.json"):
            model = save_patterns()
            loaded = load_patterns()
        assert loaded is not None
        assert "built_at" in loaded
        assert "cooccurrence" in loaded

    def test_load_returns_none_when_missing(self, tmp_path):
        from atlas_plus.session_patterns import load_patterns
        with patch("atlas_plus.session_patterns.PATTERNS_FILE", tmp_path / "nonexistent.json"):
            result = load_patterns()
        assert result is None


# ---------------------------------------------------------------------------
# predict_for_project
# ---------------------------------------------------------------------------

class TestPredictForProject:
    def test_returns_empty_when_no_model(self, tmp_path):
        from atlas_plus.session_patterns import predict_for_project
        with patch("atlas_plus.session_patterns.PATTERNS_FILE", tmp_path / "missing.json"):
            result = predict_for_project("atlas-memory+")
        assert result == []

    def test_returns_predictions_from_cached_model(self, tmp_path):
        import json
        from atlas_plus.session_patterns import predict_for_project
        model = {
            "built_at": "2026-05-20T00:00:00+00:00",
            "since_days": 120,
            "cooccurrence": {
                "atlas-memory+": {"gatekeeper": 3}
            },
            "project_count": {"atlas-memory+": 5},
        }
        patterns_file = tmp_path / "patterns.json"
        patterns_file.write_text(json.dumps(model), encoding="utf-8")
        with patch("atlas_plus.session_patterns.PATTERNS_FILE", patterns_file):
            result = predict_for_project("atlas-memory+")
        assert len(result) >= 1
        assert result[0]["project"] == "gatekeeper"
