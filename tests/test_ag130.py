"""AG130 (SEX-043 group b): every atlas-* console script keeps working, every public script has a sextant-* name
when the tree carries the public names, and the usage lines, hints and help a sextant user reads name the
sextant form. Functional: the entry points run in-process with the argv a user would have."""
from __future__ import annotations

import os
import re
import sys
import tomllib
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]

# The atlas-* console scripts at public 0.1.1 (H 0cb5d7a) and their targets. Removing one, or pointing it
# elsewhere, breaks a command an existing install has typed or scheduled.
H_ATLAS_SCRIPTS = {
    "atlas-plus": "atlas_plus.cli:main", "atlas-init": "atlas_plus.init_cmd:main",
    "atlas-transcripts": "atlas_plus.transcripts.cli:app",
    "atlas-memory-mcp": "atlas_plus.mcp.memory_server.server:main",
    "atlas-fts-rebuild": "atlas_plus.mcp.memory_server.fts:main", "atlas-backup": "atlas_plus.backup:app",
    "atlas-summarize-session": "atlas_plus.summarizer.cli:app", "atlas-review-memory": "atlas_plus.curation.cli:app",
    "atlas-tickets": "atlas_plus.tickets.cli:main", "atlas-telegram-bot": "atlas_plus.telegram.bot:main",
    "atlas-job-poller": "atlas_plus.telegram.job_poller:main", "atlas-ntfy-listener": "atlas_plus.ntfy.inbound:main",
    "atlas-web-ui": "atlas_plus.web.app:main", "atlas-session-start": "atlas_plus.hooks.session_start:main",
    "atlas-pre-compact": "atlas_plus.hooks.pre_compact:main", "atlas-prime": "atlas_plus.hooks.prime:main",
    "atlas-suggest-facts": "atlas_plus.curation.suggest_facts:main",
    "atlas-temporal": "atlas_plus.temporal_graph.cli:main",
    "atlas-contradiction-check": "atlas_plus.curation.nightly_contradiction_check:main",
    "atlas-reflexion-loop": "atlas_plus.curation.reflexion_loop:main",
    "atlas-weekly-synthesis": "atlas_plus.curation.weekly_synthesis:main",
    "atlas-dream": "atlas_plus.curation.dream_mode:main", "atlas-brain": "atlas_plus.brain.server:main",
    "atlas-brain-chat": "atlas_plus.brain.cli:main", "atlas-brain-worker": "atlas_plus.brain.worker:main",
    "atlas-doctor": "atlas_plus.doctor.cli:main", "atlas-db-backup": "atlas_plus.db_backup.cli:main",
    "atlas-rederive": "atlas_plus.curation.cli:app", "atlas-narrative": "atlas_plus.session_narrative:main",
    "atlas-sprint-summary": "atlas_plus.sprint_summary:main", "atlas-sync": "atlas_plus.sync.cli:main",
    "atlas-uninstall": "atlas_plus.uninstall_cmd:main", "atlas-reset": "atlas_plus.reset_cmd:main",
    "atlas-embed-index": "atlas_plus.mcp.memory_server.embeddings:main", "atlas-import": "atlas_plus.import_cmd:main",
    "atlas-intake": "atlas_plus.intake.cli:main",
}
# atlas-* names whose public twin has another suffix
_RENAMED = {"atlas-plus": "sextant", "atlas-memory-mcp": "sextant-mcp", "atlas-web-ui": "sextant-web"}


def _scripts() -> dict[str, str]:
    return dict(tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))["project"]["scripts"])


def test_every_atlas_script_of_0_1_1_still_exists_with_the_same_target():
    scripts = _scripts()
    missing = {n for n in H_ATLAS_SCRIPTS if n not in scripts}
    moved = {n: scripts[n] for n in H_ATLAS_SCRIPTS if n in scripts and scripts[n] != H_ATLAS_SCRIPTS[n]}
    assert not missing and not moved, (missing, moved)


def test_every_public_script_has_a_sextant_name_when_the_tree_carries_public_names():
    scripts = _scripts()
    if not any(n.startswith("sextant") for n in scripts):
        pytest.skip("private tree: the sextant-* names are added at build time from the pair table")
    if not any(n.startswith("sextant-temporal") for n in scripts):
        pytest.skip("public names for the AG130 pairs not added to [project.scripts] yet (pair table: AG130-pairs.txt)")
    lacking = []
    for name, target in H_ATLAS_SCRIPTS.items():
        twin = _RENAMED.get(name, "sextant-" + name.removeprefix("atlas-"))
        if scripts.get(twin) != target:
            lacking.append(f"{name} -> {twin}")
    assert not lacking, lacking


# ---- usage lines name what the user typed --------------------------------------------------------

def _help_out(monkeypatch, capsys, argv, entry) -> str:
    monkeypatch.setattr(sys, "argv", argv)
    try:
        rc = entry()
    except SystemExit as e:
        rc = e.code
    assert rc in (0, None), rc
    cap = capsys.readouterr()
    return cap.out + cap.err


@pytest.fixture
def sandbox_env(tmp_path, monkeypatch):
    for k in list(os.environ):
        if k.startswith("ATLAS_"):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("ATLAS_WORKSPACE_ROOT", str(tmp_path / "ws"))
    monkeypatch.setenv("ATLAS_MEMORY_ROOT", str(tmp_path / "ws" / ".memory"))
    monkeypatch.setenv("ATLAS_STATE_DIR", str(tmp_path / "ws" / ".atlas"))
    for v in ("ATLAS_BRAIN_URL", "ATLAS_NTFY_URL", "ATLAS_OLLAMA_URL", "ATLAS_MCP_URL"):
        monkeypatch.setenv(v, "http://127.0.0.1:9")
    return tmp_path


def _entries():
    from atlas_plus.hooks import pre_compact, prime, session_start
    from atlas_plus import session_narrative
    from atlas_plus.curation import dream_mode
    from atlas_plus.telegram import bot, job_poller
    from atlas_plus.ntfy import inbound
    return {"session-start": session_start.main, "pre-compact": pre_compact.main, "prime": prime.main,
            "narrative": session_narrative.main, "dream": dream_mode.main, "telegram-bot": bot.main,
            "job-poller": job_poller.main, "ntfy-listener": inbound.main}


@pytest.mark.parametrize("suffix", ["session-start", "pre-compact", "prime", "narrative", "dream",
                                    "telegram-bot", "job-poller", "ntfy-listener"])
def test_help_usage_names_the_sextant_name_it_was_started_as(suffix, sandbox_env, monkeypatch, capsys):
    out = _help_out(monkeypatch, capsys, [f"sextant-{suffix}", "--help"], _entries()[suffix])
    assert f"sextant-{suffix}" in out
    assert f"atlas-{suffix}" not in out, out


@pytest.mark.parametrize("suffix", ["session-start", "pre-compact", "prime", "narrative", "dream"])
def test_help_usage_under_the_atlas_name_is_unchanged(suffix, sandbox_env, monkeypatch, capsys):
    out = _help_out(monkeypatch, capsys, [f"atlas-{suffix}", "--help"], _entries()[suffix])
    assert f"atlas-{suffix}" in out


def test_sextant_context_help_names_sextant_context(sandbox_env, monkeypatch, capsys):
    from atlas_plus.hooks import prime
    out = _help_out(monkeypatch, capsys, ["sextant context", "--help"], prime.main)
    assert "Usage: sextant context <task description>" in out and "atlas-prime" not in out


# ---- sextant --help and sextant commands -----------------------------------------------------------

def test_umbrella_help_lists_no_atlas_only_twins(sandbox_env, monkeypatch):
    from typer.testing import CliRunner
    from atlas_plus import cli
    monkeypatch.setattr(sys, "argv", ["sextant"])
    res = CliRunner().invoke(cli.app, ["--help"], prog_name="sextant", env={"COLUMNS": "240"})
    assert res.exit_code == 0
    assert "same as atlas-review-memory" not in res.output and "same as atlas-prime" not in res.output
    assert "the atlas-* family" not in res.output


def test_commands_marks_atlas_names_as_older_names_that_keep_working(sandbox_env, monkeypatch):
    from typer.testing import CliRunner
    from atlas_plus import cli
    monkeypatch.setattr(sys, "argv", ["sextant"])
    res = CliRunner().invoke(cli.app, ["commands"], prog_name="sextant", env={"COLUMNS": "240"})
    assert res.exit_code == 0
    out = res.output
    assert "older atlas-* names, still working" in out and "also installed" not in out
    # The listing is read from the INSTALLED entry points. A source checkout run through PYTHONPATH can sit
    # beside an install that has no sextant-* script at all: then there is no first group to order (fold of
    # AG130: red on the clean publish worktree, green in the developer checkout and in an installed tree).
    from importlib import metadata
    installed = [e.name for e in metadata.entry_points(group="console_scripts") if e.name.startswith("sextant")]
    if installed:
        assert out.index("sextant-* commands") < out.index("older atlas-* names")
    else:
        assert "sextant-* commands" not in out


# ---- hints a sextant user reads ------------------------------------------------------------------

def test_review_and_script_hints_follow_the_install(monkeypatch, tmp_path):
    from atlas_plus import _next_step
    monkeypatch.setattr(sys, "argv", ["sextant"])
    assert _next_step.review("report") == "sextant review report"
    monkeypatch.setattr(sys, "argv", ["atlas-doctor"])
    assert _next_step.review("report") == "atlas-review-memory report"
    # The hint follows how the user typed the command, never which files sit beside the interpreter (fold
    # of AG130: a look at the Scripts folder made two older tests depend on the developer's venv). The
    # sextant-* names and the hints that name them ship in one build.
    ext = ".exe" if os.name == "nt" else ""
    (tmp_path / ("atlas-temporal" + ext)).write_bytes(b"")
    monkeypatch.setattr(sys, "executable", str(tmp_path / ("python" + ext)))
    monkeypatch.setattr(sys, "argv", ["sextant"])
    assert "sextant-temporal" in _next_step.script("temporal")
    assert "sextant-db-backup" in _next_step.script("db-backup")
    monkeypatch.setattr(sys, "argv", ["atlas-doctor"])
    assert "atlas-temporal" in _next_step.script("temporal")


def test_weekly_report_names_sextant_review(monkeypatch, tmp_path):
    from atlas_plus.curation.weekly_report import generate_report
    corpus = tmp_path / ".memory"
    (corpus / "reference").mkdir(parents=True)
    (corpus / "reference" / "n.md").write_text("---\nid: n\nupdated: 2024-01-01\n---\nsome fact text that is long "
                                              "enough to count\n", encoding="utf-8")
    monkeypatch.setenv("ATLAS_STATE_DIR", str(tmp_path / ".atlas"))
    monkeypatch.setattr(sys, "argv", ["sextant review"])
    report, _ = generate_report(memory_root=corpus, dry_run=True)
    assert "sextant review archive <path> --yes" in report and "atlas-review-memory" not in report


def test_reset_plan_names_sextant_init(sandbox_env, monkeypatch, capsys):
    from atlas_plus import reset_cmd
    (sandbox_env / "ws" / ".memory").mkdir(parents=True)
    monkeypatch.setattr(sys, "argv", ["sextant-reset"])
    try:
        reset_cmd.main()
    except SystemExit:
        pass
    out = capsys.readouterr()
    text = out.out + out.err
    line = next(ln for ln in text.splitlines() if "first-run skeleton" in ln)
    assert "atlas-init" not in line and re.search(r"sextant(\.exe)?['\"]? init\)", line), line
