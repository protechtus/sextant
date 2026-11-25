"""SEX-046: the two corpus scans behind the dashboard's next-actions panel read notes ahead on threads.

Measured at 20,000 notes outside the file cache: the decay scan 152 s one by one. Each test runs the scan:
  a  `find_decayed_facts` and `find_orphan_candidates` read the notes on the read-ahead threads, each note
     once (fails before the change: every read was on the calling thread);
  b  the results equal the results of the same scan with the read-ahead switched off (one thread), on a
     corpus with an unreadable note, an undecodable one, an empty one and one without frontmatter;
  c  a caller's own `read=` (doctor serves texts it already holds) is called in line, not on the pool.
"""
from __future__ import annotations

import threading
from datetime import date, timedelta
from pathlib import Path

import pytest

from atlas_plus import _readahead
from atlas_plus.curation import decay, orphan

N_NOTES = 90  # above the read-ahead threshold, so the pool runs


@pytest.fixture
def corpus(tmp_path, monkeypatch):
    mem = tmp_path / "mem"
    (mem / "reference").mkdir(parents=True)
    monkeypatch.setattr(decay, "MEMORY_ROOT", mem, raising=False)
    old = (date.today() - timedelta(days=400)).isoformat()
    for i in range(N_NOTES):
        half = f"half_life: {10 + i}\n" if i % 3 == 0 else ""
        mention = f" see note-{(i + 1) % N_NOTES:03d}" if i % 4 == 0 else ""
        (mem / "reference" / f"note-{i:03d}.md").write_text(
            f"---\ntitle: note {i}\nupdated: {old}\n{half}---\nThe staging cluster note {i}{mention}\n",
            encoding="utf-8")
    (mem / "reference" / "plain-note.md").write_text("no frontmatter here\n", encoding="utf-8")
    (mem / "reference" / "empty-note.md").write_text("", encoding="utf-8")
    (mem / "reference" / "bytes-note.md").write_bytes(
        f"---\ntitle: raw\nupdated: {old}\nhalf_life: 5\n---\nbad ".encode("utf-8") + b"\xff\xfe bytes\n")
    (mem / "reference" / "locked-note.md").write_text(
        f"---\ntitle: locked\nupdated: {old}\nhalf_life: 5\n---\nnever readable\n", encoding="utf-8")
    return mem


@pytest.fixture
def reads(monkeypatch):
    """Every note read through Path.read_text: name -> the names of the threads that read it."""
    seen: dict[str, list[str]] = {}
    lock = threading.Lock()
    real = Path.read_text

    def read_text(self, *a, **k):
        if self.name.endswith(".md"):
            with lock:
                seen.setdefault(self.name, []).append(threading.current_thread().name)
            if self.name == "locked-note.md":
                raise PermissionError(13, "denied", str(self))
        return real(self, *a, **k)

    monkeypatch.setattr(Path, "read_text", read_text)
    return seen


def _decayed():
    return [c.as_dict() for c in decay.find_decayed_facts(threshold_days=0)]


def _orphans(mem, **kw):
    return orphan.find_orphan_candidates(mem, min_age_days=90, max_health=0.5, limit=500, **kw)


def test_decay_scan_reads_ahead_once_per_note(corpus, reads):
    out = _decayed()
    assert len(out) == N_NOTES // 3 + 1          # every third note, and the undecodable one; not the locked one
    assert all(len(v) == 1 for v in reads.values()), "a note was read more than once"
    assert len(reads) == N_NOTES + 4
    assert any(t.startswith("atlas-read") for v in reads.values() for t in v), "no note was read ahead"


def test_orphan_scan_reads_ahead(corpus, reads):
    out = _orphans(corpus)
    assert out
    pool = [n for n, v in reads.items() if v[0].startswith("atlas-read")]
    assert len(pool) >= N_NOTES, "the inbound-reference pass did not read ahead"


def test_results_equal_the_one_thread_scan(corpus, reads, monkeypatch):
    ahead_d, ahead_o = _decayed(), _orphans(corpus)
    monkeypatch.setattr(_readahead, "READ_THREADS", 1)
    reads.clear()
    plain_d, plain_o = _decayed(), _orphans(corpus)
    assert not any(t.startswith("atlas-read") for v in reads.values() for t in v)   # the oracle ran in line
    assert ahead_d == plain_d
    assert ahead_o == plain_o
    assert any(d["path"].endswith("bytes-note.md") for d in ahead_d)
    assert not any(d["path"].endswith("locked-note.md") for d in ahead_d)


def test_a_callers_reader_runs_in_line(corpus):
    names: list[str] = []

    def read(path: Path) -> str:
        names.append(threading.current_thread().name)
        if path.name == "locked-note.md":
            raise PermissionError(13, "denied", str(path))
        return path.read_bytes().decode("utf-8", "replace")

    out = _orphans(corpus, read=read)
    assert out
    assert names and set(names) == {threading.current_thread().name}
