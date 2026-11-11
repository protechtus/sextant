"""Atlas temporal graph -- bitemporal fact store with current/as_of/history queries.

First slice: file-backed JSONL provider, fixture source, deterministic queries.
No SQL, no graph daemon, no network. Derived state only -- .memory/ is never written.

Entry points:
    atlas-temporal build   # or --source fixtures for the built-in demo data
    atlas-temporal query "..." --mode current
    atlas-temporal query "..." --mode as-of --at 2026-05-08
    atlas-temporal history "Atlas MCP Tunnel"
"""

from atlas_plus.temporal_graph.models import (
    TemporalEpisode,
    TemporalEntity,
    TemporalFact,
    TemporalResult,
    TemporalHistory,
)
from atlas_plus.temporal_graph.provider import TemporalGraphProvider
from atlas_plus.temporal_graph.file_provider import FileTemporalProvider

__all__ = [
    "TemporalEpisode",
    "TemporalEntity",
    "TemporalFact",
    "TemporalResult",
    "TemporalHistory",
    "TemporalGraphProvider",
    "FileTemporalProvider",
]
