"""KL3 (sweep 30): run_bounded's ``input=`` option.

The text or bytes are fed to the child's stdin from a thread and stdin is then closed; a child that
never reads its stdin still ends at the deadline (the write never blocks the caller); a child that
exits before reading is not an error. Without ``input=`` stdin stays closed (DEVNULL).
Every child is a small script this test writes into tmp_path and starts with sys.executable.
"""
from __future__ import annotations

import sys
import time

import pytest

from atlas_plus import _proc


def _script(tmp_path, name: str, body: str) -> str:
    p = tmp_path / name
    p.write_text(body, encoding="utf-8")
    return str(p)


def test_a_child_that_echoes_its_stdin_gets_the_text(tmp_path):
    echo = _script(tmp_path, "echo.py", "import sys\nsys.stdout.buffer.write(sys.stdin.buffer.read())\n")
    res = _proc.run_bounded([sys.executable, echo], timeout=30, max_bytes=1 << 20, input="café line\n")
    assert not res.timed_out
    assert res.returncode == 0
    assert res.stdout == "café line\n".encode("utf-8")


def test_bytes_input_larger_than_a_pipe_buffer_round_trips(tmp_path):
    echo = _script(tmp_path, "echo.py", "import sys\nsys.stdout.buffer.write(sys.stdin.buffer.read())\n")
    data = bytes(range(256)) * 4096  # 1 MiB: far past any pipe buffer, both ways at once
    res = _proc.run_bounded([sys.executable, echo], timeout=60, max_bytes=2 << 20, input=data)
    assert not res.timed_out
    assert res.stdout == data


def test_a_child_that_never_reads_5_mb_of_input_returns_at_the_timeout(tmp_path):
    sleeper = _script(tmp_path, "sleeper.py", "import time\ntime.sleep(60)\n")
    t0 = time.monotonic()
    res = _proc.run_bounded([sys.executable, sleeper], timeout=2, max_bytes=1024, input=b"x" * (5 * 1024 * 1024))
    elapsed = time.monotonic() - t0
    assert res.timed_out
    assert elapsed < 20, f"returned after {elapsed:.1f}s for a 2 s timeout"


def test_a_child_that_exits_before_reading_is_not_an_error(tmp_path):
    quitter = _script(tmp_path, "quitter.py", "import sys\nsys.stdout.write('bye')\nsys.exit(3)\n")
    res = _proc.run_bounded([sys.executable, quitter], timeout=30, max_bytes=1024, input=b"y" * (5 * 1024 * 1024))
    assert not res.timed_out
    assert res.returncode == 3
    assert res.stdout == b"bye"


def test_without_input_stdin_is_closed(tmp_path):
    reader = _script(tmp_path, "reader.py", "import sys\nsys.stdout.write(repr(sys.stdin.read()) if sys.stdin else 'None')\n")
    res = _proc.run_bounded([sys.executable, reader], timeout=30, max_bytes=1024)
    assert res.returncode == 0
    assert res.stdout in (b"''", b"None")


def test_input_of_the_wrong_type_is_refused():
    with pytest.raises(TypeError):
        _proc.run_bounded([sys.executable, "x"], timeout=1, max_bytes=1, input=123)
