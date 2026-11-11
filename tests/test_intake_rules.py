"""Intake: frontmatter parsing, rule matching, destination resolution, scan filters."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from atlas_plus.intake import frontmatter, rules, scan


@pytest.fixture
def ws(tmp_path, monkeypatch):
    root = tmp_path / "ws"
    (root / ".memory").mkdir(parents=True)
    monkeypatch.setenv("ATLAS_WORKSPACE_ROOT", str(root))
    monkeypatch.setenv("ATLAS_STATE_DIR", str(root / ".atlas"))
    monkeypatch.setenv("ATLAS_MEMORY_ROOT", str(root / ".memory"))
    monkeypatch.setenv("ATLAS_PROJECTS_ROOT", str(root / "projects"))
    monkeypatch.delenv("ATLAS_INTAKE_LANDING", raising=False)
    monkeypatch.delenv("ATLAS_INTAKE_RULES", raising=False)
    return root


# -- frontmatter -----------------------------------------------------------------

def test_parse_frontmatter_mapping_and_body():
    fm, body = frontmatter.parse_frontmatter("---\nproject: MyApp\nrelated: [A, B]\n---\n# Title\n")
    assert fm == {"project": "MyApp", "related": ["A", "B"]}
    assert body == "# Title\n"


@pytest.mark.parametrize("text", ["# no frontmatter", "---\nunterminated: yes\n", "---\n- a list\n- not a map\n---\nx", "---\n: : bad\n---\n"])
def test_parse_frontmatter_degrades_to_empty(text):
    fm, body = frontmatter.parse_frontmatter(text)
    assert fm == {}
    assert body == text


def test_read_frontmatter_only_text_like_and_only_head(tmp_path):
    md = tmp_path / "a.md"
    md.write_text("---\nproject: X\n---\n" + "z" * 200_000, encoding="utf-8")
    assert frontmatter.read_frontmatter(md) == {"project": "X"}
    png = tmp_path / "a.png"
    png.write_bytes(b"---\nproject: X\n---\n")
    assert frontmatter.read_frontmatter(png) == {}


def test_read_frontmatter_strips_bom(tmp_path):
    md = tmp_path / "b.md"
    md.write_bytes("\ufeff---\nproject: Y\n---\n".encode("utf-8"))
    assert frontmatter.read_frontmatter(md) == {"project": "Y"}


# -- slug + matching ---------------------------------------------------------------

@pytest.mark.parametrize("raw,slug", [("MyApp", "myapp"), ("The Example Project", "the-example-project"), ("  ", "unsorted"), (None, "unsorted"), ("a/b\\c", "a-b-c")])
def test_project_slug(raw, slug):
    assert rules.project_slug(raw) == slug


def test_default_rule_order_frontmatter_first(ws):
    d = rules.resolve("brief.md", {"project": "MyApp"}, list(rules.DEFAULT_RULES), today="2026-11-06")
    assert d.rule.name == "frontmatter-project"
    assert d.dest == ws / "projects" / "myapp" / "intake" / "brief.md"
    assert d.version_root == ws / "projects" / "myapp"
    assert "ticket" in d.rule.on_import


def test_destination_rule_wins_over_project(ws):
    d = rules.resolve("x.md", {"project": "P", "destination": "projects/research/inbox"}, list(rules.DEFAULT_RULES))
    assert d.rule.name == "frontmatter-destination"
    assert d.dest == ws / "projects" / "research" / "inbox" / "x.md"   # directory-shaped -> name appended


def test_destination_rule_file_shaped(ws):
    d = rules.resolve("x.md", {"destination": "intake/research/renamed.md"}, list(rules.DEFAULT_RULES))
    assert d.dest == ws / "intake" / "research" / "renamed.md"


@pytest.mark.parametrize("bad", ["../escape.md", "/abs/path.md", "C:/win/path.md", "a/../b.md", "a//b.md", "..\\up\\x.md"])
def test_unsafe_destination_is_refused(ws, bad):
    d = rules.resolve("x.md", {"destination": bad}, list(rules.DEFAULT_RULES))
    assert d.dest is None
    assert "unsafe" in d.reason or "escapes" in d.reason


@pytest.mark.parametrize("name,rule,sub", [
    ("notes.md", "markdown", "notes"), ("paper.PDF", "documents", "docs"), ("rows.csv", "data", "data"),
    ("shot.jpeg", "images", "media/images"), ("clip.mp4", "audio-video", "media/av"), ("blob.xyz", "unsorted", "unsorted"),
])
def test_extension_rules_land_under_landing_by_date(ws, name, rule, sub):
    d = rules.resolve(name, {}, list(rules.DEFAULT_RULES), today="2026-11-06")
    assert d.rule.name == rule
    assert d.dest == ws / "intake" / Path(sub) / "2026-11-06" / name


def test_landing_root_env_override(ws, monkeypatch):
    monkeypatch.setenv("ATLAS_INTAKE_LANDING", str(ws / "elsewhere"))
    d = rules.resolve("n.md", {}, list(rules.DEFAULT_RULES), today="2026-11-06")
    assert d.dest == ws / "elsewhere" / "notes" / "2026-11-06" / "n.md"


def test_user_rules_prepend_and_can_replace(ws, tmp_path):
    rf = ws / ".atlas" / "intake" / "rules.json"
    rf.parent.mkdir(parents=True)
    rf.write_text(json.dumps({"rules": [{"name": "receipts", "match": {"glob": "receipt*"}, "dest": "{landing}/finance/{name}", "on_import": []}]}), encoding="utf-8")
    loaded = rules.load_rules()
    assert loaded[0].name == "receipts" and loaded[-1].name == "unsorted"
    d = rules.resolve("Receipt-001.pdf", {}, loaded)
    assert d.rule.name == "receipts" and d.dest == ws / "intake" / "finance" / "Receipt-001.pdf"
    rf.write_text(json.dumps({"replace_defaults": True, "rules": [{"name": "only", "dest": "{landing}/all/{name}"}]}), encoding="utf-8")
    assert [r.name for r in rules.load_rules()] == ["only"]


def test_malformed_rules_file_falls_back_to_defaults(ws):
    rf = rules.rules_path()
    rf.parent.mkdir(parents=True)
    rf.write_text("{not json", encoding="utf-8")
    assert [r.name for r in rules.load_rules()] == [r.name for r in rules.DEFAULT_RULES]


def test_frontmatter_equals_match():
    r = rules.Rule(name="sx", dest="x", match={"frontmatter_equals": {"intended_consumer": "sextant"}})
    assert r.matches("a.md", {"intended_consumer": "Sextant"})
    assert not r.matches("a.md", {"intended_consumer": "Atlas"})
    assert not r.matches("a.md", {})


# -- scan ------------------------------------------------------------------------

@pytest.mark.parametrize("name,reason", [
    ("Doc.gdoc", "native-gdoc-shortcut"), ("x.gsheet", "native-gdoc-shortcut"), ("~$word.docx", "temp-or-hidden"),
    ("file.crdownload", "partial-download"), ("desktop.ini", "ignored-name"), ("MANIFEST.md", "ignored-name"),
    (".hidden", "temp-or-hidden"), ("ok.md", None),
])
def test_skip_reason(name, reason):
    assert scan.skip_reason(Path(name)) == reason


def test_discover_skips_archive_and_private_dirs_and_empty_files(tmp_path):
    (tmp_path / "_imported" / "2026-11-06").mkdir(parents=True)
    (tmp_path / "_imported" / "2026-11-06" / "old.md").write_text("x")
    (tmp_path / ".trash").mkdir()
    (tmp_path / ".trash" / "t.md").write_text("x")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "keep.md").write_text("x")
    (tmp_path / "empty.md").write_text("")
    (tmp_path / "shortcut.gdoc").write_text("{}")
    found, skipped = scan.discover(tmp_path)
    assert [c.rel for c in found] == ["sub/keep.md"]
    assert ("empty.md", "empty") in skipped and ("shortcut.gdoc", "native-gdoc-shortcut") in skipped


def test_is_stable_rejects_recent_and_changing(tmp_path):
    f = tmp_path / "f.bin"
    f.write_bytes(b"abc")
    ok, why = scan.is_stable(f, settle_seconds=0, min_age_seconds=3600)
    assert (ok, why) == (False, "too-recent")
    calls = []

    def grow(_):
        f.write_bytes(b"abcdef")
        calls.append(1)

    ok, why = scan.is_stable(f, settle_seconds=1, min_age_seconds=0, sleeper=grow)
    assert (ok, why) == (False, "still-changing") and calls
    ok, why = scan.is_stable(f, settle_seconds=1, min_age_seconds=0, sleeper=lambda s: None)
    assert (ok, why) == (True, "")


def test_sha256_file_matches_hashlib(tmp_path):
    import hashlib
    f = tmp_path / "f.bin"
    f.write_bytes(b"0123456789" * 5000)
    assert scan.sha256_file(f, chunk=7) == hashlib.sha256(f.read_bytes()).hexdigest()
