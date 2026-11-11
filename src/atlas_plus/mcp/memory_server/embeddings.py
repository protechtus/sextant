"""Hybrid semantic search: local embeddings fused with FTS (product gap G-2).

FTS5 finds what you literally said; this finds what you meant. Vectors come
from a LOCAL Ollama embeddings endpoint (no cloud on the search path, ever)
and live as packed float32 blobs in the same state.sqlite the FTS index
uses -- no new dependencies at corpus scale (a few thousand small files;
pure-Python cosine costs milliseconds).

DEGRADATION IS THE CONTRACT: with no Ollama, no index, or any error, search
behaves exactly as before (FTS-only) and says so via `retrieval: "fts-only"`.
A public install without Ollama loses nothing it had.

Index:   atlas-embed-index [--force | --status]   (also nightly via Brain)
Query:   memory_search_tool fuses both retrievers by Reciprocal Rank Fusion.
Env:     ATLAS_SEMANTIC_SEARCH=off   kill switch (default: auto)
         ATLAS_EMBED_URL             default http://127.0.0.1:11434
         ATLAS_EMBED_MODEL           default nomic-embed-text
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import struct
import urllib.request
from pathlib import Path

from . import fts as _fts
from .fts import _connect, _walk_memory

_SCHEMA = """
CREATE TABLE IF NOT EXISTS memory_vectors (
    path         TEXT PRIMARY KEY,
    content_hash TEXT NOT NULL,
    dim          INTEGER NOT NULL,
    vec          BLOB NOT NULL
);
"""

RRF_K = 60


def _ensure_schema(conn) -> None:
    conn.execute(_SCHEMA)


def enabled() -> bool:
    return os.environ.get("ATLAS_SEMANTIC_SEARCH", "").strip().lower() != "off"


class OllamaEmbedder:
    """Minimal client for Ollama's embeddings endpoint. Stdlib only."""

    def __init__(self, base_url: str | None = None, model: str | None = None,
                 timeout: float = 15.0) -> None:
        self.base_url = (base_url or os.environ.get("ATLAS_EMBED_URL")
                         or "http://127.0.0.1:11434").rstrip("/")
        # Scheme allowlist (bandit B310 hardening): ATLAS_EMBED_URL is
        # user-config; refuse file:// and custom schemes before any urlopen.
        if not self.base_url.startswith(("http://", "https://")):
            raise ValueError(
                f"ATLAS_EMBED_URL must be http(s), got: {self.base_url!r}"
            )
        self.model = model or os.environ.get("ATLAS_EMBED_MODEL") or "nomic-embed-text"
        self.timeout = timeout

    def embed(self, text: str) -> list[float]:
        # /api/embed (modern), not the legacy /api/embeddings: newer Ollama
        # hard-errors the legacy endpoint on over-context input ("the input
        # length exceeds the context length") where /api/embed truncates
        # gracefully -- an Ollama update silently broke embedding for every
        # corpus file over the model's context (found 2026-10-04: the same
        # 68 large files failed on every index run).
        payload = json.dumps({"model": self.model, "input": text}).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base_url}/api/embed", data=payload,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:  # nosec B310 (scheme allowlisted in __init__)
            data = json.loads(resp.read().decode("utf-8"))
        rows = data.get("embeddings")
        vec = rows[0] if isinstance(rows, list) and rows else None
        if not isinstance(vec, list) or not vec:
            raise ValueError("embed endpoint returned no vector")
        return [float(x) for x in vec]


def _normalize(vec: list[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in vec))
    if norm == 0:
        return vec
    return [x / norm for x in vec]


def _pack(vec: list[float]) -> bytes:
    return struct.pack(f"{len(vec)}f", *vec)


def _unpack(blob: bytes, dim: int) -> list[float]:
    return list(struct.unpack(f"{dim}f", blob))


def _body_of(path: Path) -> str:
    """File text minus YAML frontmatter (search should match content, not keys)."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            text = text[end + 4:]
    return text.strip()


def index_status() -> dict:
    conn = _connect()
    try:
        _ensure_schema(conn)
        indexed = conn.execute("SELECT COUNT(*) FROM memory_vectors").fetchone()[0]
    finally:
        conn.close()
    total = sum(1 for _ in _walk_memory())
    return {
        "indexed": indexed,
        "corpus_files": total,
        "model": os.environ.get("ATLAS_EMBED_MODEL") or "nomic-embed-text",
        "enabled": enabled(),
    }


def rebuild_index(embedder=None, force: bool = False) -> dict:
    """Incrementally (re)embed changed files. Returns counters; never raises
    on a per-file embed failure (skips and counts it)."""
    if embedder is None:
        embedder = OllamaEmbedder()
    conn = _connect()
    added = updated = skipped = removed = failed = truncated = 0
    try:
        _ensure_schema(conn)
        existing = {
            row[0]: row[1]
            for row in conn.execute("SELECT path, content_hash FROM memory_vectors")
        }
        seen: set[str] = set()
        for rel, abs_path in _walk_memory():
            seen.add(rel)
            body = _body_of(Path(abs_path))
            if not body:
                continue
            content_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()
            if not force and existing.get(rel) == content_hash:
                skipped += 1
                continue
            if len(body) > 8000:
                # Visible, not silent (expert pass 2026-10-05, finding 10):
                # only the head of an over-length file is embedded (and the
                # model's own context truncates further) -- semantic recall
                # for the tail is weaker. The count surfaces it to the
                # operator; splitting/chunking stays a deliberate decision.
                truncated += 1
            try:
                vec = _normalize(embedder.embed(body[:8000]))
            except Exception:
                failed += 1
                continue
            conn.execute(
                "INSERT OR REPLACE INTO memory_vectors(path, content_hash, dim, vec)"
                " VALUES (?,?,?,?)",
                (rel, content_hash, len(vec), _pack(vec)),
            )
            if rel in existing:
                updated += 1
            else:
                added += 1
        gone = set(existing) - seen
        for rel in gone:
            conn.execute("DELETE FROM memory_vectors WHERE path = ?", (rel,))
            removed += 1
        conn.commit()
    finally:
        conn.close()
    return {"added": added, "updated": updated, "skipped": skipped,
            "removed": removed, "failed": failed,
            "truncated_to_head": truncated}


# Vector cache (measured, quality loop run 31): per-query load+unpack of
# every row cost 63ms of the 146ms vector leg at ~2,200 vectors. Cache the
# unpacked vectors keyed by the DB file's (st_mtime_ns, st_size) -- an
# embed-index run rewrites the DB and invalidates naturally. Built-then-
# swapped as one tuple, so concurrent readers see old-or-new, never torn.
# numpy, WHEN IMPORTABLE, turns the 91ms pure-python cosine loop into a
# 0.2ms matmul; it is an optional fast path, never a dependency.
_vec_cache: tuple | None = None  # (stat_key, paths, dims, vecs, np_matrix)

try:  # optional accelerator only -- the engine never requires numpy
    import numpy as _np
except ImportError:  # pragma: no cover -- exercised on minimal installs
    _np = None


def _load_vectors():
    """Return (paths, dims, vecs, np_matrix|None) for all rows, cached."""
    global _vec_cache
    try:
        # The vectors share fts.py's state.sqlite; read the path through
        # the module so test monkeypatching of fts.FTS_DB is honored. Any
        # DB write (embed-index OR an FTS freshness sweep) bumps the stat
        # key -- over-invalidation is correctness-safe, just a reload.
        st = os.stat(_fts.FTS_DB)
        key = (st.st_mtime_ns, st.st_size)
    except OSError:
        key = None
    cached = _vec_cache
    if cached is not None and cached[0] == key and key is not None:
        return cached[1], cached[2], cached[3], cached[4]
    conn = _connect()
    try:
        _ensure_schema(conn)
        rows = conn.execute("SELECT path, dim, vec FROM memory_vectors").fetchall()
    finally:
        conn.close()
    paths = [r[0] for r in rows]
    dims = [r[1] for r in rows]
    vecs = [_unpack(blob, dim) for _, dim, blob in rows]
    matrix = None
    if _np is not None and vecs:
        main_dim = max(set(dims), key=dims.count)
        if all(d == main_dim for d in dims):
            matrix = _np.array(vecs, dtype=_np.float32)
    if key is not None:
        _vec_cache = (key, paths, dims, vecs, matrix)
    return paths, dims, vecs, matrix


def vector_search(query: str, scope: str = "", limit: int = 10,
                  embedder=None) -> list[dict]:
    """Top-k cosine matches: [{path, score(0..1)}]. [] on ANY failure."""
    try:
        if embedder is None:
            embedder = OllamaEmbedder()
        qvec = _normalize(embedder.embed(query))
        paths, dims, vecs, matrix = _load_vectors()
        # a scope naming a file matches that file; otherwise a directory prefix
        prefix = scope if scope.lower().endswith(".md") else (scope.rstrip("/") + "/" if scope else "")
        scored: list[tuple[float, str]] = []
        if matrix is not None and matrix.shape[1] == len(qvec):
            sims = matrix @ _np.array(qvec, dtype=_np.float32)
            for i, path in enumerate(paths):
                if prefix and not path.startswith(prefix):
                    continue
                scored.append((float(sims[i]), path))
        else:
            for path, dim, vec in zip(paths, dims, vecs):
                if prefix and not path.startswith(prefix):
                    continue
                if len(vec) != len(qvec):
                    continue  # model changed; stale row until next rebuild
                cos = sum(a * b for a, b in zip(qvec, vec))
                scored.append((cos, path))
        scored.sort(reverse=True)
        return [
            {"path": p, "score": max(0.0, min(1.0, round(c, 4)))}
            for c, p in scored[:limit]
        ]
    except Exception:
        return []


def _snippet_for(rel: str, length: int = 150) -> str:
    from .config import MEMORY_ROOT

    body = _body_of(MEMORY_ROOT / rel)
    return body[:length] + ("..." if len(body) > length else "")


def hybrid_search(query: str, scope: str = "", limit: int = 10,
                  embedder=None) -> tuple[list[dict], str]:
    """Fuse FTS and vector retrieval by RRF. Returns (results, mode).

    Result contract preserved for confidence bands: `score` stays a 0..1
    quality signal (the best of the two retrievers' normalized scores for
    that file); ORDERING comes from RRF. Each result lists its sources.
    """
    from .fts import search as fts_search

    fts_results = fts_search(query, scope=scope, limit=limit)
    if not enabled():
        return fts_results, "fts-only"
    vec_results = vector_search(query, scope=scope, limit=limit, embedder=embedder)
    if not vec_results:
        return fts_results, "fts-only"

    fused: dict[str, dict] = {}
    for rank, r in enumerate(fts_results):
        entry = fused.setdefault(r["path"], {
            "path": r["path"], "score": 0.0, "rrf": 0.0, "sources": [],
        })
        entry["rrf"] += 1.0 / (RRF_K + rank + 1)
        entry["score"] = max(entry["score"], float(r.get("score", 0.0)))
        entry["snippet"] = r.get("snippet", "")
        entry["sources"].append("fts")
    for rank, r in enumerate(vec_results):
        entry = fused.setdefault(r["path"], {
            "path": r["path"], "score": 0.0, "rrf": 0.0, "sources": [],
        })
        entry["rrf"] += 1.0 / (RRF_K + rank + 1)
        entry["score"] = max(entry["score"], float(r.get("score", 0.0)))
        entry["sources"].append("vector")

    merged = sorted(fused.values(), key=lambda e: e["rrf"], reverse=True)[:limit]
    for entry in merged:
        entry.pop("rrf", None)
        if not entry.get("snippet"):
            entry["snippet"] = _snippet_for(entry["path"])
    return merged, "hybrid"


def main() -> None:
    """Entry point for atlas-embed-index."""
    import argparse

    parser = argparse.ArgumentParser(
        prog=__import__("atlas_plus.branding", fromlist=["prog"]).prog("atlas-embed-index"),
        description="Build/refresh the local semantic index (Ollama embeddings).",
    )
    parser.add_argument("--force", action="store_true", help="re-embed everything")
    parser.add_argument("--status", action="store_true", help="show index status and exit")
    args = parser.parse_args()

    if args.status:
        print(json.dumps(index_status(), indent=2))
        return
    result = rebuild_index(force=args.force)
    print(json.dumps(result, indent=2))
    if result["failed"] and not any(
        (result["added"], result["updated"])
    ):
        raise SystemExit(
            "all embeds failed -- is Ollama running with the embed model pulled? "
            "(ollama pull nomic-embed-text)"
        )


if __name__ == "__main__":
    main()
