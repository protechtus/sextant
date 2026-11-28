"""The vector cache keeps one copy of the vectors (Q25, 2026-11-27).

Measured on a corpus of 2,323 vectors of 768 numbers: after the first search a server process kept
the numpy matrix (6.8 MB) and, beside it, the same numbers as Python lists (54.6 MB). These tests
ask a fresh interpreter what the cache holds after a search and what the search answers.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap

import pytest

pytest.importorskip("numpy")

_CHILD = textwrap.dedent(
    """
    import json, random, struct, tracemalloc
    from atlas_plus.mcp.memory_server import embeddings as E, fts

    dim, rows = 256, 1500
    rnd = random.Random(25)
    conn = fts._connect()
    E._ensure_schema(conn)
    cols = [r[1] for r in conn.execute("pragma table_info(memory_vectors)")]
    for i in range(rows):
        v = E._normalize([rnd.uniform(-1, 1) for _ in range(dim)])
        rec = {"path": f"reference/n{i:04d}.md", "dim": dim, "vec": struct.pack(f"<{dim}f", *v)}
        for c in cols:
            rec.setdefault(c, "")
        conn.execute(f"insert into memory_vectors ({','.join(cols)}) values ({','.join('?' for _ in cols)})",
                     [rec[c] for c in cols])
    conn.commit(); conn.close()

    class Fixed:
        def __init__(self, n): self.n = n
        def embed(self, text):
            r = random.Random(text)
            return [r.uniform(-1, 1) for _ in range(self.n)]

    tracemalloc.start()
    fast = E.vector_search("alpha", limit=15, embedder=Fixed(dim))
    held = tracemalloc.get_traced_memory()[0]
    tracemalloc.stop()
    scoped = E.vector_search("alpha", scope="reference", limit=5, embedder=Fixed(dim))
    other_len_fast = E.vector_search("alpha", limit=15, embedder=Fixed(dim + 8))
    used_matrix = E._vec_cache is not None and E._vec_cache[4] is not None
    E._np = None
    E._vec_cache = None
    pure = E.vector_search("alpha", limit=15, embedder=Fixed(dim))
    other_len_pure = E.vector_search("alpha", limit=15, embedder=Fixed(dim + 8))
    print(json.dumps({"fast": fast, "pure": pure, "scoped": scoped, "held_mb": held / 1048576,
                      "used_matrix": used_matrix, "numbers_mb": rows * dim * 4 / 1048576,
                      "other_len_fast": other_len_fast, "other_len_pure": other_len_pure}))
    """
)


@pytest.fixture(scope="module")
def outcome(tmp_path_factory):
    ws = tmp_path_factory.mktemp("q25ws")
    (ws / ".memory").mkdir()
    (ws / ".atlas").mkdir()
    env = dict(os.environ)
    env["ATLAS_WORKSPACE_ROOT"] = str(ws)
    env["ATLAS_MEMORY_ROOT"] = str(ws / ".memory")
    env["ATLAS_STATE_DIR"] = str(ws / ".atlas")
    out = subprocess.run([sys.executable, "-c", _CHILD], env=env, capture_output=True, text=True,
                         timeout=180, stdin=subprocess.DEVNULL, cwd=str(ws))
    assert out.returncode == 0, out.stderr[-2000:]
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_the_search_used_the_matrix(outcome):
    assert outcome["used_matrix"] is True
    assert len(outcome["fast"]) == 15


def test_after_a_search_the_process_holds_about_one_copy_of_the_numbers(outcome):
    # One float32 copy is `numbers_mb` (1.5 MB here). The Python lists beside it were another
    # 8 times that. Twice the numbers plus 1 MB leaves room for the paths and the result.
    assert outcome["held_mb"] < 2 * outcome["numbers_mb"] + 1, (
        f"{outcome['held_mb']:.1f} MB held for {outcome['numbers_mb']:.1f} MB of vectors")


def test_the_matrix_path_and_the_pure_path_give_the_same_answer(outcome):
    assert [r["path"] for r in outcome["fast"]] == [r["path"] for r in outcome["pure"]]
    for a, b in zip(outcome["fast"], outcome["pure"]):
        assert abs(a["score"] - b["score"]) < 1e-3


def test_a_scope_still_filters(outcome):
    assert len(outcome["scoped"]) == 5
    assert all(r["path"].startswith("reference/") for r in outcome["scoped"])


def test_a_query_vector_of_another_length_matches_nothing_on_both_paths(outcome):
    # The embedding model changed since the index was built: no row can be compared, and the
    # search degrades to an empty vector leg (the caller falls back to FTS), never an error.
    assert outcome["other_len_fast"] == []
    assert outcome["other_len_pure"] == []
