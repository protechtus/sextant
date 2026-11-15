"""MN73 (sweep 22 manual wave): setup --out merges, the report's budget notes, ticket numbering.

Each test runs the path a person hits and checks the outcome; none reads source text.
"""
from __future__ import annotations

import json
import shlex

import pytest


def _stop(ws: str, extra: str = "") -> dict:
    cmd = ("python -m atlas_plus.hooks.run --env " + shlex.quote(f"ATLAS_WORKSPACE_ROOT={ws}")
           + extra + " stop")
    return {"matcher": "", "hooks": [{"type": "command", "command": cmd}]}


def test_a_quote_in_the_workspace_does_not_merge_two_installs():
    from atlas_plus.setup_cmd import _merge
    a, b = _stop("E:\\sb\\ws o'q"), _stop("E:\\sb\\ws o'r")
    merged = _merge({"hooks": {"Stop": [a]}}, {"hooks": {"Stop": [b]}})
    assert merged["hooks"]["Stop"] == [a, b]          # fac4034 removed ws o'q's hook: both read as `e:\sb\ws o`


def test_a_quote_workspace_rerun_still_replaces_its_own_entry():
    from atlas_plus.setup_cmd import _merge
    old = _stop("E:\\sb\\ws o'q", " --env ATLAS_SESSION_AUTONOMOUS=on")
    new = _stop("E:\\sb\\ws o'q")
    merged = _merge({"hooks": {"Stop": [old]}}, {"hooks": {"Stop": [new]}})["hooks"]["Stop"]
    assert len(merged) == 1 and "ATLAS_SESSION_AUTONOMOUS=on" in merged[0]["hooks"][0]["command"]


def test_forward_slash_spelling_is_the_same_windows_workspace():
    from atlas_plus.setup_cmd import _merge
    old = _stop("E:\\sb\\ws one", " --env ATLAS_SESSION_AUTONOMOUS=on")
    new = _stop("E:/sb/ws one")
    merged = _merge({"hooks": {"Stop": [old]}}, {"hooks": {"Stop": [new]}})["hooks"]["Stop"]
    assert len(merged) == 1                            # fac4034: two Stop hooks into one corpus
    assert "ATLAS_SESSION_AUTONOMOUS=on" in merged[0]["hooks"][0]["command"]


def test_an_empty_variable_at_setup_time_removes_the_carried_value(monkeypatch):
    from atlas_plus.setup_cmd import _merge
    old = _stop("/tmp/ws", " --env ATLAS_HOOK_SCOPE=/tmp/projA --env ATLAS_SESSION_AUTONOMOUS=on")
    new = _stop("/tmp/ws")
    monkeypatch.setenv("ATLAS_HOOK_SCOPE", "")
    cmd = _merge({"hooks": {"Stop": [old]}}, {"hooks": {"Stop": [new]}})["hooks"]["Stop"][0]["hooks"][0]["command"]
    assert "ATLAS_HOOK_SCOPE" not in cmd and "ATLAS_SESSION_AUTONOMOUS=on" in cmd
    monkeypatch.delenv("ATLAS_HOOK_SCOPE")
    cmd = _merge({"hooks": {"Stop": [old]}}, {"hooks": {"Stop": [new]}})["hooks"]["Stop"][0]["hooks"][0]["command"]
    assert "ATLAS_HOOK_SCOPE=/tmp/projA" in cmd       # unset (not empty) still keeps a hand-added value


def test_report_puts_each_budget_note_under_its_own_scan(tmp_path, monkeypatch):
    from atlas_plus.curation import duplicates as d
    from atlas_plus.curation import weekly_report as w

    def files(**_k):
        d._LAST_PARTIAL[0], d._LAST_PARTIAL[1] = 0, 0   # the file scan finished
        return []

    def facts(**_k):
        d._LAST_PARTIAL[0], d._LAST_PARTIAL[1] = 7, 70  # the facts scan ran out of time
        return []

    monkeypatch.setattr(d, "find_duplicates", files)
    monkeypatch.setattr(d, "find_fact_duplicates", facts)
    (tmp_path / "reference").mkdir()
    text, _ = w.generate_report(memory_root=tmp_path, dry_run=True)
    assert "The duplicate scan stopped" not in text      # fac4034 blamed the file scan
    head, _, _ = text.partition("## Never confirmed")
    assert "repeated-facts scan stopped at its time budget after 7 of 70 facts" in head


def test_cli_json_says_when_the_bands_ran(tmp_path, monkeypatch):
    from typer.testing import CliRunner
    from atlas_plus.curation import cli, duplicates as d

    def fake(**_k):
        d._LAST_APPROX[0] = True
        d._LAST_PARTIAL[0] = d._LAST_PARTIAL[1] = 0
        return []

    monkeypatch.setattr(d, "find_duplicates", fake)
    res = CliRunner().invoke(cli.app, ["duplicates", "--json"])
    assert res.exit_code == 0
    out = res.output  # stdout and stderr together under CliRunner's default
    assert "MinHash bands" in out
    d._LAST_APPROX[0] = False


@pytest.fixture
def troot(tmp_path, monkeypatch):
    monkeypatch.setenv("ATLAS_AUTONOMOUS", "on")
    from atlas_plus.tickets import store
    store._NEXT_MEMO.clear()
    return tmp_path


def test_a_ticket_dropped_flat_into_archive_keeps_its_number(troot):
    from atlas_plus.tickets import store
    ids = [store.create_ticket(f"t{i}", "alpha", tickets_root=troot, interactive=True).id for i in range(3)]
    assert ids[-1] == "ALP-003"
    (troot / "_archive").mkdir()
    (troot / "alpha" / "ALP-003.md").rename(troot / "_archive" / "ALP-003.md")
    store._NEXT_MEMO.clear()
    nxt = store.create_ticket("next", "alpha", tickets_root=troot, interactive=True).id
    assert nxt == "ALP-004"                            # fac4034 issued ALP-003 again


def test_ambiguity_hint_names_the_folder_of_a_renamed_project(troot):
    from atlas_plus.tickets import store
    store.create_ticket("one", "alpha", tickets_root=troot, interactive=True)
    (troot / "alpha").rename(troot / "beta")            # the frontmatter still says project: alpha
    (troot / "alpha").mkdir()
    (troot / "alpha" / "ALP-001.md").write_text((troot / "beta" / "ALP-001.md").read_text(encoding="utf-8"),
                                                encoding="utf-8")
    assert store.get_ticket("ALP-001", troot) is None
    hint = store.ambiguous_hint()
    assert "alpha/ALP-001" in hint and "beta/ALP-001" in hint   # fac4034: "alpha/ALP-001, alpha/ALP-001"
    assert store.get_ticket("beta/ALP-001", troot) is not None
