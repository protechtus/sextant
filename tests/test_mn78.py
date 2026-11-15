"""MN78 (sweep 22 manual, newcomer + upgrade): the MN74 stroke-letter fold left the other letters that have no
Unicode decomposition. A note about Ħamrun, Ærøskøbing or a cœur was not found by its plain spelling."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from atlas_plus.mcp.memory_server import fts


@pytest.fixture
def corpus(tmp_path, monkeypatch):
    monkeypatch.setenv("ATLAS_FTS_FRESHNESS_WINDOW", "0")
    mem = tmp_path / "mem"
    db = tmp_path / "state" / "search.sqlite"

    def write(name: str, body: str) -> None:
        p = mem / "reference" / f"{name}.md"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f"---\ntitle: {name}\n---\n{body}\n", encoding="utf-8")

    with patch.object(fts, "MEMORY_ROOT", mem), patch.object(fts, "FTS_DB", db):
        def search(q: str) -> dict:
            return {Path(r["path"]).stem: r["snippet"] for r in fts.search(q)}
        yield write, search


def test_maltese_and_sami_stroke_letters_meet_the_plain_spelling(corpus):
    write, search = corpus
    write("mt", "Il-Ħamrun depot u l-ħobż.")
    write("se", "Ŧŧ in a Sami name: Deaŧnu.")
    assert set(search("hamrun")) == {"mt"}
    assert set(search("deatnu")) == {"se"}
    assert "[Ħamrun]" in search("hamrun")["mt"]  # the snippet keeps the note's own letters


def test_ae_and_oe_ligatures_meet_the_two_letter_spelling(corpus):
    write, search = corpus
    write("da", "Færgen til Ærøskøbing sejler kl. 9.")
    write("fr", "Le cœur du projet.")
    assert set(search("aeroskobing")) == {"da"}
    assert set(search("coeur")) == {"fr"}
    assert set(search("cœur")) == {"fr"} and set(search("Ærøskøbing")) == {"da"}


def test_an_index_of_the_previous_format_rebuilds_once(corpus):
    write, search = corpus
    write("mt", "Ħamrun")
    assert set(search("hamrun")) == {"mt"}
    conn = fts._connect()
    try:
        conn.execute("UPDATE atlas_meta SET value='7' WHERE key='index_format'")
        conn.commit()
    finally:
        conn.close()
    assert set(search("hamrun")) == {"mt"}
    conn = fts._connect()
    try:
        assert conn.execute("SELECT value FROM atlas_meta WHERE key='index_format'").fetchone()[0] == fts.INDEX_FORMAT
    finally:
        conn.close()
