"""AG96 (sweep 25, SEX-021): a query typed in digraph spelling finds the note written with the letter.

`aarhus`, `oere`, `goeteborg`, `vaesteraas`, `muenchen` found nothing in notes spelling `Århus`, `øre`, `Göteborg`, `Västerås`,
`München` (the other direction works since AG91). The index now also holds each å/ø/ö/ä/ü word of a note in its digraph spelling,
in a tail after the note's text, so quoted phrases are unchanged and the tail is never shown in a snippet. A typed `aa`/`oe` is
still not folded onto the letter, so ASCII notes keep their words apart (`saada`/`sada`, `poet`/`pot`). The German ü joins the
query side too (`München` finds `Muenchen`). INDEX_FORMAT "10": an index built by the previous version is rebuilt once.
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

os.environ.setdefault("ATLAS_WEB_API_KEY", "test-key-abc")
os.environ.setdefault("ATLAS_BRAIN_URL", "http://127.0.0.1:9")

from fastapi.testclient import TestClient  # noqa: E402

from atlas_plus.mcp.memory_server import fts  # noqa: E402
from atlas_plus.mcp.memory_server.tools import search as search_tool  # noqa: E402
from atlas_plus.web import app as web_app  # noqa: E402


@pytest.fixture
def corpus(tmp_path, monkeypatch):
    monkeypatch.setenv("ATLAS_FTS_FRESHNESS_WINDOW", "0")
    mem = tmp_path / "mem"
    db = tmp_path / "state" / "search.sqlite"

    def write(name: str, body: str, title: str = "t") -> None:
        p = mem / "reference" / f"{name}.md"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f"---\ntitle: {title}\n---\n{body}\n", encoding="utf-8")

    with patch.object(fts, "MEMORY_ROOT", mem), patch.object(fts, "FTS_DB", db):
        def search(q: str) -> dict:
            return {Path(r["path"]).stem: r["snippet"] for r in fts.search(q)}
        yield mem, db, write, search


def _notes(write):
    write("n1", "Vi flyttede til Århus i 2019 og bor nu i Århus kommune.")
    write("n2", "En krone er hundrede øre.")
    write("n3", "Vi bor i Malmö nära Göteborg och Västerås.")
    write("n4", "Ich wohne in München an der Isar.")
    write("n5", "Arhus is how the English press spells it.")   # ASCII, no letter: no alias
    write("n6", "Muenchen ist die Hauptstadt Bayerns.")
    write("n7", "Meidän täytyy saada lisää aikaa. Sada kertaa. The poet wrote about a pot.")


def test_a_digraph_query_finds_the_letter(corpus):
    _, _, write, search = corpus
    _notes(write)
    hits = search("aarhus")
    assert set(hits) == {"n1"}
    assert hits["n1"].startswith("Vi flyttede til [Århus] i 2019")   # the note's own word, bracketed
    assert set(search("AARHUS")) == {"n1"} and set(search("Aarhus")) == {"n1"} and set(search("aarh*")) == {"n1"}
    assert search("oere") == {"n2": "En krone er hundrede [øre].\nt"}   # the title value follows, as for every hit
    assert "[Göteborg]" in search("goeteborg")["n3"] and "[Västerås]" in search("vaesteraas")["n3"]
    assert "[Malmö]" in search("malmoe")["n3"]
    assert set(search("muenchen")) == {"n4", "n6"}
    assert "[München]" in search("muenchen")["n4"]
    assert set(search("aarhus 2019")) == {"n1"} and set(search("krone oere")) == {"n2"}


def test_the_letter_query_still_finds_both_and_the_umlaut_u_joins(corpus):
    _, _, write, search = corpus
    _notes(write)
    assert set(search("Århus")) == {"n1", "n5"}   # `Århus` folds to `arhus` (unicode61), as before
    assert set(search("München")) == {"n4", "n6"}   # ü on the query side too (AG91 had å/ø/ö/ä only)
    assert "[Muenchen]" in search("München")["n6"]


def test_ascii_words_are_never_folded(corpus):
    _, _, write, search = corpus
    _notes(write)
    assert set(search("aarhus")) == {"n1"}            # n5's ASCII `Arhus` is not `aarhus`: no fold of a typed `aa`
    assert set(search("saada")) == {"n7"}
    assert set(search("pot")) == {"n7"} and set(search("poet")) == {"n7"}
    write("n8", "Hun er en god poet.")
    write("n9", "Sada on luku.")
    assert set(search("pot")) == {"n7"}               # `poet` (n8) is not `pot`
    assert set(search("sada")) == {"n7", "n9"} and set(search("saada")) == {"n7"}
    assert set(search("aarhus")) == {"n1"}


def test_phrases_and_snippets_never_meet_the_tail(corpus):
    _, db, write, search = corpus
    _notes(write)
    write("p1", "Mødet er i Århus")
    assert set(search('"Århus kommune"')) == {"n1"}   # an alias beside its word would break this phrase
    assert search('"Århus aarhus"') == {}             # the last word and the tail are not adjacent
    for q in ("aarhus", "Århus", "oere", "muenchen", "p1", "mødet"):
        for snip in search(q).values():
            assert "" not in snip and "aarhus" not in snip and "oere" not in snip
    snip = search("aarhus")["p1"]
    assert snip == "Mødet er i [Århus]\nt"
    assert search("") == {} and search('""') == {}   # the tail's private mark is never a query
    write("p2", "Kørte til Århus i går.")                      # decomposed å
    assert search("aarhus")["p2"].startswith("Kørte til [Århus]")


def test_frontmatter_values_and_huge_notes(corpus):
    _, _, write, search = corpus
    write("f1", "A note about the far north.", title="Tromsø")
    assert set(search("tromsoe")) == {"f1"}
    big = ("Linje uden noget særligt. " * 3000) + "Til sidst kørte vi til Århus." + (" Mere tekst her." * 200)
    write("big", big)
    assert len(big) > 60000
    snip = search("aarhus")["big"]
    assert "[Århus]" in snip and "" not in snip


def test_an_index_from_the_previous_version_is_rebuilt(corpus):
    _, db, write, search = corpus
    _notes(write)
    assert set(search("Århus")) >= {"n1"}
    conn = sqlite3.connect(db)
    try:   # make it look like an index written by d790dc1: no alias tails, format 9
        for rowid, body in conn.execute("SELECT rowid, body FROM memory_fts").fetchall():
            if "" in body:
                conn.execute("UPDATE memory_fts SET body=? WHERE rowid=?", (body[:body.rfind("\n")], rowid))
        conn.execute("UPDATE atlas_meta SET value='9' WHERE key='index_format'")
        conn.commit()
    finally:
        conn.close()
    assert set(search("aarhus")) == {"n1"}
    conn = sqlite3.connect(db)
    try:
        assert conn.execute("SELECT value FROM atlas_meta WHERE key='index_format'").fetchone()[0] == fts.INDEX_FORMAT
    finally:
        conn.close()


def test_mcp_tool_and_dashboard(corpus):
    mem, _, write, _ = corpus
    _notes(write)
    res = search_tool.memory_search("goeteborg")
    assert [Path(r["path"]).stem for r in res] == ["n3"] and "[Göteborg]" in res[0]["snippet"]
    with patch.object(web_app, "MEMORY_ROOT", mem), patch.object(web_app, "_check_auth", lambda request: None):
        r = TestClient(web_app.app).get("/api/memory/search", params={"q": "aarhus"})
    assert r.status_code == 200
    assert "n1.md" in r.text and "n5.md" not in r.text and "" not in r.text
