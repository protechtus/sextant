"""Pydantic models for the Atlas temporal graph.

Five timestamps on TemporalFact answer five different questions:
  valid_from        -- when did this become true in the world?
  invalid_at        -- when did it stop being true? (null = still true)
  observed_at       -- when did Atlas first see evidence for it?
  ingested_at       -- when did the graph index it?
  last_verified_at  -- when was it last confirmed by a source re-read?

This avoids the RAG failure where "old but semantically relevant" beats "new and correct."
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Optional

from pydantic import BaseModel, Field


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")


def _sha256_short(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:12]


class TemporalEpisode(BaseModel):
    """A raw ingested chunk from one source system (memory file, transcript turn, session log entry)."""

    episode_id: str
    source_system: str          # memory | transcript | session_log | fixture
    source_path: str            # local path or stable id
    source_type: str            # markdown | turn | summary | event | fixture
    body: str
    body_hash: str
    reference_time: str         # when the content happened (ISO 8601 UTC)
    ingested_at: str            # when indexed (ISO 8601 UTC)
    metadata: dict = Field(default_factory=dict)


class TemporalEntity(BaseModel):
    """A named entity (person, project, system) extracted from episodes and tracked over time."""

    entity_id: str
    name: str
    labels: list[str] = Field(default_factory=list)
    aliases: list[str] = Field(default_factory=list)
    summary: str = ""
    first_seen_at: str          # ISO 8601 UTC
    last_seen_at: str           # ISO 8601 UTC
    source_refs: list[str] = Field(default_factory=list)


class TemporalFact(BaseModel):
    """A time-scoped predicate triple (subject-predicate-object) with provenance and confidence."""

    fact_id: str
    subject_id: str             # entity_id
    predicate: str              # relationship type, e.g. "blocked_by", "is_live_at"
    object_id: str              # entity_id or literal value
    fact_text: str              # human-readable statement
    valid_from: str             # ISO 8601 UTC
    invalid_at: Optional[str] = None
    observed_at: str            # ISO 8601 UTC
    ingested_at: str            # ISO 8601 UTC
    source_updated_at: Optional[str] = None
    last_verified_at: Optional[str] = None
    confidence: float = 1.0
    authority: str = "memory"   # memory | session_log | transcript | fixture
    evidence_refs: list[str] = Field(default_factory=list)
    supersedes_fact_id: Optional[str] = None
    invalidated_by_fact_id: Optional[str] = None


class TemporalResult(BaseModel):
    """Search result wrapper: a scored fact with optional entity context and freshness warnings."""

    fact: TemporalFact
    entity: Optional[TemporalEntity] = None
    score: float = 0.0
    provenance: list[str] = Field(default_factory=list)
    freshness_warning: Optional[str] = None


class TemporalHistory(BaseModel):
    """Full fact history for a single entity: all facts ordered by valid_from with the current one highlighted."""

    entity: TemporalEntity
    facts: list[TemporalFact]   # chronological by valid_from
    current_fact: Optional[TemporalFact] = None
    warnings: list[str] = Field(default_factory=list)
