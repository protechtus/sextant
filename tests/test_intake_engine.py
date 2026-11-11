"""Intake engine: end-to-end runs against a temp drop folder and temp workspace."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from atlas_plus.intake import cli, engine, scan, store

BRIEF = "---\nproject: MyApp\nartifact_type: product-brief\nintended_consumer: Sextant\n---\n# MyApp\nbody\n"


@pytest.fixture
def ws(tmp_path, monkeypatch):
    root = tmp_path / "ws"
    (root / ".memory").mkdir(parents=True)
    drop = tmp_path / "drive" / "Intake"
    drop.mkdir(parents=True)
    monkeypatch.setenv("ATLAS_WORKSPACE_ROOT", str(root))
    monkeypatch.setenv("ATLAS_STATE_DIR", str(root / ".atlas"))
    monkeypatch.setenv("ATLAS_MEMORY_ROOT", str(root / ".memory"))
    monkeypatch.setenv("ATLAS_PROJECTS_ROOT", str(root / "projects"))
    monkeypatch.setenv("ATLAS_INTAKE_DIR", str(drop))
    monkeypatch.delenv("ATLAS_INTAKE_LANDING", raising=False)
    monkeypatch.delenv("ATLAS_INTAKE_RULES", raising=False)
    monkeypatch.delenv("ATLAS_NTFY_TOPIC", raising=False)
    # Tickets: the autouse conftest fixture already isolates TICKETS_ROOT.
    return root, drop


def run(**kw):
    kw.setdefault("settle_seconds", 0)
    kw.setdefault("min_age_seconds", 0)
    kw.setdefault("notify", False)
    return engine.run_once(**kw)


def test_source_dir_requires_config(monkeypatch):
    monkeypatch.delenv("ATLAS_INTAKE_DIR", raising=False)
    with pytest.raises(engine.IntakeConfigError):
        engine.source_dir()


def test_missing_source_folder_is_a_note_not_a_crash(ws):
    root, drop = ws
    report = run(source=drop / "nope")
    assert report.records == [] and any("not found" in n for n in report.notes)


def test_reachable_retries_through_cloud_mount_hiccups(tmp_path, monkeypatch):
    calls = {"n": 0}
    real_is_dir = Path.is_dir

    def flaky(self):
        calls["n"] += 1
        if calls["n"] < 3:
            raise OSError(22, "The I/O operation has been aborted", None, 995)
        return real_is_dir(self)

    monkeypatch.setattr(Path, "is_dir", flaky)
    slept = []
    ok, why = engine._reachable(tmp_path, sleeper=slept.append)
    assert (ok, why) == (True, "") and calls["n"] == 3 and slept == [0.5, 1.0]


def _abort(*a, **k):
    raise OSError(22, "The I/O operation has been aborted", None, 995)


def test_unreachable_source_is_a_note_not_a_traceback(ws, monkeypatch):
    root, drop = ws
    # Scoped patch: a bare monkeypatch.undo() would also drop the ws env vars
    # and point the asserts at the real ledger.
    with monkeypatch.context() as m:
        m.setattr(Path, "is_dir", _abort)
        report = run(sleeper=lambda s: None)
    assert report.records == [] and len(report.notes) == 1
    assert "unreachable after 5 attempts" in report.notes[0] and "aborted" in report.notes[0]
    assert store.ledger_load() == [] and not (drop / "_imported").exists()


def test_io_abort_mid_read_is_unstable_and_unledgered(ws, monkeypatch):
    root, drop = ws
    (drop / "g.md").write_text(BRIEF, encoding="utf-8")
    with monkeypatch.context() as m:
        m.setattr(engine.scan, "sha256_file", _abort)
        report = run()
    assert report.counts() == {"unstable": 1} and report.records[0].reason.startswith("io: OSError")
    assert (drop / "g.md").exists() and store.ledger_load() == []
    assert run().counts() == {"imported": 1}          # next run succeeds untouched


def test_brief_lands_in_project_intake_with_ticket_ledger_archive_and_manifest(ws):
    root, drop = ws
    (drop / "MyApp_Brief.md").write_text(BRIEF, encoding="utf-8")
    report = run()
    assert report.counts() == {"imported": 1}
    rec = report.records[0]
    dest = root / "projects" / "myapp" / "intake" / "MyApp_Brief.md"
    assert Path(rec.dest) == dest and dest.read_text(encoding="utf-8") == BRIEF
    assert rec.sha256 == scan.sha256_file(dest)
    assert rec.frontmatter == {"project": "MyApp", "artifact_type": "product-brief", "intended_consumer": "Sextant"}
    # ticket action ran (autouse fixture isolates the ticket root)
    assert rec.actions["ticket"].startswith("created")
    # original archived with sidecar; drop folder now empty of candidates
    assert not (drop / "MyApp_Brief.md").exists()
    parked = Path(rec.archived_to)
    assert parked.parent.parent.name == "_imported" and parked.read_text(encoding="utf-8") == BRIEF
    meta = json.loads(parked.with_name(parked.name + ".meta.json").read_text(encoding="utf-8"))
    assert meta["sha256"] == rec.sha256 and meta["destination"] == str(dest)
    assert scan.discover(drop)[0] == []
    # ledger + run report + manifest
    ledger = store.ledger_load()
    assert len(ledger) == 1 and ledger[0]["status"] == "imported" and ledger[0]["run_id"] == report.run_id
    assert store.last_run()["counts"] == {"imported": 1}
    manifest = (drop / "_imported" / "MANIFEST.md").read_text(encoding="utf-8")
    assert "MyApp_Brief.md" in manifest and "frontmatter-project" in manifest and "project=MyApp" in manifest


def test_same_bytes_again_is_duplicate_and_archived_not_recopied(ws):
    root, drop = ws
    (drop / "a.md").write_text(BRIEF, encoding="utf-8")
    run()
    dest = root / "projects" / "myapp" / "intake" / "a.md"
    first_mtime = dest.stat().st_mtime_ns
    (drop / "sub").mkdir()
    (drop / "sub" / "a-again.md").write_text(BRIEF, encoding="utf-8")
    report = run()
    assert report.counts() == {"duplicate": 1}
    assert dest.stat().st_mtime_ns == first_mtime
    assert not (root / "projects" / "myapp" / "intake" / "a-again.md").exists()
    assert not (drop / "sub" / "a-again.md").exists()           # archived, not left to loop forever
    assert "same bytes imported" in report.records[0].reason


def test_changed_content_same_name_versions_destination_first(ws):
    root, drop = ws
    (drop / "a.md").write_text(BRIEF, encoding="utf-8")
    run()
    (drop / "a.md").write_text(BRIEF + "\nrevised\n", encoding="utf-8")
    report = run()
    rec = report.records[0]
    assert rec.status == "imported" and rec.versioned
    assert Path(rec.versioned).exists() and Path(rec.versioned).read_text(encoding="utf-8") == BRIEF
    assert "versions" in Path(rec.versioned).parts
    dest = root / "projects" / "myapp" / "intake" / "a.md"
    assert dest.read_text(encoding="utf-8").endswith("revised\n")


def test_identical_bytes_already_at_destination_writes_nothing(ws):
    root, drop = ws
    dest = root / "intake" / "notes"
    (drop / "n.md").write_text("plain note\n", encoding="utf-8")
    report = run()
    assert report.counts() == {"imported": 1}
    rec = report.records[0]
    # Drop a different file whose bytes already live at that exact path: hash differs
    # from nothing in the ledger only if we clear it -- simulate by using a fresh ledger.
    store.ledger_path().unlink()
    (drop / "n.md").write_text("plain note\n", encoding="utf-8")
    report = run()
    assert report.counts() == {"already-at-dest": 1}
    assert Path(report.records[0].dest) == Path(rec.dest)


def test_unsafe_frontmatter_destination_is_quarantined(ws):
    root, drop = ws
    (drop / "evil.md").write_text("---\ndestination: ../../outside.md\n---\nx\n", encoding="utf-8")
    report = run()
    assert report.counts() == {"quarantined": 1}
    rec = report.records[0]
    q = Path(rec.dest)
    assert q.parent.parent.name == "_quarantine" and q.read_text(encoding="utf-8").endswith("x\n")
    assert not (tmp_outside := root.parent / "outside.md").exists(), tmp_outside
    assert json.loads(q.with_name(q.name + ".meta.json").read_text(encoding="utf-8"))["reason"].startswith("unsafe")


def test_unstable_file_is_left_for_next_run_and_not_ledgered(ws):
    root, drop = ws
    (drop / "fresh.md").write_text("x\n", encoding="utf-8")
    report = run(min_age_seconds=3600)
    assert report.counts() == {"unstable": 1}
    assert (drop / "fresh.md").exists() and store.ledger_load() == []


def test_dry_run_writes_nothing_anywhere(ws):
    root, drop = ws
    (drop / "b.md").write_text(BRIEF, encoding="utf-8")
    report = run(dry_run=True)
    assert report.counts() == {"dry-run": 1}
    assert (drop / "b.md").exists()
    assert not (root / "projects").exists() and store.ledger_load() == [] and store.last_run() is None
    assert not (drop / "_imported").exists()


def test_leave_originals_keeps_the_drop_folder_intact(ws):
    root, drop = ws
    (drop / "c.md").write_text(BRIEF, encoding="utf-8")
    report = run(leave_originals=True)
    assert report.counts() == {"imported": 1} and (drop / "c.md").exists() and report.records[0].archived_to == ""
    # and the next run treats it as a duplicate without moving it
    report = run(leave_originals=True)
    assert report.counts() == {"duplicate": 1} and (drop / "c.md").exists()


def test_native_gdoc_and_temp_files_are_reported_skipped(ws):
    root, drop = ws
    (drop / "Shared doc.gdoc").write_text('{"url": "x"}', encoding="utf-8")
    (drop / "~$draft.docx").write_bytes(b"lock")
    report = run()
    assert report.counts() == {"skipped": 2} and report.records == []


def test_ticket_failure_does_not_undo_import(ws, monkeypatch):
    root, drop = ws
    monkeypatch.setenv("ATLAS_AUTONOMOUS", "off")   # kill switch closed: ticket write must fail
    (drop / "d.md").write_text(BRIEF, encoding="utf-8")
    report = run()
    rec = report.records[0]
    assert rec.status == "imported" and rec.actions["ticket"].startswith("failed:")
    assert (root / "projects" / "myapp" / "intake" / "d.md").exists()


def test_manifest_groups_newest_first_and_escapes_pipes():
    recs = [
        {"ts": "2026-11-05T10:00:00+00:00", "status": "imported", "source_rel": "a|b.md", "rule": "markdown", "dest": "x"},
        {"ts": "2026-11-06T09:00:00+00:00", "status": "duplicate", "source_rel": "c.md", "rule": "markdown", "dest": "y", "reason": "same bytes"},
    ]
    text = store.render_manifest(recs, source="G:/Intake")
    assert text.index("## 2026-11-06") < text.index("## 2026-11-05")
    assert "a\\|b.md" in text and "same bytes" in text and "G:/Intake" in text


# -- CLI ---------------------------------------------------------------------------

def test_cli_run_status_manifest_rules(ws, capsys):
    root, drop = ws
    (drop / "e.md").write_text("note\n", encoding="utf-8")
    with pytest.raises(SystemExit) as ex:
        cli.main(["run", "--settle", "0", "--min-age", "0", "--no-notify"])
    assert ex.value.code == 0
    out = capsys.readouterr().out
    assert "imported=1" in out and "e.md ->" in out
    with pytest.raises(SystemExit) as ex:
        cli.main(["status"])
    assert ex.value.code == 0 and "history: imported=1" in capsys.readouterr().out
    with pytest.raises(SystemExit) as ex:
        cli.main(["manifest"])
    assert ex.value.code == 0 and "e.md" in capsys.readouterr().out
    with pytest.raises(SystemExit) as ex:
        cli.main(["rules"])
    assert ex.value.code == 0 and "frontmatter-project" in capsys.readouterr().out


def test_cli_run_json_and_dry_run(ws, capsys):
    root, drop = ws
    (drop / "f.md").write_text("note\n", encoding="utf-8")
    with pytest.raises(SystemExit) as ex:
        cli.main(["run", "--dry-run", "--json", "--settle", "0", "--min-age", "0"])
    assert ex.value.code == 0
    data = json.loads(capsys.readouterr().out)
    assert data["dry_run"] is True and data["counts"] == {"dry-run": 1}
    assert (drop / "f.md").exists()


def test_cli_without_source_exits_2(monkeypatch, capsys):
    monkeypatch.delenv("ATLAS_INTAKE_DIR", raising=False)
    with pytest.raises(SystemExit) as ex:
        cli.main(["run"])
    assert ex.value.code == 2 and "ATLAS_INTAKE_DIR" in capsys.readouterr().err
