"""The optional numpy fast path loads with one BLAS thread (Q25, 2026-11-27).

Measured on Windows with 24 logical cores: the OpenBLAS inside the numpy wheel commits one 32 MB
buffer per logical core when it loads, so every MCP session's server process held about 840 MB,
768 MB of it for a matrix-vector product one thread does in a fraction of a millisecond.

These tests ask a FRESH interpreter for the outcome (how many threads the loaded BLAS has, what the
process holds, what a child would inherit, what a search answers); none of them reads the source.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap

import pytest

pytest.importorskip("numpy")

_VARS = ("OPENBLAS_NUM_THREADS", "GOTO_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS")

# Runs in the child. Prints one JSON line: the BLAS thread count (None when the BLAS is not an
# OpenBLAS we can ask), the variable as a child process would inherit it, and whether numpy loaded.
_PROBE = textwrap.dedent(
    """
    import ctypes, glob, json, os, sys
    PRELUDE
    from atlas_plus.mcp.memory_server import embeddings as E

    def blas_threads():
        np = E._np
        if np is None:
            return None
        root = os.path.dirname(np.__file__)
        libs = []
        for pat in ("../numpy.libs/*openblas*", ".dylibs/*openblas*", "../numpy.libs/*scipy_openblas*"):
            libs += glob.glob(os.path.join(root, pat))
        for path in libs:
            try:
                lib = ctypes.CDLL(path)
            except OSError:
                continue
            for name in ("scipy_openblas_get_num_threads64_", "scipy_openblas_get_num_threads",
                         "openblas_get_num_threads64_", "openblas_get_num_threads"):
                fn = getattr(lib, name, None)
                if fn is not None:
                    fn.restype = ctypes.c_int
                    return int(fn())
        return None

    print(json.dumps({"numpy": E._np is not None, "blas_threads": blas_threads(),
                      "inherited": os.environ.get("OPENBLAS_NUM_THREADS"),
                      "extra": EXTRA}))
    """
)


def _fresh(env_over: dict | None = None, prelude: str = "", extra: str = "None") -> dict:
    env = {k: v for k, v in os.environ.items() if k not in _VARS}
    env.update(env_over or {})
    code = _PROBE.replace("PRELUDE", prelude).replace("EXTRA", extra)
    out = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True,
                         timeout=120, stdin=subprocess.DEVNULL)
    assert out.returncode == 0, out.stderr[-2000:]
    return json.loads(out.stdout.strip().splitlines()[-1])


def _need_openblas(got: dict) -> int:
    if got["blas_threads"] is None:
        pytest.skip("this numpy's BLAS is not an OpenBLAS that reports its thread count")
    return got["blas_threads"]


def test_a_fresh_process_gets_one_blas_thread():
    if (os.cpu_count() or 1) < 2:
        pytest.skip("one core: one thread either way")
    got = _fresh()
    assert got["numpy"] is True
    assert _need_openblas(got) == 1


def test_the_users_own_thread_count_wins():
    if (os.cpu_count() or 1) < 2:
        pytest.skip("one core: OpenBLAS cannot be given two threads")
    got = _fresh({"OPENBLAS_NUM_THREADS": "2"})
    assert _need_openblas(got) == 2
    assert got["inherited"] == "2", "the user's variable must be left as the user set it"


def test_another_thread_variable_set_by_the_user_is_respected():
    if (os.cpu_count() or 1) < 2:
        pytest.skip("one core")
    got = _fresh({"OMP_NUM_THREADS": "2"})
    assert _need_openblas(got) == 2
    assert got["inherited"] is None


def test_child_processes_do_not_inherit_the_cap():
    got = _fresh()
    assert got["inherited"] is None


def test_numpy_already_imported_by_the_host_is_left_alone():
    # A host program that imported numpy first chose its own threading; the import must not touch
    # the environment after the fact.
    got = _fresh(prelude="import numpy", extra="os.environ.get('OPENBLAS_NUM_THREADS')")
    assert got["numpy"] is True
    assert got["inherited"] is None and got["extra"] is None


def test_without_numpy_the_engine_still_loads_and_leaves_no_variable():
    got = _fresh(prelude="sys.modules['numpy'] = None")
    assert got["numpy"] is False
    assert got["inherited"] is None


def test_search_answers_the_same_with_one_thread(tmp_path):
    """The capped numpy path and the pure-Python path rank the same rows with the same scores."""
    code = textwrap.dedent(
        """
        import json, os, struct, sqlite3, sys
        from atlas_plus.mcp.memory_server import embeddings as E, fts
        import random
        rnd = random.Random(25)
        dim, rows = 64, 300
        conn = fts._connect()
        E._ensure_schema(conn)
        cols = [r[1] for r in conn.execute("pragma table_info(memory_vectors)")]
        for i in range(rows):
            v = E._normalize([rnd.uniform(-1, 1) for _ in range(dim)])
            rec = {"path": f"reference/n{i:03d}.md", "dim": dim, "vec": struct.pack(f"<{dim}f", *v)}
            for c in cols:
                rec.setdefault(c, "")
            conn.execute(f"insert into memory_vectors ({','.join(cols)}) values ({','.join('?' for _ in cols)})",
                         [rec[c] for c in cols])
        conn.commit(); conn.close()

        class Fixed:
            def embed(self, text):
                r = random.Random(text)
                return [r.uniform(-1, 1) for _ in range(dim)]

        fast = E.vector_search("alpha", limit=20, embedder=Fixed())
        assert E._vec_cache is not None and E._vec_cache[4] is not None, "the numpy matrix was not used"
        E._np = None
        E._vec_cache = None
        pure = E.vector_search("alpha", limit=20, embedder=Fixed())
        print(json.dumps({"fast": fast, "pure": pure}))
        """
    )
    env = {k: v for k, v in os.environ.items() if k not in _VARS}
    env["ATLAS_WORKSPACE_ROOT"] = str(tmp_path)
    env["ATLAS_MEMORY_ROOT"] = str(tmp_path / ".memory")
    env["ATLAS_STATE_DIR"] = str(tmp_path / ".atlas")
    (tmp_path / ".memory").mkdir()
    (tmp_path / ".atlas").mkdir()
    out = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True,
                         timeout=120, stdin=subprocess.DEVNULL, cwd=str(tmp_path))
    assert out.returncode == 0, out.stderr[-2000:]
    got = json.loads(out.stdout.strip().splitlines()[-1])
    assert len(got["fast"]) == 20
    assert [r["path"] for r in got["fast"]] == [r["path"] for r in got["pure"]]
    for a, b in zip(got["fast"], got["pure"]):
        assert abs(a["score"] - b["score"]) < 1e-3


@pytest.mark.skipif(sys.platform != "win32", reason="private bytes are read through the Windows API")
def test_a_fresh_server_process_holds_far_less_than_a_buffer_per_core():
    """Importing the server module (what every MCP session's process does) with numpy installed.
    Before the cap: about 66 MB + 32 MB per logical core (840 MB on 24). After: about 100 MB."""
    cores = os.cpu_count() or 1
    if cores < 8:
        pytest.skip("under 8 logical cores the two outcomes are too close to tell apart")
    code = textwrap.dedent(
        """
        import ctypes, ctypes.wintypes as wt, json
        from atlas_plus.mcp.memory_server import server, embeddings

        class PMC(ctypes.Structure):
            _fields_ = [("cb", wt.DWORD), ("pf", wt.DWORD)] + [(n, ctypes.c_size_t) for n in
                        ("pws", "ws", "a", "b", "c", "d", "pagefile", "peakpagefile", "private")]
        k32 = ctypes.WinDLL("kernel32"); ps = ctypes.WinDLL("psapi")
        k32.GetCurrentProcess.restype = wt.HANDLE
        ps.GetProcessMemoryInfo.argtypes = [wt.HANDLE, ctypes.POINTER(PMC), wt.DWORD]
        m = PMC(); m.cb = ctypes.sizeof(PMC)
        assert ps.GetProcessMemoryInfo(k32.GetCurrentProcess(), ctypes.byref(m), m.cb)
        print(json.dumps({"private_mb": m.private / 1048576, "numpy": embeddings._np is not None}))
        """
    )
    env = {k: v for k, v in os.environ.items() if k not in _VARS}
    out = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True,
                         timeout=120, stdin=subprocess.DEVNULL)
    assert out.returncode == 0, out.stderr[-2000:]
    got = json.loads(out.stdout.strip().splitlines()[-1])
    assert got["numpy"] is True
    assert got["private_mb"] < 200, f"a fresh server process holds {got['private_mb']:.0f} MB of private bytes"
