"""MN74 (sweep 22 manual): the dashboard search's grep fallback must not print a linked file's text.

The FTS index skips a note that is a link and the file viewer rejects a path that resolves outside the corpus,
but /api/memory/search fell through to a file grep that read through the link and showed the target's text,
even a file outside the corpus."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

_TEST_KEY = "mn74-test-key"
os.environ.setdefault("ATLAS_WEB_API_KEY", _TEST_KEY)
os.environ.setdefault("ATLAS_BRAIN_URL", "http://127.0.0.1:9")

from fastapi.testclient import TestClient  # noqa: E402

from atlas_plus.web import app as web_app  # noqa: E402

AUTH = {"Authorization": f"Bearer {web_app.API_KEY if hasattr(web_app, 'API_KEY') else _TEST_KEY}"}


def _client():
    return TestClient(web_app.app)


def _symlink_or_skip(target: Path, link: Path) -> None:
    try:
        os.symlink(target, link)
    except (OSError, NotImplementedError) as e:
        pytest.skip(f"symlinks unavailable: {e}")


def _search(mem: Path, q: str) -> str:
    with patch.object(web_app, "MEMORY_ROOT", mem), \
         patch("atlas_plus.mcp.memory_server.tools.search.memory_search", return_value=[]), \
         patch.object(web_app, "_check_auth", lambda request: None):
        r = _client().get("/api/memory/search", params={"q": q})
    assert r.status_code == 200
    return r.text


def test_grep_fallback_skips_link_to_outside_file():
    with tempfile.TemporaryDirectory() as tmp:
        mem = Path(tmp) / "mem"
        (mem / "reference").mkdir(parents=True)
        (mem / "reference" / "plain.md").write_text("an ordinary note about rivers\n", encoding="utf-8")
        outside = Path(tmp) / "outside.md"
        outside.write_text("private text sentinelzq74 outside the corpus\n", encoding="utf-8")
        _symlink_or_skip(outside, mem / "reference" / "outside-link.md")
        html = _search(mem, "sentinelzq74")
        assert "sentinelzq74" not in html.replace("&quot;sentinelzq74&quot;", "")
        assert "outside-link" not in html
        # the fallback still works for a real note
        assert "plain.md" in _search(mem, "rivers")


def test_grep_fallback_skips_link_to_inside_note():
    with tempfile.TemporaryDirectory() as tmp:
        mem = Path(tmp) / "mem"
        (mem / "reference").mkdir(parents=True)
        (mem / "reference" / "real.md").write_text("Bahnhofstrasse note\n", encoding="utf-8")
        _symlink_or_skip(mem / "reference" / "real.md", mem / "reference" / "alias.md")
        html = _search(mem, "bahnhofstrasse")
        assert "real.md" in html
        assert "alias.md" not in html


def test_grep_fallback_skips_linked_folder():
    with tempfile.TemporaryDirectory() as tmp:
        mem = Path(tmp) / "mem"
        mem.mkdir()
        (mem / "reference").mkdir()
        ext = Path(tmp) / "ext"
        ext.mkdir()
        (ext / "leak.md").write_text("folder leak sentinelqq74\n", encoding="utf-8")
        try:
            os.symlink(ext, mem / "reference" / "linked", target_is_directory=True)
        except (OSError, NotImplementedError) as e:
            pytest.skip(f"symlinks unavailable: {e}")
        html = _search(mem, "sentinelqq74")
        assert "leak.md" not in html


# --- search folds (fts) -------------------------------------------------------------------------------------------
from atlas_plus.mcp.memory_server import fts  # noqa: E402


@pytest.fixture
def corpus(tmp_path, monkeypatch):
    monkeypatch.setenv("ATLAS_FTS_FRESHNESS_WINDOW", "0")  # notes are written between searches
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


def test_stroke_letters_meet_their_plain_spelling(corpus):
    write, search = corpus
    write("vi", "Đà Nẵng đẹp lắm, tôi đã đi được hai lần.")
    write("pl", "Spotkanie w Łodzi.")
    write("da", "Kontoret i København er lukket.")
    write("is", "Þórður og Guðrún fóru heim.")
    assert set(search("da nang")) == {"vi"} and set(search("duoc")) == {"vi"}
    assert set(search("lodzi")) == {"pl"}
    assert set(search("kobenhavn")) == {"da"}
    assert set(search("gudrun")) == {"is"} and set(search("thordur")) == {"is"}
    # the written form still finds itself and the snippet keeps the note's own letters
    assert "[Łodzi]" in search("Łodzi")["pl"]
    assert "[København]" in search("kobenhavn")["da"]


def test_a_toned_zwj_sequence_is_found_whole(corpus):
    write, search = corpus
    write("dev", "new hire \U0001F469\U0001F3FD‍\U0001F4BB onboarding")
    write("plain", "plain \U0001F469‍\U0001F4BB here")
    hits = search("\U0001F469‍\U0001F4BB")            # untoned query finds both, like a lone emoji does
    assert set(hits) == {"dev", "plain"}
    assert "[\U0001F469\U0001F3FD‍\U0001F4BB]" in hits["dev"]   # bracket closes after the whole sequence
    assert set(search("\U0001F469\U0001F3FD‍\U0001F4BB")) == {"dev"}   # a toned query asks for its tone
    # a lone emoji with a tone still behaves as before
    write("thumb", "approved \U0001F44D\U0001F3FD by all")
    assert set(search("\U0001F44D")) == {"thumb"}


def test_a_huge_note_brackets_the_word_not_a_longer_word_holding_it(corpus):
    write, search = corpus
    filler = "lorem ipsum dolor " * 4000          # over the 60,000-character snippet() limit: the cheap path
    write("big", "we concatenate strings first. " + filler + " the cat sat on the mat.")
    write("bigde", "Die Straßenbahn fährt. " + filler + " Die Straße ist frei.")
    assert "[cat]" in search("cat")["big"]
    assert "[Straße]" in search("strasse")["bigde"]
    # a term found only inside a longer word still gets a bracket (the first place)
    write("bigonly", "concatenate " + filler)
    assert "[concatenate]" in search("concatenate")["bigonly"]
