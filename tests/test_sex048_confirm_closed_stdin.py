"""SEX-048 (AG122's sibling paths): a confirmation on a closed stdin names the next step.

`sextant review archive` printed only "Aborted!" when its question could not be answered (a script, a
scheduled task, `< NUL`); lane AG122 gave it a next step. The two other confirmations of the tree did
the same: the temporal graph's `reset` and the database backup's `restore`. Each must say that nothing
was changed and how to run it without the question, and must change nothing.
"""
from __future__ import annotations

import sqlite3

from typer.testing import CliRunner


def _run_closed(app, args):
    # CliRunner with no input: the question meets end-of-file, as with a closed stdin.
    return CliRunner().invoke(app, args)


def test_temporal_reset_on_a_closed_stdin_names_the_next_step(tmp_path, monkeypatch):
    from atlas_plus.temporal_graph import cli
    called = []
    monkeypatch.setattr(cli, "_get_provider", lambda root: called.append(root))
    res = _run_closed(cli.app, ["reset", "--graph-root", str(tmp_path / "graph")])
    assert res.exit_code == 1
    assert "nothing was reset" in res.output
    assert "reset --yes" in res.output
    assert not called, "the reset ran without an answer"


def test_temporal_reset_answered_no_is_unchanged(tmp_path, monkeypatch):
    from atlas_plus.temporal_graph import cli
    called = []
    monkeypatch.setattr(cli, "_get_provider", lambda root: called.append(root))
    res = CliRunner().invoke(cli.app, ["reset", "--graph-root", str(tmp_path / "graph")], input="n\n")
    assert res.exit_code != 0
    assert not called


def test_db_restore_on_a_closed_stdin_names_the_next_step(tmp_path, monkeypatch):
    from atlas_plus.db_backup import cli
    snap, dest = tmp_path / "snap.sqlite", tmp_path / "live.sqlite"
    for p, v in ((snap, "old"), (dest, "live")):
        con = sqlite3.connect(p)
        con.execute("CREATE TABLE t (v TEXT)")
        con.execute("INSERT INTO t VALUES (?)", (v,))
        con.commit()
        con.close()
    called = []
    monkeypatch.setattr(cli, "restore", lambda **kw: called.append(kw))
    res = _run_closed(cli.app, ["restore", str(snap), "--dest", str(dest)])
    assert res.exit_code == 1, res.output
    assert "nothing was restored" in res.output
    assert "restore <snapshot> --yes" in res.output
    assert not called, "the restore ran without an answer"
    con = sqlite3.connect(dest)
    assert con.execute("SELECT v FROM t").fetchone() == ("live",)
    con.close()


def test_db_restore_answered_no_still_says_cancelled(tmp_path, monkeypatch):
    from atlas_plus.db_backup import cli
    snap = tmp_path / "snap.sqlite"
    snap.write_bytes(b"x")
    called = []
    monkeypatch.setattr(cli, "restore", lambda **kw: called.append(kw))
    res = CliRunner().invoke(cli.app, ["restore", str(snap), "--dest", str(tmp_path / "d.sqlite")], input="n\n")
    assert res.exit_code == 0
    assert "cancelled" in res.output.lower()
    assert not called
