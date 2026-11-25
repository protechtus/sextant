"""AG120 (sweep 36): format characters in printed note paths, and the dashboard's no-key answer (SEX-048)."""
from __future__ import annotations

import argparse

import pytest

RLO = chr(0x202E)
ZWSP = chr(0x200B)
LRI, RLI, FSI, PDI = chr(0x2066), chr(0x2067), chr(0x2068), chr(0x2069)
BOM = chr(0xFEFF)
ZWJ = chr(0x200D)


def test_safe_escapes_format_characters():
    from atlas_plus._term import safe
    for c in (RLO, ZWSP, LRI, RLI, FSI, PDI, BOM):
        out = safe(f"notes/a{c}b.md")
        assert c not in out
        assert out == "notes/a" + c.encode("unicode_escape").decode("ascii") + "b.md"


def test_safe_keeps_letters_and_joined_emoji():
    from atlas_plus._term import safe
    family = chr(0x1F468) + ZWJ + chr(0x1F469) + ZWJ + chr(0x1F467)
    persian = "????????"  # ZWNJ inside a Persian word
    for text in ("??/??.md", "?????.md",
                 "????.md", "caf?-na?ve.md", f"{family}.md", f"{persian}.md"):
        assert safe(text) == text
    assert safe("a\nb") == "a\\nb"  # the older control-character rule still holds


def test_doctor_report_escapes_bidi_in_note_paths():
    from atlas_plus.doctor import cli as doctor_cli
    evil = f"notes/{RLO}dm.exe"
    data = {
        "orphans": {"candidates": 1, "top": [{"path": evil, "age_days": 400, "score": 0.1}]},
        "convergence": {"flagged": 1, "paths": [f"notes/conv{RLO}x.md", "notes/two\nFAKE.md"]},
    }
    text = doctor_cli.render_text(data)
    assert RLO not in text
    assert "notes/\\u202edm.exe" in text
    assert "notes/conv\\u202ex.md" in text  # the convergence paths were printed raw
    assert "notes/two\\nFAKE.md" in text


def test_sync_accept_restore_escapes_paths(monkeypatch, capsys, tmp_path):
    from atlas_plus.sync import cli as sync_cli
    monkeypatch.setattr(sync_cli.atlas_paths, "memory_root", lambda: tmp_path)
    monkeypatch.setattr(sync_cli.manifest, "build_manifest", lambda root: {"files": {}})
    monkeypatch.setattr(sync_cli.store, "accept_current", lambda files: [f"projects/{RLO}gpj.md", f"a{ZWSP}b.md"])
    assert sync_cli.cmd_accept_restore(argparse.Namespace()) == 0
    out = capsys.readouterr().out
    assert RLO not in out and ZWSP not in out
    assert "projects/\\u202egpj.md" in out and "a\\u200bb.md" in out


@pytest.mark.parametrize("top, word", [
    ("This is a sentence with the word quokkafrond inside.", "quokkafrond"),
    ("- one\n- two", "- one"),
    ("42", "42"),
])
def test_mcp_read_keeps_a_top_block_that_is_text(monkeypatch, tmp_path, top, word):
    """search finds the word in the top block (AG119); the read of the same note must show it."""
    from atlas_plus.mcp.memory_server.tools import read as read_mod
    (tmp_path / "reference").mkdir()
    (tmp_path / "reference" / "n.md").write_text(f"---\n{top}\n---\nbody here\n", encoding="utf-8")
    (tmp_path / "reference" / "fm.md").write_text("---\ntitle: real\n---\nbody two\n", encoding="utf-8")
    monkeypatch.setattr(read_mod, "MEMORY_ROOT", tmp_path)
    res = read_mod.memory_read("reference/n.md")
    assert res["error"] is None and res["frontmatter"] == {}
    assert word in res["body"] and "body here" in res["body"]
    ok = read_mod.memory_read("reference/fm.md")  # a real frontmatter is still split off
    assert ok["frontmatter"] == {"title": "real"} and ok["body"] == "body two"


def test_sync_serve_prints_a_start_line(monkeypatch, capsys):
    """MN92 F8: started with its output in a file, the hub wrote nothing at all."""
    import socket
    import sys

    import uvicorn

    from atlas_plus.sync import api
    with socket.socket() as s:  # a free port for the bind pre-check
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    seen = {}
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: seen.update(k))
    monkeypatch.setattr(api, "create_app", lambda **k: object())
    monkeypatch.setattr(sys, "argv", ["sync-serve", "--host", "127.0.0.1", "--port", str(port)])
    api.main()
    assert seen.get("port") == port
    out = capsys.readouterr().out
    assert f"http://127.0.0.1:{port}" in out and out.isascii()


def test_import_into_a_read_only_corpus_names_the_folder(monkeypatch, capsys, tmp_path):
    import sys

    from atlas_plus import import_cmd
    from atlas_plus import paths as atlas_paths
    mem = tmp_path / "mem"
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "v.md").write_text("# v\n", encoding="utf-8")
    monkeypatch.setattr(atlas_paths, "memory_root", lambda: mem)

    def refused(*a, **k):
        raise PermissionError(13, "Access is denied", str(mem / "reference"))
    monkeypatch.setattr(import_cmd, "import_obsidian", refused)
    monkeypatch.setattr(sys, "argv", ["sextant-import", str(vault), "--source", "obsidian", "--yes"])
    with pytest.raises(SystemExit) as exc:
        import_cmd.main()
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert f"cannot write in {mem}" in err and "Next:" in err and "Errno" not in err


def test_review_archive_refused_and_missing_say_one_line(monkeypatch, capsys, tmp_path):
    import typer

    from atlas_plus.curation import cli as cur_cli
    from atlas_plus.curation import stale
    from atlas_plus import paths as atlas_paths
    monkeypatch.setattr(atlas_paths, "memory_root", lambda: tmp_path)

    def refused(path, label="curation"):
        raise PermissionError(13, "Access is denied", str(tmp_path / "archive"))
    monkeypatch.setattr(stale, "archive_file", refused)
    with pytest.raises(typer.Exit):
        cur_cli.cmd_archive(path="projects/a.md", label="curation", yes=True)
    err = capsys.readouterr().err
    assert f"cannot write in {tmp_path}" in err and "Next:" in err

    def missing(path, label="curation"):
        raise FileNotFoundError(f"Not found: {path}")
    monkeypatch.setattr(stale, "archive_file", missing)
    with pytest.raises(typer.Exit):
        cur_cli.cmd_archive(path="projects/nope.md", label="curation", yes=True)
    assert "Not found" in capsys.readouterr().err


def test_dashboard_without_key_names_the_next_step(monkeypatch):
    from fastapi import HTTPException

    from atlas_plus import _next_step
    from atlas_plus.web import app as web_app
    monkeypatch.setattr(web_app, "API_KEY", "")
    monkeypatch.setattr(web_app, "_ALLOW_NO_AUTH", False)
    monkeypatch.delenv("ATLAS_WEB_API_KEY", raising=False)
    with pytest.raises(HTTPException) as exc:
        web_app._check_auth(None)
    detail = exc.value.detail
    assert exc.value.status_code == 503
    assert detail.startswith(_next_step.key_missing("ATLAS_WEB_API_KEY"))
    assert "Next:" in detail and _next_step.command("web") in detail
    assert detail.isascii()
