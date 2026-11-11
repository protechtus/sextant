"""Abstract provider interface for the Atlas temporal graph.

FileTemporalProvider is the required first-slice implementation (JSONL-backed, in-memory indexes).
GraphitiTemporalProvider is an optional experiment -- add only after the file provider proves value.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from atlas_plus.temporal_graph.models import (
    TemporalEpisode,
    TemporalEntity,
    TemporalFact,
    TemporalHistory,
    TemporalResult,
)


class TemporalGraphProvider(ABC):
    """Abstract base class defining the temporal graph read/write contract."""

    @abstractmethod
    def reset_derived_state(self) -> None:
        """Delete all derived JSONL files and reset in-memory indexes. Does not touch .memory/."""

    @abstractmethod
    def add_episode(self, episode: TemporalEpisode) -> str:
        """Index an episode. Returns episode_id."""

    @abstractmethod
    def upsert_entity(self, entity: TemporalEntity) -> str:
        """Add or update an entity. Returns entity_id."""

    @abstractmethod
    def add_fact(self, fact: TemporalFact) -> tuple[str, list[str]]:
        """Add a fact. Returns (fact_id, warnings). Warnings are issued for contradictions."""

    @abstractmethod
    def invalidate_fact(self, fact_id: str, invalid_at: str, reason: str) -> None:
        """Mark a fact as no longer valid from invalid_at onward."""

    @abstractmethod
    def search_current(self, query: str, limit: int = 10) -> list[TemporalResult]:
        """Return facts valid right now, ranked by relevance and freshness."""

    @abstractmethod
    def search_as_of(self, query: str, at: str, limit: int = 10) -> list[TemporalResult]:
        """Return facts valid at the requested point in time (ISO 8601 date or datetime)."""

    @abstractmethod
    def history(self, entity_query: str, limit: int = 50) -> TemporalHistory:
        """Return full temporal history for an entity, sorted by valid_from."""

    @abstractmethod
    def list_entities(self, limit: int = 50) -> list[TemporalEntity]:
        """Return up to `limit` entities, sorted alphabetically by name."""

    @abstractmethod
    def stats(self) -> dict:
        """Return counts of episodes, entities, and facts in the store."""
