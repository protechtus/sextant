"""AG129 (SEX-054 item 3): scripts/readme_walk.py reads the README's Quickstart like a literal reader.

The walk itself installs a package (minutes), so it runs as the CI `reader` job, not here. These
tests pin the extraction on a small fixture README (order, skip reasons, the substitution) and the
failure path of the runner with a program that does not exist. No client program is resolved (G17).
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
_SCRIPT = _REPO / "scripts" / "readme_walk.py"

FIXTURE = """# Tool

```text
pip install not-this-one
```

## Quickstart

Windows:

```powershell
git clone https://example-org.invalid/tool.git; cd tool
py -3 -m venv .venv   # needs 3.11+
.\\.venv\\Scripts\\tool.exe --version
```

Optional, and NOT part of the paste: relocate.

```powershell
$env:TOOL_ROOT = "C:\\Projects\\data"
```

Linux / macOS:

```bash
git clone https://example-org.invalid/tool.git && cd tool
python3 -m venv .venv
# a comment line is not a command
.venv/bin/tool --version
```

Connect:

```
.\\.venv\\Scripts\\tool.exe setup    # Windows
.venv/bin/tool setup              # Linux / macOS
```

```bash
claude mcp add -s user tool -- "$PWD/.venv/bin/python" -m tool
codex mcp list
```

## Architecture

```bash
never-extracted
```
"""


@pytest.fixture(scope="module")
def walk():
    # No skip: the public build must ship the script beside this test (the CI reader job runs it).
    assert _SCRIPT.is_file(), "scripts/readme_walk.py is missing: add it to the public build's script set"
    spec = importlib.util.spec_from_file_location("readme_walk_under_test", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    try:
        spec.loader.exec_module(mod)
        yield mod
    finally:
        sys.modules.pop(spec.name, None)


def _run(steps):
    return [s.command for s in steps if not s.reason]


def test_posix_reader_gets_the_bash_lines_in_order(walk):
    steps = walk.extract(FIXTURE, "posix")
    assert _run(steps) == [
        "git clone https://example-org.invalid/tool.git && cd tool",
        "python3 -m venv .venv",
        ".venv/bin/tool --version",
        ".venv/bin/tool setup",
    ]
    lines = [s.line for s in steps]
    assert lines == sorted(lines), "steps must keep README order"
    assert all("never-extracted" not in s.command for s in steps), "only the Quickstart section is read"
    assert all("not-this-one" not in s.command for s in steps)


def test_windows_reader_gets_the_powershell_lines_in_order(walk):
    assert _run(walk.extract(FIXTURE, "windows")) == [
        "git clone https://example-org.invalid/tool.git; cd tool",
        "py -3 -m venv .venv",
        ".\\.venv\\Scripts\\tool.exe --version",
        ".\\.venv\\Scripts\\tool.exe setup",
    ]


def test_skip_reasons(walk):
    by_cmd = {s.command: s.reason for s in walk.extract(FIXTURE, "posix")}
    assert "optional" in by_cmd['$env:TOOL_ROOT = "C:\\Projects\\data"']
    assert "Windows only" in by_cmd["py -3 -m venv .venv"]
    assert "Windows" in by_cmd[".\\.venv\\Scripts\\tool.exe setup"]
    claude = next(r for c, r in by_cmd.items() if c.startswith("claude "))
    codex = by_cmd["codex mcp list"]
    assert "client program `claude`" in claude and "client program `codex`" in codex
    win = {s.command: s.reason for s in walk.extract(FIXTURE, "windows")}
    assert "Linux / macOS only" in win["python3 -m venv .venv"]
    assert "Linux / macOS" in win[".venv/bin/tool setup"]


def test_the_clone_line_is_substituted_by_a_copy_of_the_checkout(walk, tmp_path):
    root = tmp_path / "checkout"
    (root / ".git").mkdir(parents=True)
    (root / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    w = walk.Walk(root, tmp_path / "work", timeout=30)
    for argv in walk.split_line("git clone https://example-org.invalid/tool.git && cd tool"):
        ok, out = w.run_argv(argv)
        assert ok, out
    assert w.cwd.name == "tool"
    assert (w.cwd / "pyproject.toml").is_file()
    assert not (w.cwd / ".git").exists()


def test_the_reader_home_drops_project_variables(walk, tmp_path, monkeypatch):
    monkeypatch.setenv("ATLAS_MEMORY_ROOT", str(tmp_path / "elsewhere"))
    monkeypatch.setenv("SEXTANT_EXAMPLE", "1")
    env = walk.reader_env(tmp_path)
    assert env["HOME"] == str(tmp_path)
    assert not [k for k in env if k.upper().startswith(("ATLAS_", "SEXTANT_"))]


def _doctored(tmp_path: Path) -> Path:
    text = FIXTURE.replace("python3 -m venv .venv", "no-such-program-ag129 --flag").replace(
        "py -3 -m venv .venv", "no-such-program-ag129 --flag")
    p = tmp_path / "README.md"
    p.write_text(text, encoding="utf-8")
    return p


def test_a_missing_program_fails_the_walk_and_is_named(walk, tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    root = tmp_path / "checkout"
    root.mkdir()
    rc = walk.main([str(root), "--readme", str(_doctored(tmp_path)), "--workdir", str(tmp_path / "w"),
                    "--timeout", "30"])
    out = capsys.readouterr().out
    assert rc == 1
    assert "command not found: no-such-program-ag129" in out
    assert "FAILED at README:" in out and "no-such-program-ag129 --flag" in out


def test_a_nonzero_exit_and_a_traceback_both_fail(walk, tmp_path):
    w = walk.Walk(tmp_path, tmp_path / "work", timeout=60)
    ok, out = w.run_argv([sys.executable, "-I", "-m", "this_module_does_not_exist_ag129"])
    assert not ok and "exit 1" in out
    script = tmp_path / "tb.py"
    script.write_text("import sys, traceback\ntry:\n    1/0\nexcept ZeroDivisionError:\n"
                      "    traceback.print_exc(file=sys.stdout)\n", encoding="utf-8")
    ok, out = w.run_argv([sys.executable, "-I", str(script)])
    assert not ok and "traceback" in out


def test_a_line_that_needs_a_shell_fails_closed(walk, tmp_path):
    w = walk.Walk(tmp_path, tmp_path / "work", timeout=30)
    assert w.step(1, walk.Step(5, 'echo "$HOME" | cat')) is False
    assert "needs a shell" in "\n".join(w.log)


def test_the_real_readme_has_a_walkable_quickstart(walk):
    for rel in ("docs/README-PUBLIC.md", "README.md"):
        if (_REPO / rel).is_file():
            text = (_REPO / rel).read_text(encoding="utf-8")
            break
    else:
        pytest.skip("no public README in this tree")
    for platform in ("posix", "windows"):
        steps = walk.extract(text, platform)
        run = _run(steps)
        assert run and run[0].startswith("git clone "), (platform, run[:2])
        assert any("pip install -e" in c for c in run), platform
        assert not [c for c in run if c.split()[0].rsplit("/", 1)[-1] in walk.CLIENT_PROGRAMS]
