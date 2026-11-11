"""Intake: frontmatter destinations are confined; drops never clobber each other.

Regression tests for the 2026-11-09 data-safety audit:
- row 3: a dropped note's ``destination:`` could name any workspace path,
  including autorun folders, dot-dirs, the corpus identity file and git hooks,
  and replace what was there;
- row 35: two drop files with the same basename on the same day landed on the
  same path, so the second replaced the first.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from atlas_plus.intake import engine, rules, store


@pytest.fixture
def ws(tmp_path, monkeypatch):
    # The workspace doubles as a home directory, the default when no
    # ATLAS_WORKSPACE_ROOT is configured.
    root = tmp_path / "home"
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
    return root, drop


def run(**kw):
    kw.setdefault("settle_seconds", 0)
    kw.setdefault("min_age_seconds", 0)
    kw.setdefault("notify", False)
    return engine.run_once(**kw)


def resolve(dest_value, name="x.md"):
    return rules.resolve(name, {"destination": dest_value}, list(rules.DEFAULT_RULES), today="2026-11-09")


HOSTILE = [
    "AppData/Roaming/Microsoft/Windows/Start Menu/Programs/Startup/run.cmd",   # outside roots + .cmd
    "projects/app/run.cmd",                                                    # allowed root, script suffix
    "projects/app/tool.PS1",                                                   # suffix check is case-insensitive
    "intake/notes/evil.py",
    "projects/app/x.lnk",
    ".memory/identity/user.md",                                                # dot-dir (the corpus)
    ".git/hooks/pre-commit.md",
    ".ssh/config.txt",
    "projects/app/.git/hooks/post-checkout.md",                                # dot component deep inside a root
    "projects/app/.github/workflows/x.md",
    "Documents/WindowsPowerShell/profile.md",                                  # outside the allowed roots
    "Documents/x.ps1",
    "research/inbox/x.md",                                                     # workspace, but not an allowed root
    "projects/app./x.md",                                                      # trailing dot (Win32 alias)
    "projects/app /x.md",                                                      # trailing space
    "projects/nul/x.md",                                                       # device name
    "projects/app/CON.md",
    "../escape.md", "a/../b.md", "/abs/x.md", "C:/win/x.md", "//server/share/x.md",
    "\\\\server\\share\\x.md", "c:x.md", "projects/app/x.md:stream",
]


@pytest.mark.parametrize("bad", HOSTILE)
def test_hostile_destinations_resolve_to_quarantine(ws, bad):
    d = resolve(bad)
    assert d.dest is None, (bad, d.dest)
    assert d.reason


def test_directory_shaped_destination_cannot_append_a_script_name(ws):
    # The appended file name is checked too: "projects/app" + "run.cmd".
    d = resolve("projects/app", name="run.cmd")
    assert d.dest is None and "executable" in d.reason


@pytest.mark.parametrize("good,expect,replace", [
    ("projects/app/inbox", ("projects", "app", "inbox", "x.md"), True),
    ("projects/app/brief.md", ("projects", "app", "brief.md"), True),
    ("intake/research/x.md", ("intake", "research", "x.md"), False),
])
def test_allowed_roots(ws, good, expect, replace):
    root, _ = ws
    d = resolve(good)
    assert d.dest == root.joinpath(*expect)
    assert d.replace is replace


def test_corpus_destination_allowed_when_corpus_is_not_a_dot_dir(ws, monkeypatch):
    root, _ = ws
    corpus = root / "memory"
    corpus.mkdir()
    monkeypatch.setenv("ATLAS_MEMORY_ROOT", str(corpus))
    d = resolve("memory/reference/new.md")
    assert d.dest == corpus / "reference" / "new.md"
    assert d.version_root == corpus           # version copies go to the corpus versions/
    assert d.replace is False
    for protected in ("memory/versions/x.md", "memory/Archive/x.md", "memory/tickets/app/APP-001.md",
                      "memory/_atlas/x.md", "memory/skills/x.md"):
        assert resolve(protected).dest is None, protected


def test_hostile_destination_leaves_existing_target_byte_identical(ws):
    root, drop = ws
    target = root / ".memory" / "identity" / "user.md"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"original identity\n")
    startup = root / "AppData" / "Startup"
    startup.mkdir(parents=True)
    (startup / "run.cmd").write_bytes(b"@echo original\n")
    (drop / "a.md").write_text("---\ndestination: .memory/identity/user.md\n---\nowned\n", encoding="utf-8")
    (drop / "b.txt").write_text("---\ndestination: AppData/Startup/run.cmd\n---\ncalc.exe\n", encoding="utf-8")
    report = run()
    assert report.counts() == {"quarantined": 2}
    assert target.read_bytes() == b"original identity\n"
    assert (startup / "run.cmd").read_bytes() == b"@echo original\n"
    assert not any((root / "versions").rglob("*"))


def test_existing_file_outside_projects_is_never_replaced(ws):
    root, drop = ws
    existing = root / "intake" / "research" / "x.md"
    existing.parent.mkdir(parents=True)
    existing.write_bytes(b"hand-written\n")
    (drop / "x.md").write_text("---\ndestination: intake/research/x.md\n---\nnew\n", encoding="utf-8")
    report = run()
    assert report.counts() == {"imported": 1}
    assert existing.read_bytes() == b"hand-written\n"
    landed = Path(report.records[0].dest)
    assert landed == existing.with_name("x_v1.md")
    assert landed.read_text(encoding="utf-8").endswith("new\n")


def test_corpus_destination_never_replaces(ws, monkeypatch):
    root, drop = ws
    corpus = root / "memory"
    (corpus / "reference").mkdir(parents=True)
    monkeypatch.setenv("ATLAS_MEMORY_ROOT", str(corpus))
    live = corpus / "reference" / "note.md"
    live.write_bytes(b"live corpus file\n")
    (drop / "n.md").write_text("---\ndestination: memory/reference/note.md\n---\nincoming\n", encoding="utf-8")
    report = run()
    assert report.counts() == {"imported": 1}
    assert live.read_bytes() == b"live corpus file\n"
    assert Path(report.records[0].dest) == corpus / "reference" / "note_v1.md"
    assert not (root / "versions").exists()   # nothing versioned into the workspace mirror


def test_same_basename_same_day_does_not_collide(ws):
    root, drop = ws
    (drop / "a").mkdir()
    (drop / "b").mkdir()
    (drop / "a" / "notes.md").write_text("from a\n", encoding="utf-8")
    (drop / "b" / "notes.md").write_text("from b\n", encoding="utf-8")
    report = run()
    assert report.counts() == {"imported": 2}
    dests = {Path(r.dest) for r in report.records}
    assert len(dests) == 2
    assert {p.read_text(encoding="utf-8") for p in dests} == {"from a\n", "from b\n"}
    assert not any(r.versioned for r in report.records)


def test_project_destination_still_versions_and_replaces(ws):
    # Unchanged behaviour inside the projects root: version, then replace.
    root, drop = ws
    target = root / "projects" / "app" / "brief.md"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"v1\n")
    (drop / "brief.md").write_text("---\ndestination: projects/app/brief.md\n---\nv2\n", encoding="utf-8")
    report = run()
    rec = report.records[0]
    assert rec.status == "imported" and rec.versioned
    assert Path(rec.dest) == target and target.read_text(encoding="utf-8").endswith("v2\n")
    assert Path(rec.versioned).read_bytes() == b"v1\n"
