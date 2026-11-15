"""AG90 (sweep 23): doctor's untagged-by-scope agrees with its total, flags show no `<bool>`,
the dashboard banner never precedes a refusal, and the repeated-facts scan is no longer quadratic
(rarest-first prefix filter, same pair set as the full walk)."""
from __future__ import annotations

import os
import random
import subprocess
import sys
from pathlib import Path

import pytest


# --- doctor: untagged count vs top-scopes ---------------------------------------------------

def _write(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def test_fresh_init_untagged_total_matches_scopes(tmp_path):
    ws = tmp_path / "ws"
    env = {k: v for k, v in os.environ.items() if not k.startswith("ATLAS_")}
    env["ATLAS_WORKSPACE_ROOT"] = str(ws)
    code = "from atlas_plus.init_cmd import main; raise SystemExit(main())"
    r = subprocess.run([sys.executable, "-X", "utf8", "-c", code],
                       env=env, capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL)
    assert r.returncode == 0, r.stderr
    from atlas_plus.doctor.cli import check_memory_quality, check_untagged_by_scope, scan_memory_files
    scan = scan_memory_files(ws / ".memory")
    q = check_memory_quality(scan)
    scopes = check_untagged_by_scope(scan)
    assert q["untagged_files"] == 1  # identity/user.md; the _README.md scaffolds are not notes
    assert scopes == {"identity": 1}, scopes


def test_scope_breakdown_uses_the_quality_gate(tmp_path):
    from atlas_plus.doctor.cli import check_memory_quality, check_untagged_by_scope, scan_memory_files
    mem = tmp_path / ".memory"
    _write(mem / "feedback" / "_README.md", "Scaffold.\n")
    _write(mem / "feedback" / "a.md", "no frontmatter\n")
    _write(mem / "feedback" / "b.md", "---\ntag: style\n---\nsingular tag key counts as tagged\n")
    _write(mem / "projects" / "archive" / "old.md", "archived, not counted\n")
    _write(mem / "projects" / "p" / "versions" / "v.md", "a version copy, not counted\n")
    _write(mem / "reference" / "r.md", "---\ntags: [x]\n---\nbody\n")
    _write(mem / "reference" / "s.md", "---\ntitle: t\n---\nbody\n")
    scan = scan_memory_files(mem)
    q = check_memory_quality(scan)
    scopes = check_untagged_by_scope(scan)
    assert scopes == {"feedback": 1, "reference": 1}, scopes
    assert sum(scopes.values()) == q["untagged_files"] == 2


# --- help: no <bool> on value-less flags ------------------------------------------------------

def _help(app, args):
    from typer.testing import CliRunner
    import re as _re
    r = CliRunner().invoke(app, args, env={"COLUMNS": "100", "NO_COLOR": "1", "FORCE_COLOR": None, "TERM": "dumb"})
    return _re.sub(r"\x1b\[[0-9;]*m", "", r.output)  # CI forces colour: the escapes split `--version` apart


def test_help_shows_no_bool_on_flags():
    from atlas_plus.cli import app as umbrella
    from atlas_plus.curation.cli import app as review
    from atlas_plus.transcripts.cli import app as transcripts
    top = _help(umbrella, ["--help"])
    assert "--version" in top and "<bool>" not in top
    dup = _help(review, ["duplicates", "--help"])
    assert "--facts" in dup and "<bool>" not in dup
    from importlib.metadata import version as _v
    if tuple(int(x) for x in _v("typer").split(".")[:2]) >= (0, 29):  # older Typer prints no types at all
        assert "<float>" in dup and "<str>" in dup  # options that take a value keep their type
    assert "<bool>" not in _help(transcripts, ["--help"])


# --- dashboard: the banner prints only after every refusal has passed -------------------------

def _dash(args, extra_env):
    env = {k: v for k, v in os.environ.items() if not k.startswith("ATLAS_")}
    env.update({"ATLAS_WEB_PORT": "9359", "ATLAS_BRAIN_URL": "http://127.0.0.1:9352", **extra_env})
    return subprocess.run([sys.executable, "-X", "utf8", "-m", "atlas_plus", "dashboard", *args],
                          env=env, capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL)


@pytest.mark.parametrize("args,env,code", [
    ([], {}, 1),                                  # no ATLAS_WEB_API_KEY
    ([], {"ATLAS_WEB_API_KEY": "short"}, 1),      # a 5-character key
    (["--por", "9359"], {}, 2),                   # argparse refusal of an unknown flag
    (["--port", "9359"], {}, 2),
])
def test_dashboard_refusals_print_no_start_banner(args, env, code):
    r = _dash(args, env)
    assert r.returncode == code, (r.stdout, r.stderr)
    assert "Starting the dashboard" not in r.stdout + r.stderr


def test_dashboard_banner_prints_when_it_starts(monkeypatch):
    import uvicorn
    from typer.testing import CliRunner
    from atlas_plus import cli
    from atlas_plus.web import app as webapp
    ran = []
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: ran.append(k.get("port")))
    monkeypatch.setattr(webapp, "API_KEY", "k" * 24)
    monkeypatch.setattr(webapp, "_port_free", lambda h, p: True)
    monkeypatch.setattr(webapp, "_START_BANNER", "", raising=False)
    r = CliRunner().invoke(cli.app, ["dashboard"])
    assert r.exit_code == 0, r.output
    assert ran and "Starting the dashboard" in r.output


# --- repeated facts: exact prefix filter -------------------------------------------------------

def _corpus(root: Path, n: int, seed: int = 7) -> None:
    rnd = random.Random(seed)
    vocab = [f"w{chr(97 + i % 26)}{chr(97 + (i // 26) % 26)}{i:03d}" for i in range(600)]
    weights = [1.0 / (r + 1) for r in range(len(vocab))]
    common = ["server", "deploy", "config", "always", "never", "because", "project"]
    facts: list[str] = []
    for _ in range(n):
        if facts and rnd.random() < 0.15:
            base = rnd.choice(facts).split()
            for _k in range(rnd.randint(1, 3)):
                base[rnd.randrange(len(base))] = rnd.choice(vocab)
            facts.append(" ".join(base))
            continue
        words = rnd.choices(vocab, weights=weights, k=rnd.randint(3, 12)) + rnd.sample(common, 3)
        rnd.shuffle(words)
        facts.append(" ".join(words))
    for i in range(0, n, 5):
        _write(root / "projects" / f"n{i:05d}.md", "\n".join(f"- {f}" for f in facts[i:i + 5]) + "\n")


def _brute(root: Path, t: float) -> set:
    from atlas_plus.curation import duplicates as d
    entries = []
    for f in sorted(root.rglob("*.md")):
        rel = str(f.relative_to(root)).replace("\\", "/")
        for fact in d._note_facts(f.read_text(encoding="utf-8")):
            norm = d._norm_fact(fact)
            if d._weight(norm) < 20:
                continue
            toks = d._tokens(norm)
            if len(toks) >= 4:
                entries.append((rel, fact, toks, norm))
    out = set()
    for i in range(len(entries)):
        for j in range(i + 1, len(entries)):
            a, b = entries[i], entries[j]
            shared = len(a[2] & b[2])
            if shared < 3 or a[3] == b[3]:
                continue
            sim = shared / len(a[2] | b[2])
            if sim >= t:
                out.add((a[0], b[0], round(sim, 2), a[1][:160], b[1][:160]))
    return out


@pytest.mark.parametrize("t", [0.7, 0.5, 0.9, 0.0])
def test_fact_near_pairs_equal_full_walk(tmp_path, monkeypatch, t):
    from atlas_plus.curation.duplicates import find_fact_duplicates
    monkeypatch.setenv("ATLAS_DUPLICATES_BUDGET_SECONDS", "0")
    _corpus(tmp_path, 600)
    rows = find_fact_duplicates(memory_root=tmp_path, near_threshold=t, limit=10**9)
    got = {(r["file1"], r["file2"], r["similarity"], r["fact"], r["other"]) for r in rows if r["kind"] == "near"}
    want = _brute(tmp_path, t)
    assert want, "the corpus must plant near repeats"
    assert got == want


def test_fact_threshold_edges(tmp_path, monkeypatch):
    from atlas_plus.curation.duplicates import find_fact_duplicates
    monkeypatch.setenv("ATLAS_DUPLICATES_BUDGET_SECONDS", "0")
    # exactly 0.7: 7 shared of a 10-token union; a 4-token fact inside a 5-token fact (0.8);
    # 3 shared of 5 (0.6, below the threshold).
    _write(tmp_path / "a.md", "\n".join([
        "- alpha bravo charlie delta echoa foxtrot golfa hotel",
        "- alpha bravo charlie delta echoa foxtrot golfa india julia",
        "- kilo lima mike november",
        "- kilo lima mike november oscar",
        "- papa quebec romeo sierra",
        "- papa quebec romeo tango",
    ]) + "\n")
    rows = find_fact_duplicates(memory_root=tmp_path, limit=100)
    sims = sorted(r["similarity"] for r in rows if r["kind"] == "near")
    assert sims == [0.7, 0.8], rows


def test_fact_scan_is_not_quadratic(tmp_path, monkeypatch):
    """2,500 facts over a shared vocabulary took 50 s at 4eaa93a; under a 15 s budget the old walk
    stops early (a partial), the prefix filter finishes."""
    from atlas_plus.curation import duplicates as d
    monkeypatch.setenv("ATLAS_DUPLICATES_BUDGET_SECONDS", "15")
    rnd = random.Random(90)
    vocab = [f"v{chr(97 + i % 26)}{chr(97 + (i // 26) % 26)}{i:04d}" for i in range(3000)]
    weights = [1.0 / (r + 1) for r in range(len(vocab))]
    common = ["server", "deploy", "config", "always", "never", "because", "project", "the", "and", "uses"]
    for i in range(0, 2500, 5):
        lines = []
        for _ in range(5):
            words = rnd.choices(vocab, weights=weights, k=rnd.randint(8, 14)) + rnd.sample(common, 3)
            rnd.shuffle(words)
            lines.append("- " + " ".join(words))
        _write(tmp_path / "projects" / f"n{i:05d}.md", "\n".join(lines) + "\n")
    d.find_fact_duplicates(memory_root=tmp_path, limit=10**9)
    assert d._LAST_PARTIAL == [0, 0], d._LAST_PARTIAL
