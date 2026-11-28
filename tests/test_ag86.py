"""AG86 (sweep 23 agentic fix wave): ticket numbers after a folder rename, deep archives, one prefix
per renamed folder, and the `show` / ambiguity path with its siblings (CLI, dashboard, Telegram)."""
from __future__ import annotations

import asyncio
import os
import time

import pytest


@pytest.fixture
def troot(tmp_path, monkeypatch):
    monkeypatch.setenv("ATLAS_AUTONOMOUS", "on")
    monkeypatch.setenv("ATLAS_STATE_DIR", str(tmp_path / "state"))
    from atlas_plus.tickets import store
    store._NEXT_MEMO.clear()
    root = tmp_path / "tickets"
    root.mkdir()
    monkeypatch.setattr(store, "TICKETS_ROOT", root)
    return root


def _mk(root, project, k=1):
    from atlas_plus.tickets import store
    return [store.create_ticket(f"t {project} {i}", project, tickets_root=root, interactive=True).id for i in range(k)]


def _all_ids(root):
    return sorted(p.stem for p in root.rglob("*.md"))


def _age(path, seconds=3600):
    t = time.time() - seconds
    os.utime(path, (t, t))


# ---------------------------------------------------------------- numbering
def test_a_new_folder_under_a_renamed_projects_old_name_does_not_reissue_ids(troot):
    assert _mk(troot, "alpha", 3) == ["ALP-001", "ALP-002", "ALP-003"]
    (troot / "alpha").rename(troot / "beta")
    new = _mk(troot, "alpha", 2)
    assert not set(new) & {"ALP-001", "ALP-002", "ALP-003"}, new   # 4eaa93a: ALP-001, ALP-002 again
    ids = _all_ids(troot)
    assert len(ids) == len(set(ids)), ids


def test_the_same_after_the_renamed_folder_went_to_the_archive(troot):
    _mk(troot, "alpha", 2)
    (troot / "_archive").mkdir()
    (troot / "alpha").rename(troot / "_archive" / "beta")
    assert _mk(troot, "alpha") == ["ALP-003"]                     # 4eaa93a: ALP-001


def test_a_renamed_folder_keeps_minting_the_prefix_its_tickets_carry(troot):
    _mk(troot, "alpha", 3)
    (troot / "alpha").rename(troot / "beta")
    assert _mk(troot, "beta") == ["ALP-004"]                      # 4eaa93a: BET-004 beside ALP-001..003
    reborn = _mk(troot, "alpha")[0]
    assert not reborn.startswith("ALP-"), reborn                  # the letters now belong to beta
    ids = _all_ids(troot)
    assert len(ids) == len(set(ids)), ids


def test_a_legacy_project_with_its_own_name_keeps_its_letters(troot):
    # MN32's rule is untouched: a folder whose tickets carry its own name is not "renamed"
    d = troot / "proj2"
    d.mkdir()
    (d / "PRO-001.md").write_text("---\nid: PRO-001\nproject: proj2\ntitle: x\n---\n", encoding="utf-8")
    assert _mk(troot, "proj2") == ["PRO-002"]


def test_a_ticket_archived_into_a_dated_folder_keeps_its_number(troot):
    _mk(troot, "alpha", 3)
    day = troot / "_archive" / "2026-11-15"
    day.mkdir(parents=True)
    os.replace(troot / "alpha" / "ALP-003.md", day / "ALP-003.md")
    from atlas_plus.tickets import store
    store._NEXT_MEMO.clear()                                       # a fresh process
    assert _mk(troot, "alpha") == ["ALP-004"]                     # 4eaa93a: ALP-003 again


def test_a_deeper_archive_folder_is_seen_by_a_warm_process(troot):
    deep = troot / "_archive" / "2026" / "q4" / "alpha"
    deep.mkdir(parents=True)
    for p in (deep, deep.parent, deep.parent.parent, troot / "_archive"):
        _age(p)
    _mk(troot, "alpha")
    (deep / "ALP-040.md").write_text("---\nid: ALP-040\nproject: alpha\ntitle: x\n---\n", encoding="utf-8")
    assert _mk(troot, "alpha") == ["ALP-041"]                     # 4eaa93a: ALP-002 (the stamp missed the level)


def test_a_move_into_an_old_archive_folder_right_before_a_create(troot):
    day = troot / "_archive" / "2026-11-01"
    day.mkdir(parents=True)
    _age(day)
    _age(troot / "_archive")
    _mk(troot, "alpha", 3)
    os.replace(troot / "alpha" / "ALP-003.md", day / "ALP-003.md")
    assert _mk(troot, "alpha") == ["ALP-004"]                     # 4eaa93a: ALP-003 again


def test_outside_writers_are_still_seen(troot):
    # AG84's guarantees, through the new memo
    _mk(troot, "proj", 3)
    arch = troot / "_archive" / "2026-11-14" / "proj"
    arch.mkdir(parents=True)
    os.replace(troot / "proj" / "PRO-003.md", arch / "PRO-003.md")
    assert _mk(troot, "proj") == ["PRO-004"]
    (troot / "proj" / "PRO-020.md").write_text("---\nid: PRO-020\n---\n", encoding="utf-8")
    assert _mk(troot, "proj") == ["PRO-021"]
    (arch / "PRO-030.md").write_text("---\nid: PRO-030\n---\n", encoding="utf-8")
    assert _mk(troot, "proj") == ["PRO-031"]


def test_create_cost_stays_flat_with_many_projects_and_archive_folders(troot, monkeypatch):
    import os
    from atlas_plus.tickets import store

    # The cost that must stay flat is FOLDER READING: before AG84 and AG86 every create listed the whole
    # project folder and every archive folder. It is counted, not timed: a wall-clock ratio (late < 3 * early)
    # failed in two full suites that ran beside heavy disk work and passed alone, with the product unchanged
    # (a file created in a folder of 1,500 entries is slower on a busy disk whatever this code does).
    read = {"entries": 0}
    real_scandir = os.scandir

    class _Counting:
        def __init__(self, path):
            self._it = real_scandir(path)

        def __enter__(self):
            self._it.__enter__()
            return self

        def __exit__(self, *exc):
            return self._it.__exit__(*exc)

        def __iter__(self):
            for entry in self._it:
                read["entries"] += 1
                yield entry

        def close(self):
            self._it.close()

    monkeypatch.setattr(os, "scandir", _Counting)
    # A folder whose time stamp is younger than one second is read again on purpose (a second change in the
    # same tick would hide behind it). That window is wall-clock too: on a fast Linux runner half of the late
    # batch fell inside it and each of those creates re-read the grown folder (CI run 46115591798: 798 and
    # 502 entries per create where Windows counted 46). The window is closed here: with it closed the count is
    # the same on every machine (46.0 early and late, 1,646 for the one re-read, measured 2026-11-27).
    monkeypatch.setattr(store, "_RACY_NS", 0)
    for p in range(20):
        d = troot / f"other{p:02d}"
        d.mkdir()
        for i in range(20):
            (d / f"OT{p:02d}-{i:03d}.md").write_text("---\nid: x\n---\n", encoding="utf-8")
    for day in range(30):
        for p in range(5):
            d = troot / "_archive" / f"2026-10-{day + 1:02d}" / f"other{p:02d}"
            d.mkdir(parents=True)
            (d / f"OT{p:02d}-9{day:02d}.md").write_text("---\nid: x\n---\n", encoding="utf-8")

    def batch(k):
        read["entries"] = 0
        for i in range(k):
            store.create_ticket(f"t {i}", "proj", tickets_root=troot, interactive=True)
        return read["entries"] / k   # folder entries read per create

    batch(20)
    early = max(batch(40), batch(40))
    for i in range(1500):
        (troot / "proj" / f"PRO-{i + 1000:04d}.md").write_text("---\nid: x\n---\n", encoding="utf-8")
    regrown = batch(1)                      # the grown folder is read again, once
    late = max(batch(40), batch(40))
    assert regrown >= 1500, regrown    # the counter sees a real folder read, so this test can fail
    assert early < 100, early          # no create lists the other projects or the archive folders
    assert late < 100, late            # 1,580 tickets in the project and none of them is listed per create


# ---------------------------------------------------------------- the show / ambiguity path
def _renamed_with_a_duplicate(root):
    """The pre-fix state MN73 found: alpha renamed to beta, a new alpha that reissued ALP-001."""
    a = root / "beta"
    a.mkdir()
    (a / "ALP-001.md").write_text("---\nid: ALP-001\nproject: alpha\nstatus: todo\ntitle: old one\nassignee: human\n---\nb\n",
                                  encoding="utf-8")
    n = root / "alpha"
    n.mkdir()
    (n / "ALP-001.md").write_text("---\nid: ALP-001\nproject: alpha\nstatus: todo\ntitle: new one\nassignee: human\n---\nb\n",
                                  encoding="utf-8")


def test_a_lowercase_id_finds_the_ticket_like_the_dashboard_does(troot):
    from atlas_plus.tickets import store
    _mk(troot, "alpha")
    t = store.get_ticket("alp-001", troot)
    assert t is not None and t.id == "ALP-001"                    # 4eaa93a: None ("not found")
    assert store.get_ticket("alpha/alp-001", troot) is not None


def test_cli_show_names_the_folder_of_a_renamed_project(troot):
    from typer.testing import CliRunner
    from atlas_plus.tickets.cli import app
    _renamed_with_a_duplicate(troot)
    r = CliRunner().invoke(app, ["show", "beta/ALP-001"])
    assert r.exit_code == 0, r.output
    assert "folder beta/" in r.output                              # 4eaa93a: "Project: alpha" only
    r = CliRunner().invoke(app, ["show", "alp-001"])
    assert r.exit_code == 1 and "alpha/ALP-001" in r.output and "beta/ALP-001" in r.output


def test_dispatch_sets_the_dispatched_ticket_in_progress_not_its_namesake(troot, tmp_path, monkeypatch):
    key = "ag86-test-key-" + "0123456789abcdef"
    monkeypatch.setenv("ATLAS_WEB_API_KEY", key)
    from fastapi.testclient import TestClient
    from atlas_plus.web import app as webapp
    from atlas_plus.tickets import store
    monkeypatch.setattr(webapp, "API_KEY", key, raising=False)
    _renamed_with_a_duplicate(troot)

    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"task_id": "t-ag86"}

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return None

        async def post(self, *a, **k):
            return _Resp()

    monkeypatch.setattr(webapp, "_PeerClient", _Client)

    async def _nobroadcast(*a, **k):
        return None

    monkeypatch.setattr(webapp, "_broadcast", _nobroadcast)
    c = TestClient(webapp.app, raise_server_exceptions=False)
    c.headers["Authorization"] = "Bearer " + key
    r = c.post("/api/tickets/beta/ALP-001/dispatch", json={})
    assert r.status_code in (200, 202), r.text
    assert r.json()["ticket_ref"] == "beta/ALP-001"                # 4eaa93a: "alpha/ALP-001"
    assert store.get_ticket("beta/ALP-001", troot).status == "in-progress"
    assert store.get_ticket("alpha/ALP-001", troot).status == "todo"   # 4eaa93a: this one was changed


def _tg(cmd, args):
    pytest.importorskip("telegram")
    from unittest.mock import AsyncMock, MagicMock
    from atlas_plus.telegram import commands
    update = MagicMock()
    update.message.reply_text = AsyncMock()
    ctx = MagicMock()
    ctx.args = list(args)
    asyncio.run(getattr(commands, cmd)(update, ctx))
    return " ".join(str(c.args[0]) for c in update.message.reply_text.call_args_list)


def test_telegram_done_and_priority_name_the_choices_for_an_ambiguous_id(troot):
    _renamed_with_a_duplicate(troot)
    out = _tg("cmd_done", ["alp-001"])
    assert "alpha/ALP-001" in out and "beta/ALP-001" in out, out   # 4eaa93a: "Ticket `ALP-001` not found."
    out = _tg("cmd_priority", ["alp-001", "high"])
    assert "alpha/ALP-001" in out and "beta/ALP-001" in out, out


def test_telegram_ticketstatus_takes_a_qualified_id_and_refuses_to_guess(troot):
    _renamed_with_a_duplicate(troot)
    out = _tg("cmd_ticket_status", ["beta/alp-001"])
    assert "not found" not in out and "ALP-001" in out, out        # 4eaa93a: "Ticket BETA/ALP-001 not found."
    out = _tg("cmd_ticket_status", ["ALP-001"])
    assert "alpha/ALP-001" in out and "beta/ALP-001" in out, out   # 4eaa93a: the first list match


def test_telegram_keeps_the_folder_case_of_a_qualified_id():
    pytest.importorskip("telegram")
    from atlas_plus.telegram.commands import _ticket_arg
    assert _ticket_arg("beta/alp-001") == "beta/ALP-001"
    assert _ticket_arg("alp-001") == "ALP-001"
