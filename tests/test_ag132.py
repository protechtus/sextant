"""SEX-043 group (c), AG132: SEXTANT_<NAME> is honoured wherever ATLAS_<NAME> is read; ATLAS_* alone is unchanged.

Both directions: only-old works and is left exactly as it was, only-new works, both set follows the precedence rule
(ATLAS_* wins, one stderr line naming both), an unrelated SEXTANT_* name is ignored quietly, a child process sees the
value, and the suite's own isolation clears the new spelling too. The children run this package from the tree under
test (sys.executable + PYTHONPATH), never a console-script name.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import atlas_plus
from atlas_plus import _proc


def _envnames():
    from atlas_plus import _envnames as mod   # imported per test: on the base commit each test fails on its own
    return mod

_SRC = str(Path(atlas_plus.__file__).resolve().parents[1])

_PROBE = r'''
import json, os, sys
before = sorted(os.environ)
import atlas_plus
from atlas_plus import paths, _proc
after = sorted(os.environ)
out = {"added": sorted(set(after) - set(before)), "state": str(paths.state_dir()), "state2": str(paths.state_dir()),
       "ws": str(paths.workspace_root()), "mem": str(paths.memory_root())}
if len(sys.argv) > 1:   # start a grandchild that does NOT import the package: it sees only what it inherited
    r = _proc.run_bounded([sys.executable, "-I", sys.argv[1]], timeout=60, max_bytes=65536)
    out["grandchild"] = r.stdout_text().strip()
print(json.dumps(out))
'''

_GRANDCHILD = 'import os\nprint(os.environ.get("ATLAS_STATE_DIR", "<unset>"))\n'


def _clean_env(tmp_path: Path) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith(("ATLAS_", "SEXTANT_"))}
    env["PYTHONPATH"] = _SRC
    env["PYTHONIOENCODING"] = "utf-8"
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    env["HOME"] = env["USERPROFILE"] = str(home)
    return env


def _run(tmp_path: Path, env: dict[str, str], grandchild: bool = False):
    probe = tmp_path / "probe.py"
    probe.write_text(_PROBE, encoding="utf-8")
    argv = [sys.executable, str(probe)]
    if grandchild:
        gc = tmp_path / "grandchild.py"
        gc.write_text(_GRANDCHILD, encoding="utf-8")
        argv.append(str(gc))
    r = _proc.run_bounded(argv, timeout=120, max_bytes=1 << 20, env=env, cwd=str(tmp_path))
    assert r.returncode == 0, r.stderr_text()
    return json.loads(r.stdout_text().strip().splitlines()[-1]), r.stderr_text()


def test_only_old_spelling_is_left_exactly_as_it_was(tmp_path):
    env = _clean_env(tmp_path)
    env["ATLAS_WORKSPACE_ROOT"] = str(tmp_path / "ws")
    env["ATLAS_STATE_DIR"] = str(tmp_path / "old-state")
    listing = sorted(p.name for p in tmp_path.iterdir())
    out, err = _run(tmp_path, env)
    assert out["added"] == []                          # no key added to the environment
    assert out["state"] == str(tmp_path / "old-state")
    assert out["ws"] == str(tmp_path / "ws")
    assert err == ""                                    # no new output
    assert sorted(p.name for p in tmp_path.iterdir()) == sorted(listing + ["probe.py"])  # no new files


def test_only_old_spelling_in_process_mapping_unchanged(capsys):
    env = {"ATLAS_MEMORY_ROOT": "/home/user/m", "ATLAS_HOOK_SCOPE": "", "PATH": "/usr/bin"}
    snapshot = dict(env)
    _envnames().adopt(env)
    assert env == snapshot
    assert capsys.readouterr().err == ""


def test_only_new_spelling_is_read(tmp_path):
    env = _clean_env(tmp_path)
    env["SEXTANT_WORKSPACE_ROOT"] = str(tmp_path / "ws")
    env["SEXTANT_STATE_DIR"] = str(tmp_path / "new-state")
    out, err = _run(tmp_path, env)
    assert out["ws"] == str(tmp_path / "ws")
    assert out["state"] == str(tmp_path / "new-state")
    assert out["mem"] == str(tmp_path / "ws" / ".memory")
    assert set(out["added"]) == {"ATLAS_WORKSPACE_ROOT", "ATLAS_STATE_DIR"}
    assert "never read" not in err and err == ""


def test_empty_new_value_is_copied_as_set_and_empty():
    env = {"SEXTANT_HOOK_SCOPE": ""}       # `ATLAS_HOOK_SCOPE=` means "unscoped": set-and-empty must survive
    _envnames().adopt(env)
    assert env == {"SEXTANT_HOOK_SCOPE": "", "ATLAS_HOOK_SCOPE": ""}


def test_both_set_and_differ_old_wins_with_one_warning(tmp_path):
    env = _clean_env(tmp_path)
    env["ATLAS_STATE_DIR"] = str(tmp_path / "old-state")
    env["SEXTANT_STATE_DIR"] = str(tmp_path / "new-state")
    out, err = _run(tmp_path, env)
    assert out["state"] == out["state2"] == str(tmp_path / "old-state")
    assert out["added"] == []
    assert err.count("SEXTANT_STATE_DIR and ATLAS_STATE_DIR are both set and differ; using ATLAS_STATE_DIR") == 1
    assert str(tmp_path) not in err                     # names only, never a value
    assert err.isascii()


def test_both_set_to_the_same_value_is_quiet(tmp_path):
    env = _clean_env(tmp_path)
    env["ATLAS_STATE_DIR"] = env["SEXTANT_STATE_DIR"] = str(tmp_path / "same")
    out, err = _run(tmp_path, env)
    assert out["state"] == str(tmp_path / "same") and err == ""


def test_unrelated_and_reserved_new_names_are_ignored_quietly(tmp_path):
    env = _clean_env(tmp_path)
    env["SEXTANT_PROOF_WORK"] = str(tmp_path / "bench")   # the benchmark harness's own namespace
    env["SEXTANT_NOT_A_SETTING_AG132"] = "1"
    env["SEXTANT_"] = "1"
    out, err = _run(tmp_path, env)
    assert "ATLAS_PROOF_WORK" not in out["added"]
    assert "ATLAS_" not in out["added"]
    assert err == ""
    for bad in ("SEXTANT_", "SEXTANT__X", "SEXTANT_lower", "SEXTANT_PROOF_TAG", "ATLAS_X", "SEXTANT_X-Y"):
        assert _envnames().twin_of(bad) is None, bad
    assert _envnames().twin_of("SEXTANT_MCP_PORT") == "ATLAS_MCP_PORT"


def test_a_child_process_the_product_starts_sees_the_value(tmp_path):
    env = _clean_env(tmp_path)
    env["SEXTANT_STATE_DIR"] = str(tmp_path / "new-state")
    out, _ = _run(tmp_path, env, grandchild=True)
    assert out["grandchild"] == str(tmp_path / "new-state")


def test_brain_verify_child_env_drops_the_new_spelling(monkeypatch):
    from atlas_plus.brain import executors
    monkeypatch.setenv("SEXTANT_STATE_DIR", "/home/user/live-state")
    monkeypatch.setenv("sextant_web_api_key", "x")
    clean = executors._clean_pytest_env()
    assert not [k for k in clean if k.upper().startswith(("ATLAS_", "SEXTANT_"))]


def test_suite_isolation_leaves_no_new_spelling_from_the_shell():
    # tests/conftest.py drops SEXTANT_* with ATLAS_* before any atlas_plus import; a leaked SEXTANT_STATE_DIR
    # would otherwise be adopted as ATLAS_STATE_DIR in every child a test starts.
    leaked = [k for k in os.environ if k.upper().startswith("SEXTANT_")]
    assert leaked == []
