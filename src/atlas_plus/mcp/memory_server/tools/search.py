"""memory.search — FTS5 full-text search over .memory/."""

from .. import fts as _fts


def memory_search(query: str, scope: str = "", limit: int = 10) -> list[dict]:
    """Search .memory/ using FTS5.

    Args:
        query: FTS5 query string (e.g. "dark mode", "preference AND editor").
        scope: Optional subdirectory prefix to narrow search (e.g. "feedback").
        limit: Max results, capped at 50.

    Returns list of {path, snippet, score}.
    """
    limit = min(limit, 50)
    if not query.strip():
        return []
    return _fts.search(query, scope=scope, limit=limit)


MAX_QUERY_CHARS = 2_000


def memory_search_hybrid(query: str, scope: str = "", limit: int = 10) -> tuple[list[dict], str]:
    """Hybrid (FTS + local-embedding) search. Returns (results, retrieval_mode).

    retrieval_mode is "hybrid" when the semantic index contributed, else
    "fts-only" (no index, Ollama down, or kill switch) -- in which case the
    results are byte-identical to memory_search's.
    """
    limit = min(limit, 50)
    if not query.strip():
        return [], "fts-only"
    # A query is a few words; past this length nothing matches better and the embedder and the
    # log pay for every byte (AG10: a 50 MB query took 18.6 s).
    query = query[:MAX_QUERY_CHARS]
    from .. import embeddings as _emb

    return _emb.hybrid_search(query, scope=scope, limit=limit)


def normalize_search_scope(scope: str) -> str:
    """Accept the same scope grammar the write tools use (``project:<name>``,
    ``reference:<topic>``, ``preference:<topic>``) as a search scope; a plain directory
    prefix passes through unchanged. Unknown scopes search unscoped rather than lie."""
    scope = (scope or "").strip()
    if ":" not in scope:
        return scope
    try:
        import importlib
        from pathlib import Path as _Path
        _cfg = importlib.import_module("atlas_plus.mcp.memory_server.config")
        from ..routing import resolve_target
        target, _rule = resolve_target(scope, for_read=True)
        return target.resolve().relative_to(_Path(_cfg.MEMORY_ROOT).resolve()).as_posix()
    except Exception:  # noqa: BLE001
        return ""


def confidence_band(results: list) -> str:
    """high / medium / low / starved from the top re-ranked score (one definition for the
    MCP tool, the CLI and the dashboard)."""
    top = max((float(r.get("score", 0)) for r in results), default=0.0)
    if not results or top < 0.2:
        return "starved"
    if top < 0.4:
        return "low"
    if top < 0.7:
        return "medium"
    return "high"
