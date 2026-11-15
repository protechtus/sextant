"""AG89 (sweep 23): search parity across the CLI, MCP and dashboard, Hebrew acronyms, and the duplicate scans.

- The dashboard fell back to a substring grep whenever the index found nothing, so `لله`, `רגיש` and `💻` answered on
  the dashboard while `sextant search` and the MCP tool said "No hits"; it also printed `reference\\x.md` paths.
- A Hebrew acronym behind a prefix (`בצה״ל`, `בצה"ל`) was the tokens `בצה` + `ל`: `צה״ל` never found it, and the
  ASCII-quote query `צה"ל` became the FTS5 phrase `"ל`, which bracketed every ל-prefixed word.
- The duplicate scans did not fold the Arabic/Hebrew prefixes and ignored emoji, unlike search.
"""
from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

os.environ.setdefault("ATLAS_WEB_API_KEY", "test-key-abc")  # the key test_web_app expects: the first module collected sets it
os.environ.setdefault("ATLAS_BRAIN_URL", "http://127.0.0.1:9")

from fastapi.testclient import TestClient  # noqa: E402

from atlas_plus.curation import duplicates as dup  # noqa: E402
from atlas_plus.mcp.memory_server import fts  # noqa: E402
from atlas_plus.mcp.memory_server.tools import remember  # noqa: E402
from atlas_plus.web import app as web_app  # noqa: E402

GERSHAYIM = "״"
IDF = "צה" + GERSHAYIM + "ל"          # צה״ל
IDF_ASCII = "צה\"ל"                    # צה"ל
IDF_PLAIN = "צהל"                      # צהל
BE = "ב"                                         # ב (in)
LEVAYIT = "לבית"                  # לבית (to the house)


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
        yield mem, write, search


# --- Hebrew acronyms --------------------------------------------------------------------------------------------

def test_an_acronym_behind_a_prefix_is_found_by_every_spelling(corpus):
    _, write, search = corpus
    write("gershayim", "הוא שירת " + BE + IDF + " שלוש שנים")
    write("ascii", "היא שירתה " + BE + IDF_ASCII + " שנתיים")
    write("bare", IDF + " הודיע היום")
    write("other", "שלום עולם")
    for q in (IDF, IDF_ASCII, IDF_PLAIN, BE + IDF, BE + IDF_ASCII):
        assert set(search(q)) == {"gershayim", "ascii", "bare"}, q
    hits = search(IDF)
    # the snippet is the note's own text: the gershayim (or the ASCII quote) and the prefix stay inside the bracket
    assert "[" + BE + IDF + "]" in hits["gershayim"]
    assert "[" + BE + IDF_ASCII + "]" in hits["ascii"]
    assert "[" + IDF + "]" in hits["bare"]


def test_an_ascii_quote_acronym_query_does_not_bracket_lamed_prefixed_words(corpus):
    _, write, search = corpus
    write("lamed", IDF + " הגיע " + LEVAYIT + " ולעבודה")
    write("lamed-only", "הלכנו " + LEVAYIT + " אתמול")
    hits = search(IDF_ASCII)
    assert set(hits) == {"lamed"}                    # `"ל` matched any note holding a ל-prefixed word
    assert "[" + LEVAYIT + "]" not in hits["lamed"]
    assert "[" + IDF + "]" in hits["lamed"]


def test_a_quoted_hebrew_phrase_is_still_a_phrase(corpus):
    _, write, search = corpus
    shalom, olam = "שלום", "עולם"
    write("together", shalom + " " + olam)
    write("apart", shalom + " וגם " + olam)
    assert set(search('"' + shalom + " " + olam + '"')) == {"together"}


# --- one answer on every surface --------------------------------------------------------------------------------

def _dash(mem: Path, q: str, **kw) -> str:
    with patch.object(web_app, "MEMORY_ROOT", mem), patch.object(web_app, "_check_auth", lambda request: None):
        if kw:
            with patch("atlas_plus.mcp.memory_server.tools.search.memory_search", **kw):
                r = TestClient(web_app.app).get("/api/memory/search", params={"q": q})
        else:
            r = TestClient(web_app.app).get("/api/memory/search", params={"q": q})
    assert r.status_code == 200
    return r.text


def test_the_dashboard_says_no_hits_where_the_index_says_no_hits(corpus):
    mem, write, search = corpus
    write("arabic", "بسم الله الرحمن")   # بسم الله الرحمن
    write("feel", "אני מרגיש טוב")              # אני מרגיש טוב
    write("dev", "The developer \U0001F469\U0001F3FD‍\U0001F4BB shipped it.")
    for q in ("لله", "רגיש", "\U0001F4BB"):
        assert search(q) == {}, q                     # what the CLI and the MCP tool get
        html = _dash(mem, q)
        assert "No hits" in html, q
        for stem in ("arabic", "feel", "dev"):
            assert stem + ".md" not in html, (q, html)
    # and a real hit still lists the note with a forward-slash path
    html = _dash(mem, "الله")
    assert "reference/arabic.md" in html and "No hits" not in html


def test_the_substring_fallback_is_labelled_folded_and_prints_forward_slashes(tmp_path):
    mem = tmp_path / "mem"
    (mem / "reference" / "deep").mkdir(parents=True)
    (mem / "reference" / "deep" / "street.md").write_text("Die Straße nach Łódź\n", encoding="utf-8")
    html = _dash(mem, "strasse", side_effect=RuntimeError("index unavailable"))
    assert "substring search" in html
    assert "reference/deep/street.md" in html and "\\" not in html.split("substring search")[1].split("</div>")[1]
    assert "Straße" in html                      # folded like search: `strasse` meets `Straße`
    assert "street.md" in _dash(mem, "lodz", side_effect=RuntimeError("index unavailable"))


# --- duplicate scans fold like search ---------------------------------------------------------------------------

def test_file_duplicates_fold_prefixes_and_see_emoji():
    hasharat = "השרת הראשי נפל אתמול"  # השרת הראשי נפל אתמול
    sharat = "שרת ראשי נפל אתמול"
    assert dup._tokenize(hasharat) == dup._tokenize(sharat)
    al = "الخادم الرئيسي توقف"   # الخادم الرئيسي توقف
    bare = "خادم رئيسي توقف"
    assert dup._tokenize(al) == dup._tokenize(bare)
    rocket = dup._tokenize("deploy finished tonight \U0001F680")
    bug = dup._tokenize("deploy finished tonight \U0001F41B")
    assert rocket != bug and dup._jaccard(rocket, bug) < 1.0
    # a skin tone is a variant of the same emoji, as in search
    assert dup._tokenize("great work team \U0001F44D\U0001F3FD") == dup._tokenize("great work team \U0001F44D")


def test_find_duplicates_pairs_prefixed_hebrew_notes(tmp_path):
    mem = tmp_path / "mem"
    (mem / "reference").mkdir(parents=True)
    a = "השרת הראשי נפל אתמול ושוב קרס בערב"
    b = "שרת ראשי נפל אתמול שוב קרס ערב"
    (mem / "reference" / "a.md").write_text(a + "\n", encoding="utf-8")
    (mem / "reference" / "b.md").write_text(b + "\n", encoding="utf-8")
    pairs = dup.find_duplicates(memory_root=mem, threshold=0.9)
    assert [(Path(p.file1).name, Path(p.file2).name) for p in pairs] in ([("a.md", "b.md")], [("b.md", "a.md")])


def test_fact_duplicates_fold_prefixes_and_keep_emoji(tmp_path):
    mem = tmp_path / "mem"
    (mem / "reference").mkdir(parents=True)
    fact_he = "השרת הראשי נפל אתמול ושוב קרס בערב שעבר"
    fact_he2 = "שרת ראשי נפל אתמול שוב קרס ערב שעבר"
    (mem / "reference" / "one.md").write_text(fact_he + "\nThe release train leaves on Friday \U0001F680\n", encoding="utf-8")
    (mem / "reference" / "two.md").write_text(fact_he2 + "\nThe release train leaves on Friday \U0001F41B\n", encoding="utf-8")
    exact = [d for d in dup.find_fact_duplicates(memory_root=mem) if d["kind"] == "exact"]
    facts = [d["fact"] for d in exact]
    assert len(exact) == 1 and facts[0] in (fact_he, fact_he2)   # the prefixed spelling is the same fact
    assert dup._norm_fact("Friday \U0001F680") != dup._norm_fact("Friday \U0001F41B")   # astral emoji were stripped


def test_the_remember_hint_folds_prefixes_like_the_snippet_it_is_compared_with():
    fact = "שרת ראשי נפל"
    snippet = "[השרת] הראשי נפל"   # search shows the note's own `השרת`
    assert remember._jaccard(fact, snippet) == 1.0
