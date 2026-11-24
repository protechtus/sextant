"""AG100 (sweep 26): the dashboard file tree (/api/memory/files) at 20,000 notes.

Measured on 74e57a7: 3.49 MB uncompressed and 3.11 s on EVERY load (6.05 s cold) at 20,000 notes,
because the tree was never cached and each entry paid two lstat calls for the link test. The tree is
now cached per corpus signature (ATLAS_WEB_PANEL_CACHE governs it), sent gzip-compressed to a client
that accepts it, and the link test reads the listing's own entry.
"""
from __future__ import annotations

import gzip
import os

import pytest


def _web(monkeypatch, mem):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from atlas_plus.web import app as webapp
    monkeypatch.setattr(webapp, "_ALLOW_NO_AUTH", True, raising=False)
    monkeypatch.setattr(webapp, "API_KEY", "", raising=False)
    monkeypatch.setattr(webapp, "BRAIN_URL", "http://127.0.0.1:9", raising=False)
    monkeypatch.setattr(webapp, "MEMORY_ROOT", mem)
    monkeypatch.setattr(webapp, "_PANEL_CACHE", {})
    monkeypatch.delenv("ATLAS_WEB_PANEL_CACHE", raising=False)
    return webapp, TestClient(webapp.app)


@pytest.fixture
def corpus(tmp_path):
    mem = tmp_path / "mem"
    for d in ("projects/alpha", "projects/beta", "reference"):
        (mem / d).mkdir(parents=True)
    for i in range(30):
        (mem / "projects" / ("alpha" if i % 2 else "beta") / f"n{i:03d}.md").write_text(
            f"---\ntitle: n{i}\n---\nfact {i}\n", encoding="utf-8")
    (mem / "reference" / "r.md").write_text("ref\n", encoding="utf-8")
    return mem


def _count_scandir(monkeypatch, webapp):
    calls = {"n": 0}
    real = os.scandir

    def counting(path="."):
        calls["n"] += 1
        return real(path)
    monkeypatch.setattr(webapp.os, "scandir", counting)
    return calls


def test_tree_is_sent_gzip_compressed_to_a_client_that_accepts_it(corpus, monkeypatch):
    _webapp, c = _web(monkeypatch, corpus)
    r = c.get("/api/memory/files", headers={"Accept-Encoding": "gzip"})
    assert r.status_code == 200
    assert r.headers.get("content-encoding") == "gzip"
    assert "n007.md" in r.text  # the client decodes it; the tree is whole
    plain = c.get("/api/memory/files", headers={"Accept-Encoding": "identity"})
    assert plain.status_code == 200 and "content-encoding" not in plain.headers
    assert plain.text == r.text


def test_second_load_does_not_rebuild_the_tree(corpus, monkeypatch):
    webapp, c = _web(monkeypatch, corpus)
    calls = _count_scandir(monkeypatch, webapp)
    first_html = c.get("/api/memory/files").text
    first = calls["n"]
    calls["n"] = 0
    second_html = c.get("/api/memory/files").text
    # a cache hit costs the signature walk only; a rebuild walks the tree again on top of it
    assert second_html == first_html
    assert calls["n"] < first, (first, calls["n"])


def test_cache_off_rebuilds_every_load(corpus, monkeypatch):
    webapp, c = _web(monkeypatch, corpus)
    monkeypatch.setenv("ATLAS_WEB_PANEL_CACHE", "off")
    calls = _count_scandir(monkeypatch, webapp)
    c.get("/api/memory/files")
    first = calls["n"]
    calls["n"] = 0
    c.get("/api/memory/files")
    assert calls["n"] == first and first > 0


def test_new_and_deleted_notes_show_on_the_next_load(corpus, monkeypatch):
    _webapp, c = _web(monkeypatch, corpus)
    assert "fresh-note.md" not in c.get("/api/memory/files").text
    new = corpus / "reference" / "fresh-note.md"
    new.write_text("new\n", encoding="utf-8")
    assert "fresh-note.md" in c.get("/api/memory/files").text
    new.unlink()
    (corpus / "projects" / "beta" / "n000.md").unlink()
    after = c.get("/api/memory/files").text
    assert "fresh-note.md" not in after and "n000.md" not in after and "n002.md" in after


def test_entry_is_link_matches_is_link(tmp_path):
    from atlas_plus._fsutil import entry_is_link, is_link
    (tmp_path / "d").mkdir()
    (tmp_path / "f.md").write_text("x", encoding="utf-8")
    out = tmp_path / "outside"
    out.mkdir()
    try:
        if os.name == "nt":
            import subprocess
            subprocess.run(["cmd", "/c", "mklink", "/J", str(tmp_path / "j"), str(out)],
                           check=True, capture_output=True, stdin=subprocess.DEVNULL, timeout=30)
        else:
            os.symlink(out, tmp_path / "j", target_is_directory=True)
    except (OSError, Exception):
        pass
    with os.scandir(tmp_path) as it:
        entries = list(it)
    assert entries
    for e in entries:
        assert entry_is_link(e) == is_link(tmp_path / e.name), e.name
