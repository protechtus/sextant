"""Transcript Manager — crash-resilient capture across every LLM client.

See `docs/adr/006-transcript-manager.md` for the design rationale and
`docs/HOWTO-proxies.md` for usage.

Public API:
    TranscriptStore     append-only NDJSON writer with per-conversation hashing
    TranscriptIndex     SQLite + FTS5 metadata index
    TurnRecord          canonical record schema
    recover_all         startup .partial walker
"""

from atlas_plus.transcripts.store import TranscriptStore, TurnRecord
from atlas_plus.transcripts.index import TranscriptIndex
from atlas_plus.transcripts.recovery import recover_all

__all__ = ["TranscriptStore", "TurnRecord", "TranscriptIndex", "recover_all"]
