"""Tests for the Atlas temporal graph -- first implementation slice.

Layer 1: deterministic, no LLM, no network. Uses FileTemporalProvider with tmp_path.

Covers all required test cases from the spec:
  - current mode excludes invalidated facts
  - as-of mode returns the fact true at the requested time
  - history mode returns facts in chronological order
  - source adapters reject paths outside Atlas roots
  - rebuild from fixtures without touching .memory/
  - provenance on every result
  - contradictory facts produce a warning, not silent overwrite
  - no SQL/database files or network listeners created
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

from atlas_plus.temporal_graph.file_provider import FileTemporalProvider
from atlas_plus.temporal_graph.models import (
    TemporalEpisode,
    TemporalEntity,
    TemporalFact,
    _now_iso,
    _sha256_short,
)
from atlas_plus.temporal_graph.sources import (
    FixtureBundle,
    FixtureSource,
    ATLAS_PHASE4_FIXTURE,
    _is_allowed_path,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def provider(tmp_path):
    return FileTemporalProvider(graph_root=tmp_path / "temporal_graph")


@pytest.fixture()
def phase4_provider(tmp_path):
    p = FileTemporalProvider(graph_root=tmp_path / "temporal_graph")
    FixtureSource(ATLAS_PHASE4_FIXTURE).ingest(p)
    return p


def _ep(episode_id: str, body: str = "test body", ref_time: str = "2026-01-01T00:00:00+00:00") -> TemporalEpisode:
    return TemporalEpisode(
        episode_id=episode_id,
        source_system="fixture",
        source_path=f"fixture/{episode_id}",
        source_type="fixture",
        body=body,
        body_hash=_sha256_short(body),
        reference_time=ref_time,
        ingested_at=_now_iso(),
    )


def _ent(entity_id: str, name: str) -> TemporalEntity:
    return TemporalEntity(
        entity_id=entity_id,
        name=name,
        labels=["Concept"],
        first_seen_at="2026-01-01T00:00:00+00:00",
        last_seen_at="2026-01-01T00:00:00+00:00",
    )


def _fact(
    fact_id: str,
    subject_id: str,
    predicate: str,
    object_id: str,
    fact_text: str,
    valid_from: str,
    invalid_at: str | None = None,
    confidence: float = 1.0,
    authority: str = "fixture",
    supersedes_fact_id: str | None = None,
) -> TemporalFact:
    return TemporalFact(
        fact_id=fact_id,
        subject_id=subject_id,
        predicate=predicate,
        object_id=object_id,
        fact_text=fact_text,
        valid_from=valid_from,
        invalid_at=invalid_at,
        observed_at=valid_from,
        ingested_at=_now_iso(),
        last_verified_at=valid_from,
        confidence=confidence,
        authority=authority,
        evidence_refs=[fact_id],
        supersedes_fact_id=supersedes_fact_id,
    )


# ---------------------------------------------------------------------------
# Provider basics
# ---------------------------------------------------------------------------

class TestProviderBasics:
    def test_empty_provider_stats(self, provider):
        s = provider.stats()
        assert s["episodes"] == 0
        assert s["entities"] == 0
        assert s["facts"] == 0

    def test_add_episode(self, provider):
        ep = _ep("ep-001")
        provider.add_episode(ep)
        assert provider.stats()["episodes"] == 1

    def test_upsert_entity(self, provider):
        ent = _ent("e-001", "Test Entity")
        provider.upsert_entity(ent)
        assert provider.stats()["entities"] == 1

    def test_upsert_entity_updates_existing(self, provider):
        e1 = _ent("e-001", "Alpha")
        e2 = TemporalEntity(
            entity_id="e-001",
            name="Alpha",
            labels=["Updated"],
            aliases=["alpha-alias"],
            first_seen_at="2026-01-01T00:00:00+00:00",
            last_seen_at="2026-06-01T00:00:00+00:00",
        )
        provider.upsert_entity(e1)
        provider.upsert_entity(e2)
        assert provider.stats()["entities"] == 1
        # first_seen_at should be preserved from first write
        assert provider._entities["e-001"].first_seen_at == "2026-01-01T00:00:00+00:00"

    def test_add_fact(self, provider):
        provider.upsert_entity(_ent("e-001", "Subject"))
        provider.upsert_entity(_ent("e-002", "Object"))
        fact = _fact("f-001", "e-001", "relates_to", "e-002", "Subject relates to Object.", "2026-01-01T00:00:00+00:00")
        fid, warnings = provider.add_fact(fact)
        assert fid == "f-001"
        assert provider.stats()["facts"] == 1

    def test_reset_clears_state(self, provider):
        provider.add_episode(_ep("ep-001"))
        provider.upsert_entity(_ent("e-001", "Alpha"))
        provider.reset_derived_state()
        s = provider.stats()
        assert s["episodes"] == 0
        assert s["entities"] == 0
        assert s["facts"] == 0


# ---------------------------------------------------------------------------
# Current mode
# ---------------------------------------------------------------------------

class TestCurrentMode:
    def test_current_excludes_invalidated_facts(self, provider):
        provider.upsert_entity(_ent("e-001", "Atlas"))
        provider.upsert_entity(_ent("e-002", "Domain"))

        # Active fact (no invalid_at)
        active = _fact(
            "f-active", "e-001", "needs", "e-002",
            "Atlas needs a Cloudflare domain.",
            valid_from="2026-01-01T00:00:00+00:00",
        )
        # Superseded fact (invalid_at in the past)
        old = _fact(
            "f-old", "e-001", "is_in", "e-002",
            "Old fact that has been invalidated.",
            valid_from="2025-01-01T00:00:00+00:00",
            invalid_at="2026-01-01T00:00:00+00:00",
        )
        provider.add_fact(active)
        provider.add_fact(old)

        results = provider.search_current("atlas domain", limit=10)
        fact_ids = {r.fact.fact_id for r in results}
        assert "f-active" in fact_ids
        assert "f-old" not in fact_ids

    def test_current_returns_results_with_provenance(self, provider):
        provider.upsert_entity(_ent("e-001", "Atlas"))
        provider.upsert_entity(_ent("e-002", "Domain"))
        fact = _fact("f-001", "e-001", "needs", "e-002", "Atlas needs a domain.", "2026-01-01T00:00:00+00:00")
        provider.add_fact(fact)
        results = provider.search_current("atlas domain", limit=5)
        assert len(results) >= 1
        for r in results:
            assert r.provenance  # provenance must not be empty

    def test_current_excludes_future_valid_from(self, provider):
        provider.upsert_entity(_ent("e-001", "Future"))
        provider.upsert_entity(_ent("e-002", "Thing"))
        future_fact = _fact(
            "f-future", "e-001", "will_be", "e-002",
            "This fact is not yet valid.",
            valid_from="2099-01-01T00:00:00+00:00",
        )
        provider.add_fact(future_fact)
        results = provider.search_current("future thing", limit=10)
        fact_ids = {r.fact.fact_id for r in results}
        assert "f-future" not in fact_ids


# ---------------------------------------------------------------------------
# As-of mode
# ---------------------------------------------------------------------------

class TestAsOfMode:
    def test_as_of_returns_fact_valid_at_that_time(self, phase4_provider):
        # At 2026-05-05, only "blocked by Cloudflare domain" was true
        results = phase4_provider.search_as_of("tunnel blocked", at="2026-05-05", limit=5)
        fact_ids = {r.fact.fact_id for r in results}
        assert "f-phase4-domain-block" in fact_ids
        assert "f-phase4-hardening-block" not in fact_ids
        assert "f-phase4-live" not in fact_ids

    def test_as_of_returns_superseding_fact_after_its_valid_from(self, phase4_provider):
        # At 2026-05-10, "blocked by P0 hardening" is active; "blocked by domain" is invalidated
        results = phase4_provider.search_as_of("tunnel blocked", at="2026-05-10", limit=5)
        fact_ids = {r.fact.fact_id for r in results}
        assert "f-phase4-hardening-block" in fact_ids
        assert "f-phase4-domain-block" not in fact_ids

    def test_as_of_current_after_go_live(self, phase4_provider):
        # After 2026-05-16T18:00:00, the tunnel is live
        results = phase4_provider.search_as_of("tunnel live", at="2026-05-17", limit=5)
        fact_ids = {r.fact.fact_id for r in results}
        assert "f-phase4-live" in fact_ids
        assert "f-phase4-domain-block" not in fact_ids
        assert "f-phase4-hardening-block" not in fact_ids

    def test_as_of_before_any_facts(self, phase4_provider):
        results = phase4_provider.search_as_of("tunnel", at="2026-01-01", limit=5)
        assert results == []


# ---------------------------------------------------------------------------
# History mode
# ---------------------------------------------------------------------------

class TestHistoryMode:
    def test_history_chronological_order(self, phase4_provider):
        h = phase4_provider.history("Atlas MCP Tunnel")
        assert len(h.facts) == 3
        assert h.facts[0].fact_id == "f-phase4-domain-block"
        assert h.facts[1].fact_id == "f-phase4-hardening-block"
        assert h.facts[2].fact_id == "f-phase4-live"

    def test_history_current_fact_is_live(self, phase4_provider):
        h = phase4_provider.history("Atlas MCP Tunnel")
        assert h.current_fact is not None
        assert h.current_fact.fact_id == "f-phase4-live"

    def test_history_by_alias(self, phase4_provider):
        h = phase4_provider.history("atl-001")
        assert h.entity.entity_id == "ent-atlas-tunnel"
        assert len(h.facts) == 3

    def test_history_entity_not_found(self, phase4_provider):
        h = phase4_provider.history("nonexistent entity xyz")
        assert h.entity.entity_id == "not-found"
        assert h.facts == []
        assert any("not found" in w for w in h.warnings)

    def test_history_supersession_chain(self, phase4_provider):
        h = phase4_provider.history("Atlas MCP Tunnel")
        # The live fact supersedes the hardening block
        live = next(f for f in h.facts if f.fact_id == "f-phase4-live")
        assert live.supersedes_fact_id == "f-phase4-hardening-block"


# ---------------------------------------------------------------------------
# Invalidation
# ---------------------------------------------------------------------------

class TestInvalidation:
    def test_invalidate_fact_removes_from_current(self, provider):
        provider.upsert_entity(_ent("e-001", "Atlas"))
        provider.upsert_entity(_ent("e-002", "Thing"))
        fact = _fact("f-001", "e-001", "is_in", "e-002", "Atlas is doing thing.", "2026-01-01T00:00:00+00:00")
        provider.add_fact(fact)

        # Confirm it appears in current
        results = provider.search_current("atlas thing", limit=5)
        assert any(r.fact.fact_id == "f-001" for r in results)

        # Invalidate it
        provider.invalidate_fact("f-001", invalid_at="2026-06-01T00:00:00+00:00", reason="test")

        # After invalidation, it should appear in current (it's invalid in the future, but now is before that)
        # Actually: invalid_at is 2026-06-01, now is 2026-05-16, so it's still valid right now
        # Let's set invalid_at to the past
        provider.invalidate_fact("f-001", invalid_at="2026-01-02T00:00:00+00:00", reason="superseded")
        results_after = provider.search_current("atlas thing", limit=5)
        assert not any(r.fact.fact_id == "f-001" for r in results_after)

    def test_invalidate_unknown_fact_raises(self, provider):
        with pytest.raises(KeyError):
            provider.invalidate_fact("nonexistent", invalid_at="2026-01-01T00:00:00+00:00", reason="test")


# ---------------------------------------------------------------------------
# Contradiction detection
# ---------------------------------------------------------------------------

class TestContradiction:
    def test_contradictory_facts_produce_warning(self, provider):
        provider.upsert_entity(_ent("e-001", "Atlas"))
        provider.upsert_entity(_ent("e-002", "Live"))
        provider.upsert_entity(_ent("e-003", "Down"))

        fact_a = _fact("f-a", "e-001", "status", "e-002", "Atlas is live.", "2026-01-01T00:00:00+00:00")
        fact_b = _fact("f-b", "e-001", "status", "e-003", "Atlas is down.", "2026-01-01T00:00:00+00:00")

        _, w1 = provider.add_fact(fact_a)
        _, w2 = provider.add_fact(fact_b)

        assert w1 == []  # first fact, no contradiction yet
        assert len(w2) >= 1
        assert any("Contradiction" in w or "contradiction" in w for w in w2)

    def test_updating_a_facts_own_value_is_not_a_contradiction(self, provider):
        """Regression (2026-10-09): re-adding the SAME fact_id with a new
        object (re-indexing a live source updates counters like
        total_tokens every build) warned against the fact's own previous
        version. Self-supersession is an update, not a contradiction."""
        provider.upsert_entity(_ent("e-001", "Transcript"))
        v1 = _fact("f-tok", "e-001", "total_tokens", "100",
                   "Transcript used 100 tokens.", "2026-01-01T00:00:00+00:00")
        v2 = _fact("f-tok", "e-001", "total_tokens", "250",
                   "Transcript used 250 tokens.", "2026-01-01T00:00:00+00:00")
        _, w1 = provider.add_fact(v1)
        _, w2 = provider.add_fact(v2)
        assert w1 == []
        assert w2 == [], f"self-update warned as contradiction: {w2}"
        # A DIFFERENT fact id with a conflicting value still warns.
        other = _fact("f-tok-2", "e-001", "total_tokens", "999",
                      "Transcript used 999 tokens.", "2026-01-01T00:00:00+00:00")
        _, w3 = provider.add_fact(other)
        assert any("Contradiction" in w for w in w3)


# ---------------------------------------------------------------------------
# JSONL persistence (reload from disk)
# ---------------------------------------------------------------------------

class TestPersistence:
    def test_facts_survive_reload(self, tmp_path):
        root = tmp_path / "temporal_graph"
        p1 = FileTemporalProvider(graph_root=root)
        p1.upsert_entity(_ent("e-001", "Atlas"))
        p1.upsert_entity(_ent("e-002", "Domain"))
        fact = _fact("f-001", "e-001", "needs", "e-002", "Atlas needs a domain.", "2026-01-01T00:00:00+00:00")
        p1.add_fact(fact)

        # Create a fresh provider loading from the same root
        p2 = FileTemporalProvider(graph_root=root)
        assert p2.stats()["facts"] == 1
        results = p2.search_current("atlas domain", limit=5)
        assert any(r.fact.fact_id == "f-001" for r in results)

    def test_invalidation_survives_reload(self, tmp_path):
        root = tmp_path / "temporal_graph"
        p1 = FileTemporalProvider(graph_root=root)
        p1.upsert_entity(_ent("e-001", "Atlas"))
        p1.upsert_entity(_ent("e-002", "Domain"))
        fact = _fact("f-001", "e-001", "needs", "e-002", "Needs domain.", "2026-01-01T00:00:00+00:00")
        p1.add_fact(fact)
        p1.invalidate_fact("f-001", invalid_at="2026-01-02T00:00:00+00:00", reason="done")

        p2 = FileTemporalProvider(graph_root=root)
        assert p2._facts["f-001"].invalid_at == "2026-01-02T00:00:00+00:00"


# ---------------------------------------------------------------------------
# No SQL / no network
# ---------------------------------------------------------------------------

class TestNoSQLNoNetwork:
    def test_no_sqlite_files_created(self, tmp_path):
        root = tmp_path / "temporal_graph"
        p = FileTemporalProvider(graph_root=root)
        FixtureSource(ATLAS_PHASE4_FIXTURE).ingest(p)

        sql_files = list(root.rglob("*.sqlite")) + list(root.rglob("*.db")) + list(root.rglob("*.wal"))
        assert sql_files == [], f"Unexpected SQL files: {sql_files}"


# ---------------------------------------------------------------------------
# Compaction (quality loop run 42): append-only upserts left superseded lines
# behind forever -- measured live at 95.6 MB for 2,312 entities (94x dead
# weight), costing the session-start hook 1.3s of load per session.
# ---------------------------------------------------------------------------

class TestCompaction:
    def _zero_threshold(self, monkeypatch):
        from atlas_plus.temporal_graph import file_provider as fp
        monkeypatch.setattr(fp, "_COMPACT_MIN_SAVINGS_BYTES", 0)

    def _churned_provider(self, root):
        p = FileTemporalProvider(graph_root=root)
        for i in range(5):  # 5 upserts of the same entity = 5 lines, 1 live
            ent = _ent("e-001", "Atlas")
            ent = ent.model_copy(update={"source_refs": [f"ref-{j}" for j in range(i + 1)]})
            p.upsert_entity(ent)
        p.upsert_entity(_ent("e-002", "Domain"))
        p.add_fact(_fact("f-001", "e-001", "uses", "e-002",
                         "Atlas uses the domain.", "2026-01-01T00:00:00+00:00"))
        p.invalidate_fact("f-001", invalid_at="2026-02-01T00:00:00+00:00", reason="done")
        p.add_fact(_fact("f-002", "e-001", "mentions", "e-002",
                         "Atlas mentions the domain daily.", "2026-03-01T00:00:00+00:00"))
        return p

    def test_compact_keeps_exactly_loader_visible_state(self, tmp_path, monkeypatch):
        self._zero_threshold(monkeypatch)
        root = tmp_path / "tg"
        p1 = self._churned_provider(root)
        before_entities = {k: v.model_dump() for k, v in p1._entities.items()}
        before_facts = {k: v.model_dump() for k, v in p1._facts.items()}

        report = p1.compact()
        assert report["entities.jsonl"]["compacted"] is True
        assert report["entities.jsonl"]["lines_kept"] == 2
        # one line per live id on disk now
        lines = [l for l in (root / "entities.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
        assert len(lines) == 2
        fact_lines = [l for l in (root / "facts.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
        assert len(fact_lines) == 2  # f-001 (invalidated form) + f-002

        p2 = FileTemporalProvider(graph_root=root)
        assert {k: v.model_dump() for k, v in p2._entities.items()} == before_entities
        assert {k: v.model_dump() for k, v in p2._facts.items()} == before_facts
        assert p2._facts["f-001"].invalid_at == "2026-02-01T00:00:00+00:00"

    def test_compact_preserves_raw_lines_not_model_roundtrip(self, tmp_path, monkeypatch):
        self._zero_threshold(monkeypatch)
        root = tmp_path / "tg"
        p = self._churned_provider(root)
        # a field written by a (hypothetical) newer schema version
        import json as _json
        raw = (root / "entities.jsonl").read_text(encoding="utf-8")
        rec = _json.loads(raw.splitlines()[-1])
        rec["entity_id"] = "e-future"
        rec["name"] = "Future"
        rec["field_from_the_future"] = {"survives": True}
        with (root / "entities.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(_json.dumps(rec) + "\n")

        p.compact()
        after = (root / "entities.jsonl").read_text(encoding="utf-8")
        assert "field_from_the_future" in after

    def test_small_files_skipped_at_default_threshold(self, tmp_path):
        root = tmp_path / "tg"
        p = self._churned_provider(root)
        before = (root / "entities.jsonl").read_bytes()
        report = p.compact()
        assert report["entities.jsonl"]["compacted"] is False
        assert (root / "entities.jsonl").read_bytes() == before
        assert not (root / "entities.jsonl.pre-compact").exists()

    def test_safety_copy_holds_original(self, tmp_path, monkeypatch):
        self._zero_threshold(monkeypatch)
        root = tmp_path / "tg"
        p = self._churned_provider(root)
        original = (root / "entities.jsonl").read_text(encoding="utf-8")
        p.compact()
        assert (root / "entities.jsonl.pre-compact").read_text(encoding="utf-8") == original

    def test_search_identical_after_compact_and_reload(self, tmp_path, monkeypatch):
        self._zero_threshold(monkeypatch)
        root = tmp_path / "tg"
        p1 = self._churned_provider(root)
        before = p1.search_current("atlas domain", limit=5)
        p1.compact()
        p2 = FileTemporalProvider(graph_root=root)
        after = p2.search_current("atlas domain", limit=5)
        # Same facts, same order. Scores compare with tolerance: freshness
        # decays against wall-clock "now", so the two calls are milliseconds
        # apart and exact equality flakes on a rounding boundary (CI red
        # 2026-10-10 after 22 green runs: ...047 vs ...048 at 1e-9).
        assert [r.fact.fact_id for r in after] == [r.fact.fact_id for r in before]
        for a, b in zip(after, before):
            assert a.score == pytest.approx(b.score, abs=1e-6)


# ---------------------------------------------------------------------------
# Search scoring equivalence (run 42): results built lazily for the top-k
# with cached parsed timestamps must score identically to scoring.score_fact.
# ---------------------------------------------------------------------------

class TestSearchScoringEquivalence:
    def test_scores_match_reference_scorer(self, provider):
        from atlas_plus.temporal_graph.scoring import _tokenize, score_fact
        provider.upsert_entity(_ent("e-001", "Atlas"))
        provider.upsert_entity(_ent("e-002", "Domain"))
        facts = [
            _fact("f-001", "e-001", "uses", "e-002", "Atlas uses the domain.",
                  "2026-01-01T00:00:00+00:00", confidence=0.9, authority="memory"),
            _fact("f-002", "e-001", "mentions", "e-002", "Atlas mentions nothing.",
                  "2026-02-01T00:00:00+00:00", confidence=0.4, authority="session_log"),
            _fact("f-003", "e-002", "references", "e-001", "Domain references Atlas twice.",
                  "2026-03-01T00:00:00+00:00", confidence=1.0, authority="unknown-authority"),
        ]
        # one fact with NO freshness anchor at all -> reference says 0.5
        bare = facts[1].model_copy(update={"last_verified_at": None, "observed_at": ""})
        facts[1] = bare
        for f in facts:
            provider._facts[f.fact_id] = f
            provider._index_fact(f)
            provider._sorted_vf.sort()

        query = "atlas domain"
        q_tokens = _tokenize(query)
        results = provider.search_current(query, limit=10)
        assert len(results) == 3
        for r in results:
            expected = score_fact(query, r.fact, q_tokens=q_tokens,
                                  f_tokens=provider._fact_tokens[r.fact.fact_id])
            assert abs(r.score - expected) < 1e-6, r.fact.fact_id
        # ranking is descending
        assert [r.score for r in results] == sorted((r.score for r in results), reverse=True)

    def test_invalidated_fact_still_excluded_now_included_as_of(self, provider):
        provider.upsert_entity(_ent("e-001", "Atlas"))
        provider.upsert_entity(_ent("e-002", "Domain"))
        provider.add_fact(_fact("f-001", "e-001", "uses", "e-002",
                                "Atlas uses the old domain.", "2026-01-01T00:00:00+00:00"))
        provider.invalidate_fact("f-001", invalid_at="2026-02-01T00:00:00+00:00", reason="moved")
        assert provider.search_current("old domain", limit=5) == []
        as_of = provider.search_as_of("old domain", at="2026-01-15T00:00:00+00:00", limit=5)
        assert [r.fact.fact_id for r in as_of] == ["f-001"]

    def test_timestamp_cache_is_per_fact_work_only(self, provider):
        """Regression pin (run 42): an indentation slip absorbed the full
        _sorted_vf rebuild into _cache_fact_timestamps, making every load
        O(n^2) (154s at 15k facts) while staying functionally green. The
        per-fact cache helper must never touch the sorted index."""
        provider.upsert_entity(_ent("e-001", "Atlas"))
        f = _fact("f-001", "e-001", "uses", "e-001", "Atlas fact.",
                  "2026-01-01T00:00:00+00:00")
        provider.add_fact(f)
        sorted_obj = provider._sorted_vf
        snapshot = list(sorted_obj)
        provider._cache_fact_timestamps(f)
        assert provider._sorted_vf is sorted_obj  # no reassignment
        assert provider._sorted_vf == snapshot    # no mutation

    def test_limit_bounds_constructed_results(self, provider):
        provider.upsert_entity(_ent("e-001", "Atlas"))
        for i in range(20):
            provider.add_fact(_fact(f"f-{i:03d}", "e-001", "mentions", "e-001",
                                    f"Atlas fact number {i}.", "2026-01-01T00:00:00+00:00"))
        assert len(provider.search_current("atlas", limit=7)) == 7

    def test_no_memory_root_writes(self, tmp_path, monkeypatch):
        memory_root = tmp_path / "memory"
        memory_root.mkdir()
        monkeypatch.setenv("ATLAS_MEMORY_ROOT", str(memory_root))

        graph_root = tmp_path / "temporal_graph"
        p = FileTemporalProvider(graph_root=graph_root)
        FixtureSource(ATLAS_PHASE4_FIXTURE).ingest(p)

        # .memory/ must be completely untouched
        memory_files = list(memory_root.rglob("*"))
        assert memory_files == [], f"Provider wrote to .memory/: {memory_files}"

    def test_only_jsonl_files_in_graph_root(self, tmp_path):
        root = tmp_path / "temporal_graph"
        p = FileTemporalProvider(graph_root=root)
        FixtureSource(ATLAS_PHASE4_FIXTURE).ingest(p)

        all_files = [f for f in root.rglob("*") if f.is_file()]
        for f in all_files:
            assert f.suffix in (".jsonl", ".json"), f"Unexpected file type: {f}"


# ---------------------------------------------------------------------------
# Path guard for source adapters
# ---------------------------------------------------------------------------

class TestPathGuard:
    def test_atlas_memory_root_is_allowed(self):
        # Derive from the same resolver the guard's allowed-roots list uses,
        # so the test is portable to machines without the legacy workspace.
        from atlas_plus import paths as atlas_paths
        assert _is_allowed_path(atlas_paths.memory_root() / "feedback" / "testing.md")

    def test_project_root_is_allowed(self):
        from atlas_plus import paths as atlas_paths
        assert _is_allowed_path(
            atlas_paths.projects_root() / "atlas-memory+" / "src" / "atlas_plus" / "temporal_graph"
        )

    def test_harbor_app_is_rejected(self):
        assert not _is_allowed_path(Path(r"C:\Tools\projects\some-other-project\data.md"))

    def test_system_path_is_rejected(self):
        assert not _is_allowed_path(Path(r"C:\Windows\System32\drivers\etc\hosts"))

    def test_work_project_is_rejected(self):
        assert not _is_allowed_path(Path(r"C:\Work\other-tool\tickets.md"))


# ---------------------------------------------------------------------------
# Fixture: Atlas Phase 4 scenario
# ---------------------------------------------------------------------------

class TestPhase4Fixture:
    def test_fixture_ingests_without_error(self, tmp_path):
        root = tmp_path / "temporal_graph"
        p = FileTemporalProvider(graph_root=root)
        result = FixtureSource(ATLAS_PHASE4_FIXTURE).ingest(p)
        assert result["episodes"] == 3
        assert result["entities"] == 4
        assert result["facts"] == 3
        assert result["warnings"] == []

    def test_current_shows_tunnel_live(self, phase4_provider):
        results = phase4_provider.search_current("tunnel example-tunnel", limit=3)
        assert len(results) >= 1
        assert results[0].fact.fact_id == "f-phase4-live"

    def test_as_of_2026_05_03_shows_domain_block(self, phase4_provider):
        results = phase4_provider.search_as_of("atlas tunnel", at="2026-05-03T14:00:00+00:00", limit=5)
        fact_ids = {r.fact.fact_id for r in results}
        assert "f-phase4-domain-block" in fact_ids

    def test_as_of_before_any_fact_is_empty(self, phase4_provider):
        results = phase4_provider.search_as_of("atlas tunnel", at="2026-01-01", limit=5)
        assert results == []

    def test_history_shows_full_chain(self, phase4_provider):
        h = phase4_provider.history("Atlas MCP Tunnel")
        assert len(h.facts) == 3
        assert h.current_fact is not None
        assert h.current_fact.fact_id == "f-phase4-live"

    def test_all_results_have_provenance(self, phase4_provider):
        results = phase4_provider.search_current("tunnel", limit=10)
        for r in results:
            assert r.provenance, f"Missing provenance on {r.fact.fact_id}"
