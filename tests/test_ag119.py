"""AG119 (sweep 35): a note whose top block is not key/value pairs.

A note may open with a rule line over a sentence, a list, a number, `true` or a flow list. YAML
returns a str/list/int/bool for that block; every reader that called `.get` on it raised
AttributeError, so one such note broke `sextant review stale`, `report`, `orphans` ... for the whole
vault. These tests run the commands on a small corpus holding each shape.
"""
from __future__ import annotations

import pytest

SHAPES = {
    "sentence": "---\nJust a sentence under a rule zebracorn\n---\nbody\n",
    "list": "---\n- one zebracorn\n- two\n---\nbody\n",
    "number": "---\n42\n---\nbody zebracorn\n",
    "true": "---\ntrue\n---\nbody zebracorn\n",
    "flow": "---\n[a, zebracorn]\n---\nbody\n",
}

COMMANDS = [["stale"], ["unconfirmed-decay"], ["convergence"], ["health-score"],
            ["suggest-tags"], ["orphans"], ["report"], ["summary"], ["decay"], ["age-stats"]]


def _corpus(tmp_path, monkeypatch, shape):
    ws = tmp_path / "ws"
    mem = ws / ".memory"
    (mem / "reference").mkdir(parents=True)
    (mem / "reference" / "normal.md").write_text("---\ntitle: Normal\ntags: [x]\n---\nkettle\n", encoding="utf-8")
    (mem / "reference" / "odd.md").write_text(SHAPES[shape], encoding="utf-8")
    monkeypatch.setenv("ATLAS_WORKSPACE_ROOT", str(ws))
    monkeypatch.setenv("ATLAS_MEMORY_ROOT", str(mem))
    monkeypatch.setenv("ATLAS_STATE_DIR", str(tmp_path / "state"))
    for k in ("ATLAS_BRAIN_URL", "ATLAS_OLLAMA_URL", "ATLAS_EMBED_URL"):
        monkeypatch.setenv(k, "http://127.0.0.1:9")
    return mem


@pytest.mark.parametrize("shape", sorted(SHAPES))
@pytest.mark.parametrize("args", COMMANDS, ids=lambda a: a[0])
def test_review_commands_survive_a_non_mapping_top_block(tmp_path, monkeypatch, shape, args):
    from typer.testing import CliRunner
    from atlas_plus.curation import cli
    mem = _corpus(tmp_path, monkeypatch, shape)
    monkeypatch.setattr(cli, "MEMORY_ROOT", mem, raising=False)
    r = CliRunner().invoke(cli.app, args, env={"COLUMNS": "100", "NO_COLOR": "1", "TERM": "dumb"})
    assert not isinstance(r.exception, (AttributeError, TypeError)), (args, shape, repr(r.exception))


@pytest.mark.parametrize("shape", sorted(SHAPES))
def test_index_keeps_the_top_block_as_text(shape):
    """A non-mapping top block is text: the indexed body holds its words and the frontmatter is {}."""
    from atlas_plus.mcp.memory_server import fts
    fm_json, body, _ = fts._parse_md(SHAPES[shape])
    assert fm_json == "{}"
    assert "zebracorn" in body


def test_index_still_splits_real_frontmatter():
    from atlas_plus.mcp.memory_server import fts
    fm_json, body, _ = fts._parse_md("---\ntitle: T\n---\nkettle\n")
    assert '"title": "T"' in fm_json and body == "kettle"


@pytest.mark.parametrize("shape", sorted(SHAPES))
def test_load_mapping_contract(shape):
    from atlas_plus._yaml import load_mapping, load_mapping_or_none
    block = SHAPES[shape].split("---\n")[1]
    assert load_mapping(block) == {} and load_mapping(block, fast=False) == {}
    assert load_mapping_or_none(block) is None
    assert load_mapping_or_none("") == {} and load_mapping("title: t") == {"title": "t"}


@pytest.mark.parametrize("shape", sorted(SHAPES))
def test_mcp_note_readers_return_a_mapping(tmp_path, shape):
    """The MCP tools' frontmatter readers (list, skills, personas) hand callers a dict they `.get`."""
    from atlas_plus.mcp.memory_server.tools import list as tlist, personas, skills
    p = tmp_path / "odd.md"
    p.write_text(SHAPES[shape], encoding="utf-8")
    assert tlist._parse_frontmatter(SHAPES[shape]) == {}
    assert skills._parse_frontmatter(p) == {} and personas._parse_frontmatter(p) == {}
    assert skills._split_body(p)[0] == {} and personas._split_body(p)[0] == {}


@pytest.mark.parametrize("stamp, found", [
    ("", False),                               # the user's own note_1.md, never stamped by an import
    ("original_path: [a, b]\n", False),        # a list is not a path
    ("original_path: {a: b}\n", False),        # nor a mapping
    ("original_path: src.md\n", True),
])
def test_import_copies_reads_an_odd_or_missing_stamp_without_raising(tmp_path, stamp, found):
    """Folded in by the session (the lane left this loader unreviewed): a missing stamp raised AttributeError,
    a list or a mapping TypeError (unhashable). Fails before for the first three cases."""
    from pathlib import Path

    from atlas_plus import import_cmd
    (tmp_path / "note.md").write_text("x", encoding="utf-8")
    (tmp_path / "note_1.md").write_text(f"---\ntitle: t\n{stamp}---\nbody\n", encoding="utf-8")
    got = import_cmd._import_copies(tmp_path / "note.md", Path("src.md"), {}, rel="src.md")
    assert [q.name for q in got] == (["note.md", "note_1.md"] if found else ["note.md"])
