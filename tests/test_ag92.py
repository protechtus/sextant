"""AG92 (sweep 24): the optimization pass. Each test runs the optimized path and counts the work it
does (reads, stats, connections, scans, metadata lookups) or checks that the result is the one the
old code gave; none of them reads source text."""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import time
import zlib

import pytest


def _run_py(code: str) -> str:
    out = subprocess.run([sys.executable, "-X", "utf8", "-c", code], capture_output=True, text=True,
                         encoding="utf-8", timeout=120, env=dict(os.environ))
    assert out.returncode == 0, out.stderr[-2000:]
    return out.stdout.strip().splitlines()[-1]


# --- 1. the umbrella CLI does not resolve the package version unless it prints it ---------------

def test_cli_import_does_no_metadata_lookup():
    code = (
        "import importlib.metadata as m, json\n"
        "calls = []\n"
        "orig_v, orig_d = m.version, m.distribution\n"
        "m.version = lambda *a, **k: (calls.append(a), orig_v(*a, **k))[1]\n"
        "m.distribution = lambda *a, **k: (calls.append(a), orig_d(*a, **k))[1]\n"
        "import atlas_plus, atlas_plus.cli\n"
        "print(json.dumps({'calls': len(calls), 'resolved': '__version__' in vars(atlas_plus)}))\n"
    )
    got = json.loads(_run_py(code))
    assert got == {"calls": 0, "resolved": False}


def test_cli_version_still_printed():
    from typer.testing import CliRunner

    import atlas_plus
    from atlas_plus import cli

    res = CliRunner().invoke(cli.app, ["--version"])
    assert res.exit_code == 0
    assert atlas_plus.__version__ in res.output
    assert cli.__version__ == atlas_plus.__version__  # the module attribute still answers


# --- 2. the dashboard's corpus signature: no stat per note, same signature -----------------------

def _tree(root: pathlib.Path) -> None:
    for rel in ("a.md", "b.txt", "reference/r1.md", "reference/deep/x/y.md", "versions/old.md", ".git/c.md",
                "_atlas/s.md", "projects/p/_sync/q.md", "projects/p/n.md", "projects/.hidden/h.md", "z/UPPER.MD"):
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("# " + rel, encoding="utf-8")


def _old_signature(root) -> str:
    """The signature as 73910bc computed it (os.walk + os.stat per note)."""
    count, newest, crc = 0, 0.0, 0
    skip = {"versions", "_atlas", ".git", "_sync", "__pycache__"}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in skip and not d.startswith(".")]
        for name in filenames:
            if not name.endswith(".md"):
                continue
            count += 1
            crc = zlib.crc32((dirpath + "/" + name).encode("utf-8", "replace"), crc)
            m = os.stat(os.path.join(dirpath, name)).st_mtime
            newest = max(newest, m)
    return f"{root}|{count}:{newest:.3f}:{crc & 0xFFFFFFFF:08x}"


def test_corpus_signature_matches_and_stats_nothing(tmp_path, monkeypatch):
    from atlas_plus.web import app as web_app

    mem = tmp_path / ".memory"
    _tree(mem)
    monkeypatch.setattr(web_app, "MEMORY_ROOT", mem)
    expected = _old_signature(mem)
    stats = []
    real_stat = os.stat

    def counting_stat(p, *a, **k):
        stats.append(str(p))
        return real_stat(p, *a, **k)

    monkeypatch.setattr(os, "stat", counting_stat)
    got = web_app._corpus_signature()
    monkeypatch.setattr(os, "stat", real_stat)
    assert got == expected
    assert [s for s in stats if s.endswith(".md")] == []  # was one os.stat per note


def test_corpus_signature_still_moves_on_change(tmp_path, monkeypatch):
    from atlas_plus.web import app as web_app

    mem = tmp_path / ".memory"
    _tree(mem)
    monkeypatch.setattr(web_app, "MEMORY_ROOT", mem)
    s0 = web_app._corpus_signature()
    deep = mem / "reference" / "deep" / "x" / "y.md"
    st = deep.stat()
    os.utime(deep, (st.st_atime, st.st_mtime + 100))
    s1 = web_app._corpus_signature()
    assert s1 != s0
    (mem / "projects" / "p" / "n.md").rename(mem / "projects" / "p" / "m.md")
    s2 = web_app._corpus_signature()
    assert s2 != s1 and s2.split("|")[1].split(":")[0] == s1.split("|")[1].split(":")[0]  # same count, new name
    (mem / "versions" / "another.md").write_text("x", encoding="utf-8")  # history copies do not count
    assert web_app._corpus_signature() == s2


# --- 3. orphans: one read per note, same candidates ----------------------------------------------

def _orphan_corpus(mem: pathlib.Path) -> None:
    old = time.time() - 400 * 86400
    notes = {
        "projects/alpha-plan.md": "---\ntitle: Alpha\n---\nnothing links here\n",
        "projects/beta-notes.md": "---\ntitle: Beta\ntags: [x]\n---\nsee alpha-plan for details\n",
        "reference/gamma-ref.md": "no frontmatter at all, mentions beta-notes\n",
        "reference/delta-ref.md": "---\nupdated: 2020-01-01\n---\nplain\n",
        "feedback/kubernetes.md": "---\ntitle: K\n---\nbody\n",
        "feedback/kelvin.md": "---\ntitle: kelvin\n---\nKubernetes is not the word kubernetes by token\n",
    }
    for rel, text in notes.items():
        p = mem / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        os.utime(p, (old, old))


def test_orphans_read_each_note_once(tmp_path, monkeypatch):
    from atlas_plus.curation import orphan
    from atlas_plus.curation.health import compute_file_health

    mem = tmp_path / ".memory"
    _orphan_corpus(mem)
    reads: dict = {}
    real = pathlib.Path.read_text

    def counting(self, *a, **k):
        reads[str(self)] = reads.get(str(self), 0) + 1
        return real(self, *a, **k)

    monkeypatch.setattr(pathlib.Path, "read_text", counting)
    got = orphan.find_orphan_candidates(mem, min_age_days=180, max_health=0.5, max_inbound_refs=0, limit=50)
    monkeypatch.setattr(pathlib.Path, "read_text", real)
    assert reads and max(reads.values()) == 1, reads  # was two reads per note
    paths = {r["path"] for r in got}
    # beta-notes is mentioned by gamma-ref, alpha-plan by beta-notes; the token `kubernetes` sits in
    # kelvin.md (written `Kubernetes` with a KELVIN SIGN once, and once plainly)
    assert paths == {"reference/gamma-ref.md", "reference/delta-ref.md", "feedback/kelvin.md"}
    for r in got:  # every health field is the one compute_file_health gives for that note
        h = compute_file_health(mem / r["path"])
        assert r["score"] == h["score"] and r["issues"] == h["issues"]


def test_inbound_refs_non_ascii_text_keeps_token_rule(tmp_path):
    """The whole-note lower-casing is for ASCII text only: `K` (KELVIN SIGN) lower-cases to an ASCII
    `k`, which would invent a `kubernetes` token that the per-token rule never saw."""
    from atlas_plus.curation.orphan import _build_inbound_refs

    mem = tmp_path / ".memory"
    (mem / "a").mkdir(parents=True)
    (mem / "a" / "kubernetes.md").write_text("x", encoding="utf-8")
    (mem / "a" / "other.md").write_text("only Kubernetes here", encoding="utf-8")
    (mem / "a" / "plain.md").write_text("KUBERNETES in caps", encoding="utf-8")
    assert _build_inbound_refs(mem)["kubernetes"] == 1  # plain.md only


# --- 4. doctor's corpus check: no stat for notes in the leftover scan, no lstat walk to count ------

def test_memory_corpus_check_stats_only_candidate_names(tmp_path, monkeypatch):
    from atlas_plus.doctor import cli as dcli

    mem = tmp_path / ".memory"
    (mem / "projects").mkdir(parents=True)
    for i in range(30):
        (mem / "projects" / f"n{i}.md").write_text("---\ntitle: n\n---\nbody\n", encoding="utf-8")
    (mem / "projects" / "n3.md.w-tmp").write_text("partial", encoding="utf-8")
    monkeypatch.setattr(dcli, "MEMORY_ROOT", mem)
    checked = []
    real = pathlib.Path.is_file

    def counting(self):
        checked.append(self.name)
        return real(self)

    monkeypatch.setattr(pathlib.Path, "is_file", counting)
    res = dcli.check_memory_corpus([])
    monkeypatch.setattr(pathlib.Path, "is_file", real)
    assert res.get("crash_leftovers") == ["projects/n3.md.w-tmp"]
    assert [n for n in checked if n.endswith(".md")] == []  # was an is_file() per entry


def test_index_status_counts_without_lstat_per_note(tmp_path, monkeypatch):
    from atlas_plus.mcp.memory_server import embeddings, fts

    mem = tmp_path / ".memory"
    for i in range(40):
        p = mem / ("projects" if i % 2 else "reference") / f"n{i}.md"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x", encoding="utf-8")
    (mem / "projects" / "_README.md").write_text("x", encoding="utf-8")
    monkeypatch.setattr(fts, "MEMORY_ROOT", mem)
    monkeypatch.setattr(fts, "FTS_DB", tmp_path / ".atlas" / "search.sqlite")
    fts._connect().close()
    lstats = []
    real = os.lstat

    def counting(p, *a, **k):
        lstats.append(str(p))
        return real(p, *a, **k)

    monkeypatch.setattr(os, "lstat", counting)
    st = embeddings.index_status()
    monkeypatch.setattr(os, "lstat", real)
    assert st["corpus_files"] == 40 == sum(1 for _ in fts._walk_memory())
    assert len([p for p in lstats if p.endswith(".md")]) == 0  # was an lstat (and a stat) per note


# --- 5. the search freshness sweep: one connection, one memory_files scan, cheaper walk -----------

def _fts_world(tmp_path, monkeypatch, n=25):
    from atlas_plus.mcp.memory_server import fts

    mem = tmp_path / ".memory"
    for i in range(n):
        p = mem / "projects" / f"g{i % 3}" / f"note{i}.md"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f"---\ntitle: t{i}\n---\nkubernetes note {i}\n", encoding="utf-8")
    for rel in ("reference/research/mirror.md", "reference/research-notes.md", "archive/old.md",
                "projects/archive/kept.md", "projects/reference/research/kept2.md", "x/y/z/w/deep.md"):
        p = mem / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("kubernetes " + rel, encoding="utf-8")
    monkeypatch.setattr(fts, "MEMORY_ROOT", mem)
    monkeypatch.setattr(fts, "FTS_DB", tmp_path / ".atlas" / "search.sqlite")
    return fts, mem


def test_freshness_sweep_one_connection_one_scan(tmp_path, monkeypatch):
    fts, _mem = _fts_world(tmp_path, monkeypatch)
    assert fts.update_incremental() > 0  # the first call builds
    conns, stmts = [], []
    real = fts._connect

    def counting():
        c = real()
        conns.append(c)
        c.set_trace_callback(stmts.append)
        return c

    monkeypatch.setattr(fts, "_connect", counting)
    assert fts.update_incremental() == 0  # nothing changed
    assert len(conns) == 2  # was three: the format tag now shares the emptiness check's connection
    scans = [s for s in stmts if "FROM memory_files" in s and "COUNT(*)" not in s.upper()]
    assert len(scans) == 1, scans  # was two full scans of memory_files


def test_freshness_sweep_still_sees_every_change(tmp_path, monkeypatch):
    fts, mem = _fts_world(tmp_path, monkeypatch)
    fts.update_incremental()
    (mem / "projects" / "g0" / "note0.md").write_text("---\ntitle: t0\n---\nchanged body zebra\n", encoding="utf-8")
    (mem / "projects" / "g1" / "note1.md").unlink()
    (mem / "projects" / "newdir").mkdir()
    (mem / "projects" / "newdir" / "fresh.md").write_text("zebra fresh", encoding="utf-8")
    assert fts.update_incremental() == 3
    paths = {r["path"] for r in fts.search("zebra")}
    assert paths == {"projects/g0/note0.md", "projects/newdir/fresh.md"}


def test_walk_stats_parity_and_fewer_mirror_checks(tmp_path, monkeypatch):
    from atlas_plus.curation import scan

    fts, _mem = _fts_world(tmp_path, monkeypatch)
    calls = []
    real = scan.is_excluded_mirror

    def counting(parts):
        calls.append(tuple(parts))
        return real(parts)

    monkeypatch.setattr(scan, "is_excluded_mirror", counting)
    fast = {rel for rel, _m, _s in fts._walk_memory_stats()}
    walk_calls = len(calls)
    monkeypatch.setattr(scan, "is_excluded_mirror", real)
    slow = {rel for rel, _ in fts._walk_memory()}
    assert fast == slow
    assert "reference/research/mirror.md" not in fast and "archive/old.md" not in fast
    assert {"projects/archive/kept.md", "projects/reference/research/kept2.md", "reference/research-notes.md",
            "x/y/z/w/deep.md"} <= fast
    assert walk_calls < len(fast) // 2  # was one call per entry (every note and directory)


# --- 6. redaction: importing it does not build the mark-strip class -------------------------------

def test_redaction_import_builds_no_strip_class():
    code = ("import json, atlas_plus.redaction as r\n"
            "built = bool(r._STRIP_CLASS)\n"
            "ref = tuple(sorted({r._strip_marks(w).casefold() for w in r._PW_WORDS}, key=len, reverse=True))\n"
            "print(json.dumps({'built_at_import': built, 'same_words': ref == r._PW_WORDS_STRIPPED}))\n")
    assert json.loads(_run_py(code)) == {"built_at_import": False, "same_words": True}


@pytest.mark.parametrize("word,cue", [("पासवर्ड", "is"), ("mật khẩu", "is"),
                                      ("รหัสผ่าน", ":")])
def test_redaction_still_masks_after_marked_password_words(word, cue):
    """The words whose fold needs the mark strip (Devanagari, Vietnamese, Thai) still cue a mask."""
    from atlas_plus.redaction import redact
    secret = "Xy7" + "#kLm" + "9pQ"
    out = redact(f"the {word} {cue} {secret}")
    assert secret not in out
