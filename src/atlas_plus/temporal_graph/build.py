"""Shared temporal graph build helper."""

from __future__ import annotations

from pathlib import Path
from typing import Optional


def build_temporal_graph(source: str = "atlas", graph_root: Optional[Path] = None) -> dict:
    """Rebuild or incrementally update the Atlas temporal graph index."""
    from atlas_plus.temporal_graph.file_provider import FileTemporalProvider
    from atlas_plus.temporal_graph.sources import (
        ATLAS_PHASE4_FIXTURE,
        ClaudeCodeTranscriptSource,
        FixtureSource,
        Manifest,
        MemoryMarkdownSource,
        SessionLogSource,
        TranscriptMiningSource,
    )

    provider = FileTemporalProvider(graph_root=graph_root)
    manifest = Manifest.load(provider._manifest_file)
    totals = {
        "files_indexed": 0, "files_skipped": 0, "facts": 0, "warnings": [],
        # Retraction counters (2026-10-09): sources that notice a vanished
        # file invalidate its facts; the build result surfaces that work
        # instead of silently dropping the counts.
        "files_retracted": 0, "facts_invalidated": 0,
    }

    def _run(fn):
        result = fn()
        totals["files_indexed"] += result.get("files_indexed", result.get("episodes", 0))
        totals["files_skipped"] += result.get("files_skipped", 0)
        totals["facts"] += result.get("facts", 0)
        totals["files_retracted"] += result.get("files_retracted", 0)
        totals["facts_invalidated"] += result.get("facts_invalidated", 0)
        totals["warnings"].extend(result.get("warnings", []))

    if source == "atlas":
        _run(lambda: MemoryMarkdownSource().ingest(provider, manifest))
        _run(lambda: SessionLogSource().ingest(provider, manifest))
    elif source == "memory":
        _run(lambda: MemoryMarkdownSource().ingest(provider, manifest))
    elif source == "sessions":
        _run(lambda: SessionLogSource().ingest(provider, manifest))
    elif source == "transcripts":
        _run(lambda: ClaudeCodeTranscriptSource().ingest(provider, manifest))
    elif source == "mining":
        _run(lambda: TranscriptMiningSource().ingest(provider, manifest))
    elif source == "all":
        _run(lambda: MemoryMarkdownSource().ingest(provider, manifest))
        _run(lambda: SessionLogSource().ingest(provider, manifest))
        _run(lambda: ClaudeCodeTranscriptSource().ingest(provider, manifest))
        _run(lambda: TranscriptMiningSource().ingest(provider, manifest))
    elif source == "fixtures":
        _run(lambda: FixtureSource(ATLAS_PHASE4_FIXTURE).ingest(provider, manifest))
    else:
        return {"error": f"Unknown source {source!r}. Valid: atlas, memory, sessions, transcripts, mining, all, fixtures"}

    # Append-only upserts leave superseded lines behind; compact after every
    # build so load cost stays proportional to live data (threshold-gated,
    # so small graphs are never rewritten).
    compaction = provider.compact()

    stats = provider.stats()
    try:
        from atlas_plus import activity
        activity.record(
            "temporal", "graph_built", build_source=source,
            files_indexed=totals["files_indexed"], facts_added=totals["facts"],
            facts_active=stats["facts_active"],
        )
    except Exception:
        pass
    return {
        "source": source,
        "compaction": {
            name: rep for name, rep in compaction.items() if rep.get("compacted")
        },
        "files_indexed": totals["files_indexed"],
        "files_skipped": totals["files_skipped"],
        "files_retracted": totals["files_retracted"],
        "facts_invalidated": totals["facts_invalidated"],
        "facts_added": totals["facts"],
        "warnings": totals["warnings"][:5],
        "index": {
            "episodes": stats["episodes"],
            "entities": stats["entities"],
            "facts_total": stats["facts"],
            "facts_active": stats["facts_active"],
        },
    }
