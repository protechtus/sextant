"""FileTemporalProvider -- append-only JSONL-backed temporal graph with in-memory indexes.

Storage layout (all under graph_root, default <workspace>/.atlas/temporal_graph/):
  episodes.jsonl   -- one TemporalEpisode per line
  entities.jsonl   -- one TemporalEntity per line; later lines override by entity_id
  facts.jsonl      -- one TemporalFact per line; later lines override by fact_id
  manifest.json    -- build metadata (source counts, last indexed)

Append-only contract:
  - Episodes are immutable once written.
  - Entities: later line wins by entity_id on load (upsert via append).
  - Facts: later line wins by fact_id on load (invalidation via append).
  - Nothing is deleted -- only superseded or archived.

Hardening:
  - threading.Lock guards all JSONL appends.
  - Timestamp validation on add_fact: rejects malformed ISO strings.
  - Contradiction detection checks overlapping validity windows, not just both-null.

Optimization:
  - Pre-tokenized word sets per fact -- lexical scoring never re-tokenizes.
  - Sorted valid_from list enables O(log n + k) temporal filtering vs O(n).
"""

from __future__ import annotations

import bisect
import json
import math
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from atlas_plus import paths as atlas_paths
from atlas_plus.temporal_graph.models import (
    TemporalEpisode,
    TemporalEntity,
    TemporalFact,
    TemporalHistory,
    TemporalResult,
    _now_iso,
)
from atlas_plus.temporal_graph.provider import TemporalGraphProvider
from atlas_plus.temporal_graph.scoring import (
    _AUTHORITY_WEIGHTS,
    _FRESHNESS_HALF_LIFE_DAYS,
    _tokenize,
    freshness_warning,
    lexical_relevance_from_tokens,
)

_DEFAULT_ROOT = atlas_paths.state_dir() / "temporal_graph"

# Predicates where multiple simultaneous objects are valid -- no contradiction warning.
# applies_to_project: one memory file legitimately applies to several projects
# (every build warned falsely on multi-project applies_to lists until 2026-10-08).
_MULTI_VALUED_PREDICATES = {
    "worked_on", "has_tag", "references", "depends_on", "mentions",
    "applies_to_project",
}

# compact(): only rewrite a file carrying at least this much dead weight, so
# small corpora never churn their files. Measured live before the first
# compaction: entities.jsonl held 95.6 MB for 2,312 entities (94x superseded
# lines) and cost the session-start hook 1.3s of load time per session.
_COMPACT_MIN_SAVINGS_BYTES = 1 * 1024 * 1024


def _parse_iso(ts: str) -> datetime:
    ts = ts.strip()
    if ts.endswith("Z"):
        ts = ts[:-1] + "+00:00"
    elif len(ts) == 10:
        ts = ts + "T00:00:00+00:00"
    return datetime.fromisoformat(ts)


def _validate_iso(ts: str, field_name: str) -> None:
    try:
        _parse_iso(ts)
    except (ValueError, AttributeError) as exc:
        raise ValueError(f"Invalid ISO 8601 timestamp for {field_name!r}: {ts!r}") from exc


def _is_valid_at(fact: TemporalFact, point: datetime) -> bool:
    try:
        if _parse_iso(fact.valid_from) > point:
            return False
        if fact.invalid_at and _parse_iso(fact.invalid_at) <= point:
            return False
        return True
    except (ValueError, OverflowError):
        return False


def _windows_overlap(vf_a: str, ia_a: Optional[str], vf_b: str, ia_b: Optional[str]) -> bool:
    """True if two [valid_from, invalid_at) windows overlap."""
    try:
        a_start = _parse_iso(vf_a)
        a_end = _parse_iso(ia_a) if ia_a else None
        b_start = _parse_iso(vf_b)
        b_end = _parse_iso(ia_b) if ia_b else None
        # A starts before B ends AND B starts before A ends
        a_before_b_end = (b_end is None) or (a_start < b_end)
        b_before_a_end = (a_end is None) or (b_start < a_end)
        return a_before_b_end and b_before_a_end
    except (ValueError, OverflowError):
        return False


def _acquire_compact_lock(lock_path: Path, stale_seconds: float = 600.0) -> bool:
    import time

    lock_path.parent.mkdir(parents=True, exist_ok=True)
    for _ in range(2):
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode("ascii"))
            os.close(fd)
            return True
        except FileExistsError:
            try:
                if time.time() - lock_path.stat().st_mtime > stale_seconds:
                    lock_path.unlink()
                    continue
            except OSError:
                pass
            return False
        except PermissionError:
            return False
    return False


class FileTemporalProvider(TemporalGraphProvider):
    """JSONL-backed implementation of TemporalGraphProvider: stores episodes, entities, and facts on disk."""

    def __init__(self, graph_root: Optional[Path] = None) -> None:
        self._root = graph_root or _DEFAULT_ROOT
        self._root.mkdir(parents=True, exist_ok=True)

        self._episodes_file = self._root / "episodes.jsonl"
        self._entities_file = self._root / "entities.jsonl"
        self._facts_file = self._root / "facts.jsonl"
        self._manifest_file = self._root / "manifest.json"

        self._write_lock = threading.Lock()

        # Primary data
        self._episodes: dict[str, TemporalEpisode] = {}
        self._entities: dict[str, TemporalEntity] = {}
        self._facts: dict[str, TemporalFact] = {}

        # Derived indexes (rebuilt by _rebuild_indexes)
        self._entity_by_name: dict[str, str] = {}
        self._facts_by_entity: dict[str, list[str]] = {}
        self._fact_tokens: dict[str, set[str]] = {}       # fact_id -> token set
        self._sorted_vf: list[tuple[str, str]] = []        # sorted (valid_from, fact_id)
        # Parsed-timestamp caches: searches score every candidate fact, and
        # datetime.fromisoformat per fact per query dominated query time.
        self._fact_fresh_ts: dict[str, Optional[datetime]] = {}   # freshness anchor (None = score 0.5)
        self._fact_invalid_ts: dict[str, datetime] = {}           # parseable invalid_at only

        self._load()

    # ------------------------------------------------------------------
    # Load / index
    # ------------------------------------------------------------------

    def _load(self) -> None:
        self._episodes.clear()
        self._entities.clear()
        self._facts.clear()

        for path, model, store in (
            (self._episodes_file, TemporalEpisode, self._episodes),
            (self._entities_file, TemporalEntity, self._entities),
            (self._facts_file, TemporalFact, self._facts),
        ):
            if not path.exists():
                continue
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = model.model_validate_json(line)
                    store[obj.episode_id if hasattr(obj, "episode_id") else
                          obj.entity_id if hasattr(obj, "entity_id") else
                          obj.fact_id] = obj
                except Exception:
                    pass

        self._rebuild_indexes()

    def _rebuild_indexes(self) -> None:
        self._entity_by_name.clear()
        self._facts_by_entity.clear()
        self._fact_tokens.clear()
        self._fact_fresh_ts.clear()
        self._fact_invalid_ts.clear()

        for ent in self._entities.values():
            self._entity_by_name[ent.name.lower()] = ent.entity_id
            for alias in ent.aliases:
                self._entity_by_name[alias.lower()] = ent.entity_id

        for fact in self._facts.values():
            self._fact_tokens[fact.fact_id] = _tokenize(fact.fact_text)
            self._cache_fact_timestamps(fact)
            for eid in (fact.subject_id, fact.object_id):
                if eid and eid in self._entities:
                    bucket = self._facts_by_entity.setdefault(eid, [])
                    if fact.fact_id not in bucket:
                        bucket.append(fact.fact_id)

        self._sorted_vf = sorted(
            ((f.valid_from, fid) for fid, f in self._facts.items()),
            key=lambda x: x[0],
        )

    def _cache_fact_timestamps(self, fact: TemporalFact) -> None:
        """Parse a fact's score-relevant timestamps once, at index time."""
        ts_str = fact.last_verified_at or fact.observed_at
        parsed: Optional[datetime] = None
        if ts_str:
            try:
                parsed = _parse_iso(ts_str)
            except (ValueError, OverflowError):
                parsed = None  # freshness_score treats this as 0.5 too
        self._fact_fresh_ts[fact.fact_id] = parsed
        self._fact_invalid_ts.pop(fact.fact_id, None)
        if fact.invalid_at:
            try:
                self._fact_invalid_ts[fact.fact_id] = _parse_iso(fact.invalid_at)
            except (ValueError, OverflowError):
                pass  # absent from the cache -> search parses inline as before

    # ------------------------------------------------------------------
    # Write helpers
    # ------------------------------------------------------------------

    def _append(self, path: Path, record) -> None:
        with self._write_lock:
            with path.open("a", encoding="utf-8") as fh:
                fh.write(record.model_dump_json() + "\n")

    def _index_fact(self, fact: TemporalFact) -> None:
        self._fact_tokens[fact.fact_id] = _tokenize(fact.fact_text)
        self._cache_fact_timestamps(fact)
        for eid in (fact.subject_id, fact.object_id):
            if eid and eid in self._entities:
                bucket = self._facts_by_entity.setdefault(eid, [])
                if fact.fact_id not in bucket:
                    bucket.append(fact.fact_id)
        # Insert into sorted list maintaining order
        entry = (fact.valid_from, fact.fact_id)
        idx = bisect.bisect_left(self._sorted_vf, entry)
        if idx >= len(self._sorted_vf) or self._sorted_vf[idx] != entry:
            self._sorted_vf.insert(idx, entry)

    # ------------------------------------------------------------------
    # TemporalGraphProvider interface
    # ------------------------------------------------------------------

    def compact(self) -> dict:
        """Rewrite the JSONL files latest-wins, dropping superseded lines.

        The append-only write contract makes every upsert a new line, and
        loads re-read and re-validate every superseded line forever.
        Compaction keeps exactly the lines the loader would keep -- the raw
        line text of the last occurrence per id, NOT a model round-trip, so
        fields a newer schema version wrote survive untouched. Per file:
        skipped unless at least _COMPACT_MIN_SAVINGS_BYTES would be saved;
        a one-generation `<name>.pre-compact` safety copy is written first;
        the live file is swapped by atomic replace, so a concurrent reader
        always sees a complete file. Episodes are immutable (duplicate ids
        come only from re-ingestion) and compact identically. In-memory
        state is already latest-wins, so nothing is reloaded. Run after
        builds -- never on the read path, where a writer would race
        concurrent appenders in other processes.
        """
        id_keys = (
            (self._episodes_file, "episode_id"),
            (self._entities_file, "entity_id"),
            (self._facts_file, "fact_id"),
        )
        report: dict[str, dict] = {}
        # Cross-process guard: only one compactor at a time (lock file with
        # O_EXCL; a stale lock older than 10 minutes is broken). Compaction is
        # meant to run from the single builder process, after builds.
        lock_path = self._episodes_file.parent / ".compact.lock"
        if not _acquire_compact_lock(lock_path):
            return {"skipped": {"compacted": False, "reason": "another compact is running"}}
        try:
            return self._compact_locked(id_keys, report)
        finally:
            try:
                lock_path.unlink(missing_ok=True)
            except OSError:
                pass

    def _compact_locked(self, id_keys, report: dict) -> dict:
        with self._write_lock:
            for path, key in id_keys:
                if not path.exists():
                    continue
                raw = path.read_text(encoding="utf-8")
                latest: dict[str, str] = {}
                unparseable = 0
                for line in raw.splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        latest[json.loads(line)[key]] = line
                    except (ValueError, KeyError, TypeError):
                        unparseable += 1  # the loader skips these too
                kept_text = "\n".join(latest.values()) + ("\n" if latest else "")
                before = path.stat().st_size
                after = len(kept_text.encode("utf-8"))
                if before - after < _COMPACT_MIN_SAVINGS_BYTES:
                    report[path.name] = {"compacted": False, "bytes": before}
                    continue
                backup = path.with_name(path.name + ".pre-compact")
                tmp_backup = path.with_name(f".{path.name}.{os.getpid()}.bak-tmp")
                tmp_main = path.with_name(f".{path.name}.{os.getpid()}.compact-tmp")
                try:
                    tmp_backup.write_text(raw, encoding="utf-8")
                    os.replace(tmp_backup, backup)
                    tmp_main.write_text(kept_text, encoding="utf-8")
                    os.replace(tmp_main, path)
                finally:
                    for t in (tmp_backup, tmp_main):
                        try:
                            if t.exists():
                                t.unlink()
                        except OSError:
                            pass
                report[path.name] = {
                    "compacted": True,
                    "bytes_before": before,
                    "bytes_after": after,
                    "lines_kept": len(latest),
                    "unparseable_dropped": unparseable,
                }
        return report

    def reset_derived_state(self) -> None:
        """Delete all derived JSONL files and clear in-memory indexes. Used in tests."""
        with self._write_lock:
            for f in (self._episodes_file, self._entities_file,
                      self._facts_file, self._manifest_file):
                if f.exists():
                    f.unlink()
        self._episodes.clear()
        self._entities.clear()
        self._facts.clear()
        self._rebuild_indexes()

    def add_episode(self, episode: TemporalEpisode) -> str:
        """Append an episode to the in-memory store and episodes JSONL file. Returns episode_id."""
        self._episodes[episode.episode_id] = episode
        self._append(self._episodes_file, episode)
        return episode.episode_id

    def upsert_entity(self, entity: TemporalEntity) -> str:
        """Insert or merge an entity. Preserves first_seen_at and deduplicates source_refs."""
        existing = self._entities.get(entity.entity_id)
        if existing:
            entity = entity.model_copy(update={
                "first_seen_at": existing.first_seen_at,
                "source_refs": list(dict.fromkeys(existing.source_refs + entity.source_refs)),
            })
        self._entities[entity.entity_id] = entity
        self._entity_by_name[entity.name.lower()] = entity.entity_id
        for alias in entity.aliases:
            self._entity_by_name[alias.lower()] = entity.entity_id
        self._append(self._entities_file, entity)
        return entity.entity_id

    def add_fact(self, fact: TemporalFact) -> tuple[str, list[str]]:
        """Ingest a temporal fact and return (fact_id, contradiction_warnings).

        Validates ISO timestamps. Checks for contradictions: same subject+predicate,
        different object, overlapping validity window (multi-valued predicates exempt).
        Appends to facts JSONL and updates in-memory token index.
        """
        # Validate timestamps
        _validate_iso(fact.valid_from, "valid_from")
        _validate_iso(fact.observed_at, "observed_at")
        if fact.invalid_at:
            _validate_iso(fact.invalid_at, "invalid_at")

        warnings: list[str] = []

        # Contradiction: same subject+predicate, different object, overlapping validity window.
        # Multi-valued predicates (worked_on, has_tag, etc.) are exempt, and so is a
        # fact superseding ITSELF (same fact_id, new value): re-indexing a live
        # source updates counters like total_tokens on every build, and warning
        # against the fact's own previous version is an update, not a
        # contradiction (false warnings on every transcript re-index until
        # 2026-10-09).
        for existing in self._facts.values():
            if (
                fact.predicate not in _MULTI_VALUED_PREDICATES
                and existing.fact_id != fact.fact_id
                and existing.subject_id == fact.subject_id
                and existing.predicate == fact.predicate
                and existing.object_id != fact.object_id
                and _windows_overlap(
                    existing.valid_from, existing.invalid_at,
                    fact.valid_from, fact.invalid_at,
                )
            ):
                warnings.append(
                    f"Contradiction with {existing.fact_id!r}: "
                    f"same subject+predicate, different object, overlapping validity."
                )

        self._facts[fact.fact_id] = fact
        self._index_fact(fact)
        self._append(self._facts_file, fact)
        return fact.fact_id, warnings

    def invalidate_fact(self, fact_id: str, invalid_at: str, reason: str) -> None:
        """Mark a fact as invalid at a given ISO timestamp. Raises KeyError if not found."""
        if fact_id not in self._facts:
            raise KeyError(f"Fact {fact_id!r} not found.")
        _validate_iso(invalid_at, "invalid_at")
        updated = self._facts[fact_id].model_copy(update={
            "invalid_at": invalid_at,
            "invalidated_by_fact_id": reason,
        })
        self._facts[fact_id] = updated
        self._fact_tokens[fact_id] = _tokenize(updated.fact_text)
        self._cache_fact_timestamps(updated)
        self._append(self._facts_file, updated)

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------

    def search_current(self, query: str, limit: int = 10) -> list[TemporalResult]:
        """Full-text search for facts valid RIGHT NOW (no invalid_at, valid_from <= now)."""
        return self._search_at(query, datetime.now(timezone.utc), limit)

    def search_as_of(self, query: str, at: str, limit: int = 10) -> list[TemporalResult]:
        """Full-text search for facts valid at a specific ISO-8601 point in time."""
        return self._search_at(query, _parse_iso(at), limit)

    def _search_at(self, query: str, point: datetime, limit: int) -> list[TemporalResult]:
        q_tokens = _tokenize(query)
        point_str = point.strftime("%Y-%m-%dT%H:%M:%S+00:00")
        now = datetime.now(timezone.utc)
        decay = math.log(2) / _FRESHNESS_HALF_LIFE_DAYS

        # Use sorted valid_from to skip facts that haven't started yet
        cutoff_idx = bisect.bisect_right(self._sorted_vf, (point_str + "~", ""))

        # Score every candidate cheaply (cached tokens + cached parsed
        # timestamps); construct TemporalResult objects only for the top
        # `limit` after ranking. Building a pydantic result per candidate
        # was the per-query hot spot at corpus scale (15k facts -> 2 kept).
        scored: list[tuple[float, TemporalFact]] = []
        for _, fact_id in self._sorted_vf[:cutoff_idx]:
            fact = self._facts.get(fact_id)
            if fact is None:
                continue
            if fact.invalid_at:
                invalid = self._fact_invalid_ts.get(fact_id)
                if invalid is None:
                    invalid = _parse_iso(fact.invalid_at)
                if invalid <= point:
                    continue
            f_tokens = self._fact_tokens.get(fact_id, set())
            lex = lexical_relevance_from_tokens(q_tokens, f_tokens)
            fresh_ts = self._fact_fresh_ts.get(fact_id)
            if fresh_ts is None:
                fresh = 0.5
            else:
                age_days = (now - fresh_ts).total_seconds() / 86400
                try:
                    fresh = math.exp(-age_days * decay)
                except OverflowError:
                    fresh = 0.5
            authority = _AUTHORITY_WEIGHTS.get(fact.authority, 0.7)
            score = (lex * 0.40) + (fresh * 0.30) + (fact.confidence * 0.20) + (authority * 0.10)
            scored.append((score, fact))

        scored.sort(key=lambda sf: sf[0], reverse=True)

        results = []
        for score, fact in scored[:limit]:
            provenance = list(fact.evidence_refs) or [fact.fact_id]
            if fact.supersedes_fact_id:
                provenance.append(f"supersedes:{fact.supersedes_fact_id}")
            results.append(TemporalResult(
                fact=fact,
                entity=self._entities.get(fact.subject_id),
                score=score,
                provenance=provenance,
                freshness_warning=freshness_warning(fact),
            ))
        return results

    def history(self, entity_query: str, limit: int = 50) -> TemporalHistory:
        """Return fact timeline for an entity (name, alias, or ID).

        Resolves the entity by name/alias, then returns all associated facts
        sorted by valid_from descending. When entity is not found, returns a
        TemporalHistory with the entity set to a dummy 'not-found' placeholder.
        """
        eid = self._resolve_entity(entity_query)
        if eid is None:
            dummy = TemporalEntity(
                entity_id="not-found",
                name=entity_query,
                first_seen_at=_now_iso(),
                last_seen_at=_now_iso(),
            )
            return TemporalHistory(
                entity=dummy,
                facts=[],
                warnings=[f"Entity {entity_query!r} not found in temporal graph."],
            )

        entity = self._entities[eid]
        fact_ids = self._facts_by_entity.get(eid, [])
        facts = sorted(
            [self._facts[fid] for fid in fact_ids if fid in self._facts],
            key=lambda f: f.valid_from,
        )[:limit]

        now = datetime.now(timezone.utc)
        current_fact = next((f for f in reversed(facts) if _is_valid_at(f, now)), None)

        warnings: list[str] = []
        # Only warn when the SAME predicate has multiple active facts (different objects)
        from collections import Counter
        active_by_pred = Counter(f.predicate for f in facts if f.invalid_at is None)
        for pred, count in active_by_pred.items():
            if count > 1 and pred not in _MULTI_VALUED_PREDICATES:
                warnings.append(
                    f"Predicate {pred!r} has {count} active facts -- possible contradiction."
                )

        return TemporalHistory(
            entity=entity,
            facts=facts,
            current_fact=current_fact,
            warnings=warnings,
        )

    def _resolve_entity(self, query: str) -> Optional[str]:
        q = query.lower().strip()
        if q in self._entity_by_name:
            return self._entity_by_name[q]
        for name, eid in self._entity_by_name.items():
            if name.startswith(q):
                return eid
        for name, eid in self._entity_by_name.items():
            if q in name:
                return eid
        q_tokens = set(q.split())
        best_eid, best_score = None, 0
        for name, eid in self._entity_by_name.items():
            overlap = len(q_tokens & set(name.split()))
            if overlap > best_score:
                best_score, best_eid = overlap, eid
        return best_eid if best_score > 0 else None

    def list_entities(self, limit: int = 50) -> list[TemporalEntity]:
        """Return up to `limit` entities, sorted alphabetically by name."""
        entities = sorted(self._entities.values(), key=lambda e: e.name.lower())
        return entities[:limit]

    def stats(self) -> dict:
        """Return summary counts: episodes, entities, total facts, active facts, graph_root."""
        return {
            "episodes": len(self._episodes),
            "entities": len(self._entities),
            "facts": len(self._facts),
            "facts_active": sum(1 for f in self._facts.values() if f.invalid_at is None),
            "graph_root": str(self._root),
        }
