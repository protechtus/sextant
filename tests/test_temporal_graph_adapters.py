"""Tests for the Atlas temporal graph -- second slice: source adapters, hardening, optimization.

Covers:
  - MemoryMarkdownSource: entity creation, frontmatter facts, exclusions, incremental builds
  - SessionLogSource: session entities, worked_on edges, incremental builds
  - Manifest: hash tracking, save/load, skips unchanged files
  - Hardening: timestamp validation, overlap-based contradiction detection
  - Optimization: pre-tokenized index, sorted valid_from filtering, threading safety
  - Path guard: source adapters reject non-Atlas paths
  - TranscriptMiningSource: tool pattern extraction, session classification, manifest integration
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from atlas_plus.temporal_graph.file_provider import FileTemporalProvider, _windows_overlap
from atlas_plus.temporal_graph.models import TemporalFact, _now_iso, _sha256_short
from atlas_plus.temporal_graph.sources import (
    FixtureBundle,
    FixtureSource,
    Manifest,
    MemoryMarkdownSource,
    SessionLogSource,
    TranscriptMiningSource,
    _classify_session_type,
    _extract_tool_patterns,
    _is_allowed_path,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_memory_root(tmp_path: Path) -> Path:
    root = tmp_path / ".memory"
    root.mkdir()
    return root


def _write_md(path: Path, frontmatter: dict, body: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["---"]
    for k, v in frontmatter.items():
        if isinstance(v, list):
            lines.append(f"{k}:")
            for item in v:
                lines.append(f"  - {item}")
        else:
            lines.append(f"{k}: {v}")
    lines.append("---")
    lines.append("")
    lines.append(body)
    path.write_text("\n".join(lines), encoding="utf-8")


def _provider(tmp_path: Path) -> FileTemporalProvider:
    return FileTemporalProvider(graph_root=tmp_path / "temporal_graph")


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------

class TestManifest:
    def test_empty_manifest_shows_everything_changed(self, tmp_path):
        m = Manifest.load(tmp_path / "manifest.json")
        assert m.is_changed("anything", "abc123")

    def test_recorded_file_not_changed_with_same_hash(self, tmp_path):
        m = Manifest.load(tmp_path / "manifest.json")
        m.record("feedback/testing.md", "abc123", facts_added=2)
        assert not m.is_changed("feedback/testing.md", "abc123")

    def test_recorded_file_changed_with_different_hash(self, tmp_path):
        m = Manifest.load(tmp_path / "manifest.json")
        m.record("feedback/testing.md", "abc123", facts_added=2)
        assert m.is_changed("feedback/testing.md", "xyz456")

    def test_manifest_survives_save_and_reload(self, tmp_path):
        path = tmp_path / "manifest.json"
        m1 = Manifest.load(path)
        m1.record("feedback/testing.md", "abc123", facts_added=3)
        m1.save()

        m2 = Manifest.load(path)
        assert not m2.is_changed("feedback/testing.md", "abc123")
        assert m2.files["feedback/testing.md"].facts_added == 3
        assert m2.last_built is not None


# ---------------------------------------------------------------------------
# MemoryMarkdownSource
# ---------------------------------------------------------------------------

class TestMemoryMarkdownSource:
    def test_indexes_feedback_file(self, tmp_path):
        root = _make_memory_root(tmp_path)
        _write_md(
            root / "feedback" / "testing.md",
            {"id": "testing", "title": "Testing", "type": "feedback", "status": "active",
             "priority": "high", "updated": "2026-05-16"},
            "Don't mock the database in integration tests.",
        )
        p = _provider(tmp_path)
        result = MemoryMarkdownSource(memory_root=root, _extra_allowed_roots=[root]).ingest(p)

        assert result["files_indexed"] == 1
        assert result["facts"] >= 1  # priority high is stated; status active and type are template defaults

        # Entity created
        assert "memory:feedback/testing.md" in p._entities
        ent = p._entities["memory:feedback/testing.md"]
        assert ent.name == "Testing"
        assert "MemoryFile" in ent.labels

    def test_extracts_status_fact(self, tmp_path):
        root = _make_memory_root(tmp_path)
        _write_md(
            root / "feedback" / "testing.md",
            {"title": "Testing", "type": "feedback", "status": "paused", "updated": "2026-05-16"},
        )
        p = _provider(tmp_path)
        MemoryMarkdownSource(memory_root=root, _extra_allowed_roots=[root]).ingest(p)

        results = p.search_current("testing status paused", limit=10)
        assert any("status is paused" in r.fact.fact_text for r in results)

    def test_extracts_project_fact(self, tmp_path):
        root = _make_memory_root(tmp_path)
        _write_md(
            root / "projects" / "atlas.md",
            {"title": "Atlas Memory+", "type": "project", "status": "active",
             "project": "atlas-memory+", "updated": "2026-05-16"},
        )
        p = _provider(tmp_path)
        MemoryMarkdownSource(memory_root=root, _extra_allowed_roots=[root]).ingest(p)

        results = p.search_current("atlas project", limit=10)
        assert any("atlas-memory+" in r.fact.fact_text for r in results)

    def test_upserts_project_entity_from_project_field(self, tmp_path):
        """project: frontmatter should create a first-class Project entity."""
        root = _make_memory_root(tmp_path)
        _write_md(
            root / "projects" / "atlas.md",
            {"title": "Atlas Notes", "type": "project", "project": "atlas-memory+", "updated": "2026-05-16"},
        )
        p = _provider(tmp_path)
        MemoryMarkdownSource(memory_root=root, _extra_allowed_roots=[root]).ingest(p)

        entities = {e.entity_id: e for e in p._entities.values()}
        assert "project:atlas-memory+" in entities
        proj = entities["project:atlas-memory+"]
        assert "Project" in proj.labels

    def test_upserts_project_entities_from_applies_to(self, tmp_path):
        """applies_to: list should create Project entities and belongs_to facts."""
        root = _make_memory_root(tmp_path)
        _write_md(
            root / "docs" / "arch.md",
            {"title": "Architecture", "type": "reference",
             "applies_to": ["atlas-memory+", "sidecar"], "updated": "2026-05-16"},
        )
        p = _provider(tmp_path)
        MemoryMarkdownSource(memory_root=root, _extra_allowed_roots=[root]).ingest(p)

        entities = {e.entity_id: e for e in p._entities.values()}
        assert "project:atlas-memory+" in entities
        assert "project:sidecar" in entities

        # Verify applies_to_project facts were created
        results = p.search_current("atlas-memory+ Architecture", limit=10)
        assert any("applies_to_project" in r.fact.predicate for r in results)

    def test_multi_project_applies_to_is_not_a_contradiction(self, tmp_path):
        """Regression (2026-10-08): applies_to_project is multi-valued.

        A memory file applying to several projects produced a false
        'Contradiction' warning on every build because applies_to_project
        was missing from _MULTI_VALUED_PREDICATES.
        """
        root = _make_memory_root(tmp_path)
        _write_md(
            root / "docs" / "arch.md",
            {"title": "Architecture", "type": "reference",
             "applies_to": ["atlas-memory+", "sidecar", "dashboard"],
             "updated": "2026-05-16"},
        )
        p = _provider(tmp_path)
        result = MemoryMarkdownSource(memory_root=root, _extra_allowed_roots=[root]).ingest(p)

        contradiction_warnings = [w for w in result["warnings"] if "Contradiction" in w]
        assert contradiction_warnings == []

    def test_extracts_tags(self, tmp_path):
        root = _make_memory_root(tmp_path)
        _write_md(
            root / "reference" / "tools.md",
            {"title": "Tools", "type": "reference", "status": "active",
             "updated": "2026-05-16", "tags": ["atlas", "mcp", "security"]},
        )
        p = _provider(tmp_path)
        MemoryMarkdownSource(memory_root=root, _extra_allowed_roots=[root]).ingest(p)

        results = p.search_current("tools tagged atlas", limit=10)
        assert any("tagged atlas" in r.fact.fact_text for r in results)

    def test_skips_archive_directory(self, tmp_path):
        root = _make_memory_root(tmp_path)
        _write_md(
            root / "archive" / "old.md",
            {"title": "Old", "type": "feedback", "status": "archived", "updated": "2025-01-01"},
        )
        _write_md(
            root / "feedback" / "active.md",
            {"title": "Active", "type": "feedback", "status": "active", "updated": "2026-05-16"},
        )
        p = _provider(tmp_path)
        result = MemoryMarkdownSource(memory_root=root, _extra_allowed_roots=[root]).ingest(p)

        assert result["files_indexed"] == 1
        assert "memory:archive/old.md" not in p._entities
        assert "memory:feedback/active.md" in p._entities

    def test_skips_underscore_files(self, tmp_path):
        root = _make_memory_root(tmp_path)
        _write_md(root / "feedback" / "_index.md", {"title": "Index"})
        _write_md(
            root / "feedback" / "real.md",
            {"title": "Real", "type": "feedback", "status": "active", "updated": "2026-05-16"},
        )
        p = _provider(tmp_path)
        result = MemoryMarkdownSource(memory_root=root, _extra_allowed_roots=[root]).ingest(p)

        assert result["files_indexed"] == 1
        assert "memory:feedback/_index.md" not in p._entities

    def test_skips_underscore_archive_directory(self, tmp_path):
        """Regression (2026-10-08): tickets/_archive/** leaked into the live graph.

        The exclusion set listed the literal name "archive" but the tickets
        store archives to "_archive" -- 81% of live facts were archived-ticket
        residue (project:test alone carried 1,253 source_refs). Archived
        tickets must never become entities, episodes, facts, or Project nodes.
        """
        root = _make_memory_root(tmp_path)
        _write_md(
            root / "tickets" / "_archive" / "2026-06-08" / "test-proj" / "TES-001.md",
            {"title": "Verify badge test", "type": "ticket", "status": "archived",
             "project": "test-proj", "updated": "2026-06-08"},
        )
        _write_md(
            root / "tickets" / "infra" / "INF-001.md",
            {"title": "Real ticket", "type": "ticket", "status": "todo",
             "project": "infra", "updated": "2026-10-08"},
        )
        p = _provider(tmp_path)
        result = MemoryMarkdownSource(memory_root=root, _extra_allowed_roots=[root]).ingest(p)

        assert result["files_indexed"] == 1
        archived_key = "tickets/_archive/2026-06-08/test-proj/TES-001.md"
        assert f"memory:{archived_key}" not in p._entities
        assert f"ep:{archived_key}" not in p._episodes
        assert "project:test-proj" not in p._entities
        assert not any(f.fact_id.startswith(f"f:{archived_key}") for f in p._facts.values())
        # The live ticket still lands in full
        assert "memory:tickets/infra/INF-001.md" in p._entities
        assert "project:infra" in p._entities

    def test_skips_any_underscore_directory_component(self, tmp_path):
        """Any underscore-prefixed directory anywhere in the path is not live memory."""
        root = _make_memory_root(tmp_path)
        _write_md(
            root / "reference" / "_drafts" / "wip.md",
            {"title": "WIP", "type": "reference", "status": "active", "updated": "2026-10-08"},
        )
        p = _provider(tmp_path)
        result = MemoryMarkdownSource(memory_root=root, _extra_allowed_roots=[root]).ingest(p)

        assert result["files_indexed"] == 0
        assert "memory:reference/_drafts/wip.md" not in p._entities

    def test_skips_sessions_directory(self, tmp_path):
        root = _make_memory_root(tmp_path)
        _write_md(
            root / "sessions" / "2026-05-16.md",
            {"id": "session-2026-05-16", "title": "Test Session", "type": "session",
             "status": "active", "updated": "2026-05-16"},
        )
        p = _provider(tmp_path)
        result = MemoryMarkdownSource(memory_root=root, _extra_allowed_roots=[root]).ingest(p)

        assert result["files_indexed"] == 0

    def test_incremental_skips_unchanged(self, tmp_path):
        root = _make_memory_root(tmp_path)
        _write_md(
            root / "feedback" / "testing.md",
            {"title": "Testing", "type": "feedback", "status": "active", "updated": "2026-05-16"},
        )
        manifest_path = tmp_path / "temporal_graph" / "manifest.json"
        manifest_path.parent.mkdir(parents=True, exist_ok=True)

        p = _provider(tmp_path)
        manifest = Manifest.load(manifest_path)
        result1 = MemoryMarkdownSource(memory_root=root, _extra_allowed_roots=[root]).ingest(p, manifest)
        assert result1["files_indexed"] == 1
        assert result1["files_skipped"] == 0

        # Second run -- same file, no change
        p2 = _provider(tmp_path)
        manifest2 = Manifest.load(manifest_path)
        result2 = MemoryMarkdownSource(memory_root=root, _extra_allowed_roots=[root]).ingest(p2, manifest2)
        assert result2["files_skipped"] == 1
        assert result2["files_indexed"] == 0

    def test_incremental_reindexes_changed_file(self, tmp_path):
        root = _make_memory_root(tmp_path)
        f = root / "feedback" / "testing.md"
        _write_md(f, {"title": "Testing", "type": "feedback", "status": "active", "updated": "2026-05-16"})

        manifest_path = tmp_path / "temporal_graph" / "manifest.json"
        manifest_path.parent.mkdir(parents=True, exist_ok=True)

        p = _provider(tmp_path)
        manifest = Manifest.load(manifest_path)
        MemoryMarkdownSource(memory_root=root, _extra_allowed_roots=[root]).ingest(p, manifest)

        # Modify the file
        _write_md(f, {"title": "Testing", "type": "feedback", "status": "done", "updated": "2026-05-17"})

        p2 = _provider(tmp_path)
        manifest2 = Manifest.load(manifest_path)
        result2 = MemoryMarkdownSource(memory_root=root, _extra_allowed_roots=[root]).ingest(p2, manifest2)
        assert result2["files_indexed"] == 1
        assert result2["files_skipped"] == 0

    def test_deleted_file_facts_leave_current_search(self, tmp_path):
        """Regression (2026-10-09, run-43 banked lead): deleted source files
        never retracted their graph facts -- ghost refs persisted for months
        (tickets/test/TES-001.md haunted the live graph long after archival).

        A file the manifest knows but the walk no longer sees (and which is
        gone from disk) has its facts invalidated: out of search_current,
        still present for as-of history (temporal semantics, append-only).
        """
        root = _make_memory_root(tmp_path)
        f = root / "projects" / "ghost.md"
        _write_md(f, {"title": "Ghost Project", "type": "project",
                      "status": "paused", "updated": "2026-05-16"})  # a stated status (active is the template default)
        manifest_path = tmp_path / "temporal_graph" / "manifest.json"
        manifest_path.parent.mkdir(parents=True, exist_ok=True)

        p = _provider(tmp_path)
        manifest = Manifest.load(manifest_path)
        MemoryMarkdownSource(memory_root=root, _extra_allowed_roots=[root]).ingest(p, manifest)
        manifest.save()
        assert any("Ghost Project" in r.fact.fact_text
                   for r in p.search_current("ghost project status", limit=5))

        f.unlink()

        p2 = _provider(tmp_path)
        manifest2 = Manifest.load(manifest_path)
        result = MemoryMarkdownSource(memory_root=root, _extra_allowed_roots=[root]).ingest(p2, manifest2)
        assert result.get("files_retracted", 0) == 1
        assert "projects/ghost.md" not in manifest2.files
        # Gone from NOW
        assert not any(
            "Ghost Project" in r.fact.fact_text
            for r in p2.search_current("ghost project status", limit=5)
        )
        # Still in history before the deletion (as-of 2026-06-01)
        assert any(
            "Ghost Project" in r.fact.fact_text
            for r in p2.search_as_of("ghost project status", "2026-06-01", limit=5)
        )

    def test_newly_excluded_key_is_retracted(self, tmp_path):
        """A file that MOVED under an excluded dir (tickets/_archive/...) is
        retracted even though the old manifest remembers it: pre-fix graphs
        heal on their next build instead of asserting archived facts forever."""
        root = _make_memory_root(tmp_path)
        live = root / "tickets" / "proj" / "T-001.md"
        _write_md(live, {"title": "Archivable", "type": "ticket",
                         "status": "todo", "updated": "2026-05-16"})
        manifest_path = tmp_path / "temporal_graph" / "manifest.json"
        manifest_path.parent.mkdir(parents=True, exist_ok=True)

        p = _provider(tmp_path)
        manifest = Manifest.load(manifest_path)
        MemoryMarkdownSource(memory_root=root, _extra_allowed_roots=[root]).ingest(p, manifest)
        manifest.save()

        # Simulate archive: move the file under tickets/_archive/
        dest = root / "tickets" / "_archive" / "proj" / "T-001.md"
        dest.parent.mkdir(parents=True, exist_ok=True)
        live.rename(dest)

        p2 = _provider(tmp_path)
        manifest2 = Manifest.load(manifest_path)
        result = MemoryMarkdownSource(memory_root=root, _extra_allowed_roots=[root]).ingest(p2, manifest2)
        assert result.get("files_retracted", 0) == 1
        assert not any(
            "Archivable" in r.fact.fact_text
            for r in p2.search_current("archivable ticket", limit=5)
        )

    def test_retraction_never_touches_other_source_namespaces(self, tmp_path):
        """Keys recorded by SessionLogSource / transcript sources live in the
        same manifest; the memory source must not retract them just because
        its own walk never sees them."""
        root = _make_memory_root(tmp_path)
        _write_md(root / "feedback" / "real.md",
                  {"title": "Real", "type": "feedback", "status": "active",
                   "updated": "2026-05-16"})
        manifest_path = tmp_path / "temporal_graph" / "manifest.json"
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest = Manifest.load(manifest_path)
        manifest.record("sessions/2026-10-08.md", "deadbeef")
        manifest.record("transcripts/abc.jsonl", "deadbeef")
        manifest.record("mining/abc.jsonl", "deadbeef")

        p = _provider(tmp_path)
        result = MemoryMarkdownSource(memory_root=root, _extra_allowed_roots=[root]).ingest(p, manifest)
        assert result.get("files_retracted", 0) == 0
        assert "sessions/2026-10-08.md" in manifest.files
        assert "transcripts/abc.jsonl" in manifest.files
        assert "mining/abc.jsonl" in manifest.files

    def test_rejects_non_atlas_path(self, tmp_path):
        bad_root = tmp_path / "not_atlas" / "memory"
        bad_root.mkdir(parents=True)
        p = _provider(tmp_path)
        with pytest.raises(PermissionError):
            MemoryMarkdownSource(memory_root=bad_root).ingest(p)

    def test_multiple_files_indexed(self, tmp_path):
        root = _make_memory_root(tmp_path)
        for name in ("alpha", "beta", "gamma"):
            _write_md(
                root / "feedback" / f"{name}.md",
                {"title": name.title(), "type": "feedback", "status": "active", "updated": "2026-05-16"},
            )
        p = _provider(tmp_path)
        result = MemoryMarkdownSource(memory_root=root, _extra_allowed_roots=[root]).ingest(p)
        assert result["files_indexed"] == 3


# ---------------------------------------------------------------------------
# SessionLogSource
# ---------------------------------------------------------------------------

class TestSessionLogSource:
    def test_indexes_session_file(self, tmp_path):
        root = _make_memory_root(tmp_path)
        sessions = root / "sessions"
        sessions.mkdir()
        session_text = (
            "---\nid: session-2026-05-16\ntitle: Atlas Temporal Graph\ntype: session\n"
            "status: active\nupdated: 2026-05-16\n---\n\n"
            "## atlas-memory+ - Temporal graph first slice\n\nCompleted the first slice.\n"
        )
        (sessions / "2026-05-16.md").write_text(session_text, encoding="utf-8")

        p = _provider(tmp_path)
        result = SessionLogSource(memory_root=root, _extra_allowed_roots=[root]).ingest(p)

        assert result["files_indexed"] == 1
        assert "session:2026-05-16" in p._entities
        ent = p._entities["session:2026-05-16"]
        assert "Session" in ent.labels

    def test_extracts_worked_on_edge(self, tmp_path):
        root = _make_memory_root(tmp_path)
        (root / "sessions").mkdir()
        text = (
            "---\nid: session-2026-05-16\ntitle: Test\ntype: session\nstatus: active\nupdated: 2026-05-16\n---\n\n"
            "## atlas-memory+ - Temporal graph\n\nDid some work.\n"
        )
        (root / "sessions" / "2026-05-16.md").write_text(text, encoding="utf-8")

        p = _provider(tmp_path)
        SessionLogSource(memory_root=root, _extra_allowed_roots=[root]).ingest(p)

        results = p.search_current("atlas-memory worked", limit=10)
        assert any("atlas-memory+" in r.fact.fact_text for r in results)

    def test_skips_history_file(self, tmp_path):
        root = _make_memory_root(tmp_path)
        (root / "sessions").mkdir()
        (root / "sessions" / "_history.md").write_text("# History\n\nLog entries.", encoding="utf-8")

        p = _provider(tmp_path)
        result = SessionLogSource(memory_root=root, _extra_allowed_roots=[root]).ingest(p)
        assert result["files_indexed"] == 0

    def test_skips_non_date_files(self, tmp_path):
        root = _make_memory_root(tmp_path)
        (root / "sessions").mkdir()
        (root / "sessions" / "summary.md").write_text("# Summary\n", encoding="utf-8")

        p = _provider(tmp_path)
        result = SessionLogSource(memory_root=root, _extra_allowed_roots=[root]).ingest(p)
        assert result["files_indexed"] == 0

    def test_incremental_skips_unchanged(self, tmp_path):
        root = _make_memory_root(tmp_path)
        (root / "sessions").mkdir()
        text = (
            "---\nid: session-2026-05-16\ntitle: Test\ntype: session\nstatus: active\nupdated: 2026-05-16\n---\n\nContent.\n"
        )
        (root / "sessions" / "2026-05-16.md").write_text(text, encoding="utf-8")

        manifest_path = tmp_path / "temporal_graph" / "manifest.json"
        manifest_path.parent.mkdir(parents=True, exist_ok=True)

        p = _provider(tmp_path)
        m = Manifest.load(manifest_path)
        r1 = SessionLogSource(memory_root=root, _extra_allowed_roots=[root]).ingest(p, m)
        assert r1["files_indexed"] == 1

        p2 = _provider(tmp_path)
        m2 = Manifest.load(manifest_path)
        r2 = SessionLogSource(memory_root=root, _extra_allowed_roots=[root]).ingest(p2, m2)
        assert r2["files_skipped"] == 1
        assert r2["files_indexed"] == 0

    def test_no_sessions_dir_returns_empty(self, tmp_path):
        root = _make_memory_root(tmp_path)
        p = _provider(tmp_path)
        result = SessionLogSource(memory_root=root, _extra_allowed_roots=[root]).ingest(p)
        assert result["files_indexed"] == 0


# ---------------------------------------------------------------------------
# Hardening: timestamp validation
# ---------------------------------------------------------------------------

class TestTimestampValidation:
    def test_invalid_valid_from_raises(self, tmp_path):
        p = _provider(tmp_path)
        fact = TemporalFact(
            fact_id="f-bad",
            subject_id="e-001",
            predicate="is",
            object_id="bad",
            fact_text="A fact.",
            valid_from="not-a-date",
            observed_at=_now_iso(),
            ingested_at=_now_iso(),
        )
        with pytest.raises(ValueError, match="valid_from"):
            p.add_fact(fact)

    def test_invalid_invalid_at_raises(self, tmp_path):
        p = _provider(tmp_path)
        fact = TemporalFact(
            fact_id="f-bad",
            subject_id="e-001",
            predicate="is",
            object_id="bad",
            fact_text="A fact.",
            valid_from="2026-01-01T00:00:00+00:00",
            invalid_at="BADDATE",
            observed_at=_now_iso(),
            ingested_at=_now_iso(),
        )
        with pytest.raises(ValueError, match="invalid_at"):
            p.add_fact(fact)

    def test_valid_timestamps_accepted(self, tmp_path):
        p = _provider(tmp_path)
        fact = TemporalFact(
            fact_id="f-ok",
            subject_id="e-001",
            predicate="is",
            object_id="good",
            fact_text="A valid fact.",
            valid_from="2026-01-01T00:00:00+00:00",
            invalid_at="2026-06-01T00:00:00+00:00",
            observed_at=_now_iso(),
            ingested_at=_now_iso(),
        )
        fid, warnings = p.add_fact(fact)
        assert fid == "f-ok"


# ---------------------------------------------------------------------------
# Hardening: overlap-based contradiction detection
# ---------------------------------------------------------------------------

class TestWindowOverlap:
    def test_both_open_overlaps(self):
        assert _windows_overlap("2026-01-01T00:00:00+00:00", None, "2026-06-01T00:00:00+00:00", None)

    def test_one_closed_before_other_starts(self):
        assert not _windows_overlap(
            "2026-01-01T00:00:00+00:00", "2026-03-01T00:00:00+00:00",
            "2026-04-01T00:00:00+00:00", None,
        )

    def test_one_closed_after_other_ends(self):
        assert not _windows_overlap(
            "2026-05-01T00:00:00+00:00", None,
            "2026-01-01T00:00:00+00:00", "2026-04-01T00:00:00+00:00",
        )

    def test_partial_overlap(self):
        assert _windows_overlap(
            "2026-01-01T00:00:00+00:00", "2026-06-01T00:00:00+00:00",
            "2026-03-01T00:00:00+00:00", "2026-09-01T00:00:00+00:00",
        )

    def test_contradiction_detected_with_overlapping_windows(self, tmp_path):
        p = _provider(tmp_path)
        from atlas_plus.temporal_graph.models import TemporalEntity

        p.upsert_entity(TemporalEntity(entity_id="e-1", name="A", first_seen_at=_now_iso(), last_seen_at=_now_iso()))
        p.upsert_entity(TemporalEntity(entity_id="e-2", name="B", first_seen_at=_now_iso(), last_seen_at=_now_iso()))
        p.upsert_entity(TemporalEntity(entity_id="e-3", name="C", first_seen_at=_now_iso(), last_seen_at=_now_iso()))

        # Fact A: A is_at B, valid 2026-01-01 to 2026-06-01
        fa = TemporalFact(
            fact_id="fa", subject_id="e-1", predicate="is_at", object_id="e-2",
            fact_text="A is at B.", valid_from="2026-01-01T00:00:00+00:00",
            invalid_at="2026-06-01T00:00:00+00:00",
            observed_at=_now_iso(), ingested_at=_now_iso(),
        )
        # Fact B: A is_at C, valid 2026-03-01 onwards -- overlaps with FA
        fb = TemporalFact(
            fact_id="fb", subject_id="e-1", predicate="is_at", object_id="e-3",
            fact_text="A is at C.", valid_from="2026-03-01T00:00:00+00:00",
            observed_at=_now_iso(), ingested_at=_now_iso(),
        )
        _, w1 = p.add_fact(fa)
        _, w2 = p.add_fact(fb)
        assert w1 == []
        assert len(w2) >= 1
        assert "Contradiction" in w2[0]


# ---------------------------------------------------------------------------
# Optimization: pre-tokenized index
# ---------------------------------------------------------------------------

class TestPreTokenizedIndex:
    def test_fact_tokens_populated_on_add(self, tmp_path):
        p = _provider(tmp_path)
        fact = TemporalFact(
            fact_id="f-tok",
            subject_id="e-001",
            predicate="is",
            object_id="test",
            fact_text="Integration tests must use a real database.",
            valid_from="2026-01-01T00:00:00+00:00",
            observed_at=_now_iso(),
            ingested_at=_now_iso(),
        )
        p.add_fact(fact)
        tokens = p._fact_tokens.get("f-tok")
        assert tokens is not None
        assert "integration" in tokens
        assert "database" in tokens

    def test_fact_tokens_rebuilt_on_reload(self, tmp_path):
        root = tmp_path / "temporal_graph"
        p1 = FileTemporalProvider(graph_root=root)
        fact = TemporalFact(
            fact_id="f-tok",
            subject_id="e-001",
            predicate="is",
            object_id="test",
            fact_text="Temporal graphs track changes over time.",
            valid_from="2026-01-01T00:00:00+00:00",
            observed_at=_now_iso(),
            ingested_at=_now_iso(),
        )
        p1.add_fact(fact)

        p2 = FileTemporalProvider(graph_root=root)
        tokens = p2._fact_tokens.get("f-tok")
        assert tokens is not None
        assert "temporal" in tokens

    def test_sorted_vf_maintained(self, tmp_path):
        p = _provider(tmp_path)
        facts = [
            TemporalFact(
                fact_id=f"f-{i:03d}",
                subject_id="e",
                predicate="is",
                object_id="x",
                fact_text=f"Fact {i}.",
                valid_from=f"2026-0{i}-01T00:00:00+00:00",
                observed_at=_now_iso(),
                ingested_at=_now_iso(),
            )
            for i in range(1, 6)
        ]
        # Insert in reverse order
        for f in reversed(facts):
            p.add_fact(f)
        vf_list = [vf for vf, _ in p._sorted_vf]
        assert vf_list == sorted(vf_list)


# ---------------------------------------------------------------------------
# Thread safety
# ---------------------------------------------------------------------------

class TestThreadSafety:
    def test_concurrent_add_fact(self, tmp_path):
        p = _provider(tmp_path)
        errors = []

        def add_facts(start: int) -> None:
            for i in range(start, start + 20):
                try:
                    fact = TemporalFact(
                        fact_id=f"f-thread-{i:04d}",
                        subject_id="e",
                        predicate="has",
                        object_id=str(i),
                        fact_text=f"Thread fact {i}.",
                        valid_from="2026-01-01T00:00:00+00:00",
                        observed_at=_now_iso(),
                        ingested_at=_now_iso(),
                    )
                    p.add_fact(fact)
                except Exception as exc:
                    errors.append(exc)

        threads = [threading.Thread(target=add_facts, args=(i * 20,)) for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == [], f"Thread errors: {errors}"
        assert p.stats()["facts"] == 100

    def test_jsonl_not_corrupted_after_concurrent_writes(self, tmp_path):
        root = tmp_path / "temporal_graph"
        p = FileTemporalProvider(graph_root=root)

        def write_batch(start: int) -> None:
            for i in range(start, start + 10):
                fact = TemporalFact(
                    fact_id=f"f-{i:04d}",
                    subject_id="e",
                    predicate="has",
                    object_id=str(i),
                    fact_text=f"Fact {i}.",
                    valid_from="2026-01-01T00:00:00+00:00",
                    observed_at=_now_iso(),
                    ingested_at=_now_iso(),
                )
                p.add_fact(fact)

        threads = [threading.Thread(target=write_batch, args=(i * 10,)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Reload and verify all facts parseable
        p2 = FileTemporalProvider(graph_root=root)
        assert p2.stats()["facts"] == 40


# ---------------------------------------------------------------------------
# TranscriptMiningSource
# ---------------------------------------------------------------------------

def _write_jsonl(path: Path, records: list[dict]) -> None:
    import json
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")


def _tool_use_record(name: str, file_path: str = "", ts: str = "2026-05-20T10:00:00Z") -> dict:
    inp = {"file_path": file_path} if file_path else {}
    return {
        "type": "assistant",
        "timestamp": ts,
        "sessionId": "sess-abc123",
        "message": {
            "role": "assistant",
            "model": "claude-sonnet-4-6",
            "content": [{"type": "tool_use", "name": name, "input": inp}],
            "usage": {"input_tokens": 100, "output_tokens": 20,
                      "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0},
        },
    }


class TestExtractToolPatterns:
    def test_counts_tool_uses(self, tmp_path):
        f = tmp_path / "session.jsonl"
        _write_jsonl(f, [
            _tool_use_record("Read", "src/foo.py"),
            _tool_use_record("Read", "src/bar.py"),
            _tool_use_record("Edit", "src/foo.py"),
        ])
        result = _extract_tool_patterns(f)
        assert result["tool_counts"]["Read"] == 2
        assert result["tool_counts"]["Edit"] == 1
        assert result["total_tool_calls"] == 3

    def test_collects_file_paths(self, tmp_path):
        f = tmp_path / "session.jsonl"
        _write_jsonl(f, [
            _tool_use_record("Read", r"C:\project\src\server.py"),
            _tool_use_record("Edit", r"C:\project\src\tasks.py"),
        ])
        result = _extract_tool_patterns(f)
        names = result["files_touched"]
        assert "server.py" in names
        assert "tasks.py" in names

    def test_empty_file_returns_zero_counts(self, tmp_path):
        f = tmp_path / "empty.jsonl"
        f.write_text("", encoding="utf-8")
        result = _extract_tool_patterns(f)
        assert result["total_tool_calls"] == 0
        assert result["files_touched"] == []

    def test_ignores_non_tool_use_records(self, tmp_path):
        f = tmp_path / "session.jsonl"
        import json
        user_record = {"type": "user", "message": {"role": "user", "content": "hello"}, "timestamp": "2026-05-20T10:00:00Z"}
        f.write_text(json.dumps(user_record) + "\n", encoding="utf-8")
        result = _extract_tool_patterns(f)
        assert result["total_tool_calls"] == 0

    def test_handles_missing_file_gracefully(self, tmp_path):
        result = _extract_tool_patterns(tmp_path / "nonexistent.jsonl")
        assert result["total_tool_calls"] == 0


class TestClassifySessionType:
    def test_edit_heavy(self):
        assert _classify_session_type({"Edit": 10, "Read": 3, "Bash": 1}) == "edit-heavy"

    def test_read_heavy(self):
        assert _classify_session_type({"Read": 20, "Glob": 5, "Edit": 2}) == "read-heavy"

    def test_exec_heavy(self):
        assert _classify_session_type({"Bash": 15, "Read": 2, "Edit": 1}) == "exec-heavy"

    def test_mixed_when_no_dominant(self):
        assert _classify_session_type({"Read": 5, "Edit": 5, "Bash": 5}) == "mixed"

    def test_empty_dict(self):
        assert _classify_session_type({}) == "empty"


class TestTranscriptMiningSource:
    def _provider(self, tmp_path: Path):
        return FileTemporalProvider(graph_root=tmp_path / "graph")

    def test_ingest_creates_facts(self, tmp_path):
        transcript_dir = tmp_path / "transcripts"
        _write_jsonl(transcript_dir / "sess001.jsonl", [
            _tool_use_record("Read", "foo.py"),
            _tool_use_record("Edit", "foo.py"),
            _tool_use_record("Read", "bar.py"),
        ])
        p = self._provider(tmp_path)
        src = TranscriptMiningSource(claude_root=tmp_path, target_dir="transcripts")
        result = src.ingest(p)
        assert result["files_indexed"] == 1
        assert result["facts"] > 0

    def test_ingest_adds_top_tool_fact(self, tmp_path):
        transcript_dir = tmp_path / "transcripts"
        _write_jsonl(transcript_dir / "sess002.jsonl", [
            _tool_use_record("Read"),
            _tool_use_record("Read"),
            _tool_use_record("Edit"),
        ])
        p = self._provider(tmp_path)
        src = TranscriptMiningSource(claude_root=tmp_path, target_dir="transcripts")
        src.ingest(p)
        stats = p.stats()
        # Top tool = Read, should have a fact with predicate top_tool_used
        assert stats["facts"] >= 1

    def test_ingest_skips_empty_transcript(self, tmp_path):
        transcript_dir = tmp_path / "transcripts"
        (transcript_dir / "empty.jsonl").parent.mkdir(parents=True, exist_ok=True)
        (transcript_dir / "empty.jsonl").write_text("", encoding="utf-8")
        p = self._provider(tmp_path)
        src = TranscriptMiningSource(claude_root=tmp_path, target_dir="transcripts")
        result = src.ingest(p)
        assert result["files_indexed"] == 1
        assert result["facts"] == 0  # empty transcript: no tool calls, no facts

    def test_ingest_returns_warning_when_dir_missing(self, tmp_path):
        p = self._provider(tmp_path)
        src = TranscriptMiningSource(claude_root=tmp_path, target_dir="nonexistent")
        result = src.ingest(p)
        assert result["files_indexed"] == 0
        assert len(result["warnings"]) > 0

    def test_manifest_skips_unchanged_files(self, tmp_path):
        transcript_dir = tmp_path / "transcripts"
        _write_jsonl(transcript_dir / "sess003.jsonl", [_tool_use_record("Read")])
        p = self._provider(tmp_path)
        manifest = Manifest.load(tmp_path / "manifest.json")
        src = TranscriptMiningSource(claude_root=tmp_path, target_dir="transcripts")
        r1 = src.ingest(p, manifest)
        assert r1["files_indexed"] == 1

        # Second ingest with same manifest -- should skip
        r2 = src.ingest(p, manifest)
        assert r2["files_skipped"] == 1
        assert r2["files_indexed"] == 0
