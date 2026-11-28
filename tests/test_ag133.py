"""AG133 (sweep 40): what a first-timer still sees.

`sextant commands` lists the sextant-* names with a purpose (module targets and the older names behind
--all, SEX-043); `sextant backup` is a subcommand (SEX-053 item 18); `sextant dashboard --help` names what
was typed; the import report prints readable characters where the stream can carry them; the review
convergence table escapes corpus text and its hint names this install's command; the README's install
blocks run `sextant verify` (SEX-054 item 3b).
"""
from __future__ import annotations

import importlib.util
import io
import json
import sys
from importlib import metadata
from pathlib import Path

import pytest
from typer.testing import CliRunner

ROOT = Path(__file__).resolve().parents[1]
BOLD = "[" + "bold]"
RLO = "‮"


def _installed(prefix: str) -> list[str]:
    return sorted(e.name for e in metadata.entry_points(group="console_scripts") if e.name.startswith(prefix))


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("ATLAS_MEMORY_ROOT", str(tmp_path / "mem"))
    monkeypatch.setenv("ATLAS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("ATLAS_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("COLUMNS", "220")
    monkeypatch.setattr(sys, "argv", ["sextant"])
    return tmp_path


# ---- 1. sextant commands -------------------------------------------------------------------------

def test_commands_default_view_is_names_and_purposes_without_atlas(env):
    from atlas_plus import cli
    if not _installed("sextant-"):
        pytest.skip("no sextant-* scripts in this install (the private tree injects them at build time)")
    res = CliRunner().invoke(cli.app, ["commands"], prog_name="sextant")
    assert res.exit_code == 0, res.output
    assert "atlas" not in res.output.lower()  # 110 on the old table: targets and older names
    lines = [ln for ln in res.output.splitlines() if ln.startswith("sextant")]
    assert {ln.split()[0] for ln in lines} == set(_installed("sextant"))
    for ln in lines:  # one line each, with what it does
        assert len(ln.split(None, 1)) == 2, ln
    assert "commands --all" in res.output


def test_every_installed_command_has_a_purpose():
    from atlas_plus import cli
    # the public names only: the private tree has commands this clone never saw (and no sextant-* scripts)
    names = _installed("sextant")
    if not names:
        pytest.skip("no sextant-* scripts in this install")
    missing = [n for n in names if not cli._purpose(n)]
    assert not missing, missing
    assert not [n for n in names if "atlas" in cli._purpose(n).lower()]


def test_commands_all_keeps_the_targets_and_older_names_and_json_keeps_every_field(env):
    from atlas_plus import cli
    eps = {e.name: e.value for e in metadata.entry_points(group="console_scripts")
           if e.name.startswith(("atlas", "sextant"))}
    res = CliRunner().invoke(cli.app, ["commands", "--all"], prog_name="sextant")
    assert res.exit_code == 0, res.output
    assert "atlas_plus." in res.output or ":main" in res.output  # a module target (the umbrella's own line needs its script)
    if _installed("sextant-") and _installed("atlas-"):
        assert "older atlas-* names, still working" in res.output
    js = CliRunner().invoke(cli.app, ["commands", "--json"], prog_name="sextant")
    assert json.loads(js.stdout) == eps


# ---- 2. sextant backup ---------------------------------------------------------------------------

def test_backup_is_a_subcommand(env):
    from atlas_plus import cli
    res = CliRunner().invoke(cli.app, ["backup", "--help"], prog_name="sextant")
    assert res.exit_code == 0, res.output
    assert "sextant backup" in res.output and "verify" in res.output and "status" in res.output


# ---- 3. dashboard usage, import report characters ------------------------------------------------

@pytest.mark.parametrize("typed", ["dashboard", "web"])
def test_dashboard_help_names_what_was_typed(env, typed):
    from atlas_plus import cli
    res = CliRunner().invoke(cli.app, [typed, "--help"], prog_name="sextant")
    assert res.exit_code == 0, res.output
    assert f"usage: sextant {typed}" in res.output


def test_import_report_prints_readable_cjk(env, monkeypatch, capsys):
    vault = env / "vault"
    vault.mkdir()
    name = "会議メモ"
    (vault / f"{name}.md").write_text("# x\nbody\n", encoding="utf-8")
    from atlas_plus import import_cmd
    monkeypatch.setattr(sys, "argv", ["sextant-import", str(vault), "--source", "obsidian"])
    try:
        import_cmd.main()
    except SystemExit as e:
        assert not e.code
    out = capsys.readouterr().out
    assert name in out and "\\u4f1a" not in out


def test_import_report_keeps_escapes_where_the_stream_cannot_carry_them(monkeypatch):
    from atlas_plus import import_cmd
    report = {"sample_destinations": ["reference/obsidian/会.md"]}
    monkeypatch.setattr(sys, "stdout", io.TextIOWrapper(io.BytesIO(), encoding="cp437"))
    assert "\\u4f1a" in import_cmd._report_json(report)  # a cp437 pipe would print `?`
    monkeypatch.setattr(sys, "stdout", io.TextIOWrapper(io.BytesIO(), encoding="utf-8"))
    assert "会" in import_cmd._report_json(report)


# ---- 5. review convergence -----------------------------------------------------------------------

def test_convergence_table_escapes_corpus_text_and_names_this_install(env, monkeypatch):
    root = env / "mem"
    (root / "reference").mkdir(parents=True)
    for i, typ in enumerate((f"{BOLD}x", f"a{RLO}b")):
        (root / "reference" / f"n{i}.md").write_text(
            f"---\ntitle: n{i}\ntype: \"{typ}\"\ncontradictions:\n  - other{i}.md\n---\nbody\n", encoding="utf-8")
    from atlas_plus.curation import cli as ccli
    monkeypatch.setattr(ccli, "MEMORY_ROOT", root)
    res = CliRunner().invoke(ccli.app, ["convergence"])
    assert res.exit_code == 0, res.output
    assert f"{BOLD}x" in res.output          # markup printed as written
    assert RLO not in res.output             # the override is escaped, not emitted
    assert "sextant review contradictions" in res.output
    assert "atlas-review-memory" not in res.output


# ---- 4. README: the install blocks run verify ----------------------------------------------------

def _walk():
    spec = importlib.util.spec_from_file_location("ag133_readme_walk", ROOT / "scripts" / "readme_walk.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["ag133_readme_walk"] = mod
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.modules.pop("ag133_readme_walk", None)
    return mod


@pytest.mark.parametrize("platform", ["windows", "posix"])
def test_readme_walk_runs_verify_on_both_systems(platform):
    walk = _walk()
    readme = walk.find_readme(ROOT)  # docs/README-PUBLIC.md first, then README.md
    steps = walk.extract(readme.read_text(encoding="utf-8"), platform)
    run = [s.command for s in steps if not s.reason]
    verify = [c for c in run if c.split()[-1:] == ["verify"]]
    assert verify, run
    init = [i for i, c in enumerate(run) if "sextant-init" in c]
    assert init and run.index(verify[0]) > init[0]  # after the corpus exists
