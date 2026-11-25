"""AG112 (sweep 32, SEX-024, SEX-019): doctor opens each note once (counted at the openers the readers
call; results equal the disk reads); the topic panel takes frontmatter from the search index."""

from __future__ import annotations

import os
import pathlib
import time
from collections import Counter

import pytest

from atlas_plus.doctor import cli


def _corpus(root: pathlib.Path) -> dict:
    mem = root / ".memory"
    (mem / "projects" / "alpha").mkdir(parents=True)
    (mem / "feedback").mkdir()
    (mem / "decisions").mkdir()
    (mem / "decisions" / "pick-db.md").write_text("---\ntitle: Pick\n---\n[[hub-note]]\n", encoding="utf-8")
    files = {}
    for i in range(80):  # above the read-ahead threshold, so the threaded path runs
        p = mem / "projects" / "alpha" / f"note-{i:03d}.md"
        ref = f"See [[note-{(i + 1) % 80:03d}]] and hub-note." if i % 3 else "plain body"
        p.write_text(f"---\ntitle: Note {i}\ntags: [a]\n---\n{ref}\n", encoding="utf-8")
        files[p.name] = p
    hub = mem / "feedback" / "hub-note.md"
    hub.write_text("---\ntitle: Hub\nconfirmed: true\n---\nthe hub\n", encoding="utf-8")
    bom = mem / "feedback" / "bom-note.md"
    bom.write_bytes(b"\xef\xbb\xbf---\r\ntitle: Bom\r\ntags: [b]\r\n---\r\nlinks [[hub-note]]\r\n")
    latin = mem / "feedback" / "latin-note.md"
    latin.write_bytes(b"---\ntitle: caf\xe9\n---\nna\xefve [[hub-note]]\n")
    empty = mem / "feedback" / "empty-note.md"
    empty.write_bytes(b"")
    locked = mem / "feedback" / "locked-note.md"
    locked.write_text("---\ntitle: Locked\n---\n[[hub-note]]\n", encoding="utf-8")
    old = time.time() - 400 * 86400
    for p in mem.rglob("*.md"):
        os.utime(p, (old, old))
    return {"mem": mem, "locked": locked}


@pytest.fixture()
def corpus(tmp_path, monkeypatch):
    c = _corpus(tmp_path)
    monkeypatch.setattr(cli, "MEMORY_ROOT", c["mem"])
    locked = c["locked"]
    real_bytes = pathlib.Path.read_bytes
    real_text = pathlib.Path.read_text
    opens: Counter = Counter()

    def counted(real):  # the two openers the doctor's note readers call
        def opener(self, *a, **k):
            if str(self).endswith(".md") and str(c["mem"]) in str(self):
                opens[str(self)] += 1
                if self == locked:
                    raise PermissionError(13, "denied", str(self))
            return real(self, *a, **k)
        return opener

    monkeypatch.setattr(pathlib.Path, "read_bytes", counted(real_bytes))
    monkeypatch.setattr(pathlib.Path, "read_text", counted(real_text))
    c["opens"] = opens
    return c


def _memory_checks(scan):
    return {
        "orphans": cli.check_orphans(scan),
        "ref_graph": cli.check_ref_graph(scan),
        "memory_health": cli.check_memory_health_overview(scan),
        "memory_quality": cli.check_memory_quality(scan),
        "memory_corpus": cli.check_memory_corpus(scan),
        "decisions": cli.check_decisions(scan),
    }


def test_each_note_is_opened_at_most_once(corpus):
    scan = cli.scan_memory_files(corpus["mem"])
    _memory_checks(scan)
    opens = corpus["opens"]
    assert len(opens) == 86
    assert max(opens.values()) == 1, {k: v for k, v in opens.items() if v > 1}


def test_results_match_the_reads_from_disk(corpus):
    scan = cli.scan_memory_files(corpus["mem"])
    shared = _memory_checks(scan)
    from_disk = {
        "orphans": cli.check_orphans(),
        "ref_graph": cli.check_ref_graph(),
        "memory_health": cli.check_memory_health_overview(),
        "memory_quality": cli.check_memory_quality(scan),
        "memory_corpus": cli.check_memory_corpus(scan),
        "decisions": cli.check_decisions(),
    }
    assert shared == from_disk
    assert shared["memory_health"]["total_files"] == 86
    assert shared["ref_graph"]["top"][0]["stem"] == "hub-note"
    assert "error" not in shared["orphans"]


def test_scan_reader_matches_read_text(corpus):
    scan = cli.scan_memory_files(corpus["mem"])
    read = cli.scan_reader(scan, corpus["mem"])
    for p in sorted(corpus["mem"].rglob("*.md")):
        if p == corpus["locked"]:
            with pytest.raises(OSError):
                read(p)
            continue
        assert read(p) == p.read_text(encoding="utf-8", errors="replace"), p.name


# --- the dashboard's topic panel takes each note's frontmatter from the search index (SEX-019) -------

def _topic_world(tmp_path, monkeypatch):
    from atlas_plus.mcp.memory_server import fts

    mem = tmp_path / ".memory"
    for i in range(30):
        p = mem / ("projects" if i % 2 else "reference") / f"g{i % 3}" / f"t{i:02d}.md"
        p.parent.mkdir(parents=True, exist_ok=True)
        tags = "[alpha, Beta]" if i % 3 else "gamma; delta"
        p.write_text(f"---\ntitle: t{i}\ntags: {tags}\n---\nbody {i}\n", encoding="utf-8")
    (mem / "reference" / "plain.md").write_text("no frontmatter\n", encoding="utf-8")
    (mem / "archive").mkdir()
    (mem / "archive" / "old.md").write_text("---\ntags: [archived]\n---\n", encoding="utf-8")
    (mem / "reference" / "_hidden.md").write_text("---\ntags: [hidden]\n---\n", encoding="utf-8")
    monkeypatch.setattr(fts, "MEMORY_ROOT", mem)
    monkeypatch.setattr(fts, "FTS_DB", tmp_path / ".atlas" / "search.sqlite")
    fts.update_incremental()
    return mem


def test_topic_panel_reads_no_indexed_note_and_matches_the_walk(tmp_path, monkeypatch):
    from atlas_plus.curation import _corpus_pass
    from atlas_plus.web import app as web

    mem = _topic_world(tmp_path, monkeypatch)
    late = mem / "projects" / "late.md"  # written after the index: picked up, same answer
    late.write_text("---\ntags: [alpha]\n---\n", encoding="utf-8")
    real_index = getattr(web, "_indexed_frontmatter", None)
    monkeypatch.setattr(web, "_indexed_frontmatter", lambda root: None, raising=False)
    oracle = web._build_topic_index(mem)  # every note read and parsed from disk
    if real_index is None:
        monkeypatch.delattr(web, "_indexed_frontmatter")
    else:
        monkeypatch.setattr(web, "_indexed_frontmatter", real_index)
    reads = []
    real = _corpus_pass.read_text

    def counting(path):
        reads.append(path.name)
        return real(path)

    monkeypatch.setattr(_corpus_pass, "read_text", counting)
    got = web._build_topic_index(mem)
    assert list(got.items()) == list(oracle.items())
    assert "projects/late.md" in got["alpha"]
    assert "archived" not in got and "hidden" not in got
    assert reads == [], reads  # before: every note read and YAML-parsed on each cold panel build


def test_topic_panel_without_an_index_walks_and_builds_none(tmp_path, monkeypatch):
    from atlas_plus.mcp.memory_server import fts
    from atlas_plus.web import app as web

    mem = tmp_path / ".memory"
    (mem / "reference").mkdir(parents=True)
    (mem / "reference" / "a.md").write_text("---\ntags: [solo]\n---\n", encoding="utf-8")
    db = tmp_path / ".atlas" / "search.sqlite"
    monkeypatch.setattr(fts, "MEMORY_ROOT", mem)
    monkeypatch.setattr(fts, "FTS_DB", db)
    assert web._build_topic_index(mem) == {"solo": ["reference/a.md"]}
    assert not db.exists()  # a panel never builds a whole index
