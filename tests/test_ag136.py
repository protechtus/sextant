"""AG136 (sweep 40, SEX-062; MN98 F1, F2): a backup that stops must say so.

F1: every non-zero exit of `sextant-backup now` records the failed run (state file: not ok, the time, the
reason, `last_real_success` kept) and a log line BEFORE it exits, so `status` and doctor's Backup line show it.
F2: a failed `rclone listremotes` is reported as what it is, with rclone's last line whole (redacted), and an
encrypted config names RCLONE_CONFIG_PASS; it is never read as "no remotes".

rclone is replaced by a monkeypatched `backup._rclone` (no program is started, so no PATH or Windows search
order is involved): every assertion answers the same on Windows, Linux and macOS.
"""
from __future__ import annotations

import json
import subprocess

import pytest
import typer

from atlas_plus import backup as b

_GOOD = "2026-01-01T00:00:00+00:00"
# rclone 1.73's own text for an encrypted config without its password (longer than the old 120-char cut)
_CONFIG_ERR = ('2026/11/28 01:57:10 CRITICAL: Failed to load config file "/home/user/.config/rclone/rclone.conf": '
               "unable to decrypt configuration and not allowed to ask for password - set RCLONE_CONFIG_PASS "
               "to your configuration password")


def _flat(text: str) -> str:
    return " ".join(text.split())


class _Answers:
    """A fake `_rclone`: listremotes answers as given, sync/copy answer `op_rc`."""

    def __init__(self, list_rc=0, list_out="r:\n", list_err="", op_rc=0):
        self.list_rc, self.list_out, self.list_err, self.op_rc = list_rc, list_out, list_err, op_rc
        self.calls: list[tuple] = []

    def __call__(self, *args, capture=False):
        self.calls.append(args)
        if args and args[0] == "listremotes":
            return subprocess.CompletedProcess(["rclone", *args], self.list_rc, self.list_out, self.list_err)
        if args and args[0] == "version":
            return subprocess.CompletedProcess(["rclone", *args], 0, "rclone v1.73.5\n", "")
        if args and args[0] == "lsd":
            return subprocess.CompletedProcess(["rclone", *args], 0, "", "")
        return subprocess.CompletedProcess(["rclone", *args], self.op_rc, "", "op failed" if self.op_rc else "")


@pytest.fixture
def bk(tmp_path, monkeypatch):
    """backup.py pointed at a tmp state dir, holding the record of one good real run."""
    logs = tmp_path / "state" / "logs"
    logs.mkdir(parents=True)
    monkeypatch.setattr(b, "LOG_DIR", logs)
    monkeypatch.setattr(b, "BACKUP_LOG", logs / "backup.log")
    monkeypatch.setattr(b, "BACKUP_STATE", logs / "backup_state.json")
    monkeypatch.setattr(b, "RCLONE_REMOTE", "r")
    corpus = tmp_path / "corpus"
    (corpus / "projects").mkdir(parents=True)
    for i in range(6):
        (corpus / "projects" / f"n{i}.md").write_text(f"note {i}\n", encoding="utf-8")
    monkeypatch.setattr(b, "SYNC_TARGETS", [{"name": "memory corpus", "source": str(corpus), "dest": "r:m"}])
    monkeypatch.setattr(b, "ARCHIVE_TARGETS", [])
    good = {"last_run": _GOOD, "duration_s": 1.0, "success": True, "errors": [], "dry_run": False,
            "last_real_run": _GOOD, "last_real_success": _GOOD}
    (logs / "backup_state.json").write_text(json.dumps(good), encoding="utf-8")
    (logs / "backup.log").write_text("[2026-01-01T00:00:00Z] Backup complete in 1.0s\n", encoding="utf-8")
    return tmp_path


def _run_now(dry_run=False):
    with pytest.raises((typer.Exit, SystemExit)) as ei:
        b.now(dry_run=dry_run, skip_archive=True)
    code = getattr(ei.value, "exit_code", getattr(ei.value, "code", None))
    assert code not in (0, None)


def _status_text(capsys) -> str:
    capsys.readouterr()
    b.status()
    return _flat(capsys.readouterr().out)


# Every failure path of `now`: (id, how the remote / rclone answers, what the record must name)
_PATHS = [
    ("remote-unset", dict(remote=""), "ATLAS_RCLONE_REMOTE is not set"),
    ("rclone-missing", dict(fake=_Answers(list_rc=127, list_err="rclone executable not found on PATH")),
     "rclone is not on PATH"),
    ("config-unreadable", dict(fake=_Answers(list_rc=1, list_out="", list_err=_CONFIG_ERR)),
     "could not read its config"),
    ("no-such-remote", dict(fake=_Answers(list_out="other:\n")), "no remote named 'r'"),
    ("sync-failed", dict(fake=_Answers(op_rc=1)), "SYNC FAILED"),
    ("sanity-guard", dict(fake=_Answers(), empty_source=True), "SYNC REFUSED"),
]


@pytest.mark.parametrize("case,how,reason", _PATHS, ids=[p[0] for p in _PATHS])
def test_every_failed_now_is_recorded_and_status_and_doctor_show_it(bk, monkeypatch, capsys, case, how, reason):
    if "remote" in how:
        monkeypatch.setattr(b, "RCLONE_REMOTE", how["remote"])
    monkeypatch.setattr(b, "_rclone", how.get("fake") or _Answers())
    if how.get("empty_source"):
        empty = bk / "empty"
        empty.mkdir()
        monkeypatch.setattr(b, "SYNC_TARGETS", [{"name": "memory corpus", "source": str(empty), "dest": "r:m"}])
    _run_now()

    state = json.loads(b.BACKUP_STATE.read_text(encoding="utf-8"))
    assert state["success"] is False
    assert state["last_run"] != _GOOD and state["last_real_run"] == state["last_run"]
    assert state["last_real_success"] == _GOOD  # the last good run is kept, not lost
    assert any(reason in e for e in state["errors"]), state["errors"]
    assert all("\n" not in e for e in state["errors"])  # one line each

    last_log = b.BACKUP_LOG.read_text(encoding="utf-8").strip().splitlines()[-1]
    assert "Backup complete" not in last_log

    out = _status_text(capsys)
    assert "FAILED" in out and reason in out, out

    from atlas_plus.doctor import cli as doctor_cli
    monkeypatch.setattr(doctor_cli, "STATE_DIR", bk / "state")
    line = doctor_cli.check_backup()
    assert line["result"] != "ok" and "Backup complete" not in line["last_run"], line


def test_a_refused_dry_run_is_recorded_as_a_dry_run_and_keeps_the_real_record(bk, monkeypatch, capsys):
    monkeypatch.setattr(b, "_rclone", _Answers(list_rc=1, list_out="", list_err=_CONFIG_ERR))
    _run_now(dry_run=True)
    state = json.loads(b.BACKUP_STATE.read_text(encoding="utf-8"))
    assert state["dry_run"] is True and state["success"] is False
    assert state["last_real_run"] == _GOOD and state["last_real_success"] == _GOOD
    assert "dry run" in _status_text(capsys)


def test_a_refused_now_names_the_password_and_never_says_create_a_remote(bk, monkeypatch, capsys):
    monkeypatch.setattr(b, "_rclone", _Answers(list_rc=1, list_out="", list_err="NOTICE: x\n" + _CONFIG_ERR))
    _run_now()
    out = _flat(capsys.readouterr().out)
    assert "set RCLONE_CONFIG_PASS to your configuration password" in out  # rclone's own last line, whole
    assert "Next step" in out and "RCLONE_CONFIG_PASS in this shell" in out
    assert "create a remote" not in out and "install rclone" not in out


def test_verify_reports_a_failed_listremotes_as_a_failure_not_as_no_remotes(bk, monkeypatch, capsys):
    monkeypatch.setattr(b, "_rclone", _Answers(list_rc=1, list_out="", list_err=_CONFIG_ERR))
    with pytest.raises((typer.Exit, SystemExit)):
        b.verify()
    out = _flat(capsys.readouterr().out)
    assert "could not read its config" in out
    assert "set RCLONE_CONFIG_PASS to your configuration password" in out
    assert "RCLONE_CONFIG_PASS in this shell" in out
    assert "not found" not in out and "Configured remotes" not in out


def test_an_unreadable_config_that_is_not_encrypted_gets_a_neutral_next_step(bk, monkeypatch, capsys):
    err = 'CRITICAL: Failed to load config file "/home/user/rclone.conf": permission denied'
    monkeypatch.setattr(b, "_rclone", _Answers(list_rc=1, list_out="", list_err=err))
    with pytest.raises((typer.Exit, SystemExit)):
        b.verify()
    out = _flat(capsys.readouterr().out)
    assert "permission denied" in out and "rclone config file" in out
    assert "RCLONE_CONFIG_PASS" not in out


def test_rclones_message_is_redacted_but_kept_whole(bk, monkeypatch, capsys):
    secret = "gh" + "p_" + "Ab3dE6gH9jK2mN5pQ8sT1vW4yZ7bC0eF3hJ6"  # assembled at runtime
    err = "CRITICAL: Failed to load config file: token " + secret + " rejected - " + "x" * 150 + " END"
    monkeypatch.setattr(b, "_rclone", _Answers(list_rc=1, list_out="", list_err=err))
    _run_now()
    out = _flat(capsys.readouterr().out)
    state_text = b.BACKUP_STATE.read_text(encoding="utf-8") + b.BACKUP_LOG.read_text(encoding="utf-8")
    assert secret not in out and secret not in state_text
    assert " END" in out  # not cut at 120 characters


# ---- item 3 (MN98 F5): pre-compact honours the payload's cwd, like session-start -------------------------------

def test_pre_compact_takes_the_project_from_the_payload_cwd(tmp_path, monkeypatch):
    from atlas_plus.hooks import pre_compact as pc
    from atlas_plus.hooks import scope
    projects = tmp_path / "projects"
    proj = projects / "walk"
    (proj / ".git").mkdir(parents=True)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)  # the process folder is NOT the session's
    monkeypatch.setattr(pc, "PROJECTS_ROOT", projects)
    monkeypatch.setattr(pc, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(pc, "SESSION_STATE_FILE", tmp_path / "state" / "session-state.json")
    monkeypatch.setattr(pc, "_git_oneliner", lambda d: "main | clean | last: abc")  # no git is started
    monkeypatch.setattr(scope, "read_payload", lambda timeout=2.0: {"cwd": str(proj), "hook_event_name": "PreCompact"})
    monkeypatch.setattr("sys.argv", ["pre_compact"])
    pc.main()
    state = json.loads((tmp_path / "state" / "session-state.json").read_text(encoding="utf-8"))
    assert state["project"] == "walk" and state["git"] == "main | clean | last: abc"


def test_pre_compact_without_a_payload_still_uses_the_process_folder(tmp_path, monkeypatch):
    from atlas_plus.hooks import pre_compact as pc
    from atlas_plus.hooks import scope
    proj = tmp_path / "projects" / "walk"
    (proj / ".git").mkdir(parents=True)
    monkeypatch.chdir(proj)
    monkeypatch.setattr(pc, "PROJECTS_ROOT", tmp_path / "projects")
    monkeypatch.setattr(pc, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(pc, "SESSION_STATE_FILE", tmp_path / "state" / "session-state.json")
    monkeypatch.setattr(pc, "_git_oneliner", lambda d: "g")
    monkeypatch.setattr(scope, "read_payload", lambda timeout=2.0: {})
    monkeypatch.setattr("sys.argv", ["pre_compact"])
    pc.main()
    assert json.loads((tmp_path / "state" / "session-state.json").read_text(encoding="utf-8"))["project"] == "walk"


# ---- item 4 (MN98 F6): EDITOR with arguments --------------------------------------------------------------------

_EDITOR_SCRIPT = (
    "import json, os, sys\n"
    "path = sys.argv[-1]\n"
    "with open(path, 'a', encoding='utf-8') as f:\n"
    "    f.write('edited' + chr(10))\n"
    "with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'argv.json'), 'w', encoding='utf-8') as f:\n"
    "    json.dump(sys.argv[1:-1], f)\n"
)


@pytest.fixture
def own_tmp(tmp_path, monkeypatch):
    """The review's temp notes land in a folder of the test's own, so a leftover is seen."""
    import tempfile
    d = tmp_path / "tmpnotes"
    d.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(d))
    return d


def test_an_editor_with_arguments_is_split_and_run(tmp_path, monkeypatch, own_tmp):
    import sys
    from atlas_plus.summarizer import review
    folder = tmp_path / "my editor"  # a path with a space survives the split when quoted (both systems)
    folder.mkdir()
    script = folder / "ed.py"
    script.write_text(_EDITOR_SCRIPT, encoding="utf-8")
    monkeypatch.setenv("EDITOR", f'"{sys.executable}" "{script}" --wait')
    out = review._open_in_editor("line one\n", suffix=".md")
    assert out == "line one\nedited\n"
    assert json.loads((folder / "argv.json").read_text(encoding="utf-8")) == ["--wait"]
    assert list(own_tmp.iterdir()) == []


def test_an_editor_that_cannot_start_is_one_line_and_leaves_no_temp_file(monkeypatch, capsys, own_tmp):
    from atlas_plus.summarizer import review
    monkeypatch.setenv("EDITOR", "sextant-no-such-editor-ag136 --wait")
    out = review._open_in_editor("keep me\n", suffix=".md")  # no traceback
    assert out == "keep me\n"
    said = _flat(capsys.readouterr().out)
    assert "sextant-no-such-editor-ag136" in said and "Next step" in said
    assert list(own_tmp.iterdir()) == []


def test_an_unquoted_full_path_with_spaces_stays_whole(tmp_path):
    from atlas_plus.summarizer import review
    prog = tmp_path / "Program Files" / "Ed" / "ed.exe"
    prog.parent.mkdir(parents=True)
    prog.write_bytes(b"")
    assert review._editor_argv(str(prog)) == [str(prog)]
    assert review._editor_argv("code --wait") == ["code", "--wait"]
    assert review._editor_argv("") == []


@pytest.mark.skipif(__import__("os").name != "nt", reason="a Windows drive path; Linux and macOS EDITOR values are "
                    "POSIX shell words, covered by test_an_editor_with_arguments_is_split_and_run")
def test_a_quoted_windows_editor_path_with_flags():
    from atlas_plus.summarizer import review
    value = r'"C:\Program Files\Notepad++\notepad++.exe" -multiInst -nosession'
    assert review._editor_argv(value) == [r"C:\Program Files\Notepad++\notepad++.exe", "-multiInst", "-nosession"]
