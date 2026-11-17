"""Intake sorting additions (2026-11-15): sidecar frontmatter, zip classification,
brand-import extraction, the Downloads sweep policy (skip executables, date from mtime),
and multi-source CLI runs."""
from __future__ import annotations

import io
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

from atlas_plus.intake import cli, engine, frontmatter, rules, scan


@pytest.fixture
def ws(tmp_path, monkeypatch):
    root = tmp_path / "ws"
    (root / ".memory").mkdir(parents=True)
    (root / "projects").mkdir()
    monkeypatch.setenv("ATLAS_WORKSPACE_ROOT", str(root))
    monkeypatch.setenv("ATLAS_STATE_DIR", str(root / ".atlas"))
    monkeypatch.setenv("ATLAS_MEMORY_ROOT", str(root / ".memory"))
    monkeypatch.setenv("ATLAS_PROJECTS_ROOT", str(root / "projects"))
    monkeypatch.delenv("ATLAS_INTAKE_LANDING", raising=False)
    monkeypatch.delenv("ATLAS_INTAKE_RULES", raising=False)
    monkeypatch.delenv("ATLAS_INTAKE_DIR", raising=False)
    return root


def _zip(path: Path, names: dict[str, bytes]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as z:
        for n, data in names.items():
            z.writestr(n, data)
    return path


def _run(src: Path, **kw) -> engine.store.RunReport:
    return engine.run_once(src, settle_seconds=0, min_age_seconds=0, notify=False, sleeper=lambda s: None, **kw)


# -- sidecar frontmatter --------------------------------------------------------------

def test_sidecar_frontmatter_applies_to_a_zip_and_is_not_a_candidate(tmp_path):
    z = _zip(tmp_path / "drop" / "kit.zip", {"kit/README.md": b"x"})
    (tmp_path / "drop" / "kit.zip.meta.md").write_text("---\nproject: Northstar\n---\n", encoding="utf-8")
    assert frontmatter.read_frontmatter(z) == {"project": "Northstar"}
    cands, skipped = scan.discover(tmp_path / "drop")
    assert [c.rel for c in cands] == ["kit.zip"]
    assert ("kit.zip.meta.md", "sidecar") in skipped


def test_orphan_sidecar_is_an_ordinary_note(tmp_path):
    (tmp_path / "drop").mkdir()
    (tmp_path / "drop" / "gone.zip.meta.md").write_text("---\nproject: X\n---\n", encoding="utf-8")
    cands, skipped = scan.discover(tmp_path / "drop")
    assert [c.rel for c in cands] == ["gone.zip.meta.md"]


def test_sidecar_keys_win_over_own_frontmatter(tmp_path):
    note = tmp_path / "n.md"
    note.write_text("---\nproject: Own\n---\nbody", encoding="utf-8")
    (tmp_path / "n.md.meta.md").write_text("---\nproject: Side\n---\n", encoding="utf-8")
    assert frontmatter.read_frontmatter(note)["project"] == "Side"


# -- zip classification ---------------------------------------------------------------

def test_brand_kit_zip_routes_to_archive_and_brand_import(ws, tmp_path):
    z = _zip(tmp_path / "kit.zip", {"kit/logo/mark.svg": b"<svg/>", "kit/README.md": b"r"})
    d = rules.resolve("kit.zip", {}, path=z, today="2026-11-15")
    assert d.rule.name == "brand-kit"
    assert d.dest == ws / "archive" / "brand-kit-zips-2026-11-15" / "kit.zip"
    assert "brand-import" in d.rule.on_import


def test_claude_design_export_zip_is_design_export(ws, tmp_path):
    z = _zip(tmp_path / "Hark design.zip", {"Hark/Directions.dc.html": b"<html>", "Hark/support.js": b"x"})
    d = rules.resolve("Hark design.zip", {}, path=z)
    assert d.rule.name == "design-export"


def test_plain_zip_stays_data(ws, tmp_path):
    z = _zip(tmp_path / "dump.zip", {"a/b.csv": b"1,2"})
    assert rules.resolve("dump.zip", {}, path=z).rule.name == "data"


def test_zip_rule_never_matches_without_a_path(ws):
    assert rules.resolve("kit.zip", {}).rule.name == "data"


def test_zip_names_on_a_non_zip_is_empty(tmp_path):
    p = tmp_path / "x.zip"
    p.write_bytes(b"not a zip")
    assert scan.zip_names(p) == []


# -- brand-import action ----------------------------------------------------------------

def test_brand_import_extracts_validates_and_documents(ws, tmp_path, monkeypatch):
    drop = tmp_path / "drop"
    _zip(drop / "Northstar (2).zip", {"kit/logo/mark.svg": b"<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 1 1'/>",
                                   "kit/README.md": b"kit"})
    # a stand-in validator so the test does not depend on C:\Projects\brand\tools
    tools = ws / "brand" / "tools"
    tools.mkdir(parents=True)
    (tools / "validate_brand_svgs.py").write_text("import sys; print('ok', sys.argv[1]); sys.exit(0)\n", encoding="utf-8")
    monkeypatch.setitem(engine.ACTIONS, "ticket", lambda rec: "ticket-stub")
    report = _run(drop)
    rec = report.records[0]
    assert rec.status == "imported", rec.reason
    assert rec.rule == "brand-kit"
    assert rec.actions["brand-import"].startswith("extracted 2 files")
    day = datetime.now(timezone.utc).date().isoformat()
    target = ws / "brand" / "_incoming" / day / "northstar"          # "(2)" stripped, slugified
    assert (target / "kit" / "logo" / "mark.svg").is_file()
    assert (target / "PROVENANCE.md").is_file()
    assert "exit=0" in (target / "VALIDATION.txt").read_text(encoding="utf-8")
    assert Path(rec.dest).parent.name == f"brand-kit-zips-{day}"
    assert rec.actions["ticket"] == "ticket-stub"


def test_brand_import_slug_from_sidecar_project(ws, tmp_path, monkeypatch):
    drop = tmp_path / "drop"
    _zip(drop / "assets.zip", {"brand/favicon/favicon.svg": b"<svg/>"})
    (drop / "assets.zip.meta.md").write_text("---\nproject: Sextant\n---\n", encoding="utf-8")
    monkeypatch.setitem(engine.ACTIONS, "ticket", lambda rec: "t")
    report = _run(drop)
    rec = report.records[0]
    assert rec.status == "imported", rec.reason
    assert "\\sextant" in rec.actions["brand-import"] or "/sextant" in rec.actions["brand-import"]
    # sidecar archived beside the original
    archived = Path(rec.archived_to)
    assert archived.with_name(archived.name + ".meta.md").is_file()
    assert not (drop / "assets.zip.meta.md").exists()


@pytest.mark.parametrize("bad", ["../escape.svg", "kit/run.exe", "kit/hook.ps1"])
def test_brand_import_refuses_unsafe_members(ws, tmp_path, monkeypatch, bad):
    drop = tmp_path / "drop"
    _zip(drop / "kit.zip", {"kit/logo/mark.svg": b"<svg/>", bad: b"x"})
    monkeypatch.setitem(engine.ACTIONS, "ticket", lambda rec: "t")
    report = _run(drop)
    rec = report.records[0]
    assert rec.status == "imported"                      # the zip is still archived and ledgered
    assert rec.actions["brand-import"].startswith("refused:")
    incoming = ws / "brand" / "_incoming"
    assert not list(incoming.rglob("*.svg"))             # nothing extracted
    assert list(incoming.rglob("REFUSED.md"))


# -- Downloads sweep policy ---------------------------------------------------------------

def test_skip_executables_leaves_installers_in_place(ws, tmp_path):
    drop = tmp_path / "dl"
    drop.mkdir()
    (drop / "setup.exe").write_bytes(b"MZ")
    (drop / "note.md").write_text("hello", encoding="utf-8")
    report = _run(drop, skip_executables=True)
    assert ("setup.exe", "executable-left-in-place") in report.skipped
    assert (drop / "setup.exe").is_file()
    assert [r.source_rel for r in report.records] == ["note.md"]
    assert report.records[0].status == "imported"


def test_date_from_mtime_sorts_a_backlog_by_download_day(ws, tmp_path):
    import os
    drop = tmp_path / "dl"
    drop.mkdir()
    old = drop / "old.pdf"
    old.write_bytes(b"%PDF")
    ts = datetime(2026, 5, 19, 12, 0, tzinfo=timezone.utc).timestamp()
    os.utime(old, (ts, ts))
    report = _run(drop, date_from_mtime=True)
    assert report.records[0].status == "imported"
    assert "2026-05-19" in report.records[0].dest


# -- CLI: several sources in one run -------------------------------------------------------

def test_cli_run_accepts_repeated_sources(ws, tmp_path, capsys):
    a = tmp_path / "a"; b = tmp_path / "b"
    a.mkdir(); b.mkdir()
    (a / "one.md").write_text("1", encoding="utf-8")
    (b / "two.md").write_text("2", encoding="utf-8")
    rc = cli.cmd_run(type("A", (), dict(source=[str(a), str(b)], dry_run=True, leave=False, settle=0, min_age=0,
                                        no_notify=True, skip_executables=False, date_from_mtime=False, json=False))())
    out = capsys.readouterr().out
    assert rc == 0
    assert "one.md" in out and "two.md" in out


def test_top_level_sweep_never_descends(ws, tmp_path):
    drop = tmp_path / "dl"
    (drop / "ELK" / "deep").mkdir(parents=True)
    (drop / "ELK" / "deep" / "x.md").write_text("x", encoding="utf-8")
    (drop / "loose.md").write_text("y", encoding="utf-8")
    report = _run(drop, recursive=False)
    assert [r.source_rel for r in report.records] == ["loose.md"]
    assert (drop / "ELK" / "deep" / "x.md").is_file()


def test_brand_import_allows_web_scripts_but_not_os_executables(ws, tmp_path, monkeypatch):
    drop = tmp_path / "drop"
    _zip(drop / "site.zip", {"site/index.dc.html": b"<html>", "site/config.js": b"x", "build.mjs": b"y"})
    monkeypatch.setitem(engine.ACTIONS, "ticket", lambda rec: "t")
    report = _run(drop)
    rec = report.records[0]
    assert rec.rule == "design-export"
    assert rec.actions["brand-import"].startswith("extracted 3 files")


def test_brand_import_zip_rerun_from_archive(ws, tmp_path):
    z = _zip(tmp_path / "archived" / "Acme kit (2).zip", {"kit/logo/mark.svg": b"<svg/>"})
    out = engine.brand_import_zip(z, project="Acme Co")
    assert out.startswith("extracted 1 files")
    assert (ws / "brand" / "_incoming").glob("*/acme-co/kit/logo/mark.svg")
