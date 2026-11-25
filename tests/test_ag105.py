"""AG105: atlas_plus._proc.run_bounded ends the whole tree at the timeout and bounds the output.

On the base commit the module does not exist; subprocess.run there returned a 2 s timeout only after a
grandchild holding stdout ended, and read every byte of a huge output into memory. Every child here is
a script under tmp_path run with sys.executable; every grandchild writes its pid and ends by itself
after about 30 s, and the fixture ends it by pid in any case.
"""
from __future__ import annotations

import os
import signal
import sys
import time
import tracemalloc

import pytest

from atlas_plus import _proc

GRANDCHILD = """\
import os, sys, time
with open(sys.argv[1], "w", encoding="utf-8") as f:
    f.write(str(os.getpid()))
time.sleep(30)
"""

PARENT = """\
import subprocess, sys, time
gc, pidfile, linger = sys.argv[1], sys.argv[2], float(sys.argv[3])
subprocess.Popen([sys.executable, gc, pidfile])  # inherits stdout and stderr
sys.stdout.write("started\\n")
sys.stdout.flush()
time.sleep(linger)
"""


@pytest.fixture
def tree(tmp_path):
    gc = tmp_path / "grandchild.py"
    gc.write_text(GRANDCHILD, encoding="utf-8")
    parent = tmp_path / "parent.py"
    parent.write_text(PARENT, encoding="utf-8")
    pidfile = tmp_path / "gc.pid"
    yield parent, gc, pidfile
    try:  # end what we started, by pid, whatever the test did
        pid = int(pidfile.read_text(encoding="utf-8"))
        os.kill(pid, signal.SIGTERM)
    except (OSError, ValueError):
        pass


def _script(tmp_path, name, body):
    p = tmp_path / name
    p.write_text(body, encoding="utf-8")
    return [sys.executable, str(p)]


@pytest.mark.parametrize("linger", [30, 0])
def test_grandchild_holding_the_pipe_does_not_hold_the_call(tree, linger):
    parent, gc, pidfile = tree
    t0 = time.monotonic()
    res = _proc.run_bounded([sys.executable, str(parent), str(gc), str(pidfile), str(linger)],
                            timeout=2, max_bytes=1024)
    elapsed = time.monotonic() - t0
    assert elapsed < 15, f"returned after {elapsed:.1f}s for a 2 s timeout"
    assert res.timed_out
    assert b"started" in res.stdout


def test_huge_output_is_capped_and_the_call_ends(tmp_path):
    argv = _script(tmp_path, "big.py",
                   "import sys\nblock = b'x' * (1024 * 1024)\n"
                   "for _ in range(50):\n    sys.stdout.buffer.write(block)\n")
    tracemalloc.start()
    try:
        res = _proc.run_bounded(argv, timeout=60, max_bytes=1024 * 1024)
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert peak < 8 * 1024 * 1024, f"peak {peak:,} bytes for a 1 MiB cap and 50 MiB of output"
    assert res.truncated
    assert len(res.stdout) == 1024 * 1024
    assert res.stdout_total == 50 * 1024 * 1024
    assert res.returncode == 0 and not res.timed_out


def test_normal_child_returns_output_and_exit_code(tmp_path):
    argv = _script(tmp_path, "ok.py", "print('hello \\u00e9')\n")
    res = _proc.run_bounded(argv, timeout=30, max_bytes=4096)
    assert res.returncode == 0
    assert res.stdout_text().strip() == "hello \u00e9"
    assert not res.timed_out and not res.truncated


def test_non_zero_exit_keeps_the_stderr_tail(tmp_path):
    argv = _script(tmp_path, "fail.py", "import sys\nsys.stderr.write('boom\\n')\nsys.exit(7)\n")
    res = _proc.run_bounded(argv, timeout=30, max_bytes=4096)
    assert res.returncode == 7
    assert res.stderr_text().strip() == "boom"


def test_missing_program_raises_file_not_found(tmp_path):
    with pytest.raises(FileNotFoundError):
        _proc.run_bounded([str(tmp_path / "no-such-program")], timeout=5, max_bytes=10)


def test_a_string_argv_is_refused():
    with pytest.raises(TypeError):
        _proc.run_bounded("echo hi", timeout=5, max_bytes=10)


def test_child_that_only_fills_stderr_cannot_deadlock(tmp_path):
    argv = _script(tmp_path, "err.py",
                   "import sys\nblock = b'e' * (1024 * 1024)\n"
                   "for _ in range(5):\n    sys.stderr.buffer.write(block)\n"
                   "sys.stderr.buffer.write(b'last line\\n')\nsys.stdout.write('done')\n")
    res = _proc.run_bounded(argv, timeout=60, max_bytes=4096, stderr_tail_bytes=1000)
    assert not res.timed_out
    assert res.returncode == 0 and res.stdout == b"done"
    assert len(res.stderr_tail) == 1000 and res.stderr_tail.endswith(b"last line\n")


def test_env_and_cwd_reach_the_child(tmp_path):
    argv = _script(tmp_path, "env.py", "import os\nprint(os.environ.get('AG105_X'), os.getcwd())\n")
    env = dict(os.environ, AG105_X="yes")
    res = _proc.run_bounded(argv, timeout=30, max_bytes=4096, env=env, cwd=str(tmp_path))
    word, cwd = res.stdout_text().split(None, 1)
    assert word == "yes"
    assert os.path.samefile(cwd.strip(), tmp_path)
