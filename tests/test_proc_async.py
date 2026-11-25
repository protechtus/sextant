"""run_bounded_async: the coroutine form of the bounded runner (atlas_plus._proc).

Every program is a small Python script this test writes into tmp_path and runs with sys.executable;
nothing else is started. The event loop is the one asyncio.run makes (the proactor loop on Windows).
"""
from __future__ import annotations

import asyncio
import gc
import os
import sys
import time
import warnings

import pytest

from atlas_plus import _proc

_HOLD = 20  # seconds a grandchild holds the pipe; every timeout under test is 2 s


def _script(tmp_path, name: str, body: str) -> list[str]:
    path = tmp_path / f"{name}.py"
    path.write_text(body, encoding="utf-8")
    return [sys.executable, str(path)]


def _holder(tmp_path, pid_file=None) -> list[str]:
    """Prints a line, starts a grandchild that inherits stdout and sleeps, and exits at once."""
    gc_body = "import os, time\n"
    if pid_file is not None:
        gc_body += f"open({str(pid_file)!r}, 'w', encoding='utf-8').write(str(os.getpid()))\n"
    gc_body += f"time.sleep({_HOLD})\n"
    grandchild = _script(tmp_path, "grandchild", gc_body)
    return _script(tmp_path, "holder", "import subprocess, sys\nprint('partial', flush=True)\n"
                   f"subprocess.Popen({grandchild!r})\n")


def _alive(pid: int) -> bool:
    if os.name == "nt":
        import ctypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.restype = ctypes.c_void_p
        handle = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            k32.GetExitCodeProcess(ctypes.c_void_p(handle), ctypes.byref(code))
            return code.value == 259  # STILL_ACTIVE
        finally:
            k32.CloseHandle(ctypes.c_void_p(handle))
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _gone_within(pid: int, seconds: float) -> bool:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if not _alive(pid):
            return True
        time.sleep(0.1)
    return not _alive(pid)


def test_grandchild_holding_the_pipe_returns_near_the_timeout(tmp_path):
    pid_file = tmp_path / "gc.pid"
    t0 = time.monotonic()
    res = asyncio.run(_proc.run_bounded_async(_holder(tmp_path, pid_file), timeout=2, max_bytes=4096))
    elapsed = time.monotonic() - t0
    assert elapsed < 15, f"returned after {elapsed:.1f}s for a 2 s timeout (grandchild holds stdout {_HOLD}s)"
    assert res.timed_out is True
    assert b"partial" in res.stdout
    assert _gone_within(int(pid_file.read_text(encoding="utf-8")), 10), "the grandchild outlived the call"


def test_output_over_the_cap_is_cut_and_the_child_still_ends(tmp_path):
    argv = _script(tmp_path, "big", "import sys\nsys.stdout.buffer.write(b'x' * 1_000_000)\n")
    res = asyncio.run(_proc.run_bounded_async(argv, timeout=30, max_bytes=1000))
    assert res.timed_out is False
    assert res.returncode == 0
    assert res.truncated is True
    assert res.stdout == b"x" * 1000
    assert res.stdout_total == 1_000_000


def test_input_reaches_the_child(tmp_path):
    argv = _script(tmp_path, "upper", "import sys\nsys.stdout.write(sys.stdin.read().upper())\n")
    res = asyncio.run(_proc.run_bounded_async(argv, timeout=30, max_bytes=4096, input="hello kl6"))
    assert res.returncode == 0 and res.timed_out is False
    assert res.stdout_text() == "HELLO KL6"


def test_stdin_is_closed_without_input(tmp_path):
    argv = _script(tmp_path, "reader", "import sys\nprint(repr(sys.stdin.read()))\n")
    res = asyncio.run(_proc.run_bounded_async(argv, timeout=30, max_bytes=4096))
    assert res.stdout_text().strip() == "''"


def test_exit_code_and_stderr_tail_are_reported(tmp_path):
    argv = _script(tmp_path, "fail", "import sys\nsys.stderr.write('a' * 5000 + 'END')\nsys.exit(3)\n")
    res = asyncio.run(_proc.run_bounded_async(argv, timeout=30, max_bytes=4096, stderr_tail_bytes=10))
    assert res.returncode == 3 and res.timed_out is False
    assert res.stderr_tail == b"aaaaaaaEND"


def test_missing_program_raises_what_the_sync_helper_raises(tmp_path):
    missing = [str(tmp_path / "no-such-program-kl6.exe")]
    with pytest.raises(FileNotFoundError):
        _proc.run_bounded(missing, timeout=5, max_bytes=10)
    with pytest.raises(FileNotFoundError):
        asyncio.run(_proc.run_bounded_async(missing, timeout=5, max_bytes=10))


def test_a_string_argv_is_refused():
    with pytest.raises(TypeError):
        asyncio.run(_proc.run_bounded_async("echo hi", timeout=5, max_bytes=10))


def test_cancelling_the_awaiting_task_ends_the_tree(tmp_path):
    pid_file = tmp_path / "gc.pid"
    argv = _script(tmp_path, "long", "import subprocess, sys, time\n"
                   f"subprocess.Popen({_holder(tmp_path, pid_file)!r})\ntime.sleep({_HOLD})\n")

    async def main():
        task = asyncio.create_task(_proc.run_bounded_async(argv, timeout=60, max_bytes=4096))
        end = time.monotonic() + 15
        while not pid_file.exists() or not pid_file.read_text(encoding="utf-8"):
            assert time.monotonic() < end, "the fake grandchild never started"
            await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    t0 = time.monotonic()
    asyncio.run(main())
    assert time.monotonic() - t0 < 20
    assert _gone_within(int(pid_file.read_text(encoding="utf-8")), 10), "cancel left the grandchild running"


def test_no_unclosed_transport_warning(tmp_path):
    argv = _script(tmp_path, "hello", "print('hi')\n")
    gc.collect()  # what earlier tests left unclosed is collected here, not inside the recording below
    gc.collect()
    with warnings.catch_warnings(record=True) as seen:
        warnings.simplefilter("always")
        res = asyncio.run(_proc.run_bounded_async(argv, timeout=30, max_bytes=4096))
        asyncio.run(_proc.run_bounded_async(_holder(tmp_path), timeout=2, max_bytes=4096))
        gc.collect()
    assert res.stdout_text().strip() == "hi"
    unclosed = [str(w.message) for w in seen if issubclass(w.category, ResourceWarning)
                and any(k in str(w.message).lower() for k in ("transport", "pipe", "subprocess"))]
    assert unclosed == []
