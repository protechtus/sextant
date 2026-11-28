"""AG116 (sweep 34): leftovers of SEX-052.

- item 13: doctor parses each note's frontmatter once, shared by the checks, with the same results;
- item 6: `sextant context` outside a project names the project notes and where their facts appear;
- `sextant context` with an inherited stdin that never closes exits cleanly (it died at shutdown,
  "Fatal Python error: _enter_buffered_busy", exit 139).
"""
from __future__ import annotations

import os
import subprocess
import sys
from collections import Counter
from pathlib import Path

import pytest

from atlas_plus.doctor import cli as doctor_cli

DEAD = "http://127.0.0.1:9"


def _corpus(mem: Path) -> None:
    for d in ("projects", "decisions", "feedback"):
        (mem / d).mkdir(parents=True, exist_ok=True)
    (mem / "projects" / "a.md").write_text(
        "---\ntitle: A\ntags: [x]\nconfidence: high\nlast_confirmed: 2026-01-01\nupdated: 2026-01-02\n---\nbody\n",
        encoding="utf-8")
    (mem / "projects" / "b.md").write_text("---\ntitle: B\ncontradictions: [c.md]\nanti_flagged: true\n---\nbody\n",
                                           encoding="utf-8")
    (mem / "feedback" / "empty.md").write_text("", encoding="utf-8")
    (mem / "feedback" / "broken.md").write_text("---\ntitle: [unclosed\ntags: x\n---\nbody\n", encoding="utf-8")
    (mem / "feedback" / "latin.md").write_bytes(b"---\ntitle: caf\xe9\ntags: [y]\n---\nbody\n")
    (mem / "feedback" / "gone.md").write_text("---\ntitle: G\ntags: [z]\n---\nbody\n", encoding="utf-8")
    (mem / "decisions" / "d1.md").write_text("---\ntitle: D1\nmade: 2020-01-01\n---\nbody\n", encoding="utf-8")
    (mem / "decisions" / "d2.md").write_bytes(b"\xef\xbb\xbf---\ntitle: D2\nmade: 2020-01-01\n---\nbody\n")
    (mem / "decisions" / "d3.md").write_bytes(
        b"---\r\ntitle: D3\r\nmade: 2020-01-01\r\nlast_reviewed: 2099-11-01\r\n---\r\nbody\r\n")


@pytest.fixture
def memroot(tmp_path, monkeypatch):
    mem = tmp_path / ".memory"
    _corpus(mem)
    monkeypatch.setenv("ATLAS_MEMORY_ROOT", str(mem))
    monkeypatch.setenv("ATLAS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(doctor_cli, "MEMORY_ROOT", mem)
    monkeypatch.setattr(doctor_cli, "STATE_DIR", tmp_path / "state")
    return mem


def _scan_with_unreadable(mem: Path):
    """The shared scan, with feedback/gone.md as an unreadable note (text None), as a read error leaves it."""
    scan = doctor_cli.scan_memory_files(mem)
    out = []
    for mf in scan:
        if mf.rel == "feedback/gone.md":
            mf = doctor_cli._scan_entry(mem / "feedback" / "gone.md", Path("feedback", "gone.md"), OSError("denied"))
        out.append(mf)
    return out


def _four_checks(scan):
    return (doctor_cli.check_convergence(scan), doctor_cli.check_decisions(scan),
            doctor_cli.check_memory_quality(scan), doctor_cli.check_untagged_by_scope(scan))


def test_doctor_checks_keep_their_results_on_odd_notes(memroot):
    conv, dec, qual, untagged = _four_checks(_scan_with_unreadable(memroot))
    assert conv == {"flagged": 1, "paths": ["projects/b.md"]}
    # d2 (byte order mark) stays out as before; d1 is stale, d3 was reviewed
    assert (dec["total"], dec["stale"], dec["stale_titles"]) == (2, 1, ["D1"])
    assert qual["total_files"] == 9 and qual["tagged_pct"] == round(2 / 9 * 100, 1)
    assert qual["confirmed_pct"] == round(1 / 9 * 100, 1) and qual["anti_flagged"] == 1
    assert qual["untagged_files"] == 7 and qual["confidence_dist"]["high"] == 1
    assert untagged == {"decisions": 3, "feedback": 3, "projects": 1}


def test_doctor_parses_each_note_frontmatter_at_most_once(memroot, monkeypatch):
    calls: Counter = Counter()
    real = doctor_cli._yaml_load

    def counting(text):
        calls[text] += 1
        return real(text)

    monkeypatch.setattr(doctor_cli, "_yaml_load", counting)
    _four_checks(_scan_with_unreadable(memroot))
    assert calls, "the checks parsed nothing: the counter is not on the parse path"
    twice = {t[:30]: n for t, n in calls.items() if n > 1}
    assert not twice, f"frontmatter parsed more than once per note: {twice}"


def test_doctor_collect_opens_each_note_at_most_once(memroot, monkeypatch):
    import builtins
    import io

    monkeypatch.setattr(doctor_cli, "_http_get", lambda *a, **k: (200, {}))
    monkeypatch.setattr(doctor_cli, "_powershell_json", lambda *a, **k: None)
    for k in ("ATLAS_MCP_URL", "ATLAS_WEB_URL", "ATLAS_BRAIN_URL", "ATLAS_SYNC_URL", "ATLAS_BRAIN_OLLAMA_URL"):
        monkeypatch.setenv(k, DEAD)
    root = str(memroot.resolve()).lower()
    opened: list = []
    real_open = io.open

    def counting_open(file, *a, **k):
        if isinstance(file, (str, os.PathLike)):
            p = os.path.abspath(os.fspath(file)).lower()
            if p.startswith(root) and p.endswith(".md"):
                opened.append(p)
        return real_open(file, *a, **k)

    monkeypatch.setattr(io, "open", counting_open)
    monkeypatch.setattr(builtins, "open", counting_open)
    data = doctor_cli.collect()
    assert data["memory_quality"]["total_files"] == 9
    assert opened, "no note open was seen: the counter is not on the read path"
    again = {p: n for p, n in Counter(opened).items() if n > 1}
    assert not again, f"collect() opened a note more than once: {again}"


def _child_env(tmp_path: Path) -> dict:
    import atlas_plus

    env = {k: v for k, v in os.environ.items() if not k.startswith("ATLAS_")}
    src = str(Path(atlas_plus.__file__).resolve().parents[1])
    env.update({
        "PYTHONPATH": src + os.pathsep + env.get("PYTHONPATH", ""),
        "ATLAS_WORKSPACE_ROOT": str(tmp_path),
        "ATLAS_MEMORY_ROOT": str(tmp_path / ".memory"),
        "ATLAS_STATE_DIR": str(tmp_path / "state"),
        "HOME": str(tmp_path), "USERPROFILE": str(tmp_path),
        "ATLAS_MCP_URL": DEAD, "ATLAS_WEB_URL": DEAD, "ATLAS_BRAIN_URL": DEAD, "ATLAS_BRAIN_OLLAMA_URL": DEAD,
        "PYTHONIOENCODING": "utf-8",
    })
    return env


def _fact_in_project(tmp_path: Path) -> None:
    proj = tmp_path / ".memory" / "projects"
    proj.mkdir(parents=True)
    (proj / "foo.md").write_text("---\ntitle: Foo\n---\n- The foo build uses zebrafish runners\n", encoding="utf-8")


def test_context_outside_a_project_says_where_project_facts_appear(tmp_path):
    _fact_in_project(tmp_path)
    res = subprocess.run([sys.executable, "-m", "atlas_plus.hooks.session_start"], cwd=str(tmp_path),
                         env=_child_env(tmp_path), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, timeout=120)
    out = res.stdout.decode("utf-8", "replace")
    assert res.returncode == 0, res.stderr.decode("utf-8", "replace")[-2000:]
    assert "(not in a known project directory)" in out
    line = next((ln for ln in out.splitlines() if ln.startswith("Project notes:")), "")
    assert "foo" in line and "search <words>" in line and "context <words>" in line, out[-1500:]


def test_context_survives_a_stdin_that_never_closes(tmp_path):
    _fact_in_project(tmp_path)
    r, w = os.pipe()
    try:
        proc = subprocess.Popen([sys.executable, "-m", "atlas_plus.hooks.session_start"], cwd=str(tmp_path),
                                env=_child_env(tmp_path), stdin=r, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        out, err = proc.communicate(timeout=120)
    finally:
        os.close(r)
        os.close(w)   # the writer stays open until the child is gone: the pipe never closed for it
    err_text = err.decode("utf-8", "replace")
    assert "Fatal Python error" not in err_text, err_text[-1500:]
    assert proc.returncode == 0, (proc.returncode, err_text[-1500:])
    assert "=== END SEXTANT CONTEXT ===" in out.decode("utf-8", "replace")


def test_read_payload_still_reads_a_piped_payload(tmp_path, monkeypatch):
    from atlas_plus.hooks.scope import read_payload

    p = tmp_path / "payload.json"
    p.write_bytes(b'\xef\xbb\xbf{"cwd": "C:\\\\Projects\\\\foo", "session_id": "s1"}')
    with open(p, "rb") as fh:
        monkeypatch.setattr(sys, "stdin", __import__("io").TextIOWrapper(fh, encoding="utf-8"))
        got = read_payload(timeout=10)
    assert got == {"cwd": "C:\\Projects\\foo", "session_id": "s1"}
