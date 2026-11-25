"""SEX-046: the decay scan parses YAML only for notes whose frontmatter can carry `half_life`.

With `threshold_days=0` (the dashboard's next-actions panel and the report) only an explicit `half_life:` makes a
note a candidate, so a note whose frontmatter text has neither the word nor a backslash is skipped before the
YAML parse (20,000 parses were 6 s of the panel). Each test runs `find_decayed_facts`:
  a  the result equals the result with the skip switched off, on a corpus with every shape of frontmatter;
  b  the parser runs only for the notes that can carry the key (fails before the change: once per note);
  c  with a default half-life (`threshold_days > 0`) every note is still parsed and found.
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from atlas_plus.curation import decay

OLD = (date.today() - timedelta(days=400)).isoformat()


@pytest.fixture
def corpus(tmp_path, monkeypatch):
    mem = tmp_path / "mem"
    (mem / "reference").mkdir(parents=True)
    monkeypatch.setattr(decay, "MEMORY_ROOT", mem, raising=False)
    notes = {
        "plain-00.md": f"---\ntitle: plain\nupdated: {OLD}\n---\nbody mentions half_life: 3 in the text only\n",
        "explicit.md": f"---\ntitle: explicit\nupdated: {OLD}\nhalf_life: 30\n---\nbody\n",
        "quoted-key.md": f"---\ntitle: quoted\nupdated: {OLD}\n\"half_life\": 20\n---\nbody\n",
        "escaped-key.md": f"---\ntitle: escaped\nupdated: {OLD}\n\"half\\x5flife\": 10\n---\nbody\n",
        "constraint.md": f"---\ntitle: rule\ntype: constraint\nupdated: {OLD}\nhalf_life: 5\n---\nbody\n",
        "no-decay.md": f"---\ntitle: keep\ndecays: false\nupdated: {OLD}\nhalf_life: 5\n---\nbody\n",
        "bad-number.md": f"---\ntitle: bad\nupdated: {OLD}\nhalf_life: soon\n---\nbody\n",
        "not-due.md": f"---\ntitle: fresh\nupdated: {date.today().isoformat()}\nhalf_life: 30\n---\nbody\n",
        "broken-yaml.md": f"---\ntitle: [unclosed\nhalf_life: 5\nupdated: {OLD}\n---\nbody\n",
        "no-frontmatter.md": "half_life: 5\njust text\n",
        "unterminated.md": f"---\ntitle: open\nhalf_life: 5\nupdated: {OLD}\n",
        "empty.md": "",
    }
    for i in range(1, 8):
        notes[f"plain-{i:02d}.md"] = f"---\ntitle: plain {i}\nupdated: {OLD}\ntags: [a, b]\n---\nbody {i}\n"
    for name, text in notes.items():
        (mem / "reference" / name).write_text(text, encoding="utf-8")
    return notes


def _scan(threshold=0):
    return sorted((c.as_dict() for c in decay.find_decayed_facts(threshold_days=threshold)), key=lambda d: d["path"])


def test_result_equals_the_scan_without_the_skip(corpus, monkeypatch):
    fast = _scan()
    monkeypatch.setattr(decay, "_may_set_half_life", lambda raw: True)
    full = _scan()
    assert fast == full
    assert [d["path"].rsplit("/", 1)[-1] for d in fast] == ["escaped-key.md", "explicit.md", "quoted-key.md"]


def test_yaml_is_parsed_only_where_the_key_can_be(corpus, monkeypatch):
    parsed: list[str] = []
    real = decay._load_mapping

    def load(text):
        parsed.append(text)
        return real(text)

    monkeypatch.setattr(decay, "_load_mapping", load)
    _scan()
    with_key = [n for n, t in corpus.items()
                if t.startswith("---") and "\n---" in t[3:] and ("half_life" in t[3:t.find("\n---", 3)]
                                                                 or "\\" in t[3:t.find("\n---", 3)])]
    assert len(parsed) == len(with_key) == 8
    assert len(parsed) < len(corpus) - 8, "the plain notes were parsed"


def test_a_default_half_life_still_reads_every_note(corpus, monkeypatch):
    parsed: list[str] = []
    real = decay._load_mapping
    monkeypatch.setattr(decay, "_load_mapping", lambda text: (parsed.append(text), real(text))[1])
    out = _scan(threshold=30)
    names = {d["path"].rsplit("/", 1)[-1] for d in out}
    assert {"plain-00.md", "plain-07.md", "explicit.md"} <= names
    assert len(parsed) == len([t for t in corpus.values() if t.startswith("---") and "\n---" in t[3:]])
