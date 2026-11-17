"""AG91 (sweep 24): Danish, Norwegian (bokmal, nynorsk), Swedish, Finnish, Icelandic and Faroese.

- Redaction: `kodeord: V`, `nøgle: V`, `API-nøgle: V`, `hemmelighed: V`, `lykilorð: V` and 150 more Nordic secret lines
  leaked (the key names, the password/key/secret/PIN/token words of the keyword-line pass, the `er`/`on` copulas, the
  `til`/`till` qualifiers past a definite ending, and NFD `är`); the Nordic status words (`påkrævet`, `saknas`) keep a
  label a label.
- Search: a query word typed WITH å/ø/ö/ä also finds its digraph spelling (`Ålborg` finds `Aalborg`), on the CLI/MCP path
  (fts.search) and the dashboard. The reverse (typed `aa` finding `Å`) is NOT done: measured, it merges Finnish words.
- Duplicates: the scans fold Latin diacritics the way search does (a Swedish or Finnish fact retyped without its letters).
Every secret-looking value is assembled at runtime.
"""
from __future__ import annotations

import os
import unicodedata
from pathlib import Path
from unittest.mock import patch

import pytest

os.environ.setdefault("ATLAS_WEB_API_KEY", "test-key-abc")
os.environ.setdefault("ATLAS_BRAIN_URL", "http://127.0.0.1:9")

from fastapi.testclient import TestClient  # noqa: E402

from atlas_plus.curation import duplicates as dup  # noqa: E402
from atlas_plus.mcp.memory_server import fts  # noqa: E402
from atlas_plus.redaction import redact  # noqa: E402
from atlas_plus.web import app as web_app  # noqa: E402

V = "Fj0" + "rd#K7" + "q2x"
W = "sommer" + "hus" + "2024"

LEAK = [
    # Danish
    "kodeord: {V}", "Kodeord: {W}", "kodeordet: {V}", "Kodeordet er {V}", "Kodeordet er {W}", "Mit kodeord er {V}",
    "Adgangskoden til den nye database på serveren er {W}", "Kodeordet til routeren er {W}",
    "nøgle: {V}", "Nøglen er {V}", "Nøglen er {W}", "nøglerne: {V}", "API-nøgle: {V}", "API-nøglen er {W}", "API-nøgle = {V}",
    "hemmelighed: {V}", "Hemmeligheden er {W}", "klienthemmelighed: {V}", "pinkode: {V}", "PIN-kode: {V}", "Pinkoden er {W}",
    "Tokenet er {W}", "adgangstoken: {V}", "adgangsfrase: {V}", "Adgangsfrasen er {V}", "løsen: {V}", "Løsenet er {V}",
    '"kodeord": "{V}"', "KODEORD={V}", "db_kodeord={V}", "**Kodeord:** {V}", "kodeord: hunterbaby",
    "| bruger | kodeord |\n|---|---|\n| jens | {V} |",
    # Norwegian bokmal and nynorsk
    "Passordet til serveren er {W}", "nøkkel: {V}", "Nøkkelen er {W}", "nøklene: {V}", "API-nøkkelen er {W}", "hemmelighet: {V}",
    "PIN-koden er {V}", "passfrase: {V}", "Passfrasen er {V}", "nykel: {V}", "Nykelen er {W}", "API-nykel: {V}", "løyndom: {V}",
    "Løyndomen er {W}", "hemmelegheit: {V}", "Hemmelegheita er {V}",
    # Swedish
    "Lösenordet till servern är {W}", "Lösenordet är {W}", "nyckel: {V}", "Nyckeln är {W}", "nycklarna: {V}", "API-nyckeln är {W}",
    "hemlighet: {V}", "Hemligheten är {W}", "PIN-kod: {V}", "PIN-koden är {V}", "lösenfras: {V}", "Lösenfrasen är {V}",
    "åtkomsttoken: {V}", "lösenord: hunterbaby",
    # Finnish
    "Palvelimen salasana on {W}", "Salasanasi on {V}", "avain: {V}", "Avain on {W}", "API-avain on {W}", "API-avaimesi on {V}",
    "salaisuus: {V}", "Salaisuus on {W}", "PIN-koodi on {V}", "PIN-koodisi on {V}", "tunnuslause: {V}", "Tunnuslause on {W}",
    "tunnussana: {V}", "Tokeni on {V}", "pääsytunnus: {V}", "salasana: hunterbaby",
    # Icelandic
    "lykilorð: {V}", "Lykilorðið er {W}", "Lykilorðið mitt er {V}", "lykilorðin: {V}", "lykill: {V}", "Lykillinn er {W}",
    "API-lykillinn er {V}", "leyndarmál: {V}", "Leyndarmálið er {W}", "PIN-númerið er {V}", "aðgangsorð: {V}",
    "Aðgangsorðið er {W}", "LYKILORÐ: {V}", "lykilorð: hunterbaby",
    # Faroese
    "loyniorð: {V}", "Loyniorðið er {W}", "lykil: {V}", "Lykilin er {W}", "API-lykil: {V}", "loyndarmál: {V}",
    "Loyndarmálið er {W}", "PIN-kota: {V}", "PIN-kotan er {V}",
]

CLEAN = [
    "Husk at skifte din adgangskode hver tredje måned.", "Kodeordet skal være mindst 12 tegn langt.",
    "Jeg har glemt nøglen til sommerhuset igen.", "API-nøglen udløber om 30 dage, så forny den i tide.",
    "Hemmeligheden bag god kode er enkelhed.", "Nøglerne ligger på bordet i entréen.", "Adgangskoden er udløbet, bed IT om en ny.",
    "Vi skifter kodeord via Windows11 og Office365 hver måned.", "Adgangskode: påkrævet", "Nøgle: udløbet", "Kodeord: ugyldigt",
    "Hemmelighed: ingen, alt ligger i Bitwarden.", "Nøglen er 4096 bit lang og af typen RSA.", "Nøglen er en Ed25519-nøgle fra 2024.",
    "Passordet må være minst 12 tegn langt.", "Passord: påkrevd", "Nøkkel: mangler", "Passord: påkravd",
    "Lösenordet måste vara minst 12 tecken långt.", "Lösenord: obligatoriskt", "Nyckel: saknas", "Lösenord: ogiltigt",
    "Nyckeln är av typen Ed25519.", "Lösenordet är utgånget, kontakta IT.",
    "Salasanan on oltava vähintään 12 merkkiä pitkä.", "Salasana: pakollinen", "Avain: puuttuu", "Salasana: vanhentunut",
    "Avain on tallennettu Vaultiin.", "Salasana on vanhentunut, ota yhteyttä IT-tukeen.",
    "Lykilorðið verður að vera að minnsta kosti 12 stafir.", "Lykilorð: krafist", "Lykill: vantar", "Lykilorð: útrunnið",
    "Loyniorðið má vera í minsta lagi 12 tekn.", "Loyniorð: kravt", "Loyniorðið er útgingið, set teg í samband við KT.",
]


def _line(t: str) -> str:
    return t.replace("{V}", V).replace("{W}", W).replace("hunterbaby", "hunter" + "baby")


@pytest.mark.parametrize("form", ["NFC", "NFD"])
@pytest.mark.parametrize("tmpl", LEAK)
def test_every_nordic_secret_line_is_masked(tmpl, form):
    line = unicodedata.normalize(form, _line(tmpl))
    out = redact(line, env={})
    for secret in (V, W, "hunter" + "baby"):
        assert secret not in out, (line, out)
    assert "[REDACTED" in out


@pytest.mark.parametrize("line", CLEAN)
def test_ordinary_nordic_sentences_are_untouched(line):
    assert redact(line, env={}) == line


def test_the_status_words_keep_a_label_while_a_value_is_masked():
    assert redact("Nyckel: saknas", env={}) == "Nyckel: saknas"
    assert redact("Nyckel: " + W, env={}) == "Nyckel: [REDACTED]"


# --- search -----------------------------------------------------------------------------------------------------

@pytest.fixture
def corpus(tmp_path, monkeypatch):
    monkeypatch.setenv("ATLAS_FTS_FRESHNESS_WINDOW", "0")
    mem = tmp_path / "mem"
    db = tmp_path / "state" / "search.sqlite"

    def write(name: str, body: str) -> None:
        p = mem / "reference" / f"{name}.md"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f"---\ntitle: t\n---\n{body}\n", encoding="utf-8")

    with patch.object(fts, "MEMORY_ROOT", mem), patch.object(fts, "FTS_DB", db):
        def search(q: str) -> dict:
            return {Path(r["path"]).stem: r["snippet"] for r in fts.search(q)}
        yield mem, write, search


def _nordic(write):
    write("n1", "Vi kører til Ålborg i weekenden.")
    write("n2", "Aalborg Universitet har et nyt laboratorium.")
    write("n3", "Søren bor i København og arbejder på Nørrebro.")
    write("n4", "Soeren flyttede fra Koebenhavn til Odense.")
    write("n5", "Konferensen i Goeteborg hölls i maj.")
    write("n6", "The poet wrote a poem about a toe and a pot.")


def test_a_query_with_the_letter_also_finds_the_digraph_spelling(corpus):
    _, write, search = corpus
    _nordic(write)
    hits = search("Ålborg")
    assert set(hits) == {"n1", "n2"}
    assert hits["n1"].startswith("Vi kører til [Ålborg] i weekenden.") and hits["n2"].startswith("[Aalborg]")  # in the note's own letters
    assert set(search("København")) == {"n3", "n4"}
    assert set(search("Søren")) == {"n3", "n4"}
    assert set(search("Göteborg")) == {"n5"}
    assert set(search("Ålborg*")) == {"n1", "n2"}
    assert set(search("Søren København")) == {"n3", "n4"}


def test_the_folds_do_not_reach_ascii_or_quoted_queries(corpus):
    _, write, search = corpus
    _nordic(write)
    assert set(search("poet")) == {"n6"} and set(search("toe")) == {"n6"}  # no oe->o: `toe` is not `to`
    assert set(search("aalborg")) == {"n1", "n2"}  # AG96: the note's `Ålborg` is also indexed as `aalborg` (an alias, not a fold)
    assert set(search("alborg")) == {"n1"}  # a typed `aa` is still not folded onto `a` (it merges Finnish words: measured)
    assert set(search('"Ålborg"')) == {"n1"}  # a quoted word is literal


def test_the_syntax_fallback_keeps_the_alternatives(corpus):
    _, write, search = corpus
    _nordic(write)
    assert set(search("Ålborg (")) == {"n1", "n2"}  # an unbalanced parenthesis takes the quoted-token path


def test_the_dashboard_finds_the_digraph_spelling(corpus):
    mem, write, _ = corpus
    _nordic(write)
    with patch.object(web_app, "MEMORY_ROOT", mem), patch.object(web_app, "_check_auth", lambda request: None):
        r = TestClient(web_app.app).get("/api/memory/search", params={"q": "Ålborg"})
    assert r.status_code == 200
    assert "n1.md" in r.text and "n2.md" in r.text


# --- duplicates -------------------------------------------------------------------------------------------------

def _two(root: Path, a: str, b: str) -> None:
    (root / "notes").mkdir(parents=True, exist_ok=True)
    (root / "notes" / "a.md").write_text(f"---\ntitle: a\n---\n- {a}\n", encoding="utf-8")
    (root / "notes" / "b.md").write_text(f"---\ntitle: b\n---\n- {b}\n", encoding="utf-8")


@pytest.mark.parametrize("a,b", [
    ("Konferensen i Göteborg börjar klockan nio på måndag morgon.", "Konferensen i Goteborg borjar klockan nio pa mandag morgon."),
    ("Hyvää päivää, palaveri pidetään Hämeenlinnassa tiistaina.", "Hyvaa paivaa, palaveri pidetaan Hameenlinnassa tiistaina."),
])
def test_a_fact_retyped_without_its_letters_is_a_repeat(tmp_path, a, b):
    root = tmp_path / "mem"
    _two(root, a, b)
    facts = dup.find_fact_duplicates(memory_root=root)
    assert any({f.get("file1"), f.get("file2")} == {"notes/a.md", "notes/b.md"} for f in facts), facts
    assert any({p.file1, p.file2} == {"notes/a.md", "notes/b.md"} for p in dup.find_duplicates(memory_root=root, threshold=0.4))


def test_different_nordic_facts_are_not_repeats(tmp_path):
    root = tmp_path / "mem"
    _two(root, "Mødet med kunden holdes i Århus på torsdag klokken ti.", "Sommerhuset i Skagen er lejet ud hele juli måned.")
    assert dup.find_fact_duplicates(memory_root=root) == []
    assert dup.find_duplicates(memory_root=root, threshold=0.4) == []
