"""Run another program with a hard wall-clock bound and a bounded output size.

Use ``run_bounded`` instead of ``subprocess.run(..., capture_output=True, timeout=...)`` whenever the
program is not ours (a user-configured command, a tool found on PATH) and its output is read into
memory. ``subprocess.run`` has two gaps there:

* at the timeout it kills only the direct child, then drains the pipes again without a bound; a
  grandchild that inherited stdout keeps the pipe open, so the call returns only when the grandchild
  ends (seconds or never);
* the whole output is read into memory before any size check can run.

``run_bounded`` starts the child in its own process group (POSIX: a new session; Windows: a new
process group inside a job object), reads stdout and stderr in chunks on daemon threads, keeps at
most ``max_bytes`` of stdout and the last ``stderr_tail_bytes`` of stderr, and at the deadline ends
the WHOLE process tree. It returns within a small, fixed bound after the deadline, whoever holds
the pipe.

Past the stdout cap the reader keeps reading and discarding: the child never blocks on a full pipe,
its exit code stays honest, and memory stays at the cap; the deadline still bounds the total time.

Limit: a descendant that deliberately leaves the tree (POSIX ``setsid``, Windows breakaway from the
job) is not ended; the call still returns on time, but that process keeps running.

A missing program raises ``FileNotFoundError`` and any other start failure raises ``OSError``,
exactly as ``subprocess.Popen`` does; everything after the start is reported in the result.

``run_bounded_async`` is the same contract for a coroutine (it needs an event loop that supports
subprocesses: the default loop on every platform, the proactor loop on Windows). Cancelling the
awaiting task ends the tree too.
"""
from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Mapping, Sequence

__all__ = ["BoundedResult", "model_cli_env", "other_party_env", "run_bounded", "run_bounded_async"]

_CHUNK = 64 * 1024
_KILL_GRACE_SECONDS = 5.0   # wait for the killed child to be reaped
_JOIN_GRACE_SECONDS = 2.0   # wait for the readers to see EOF after the kill


# ---- the environment of another party's program (LES-079, LES-053; AG127) -----------------------------------
#
# A child that runs a program this project does not ship (git, rclone, docker, PowerShell, an editor, a client
# program, a workspace script) gets an environment built from this allowlist, never the parent's whole one: the
# parent holds the user's tokens and keys (ATLAS_MCP_TOKEN, ATLAS_WEB_API_KEY, ... and their SEXTANT_* twins).
# A child that IS this package (`sys.executable -m atlas_plus...`, this package's own console scripts) keeps the
# full environment: it needs its settings.

#: names every program may need to start and behave: path and system, home, temp, locale, terminal, network setup
#: (proxies and CA bundles are the user's network configuration; a proxy URL can carry a password, and the program
#: needs it to reach the network at all). Matched case-insensitively (Windows names are).
CHILD_ENV_NAMES = frozenset({
    "PATH", "PATHEXT", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "OS", "NUMBER_OF_PROCESSORS",
    "PROCESSOR_ARCHITECTURE", "PROCESSOR_IDENTIFIER", "PROGRAMDATA", "PROGRAMFILES", "PROGRAMFILES(X86)",
    "PROGRAMW6432", "COMMONPROGRAMFILES", "COMMONPROGRAMFILES(X86)", "COMMONPROGRAMW6432", "ALLUSERSPROFILE",
    "PUBLIC", "HOME", "USERPROFILE", "HOMEDRIVE", "HOMEPATH", "APPDATA", "LOCALAPPDATA", "USER", "USERNAME",
    "USERDOMAIN", "LOGNAME", "SHELL", "TERM", "COLORTERM", "NO_COLOR", "TEMP", "TMP", "TMPDIR", "LANG", "LANGUAGE",
    "TZ", "PYTHONIOENCODING", "PYTHONUTF8", "DISPLAY", "WAYLAND_DISPLAY",
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "ALL_PROXY", "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE",
    "NODE_EXTRA_CA_CERTS", "CURL_CA_BUNDLE", "OPENSSL_CONF",
    # the machine and the session (SEX-060): PowerShell's module path and scripts that name the machine, the
    # processor level some installers read, the session bus and X authority a credential helper talks to
    "COMPUTERNAME", "HOSTNAME", "PSMODULEPATH", "PROCESSOR_LEVEL", "PROCESSOR_REVISION", "SESSIONNAME",
    "LOGONSERVER", "USERDOMAIN_ROAMINGPROFILE", "DRIVERDATA", "DBUS_SESSION_BUS_ADDRESS", "XAUTHORITY",
    # where the user keeps caches (SEX-060): a child that lost these wrote gigabytes to the default drive instead
    "PIP_CACHE_DIR", "UV_CACHE_DIR", "HF_HOME", "PLAYWRIGHT_BROWSERS_PATH", "NPM_CONFIG_CACHE",
})
#: name prefixes kept for every program: locale categories and the XDG base directories
CHILD_ENV_PREFIXES = ("LC_", "XDG_")
#: this package's settings, under both spellings (SEXTANT_<NAME> is adopted as ATLAS_<NAME> at import)
_SETTING_PREFIXES = ("ATLAS_", "SEXTANT_")
#: a setting whose name holds one of these words is a secret and never reaches another party's program
_SECRET_WORDS = ("TOKEN", "KEY", "SECRET", "PASS", "CREDENTIAL", "AUTH", "COOKIE")


def _is_secret_setting(upper_name: str) -> bool:
    return any(w in upper_name for w in _SECRET_WORDS)


def other_party_env(
    *,
    keep: Sequence[str] = (),
    keep_prefixes: Sequence[str] = (),
    settings: bool = False,
    extra: Mapping[str, str] | None = None,
    base: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """The environment for a child that runs another party's program, built from an allowlist.

    Kept from ``base`` (default ``os.environ``): ``CHILD_ENV_NAMES`` and ``CHILD_ENV_PREFIXES``, plus the tool's
    own variables the caller names (``keep``: exact names such as ``EDITOR``; ``keep_prefixes``: such as
    ``GIT_`` or ``RCLONE_``). ``settings=True`` (for a client program that starts this package, such as a
    model CLI whose MCP servers are ours) also keeps the ``ATLAS_*`` / ``SEXTANT_*`` settings whose names are
    not secret-shaped; a token, key, password or secret under either spelling never passes, whatever the
    caller asks. ``extra`` is set last. Matching is case-insensitive.
    """
    src = os.environ if base is None else base
    names = {n.upper() for n in CHILD_ENV_NAMES} | {n.upper() for n in keep}
    prefixes = tuple(p.upper() for p in (*CHILD_ENV_PREFIXES, *keep_prefixes))
    env: dict[str, str] = {}
    for name, value in src.items():
        up = name.upper()
        if up.startswith(_SETTING_PREFIXES):
            if settings and not _is_secret_setting(up):
                env[name] = value
            continue  # a setting passes only through `settings`, never through `keep` or a prefix
        if up in names or up.startswith(prefixes):
            env[name] = value
    if extra:
        env.update(extra)
    return env


#: a model CLI's own variables: its prefixes (its sign-in, its settings, its privacy switches, the limits of the
#: tools it starts, its telemetry exporter) and the unprefixed switches it documents
_MODEL_CLI_PREFIXES = {
    "claude": ("ANTHROPIC_", "CLAUDE_", "DISABLE_", "MCP_", "BASH_", "OTEL_"),
    "codex": ("OPENAI_", "CODEX_"),
    "opencode": ("OPENCODE_", "ANTHROPIC_", "OPENAI_"),
}
_MODEL_CLI_NAMES = {
    "claude": ("MAX_THINKING_TOKENS", "MAX_MCP_OUTPUT_TOKENS", "API_TIMEOUT_MS", "USE_BUILTIN_RIPGREP"),
}
#: a cloud's sign-in names reach the CLI only when the CLI's own switch selects that cloud
_MODEL_CLI_CLOUD = {
    "claude": (("CLAUDE_CODE_USE_BEDROCK", ("AWS_",)),
               ("CLAUDE_CODE_USE_VERTEX", ("GOOGLE_", "CLOUD_ML_", "CLOUDSDK_"))),
}


#: values that leave a CLI's cloud switch off
_SWITCH_OFF = frozenset({"0", "false", "no", "off"})


def model_cli_env(
    cli: str,
    *,
    extra: Mapping[str, str] | None = None,
    base: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """The environment for a model CLI (``claude``, ``codex``, ``opencode``; an ``-cli`` suffix is accepted).

    ``other_party_env`` with this package's settings (the CLI starts our MCP server and hooks; a secret-shaped
    setting never passes) plus the CLI's own variables. One place, so every caller hands the same CLI the same
    names (SEX-060: six call sites repeated the prefixes and none kept the CLI's unprefixed switches).
    """
    kind = cli.lower().removesuffix("-cli")
    src = os.environ if base is None else base
    # a switch set to 0/false/no/off selects no cloud (MN98: CLAUDE_CODE_USE_BEDROCK=0 passed every AWS_ name)
    present = {n.upper() for n, v in src.items() if v and v.strip().lower() not in _SWITCH_OFF}
    prefixes = list(_MODEL_CLI_PREFIXES.get(kind, ()))
    for switch, cloud in _MODEL_CLI_CLOUD.get(kind, ()):
        if switch in present:
            prefixes.extend(cloud)
    return other_party_env(keep=_MODEL_CLI_NAMES.get(kind, ()), keep_prefixes=tuple(prefixes), settings=True,
                           extra=extra, base=src)


@dataclass
class BoundedResult:
    """What a bounded run produced. ``returncode`` is None only when the child could not be reaped."""

    returncode: int | None
    stdout: bytes
    stderr_tail: bytes
    timed_out: bool
    truncated: bool
    stdout_total: int  # bytes seen on stdout, kept or discarded

    def stdout_text(self) -> str:
        return self.stdout.decode("utf-8", errors="replace")

    def stderr_text(self) -> str:
        return self.stderr_tail.decode("utf-8", errors="replace")


class _Reader(threading.Thread):
    """Drain one pipe in chunks; keep the head (stdout) or the tail (stderr) up to ``limit`` bytes."""

    def __init__(self, stream, limit: int, keep_tail: bool) -> None:
        super().__init__(daemon=True)
        self._stream = stream
        self._limit = max(0, int(limit))
        self._keep_tail = keep_tail
        self.data = bytearray()
        self.total = 0
        self.truncated = False

    def run(self) -> None:
        fd = self._stream.fileno()
        try:
            while True:
                try:
                    chunk = os.read(fd, _CHUNK)
                except OSError:
                    break
                if not chunk:
                    break
                self.total += len(chunk)
                if self._keep_tail:
                    self.data += chunk
                    if len(self.data) > self._limit:
                        del self.data[: len(self.data) - self._limit]
                        self.truncated = True
                else:
                    room = self._limit - len(self.data)
                    if room > 0:
                        self.data += chunk[:room]
                    if len(chunk) > room:
                        self.truncated = True  # keep draining so the child never blocks on a full pipe
        finally:
            try:
                self._stream.close()
            except OSError:
                pass


class _Writer(threading.Thread):
    """Feed ``data`` to the child's stdin, then close it. A child that never reads blocks this daemon
    thread, never the caller: the tree kill at the deadline breaks the pipe and the write returns."""

    def __init__(self, stream, data: bytes) -> None:
        super().__init__(daemon=True)
        self._stream = stream
        self._data = data

    def run(self) -> None:
        fd = self._stream.fileno()
        view = memoryview(self._data)
        try:
            while view:
                try:
                    n = os.write(fd, view[:_CHUNK])
                except OSError:  # the child exited or closed stdin: the rest is not wanted
                    break
                view = view[n:]
        finally:
            try:
                self._stream.close()
            except OSError:
                pass


def _new_job():
    """A Windows job object the child is put into, so its whole tree can be ended at once."""
    try:
        import ctypes
        from ctypes import wintypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateJobObjectW.restype = wintypes.HANDLE
        k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        k32.AssignProcessToJobObject.restype = wintypes.BOOL
        k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        k32.TerminateJobObject.restype = wintypes.BOOL
        k32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        k32.CloseHandle.restype = wintypes.BOOL
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        job = k32.CreateJobObjectW(None, None)
        return (k32, job) if job else None
    except (OSError, AttributeError, ImportError):
        return None


def _kill_tree(proc: subprocess.Popen, job) -> None:
    """End the child and every descendant. Never raises, never waits without a bound."""
    if os.name == "nt":
        if proc.poll() is None:
            try:  # covers a grandchild started before the child joined the job
                subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], stdin=subprocess.DEVNULL,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10, check=False)
            except (OSError, subprocess.SubprocessError):
                pass
        if job is not None:  # reaches descendants even after the child itself has exited
            job[0].TerminateJobObject(job[1], 1)
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)  # start_new_session: the group id is the child's pid
        except (ProcessLookupError, PermissionError, OSError):
            pass
    try:
        proc.kill()
    except OSError:
        pass


def run_bounded(
    argv: Sequence[str],
    *,
    timeout: float,
    max_bytes: int,
    stderr_tail_bytes: int = 64 * 1024,
    env: Mapping[str, str] | None = None,
    cwd: str | os.PathLike | None = None,
    input: str | bytes | None = None,
    no_window: bool = False,
) -> BoundedResult:
    """Run ``argv`` (a list, never a shell) with stdin closed; return within about ``timeout`` seconds.

    ``input`` (text is encoded as UTF-8) is written to the child's stdin from a thread, then stdin is
    closed; a child that never reads it still ends at the deadline. Without ``input`` stdin is DEVNULL.
    The call ends the whole process tree at the deadline (``timed_out``), keeps at most ``max_bytes``
    of stdout (``truncated``) and the last ``stderr_tail_bytes`` of stderr. A child that exits but
    leaves a grandchild holding its stdout counts as timed out when the grandchild is still there at
    the deadline. ``no_window`` (Windows only) starts a console program without a console window,
    for probes run from a background task. Raises ``FileNotFoundError`` / ``OSError`` when the program cannot be started.
    """
    if isinstance(argv, (str, bytes)):
        raise TypeError("argv must be a list of arguments, not a string")
    argv = [os.fspath(a) for a in argv]
    if isinstance(input, str):
        input = input.encode("utf-8")
    elif input is not None and not isinstance(input, (bytes, bytearray, memoryview)):
        raise TypeError("input must be str or bytes")
    kwargs: dict = {}
    job = None
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | (
            getattr(subprocess, "CREATE_NO_WINDOW", 0) if no_window else 0)
    else:
        kwargs["start_new_session"] = True
    deadline = time.monotonic() + max(0.0, float(timeout))
    stdin = subprocess.DEVNULL if input is None else subprocess.PIPE
    proc = subprocess.Popen(argv, stdin=stdin, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            env=dict(env) if env is not None else None, cwd=cwd, bufsize=0, **kwargs)
    if os.name == "nt":
        job = _new_job()
        if job is not None and not job[0].AssignProcessToJobObject(job[1], int(proc._handle)):
            job[0].CloseHandle(job[1])
            job = None
    out = _Reader(proc.stdout, max_bytes, keep_tail=False)
    err = _Reader(proc.stderr, stderr_tail_bytes, keep_tail=True)
    out.start()
    err.start()
    if input is not None:
        _Writer(proc.stdin, bytes(input)).start()
    timed_out = False
    try:
        try:
            proc.wait(timeout=max(0.0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            timed_out = True
        if not timed_out:
            # the child is gone; a grandchild may still hold the pipe: wait for EOF until the deadline only
            for reader in (out, err):
                reader.join(max(0.0, deadline - time.monotonic()))
            timed_out = out.is_alive() or err.is_alive()
        if timed_out:
            _kill_tree(proc, job)
            try:
                proc.wait(timeout=_KILL_GRACE_SECONDS)
            except subprocess.TimeoutExpired:
                pass
            for reader in (out, err):
                reader.join(_JOIN_GRACE_SECONDS)
    finally:
        if proc.poll() is None:  # an exception on the way here (Ctrl+C): the tree is not left behind
            _kill_tree(proc, job)
        if job is not None:
            if out.is_alive() or err.is_alive() or proc.poll() is None:
                job[0].TerminateJobObject(job[1], 1)
            job[0].CloseHandle(job[1])
    # a reader still alive here is a daemon blocked on a pipe nobody can write any more; it closes the
    # pipe itself at EOF. Closing it from this thread could block on the reader's pending read.
    return BoundedResult(
        returncode=proc.poll(),
        stdout=bytes(out.data),
        stderr_tail=bytes(err.data),
        timed_out=timed_out,
        truncated=out.truncated,
        stdout_total=out.total,
    )


class _AsyncCollector(asyncio.SubprocessProtocol):
    """Keep the head of stdout and the tail of stderr; note the real exit, not the pipes' EOF."""

    def __init__(self, loop, max_bytes: int, tail_bytes: int) -> None:
        self._max = max(0, int(max_bytes))
        self._tail = max(0, int(tail_bytes))
        self.out = bytearray()
        self.err = bytearray()
        self.total = 0
        self.truncated = False
        self.exited = loop.create_future()   # set when the child itself ends
        self.closed = loop.create_future()   # set when every pipe is closed too

    def pipe_data_received(self, fd: int, data: bytes) -> None:
        if fd == 1:
            self.total += len(data)
            room = self._max - len(self.out)
            if room > 0:
                self.out += data[:room]
            if len(data) > room:
                self.truncated = True  # keep draining so the child never blocks on a full pipe
        elif fd == 2:
            self.err += data
            if len(self.err) > self._tail:
                del self.err[: len(self.err) - self._tail]

    def process_exited(self) -> None:
        if not self.exited.done():
            self.exited.set_result(None)

    def connection_lost(self, exc) -> None:
        if not self.closed.done():
            self.closed.set_result(None)


def _end_async_tree(popen, job) -> None:
    """The tree kill for a child the event loop owns. POSIX: signal the group only (``Popen.kill``
    polls, and a poll here could reap the child under the loop's own child watcher)."""
    if os.name == "nt":
        _kill_tree(popen, job)
    else:
        try:
            os.killpg(popen.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass


async def run_bounded_async(
    argv: Sequence[str],
    *,
    timeout: float,
    max_bytes: int,
    stderr_tail_bytes: int = 64 * 1024,
    env: Mapping[str, str] | None = None,
    cwd: str | os.PathLike | None = None,
    input: str | bytes | None = None,
    no_window: bool = False,
) -> BoundedResult:
    """``run_bounded`` for a coroutine: the same arguments, result, limits and exceptions.

    The wait is on the child's exit, not on its pipes: a grandchild holding stdout is ended at the
    deadline like the child (``timed_out``). Cancelling the awaiting task ends the whole tree and
    re-raises ``CancelledError``. The pipes are closed before the call returns.
    """
    if isinstance(argv, (str, bytes)):
        raise TypeError("argv must be a list of arguments, not a string")
    argv = [os.fspath(a) for a in argv]
    if isinstance(input, str):
        input = input.encode("utf-8")
    elif input is not None and not isinstance(input, (bytes, bytearray, memoryview)):
        raise TypeError("input must be str or bytes")
    kwargs: dict = {}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | (
            getattr(subprocess, "CREATE_NO_WINDOW", 0) if no_window else 0)
    else:
        kwargs["start_new_session"] = True
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max(0.0, float(timeout))
    transport, proto = await loop.subprocess_exec(
        lambda: _AsyncCollector(loop, max_bytes, stderr_tail_bytes), *argv,
        stdin=subprocess.DEVNULL if input is None else subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env=dict(env) if env is not None else None, cwd=cwd, **kwargs)
    popen = transport.get_extra_info("subprocess")
    job = None
    if os.name == "nt":
        job = _new_job()
        if job is not None and not job[0].AssignProcessToJobObject(job[1], int(popen._handle)):
            job[0].CloseHandle(job[1])
            job = None
    timed_out = False
    try:
        if input is not None:
            stdin = transport.get_pipe_transport(0)
            try:
                stdin.write(bytes(input))  # buffered by the loop; a child that never reads still ends
                stdin.write_eof()
            except (OSError, RuntimeError):
                pass
        try:
            await asyncio.wait_for(asyncio.shield(proto.exited), max(0.0, deadline - loop.time()))
            # the child is gone; a grandchild may still hold a pipe: wait for EOF until the deadline only
            await asyncio.wait_for(asyncio.shield(proto.closed), max(0.0, deadline - loop.time()))
        except asyncio.TimeoutError:
            timed_out = True
        if timed_out:
            await loop.run_in_executor(None, _end_async_tree, popen, job)
            try:
                await asyncio.wait_for(asyncio.shield(proto.exited), _KILL_GRACE_SECONDS)
                await asyncio.wait_for(asyncio.shield(proto.closed), _JOIN_GRACE_SECONDS)
            except asyncio.TimeoutError:
                pass
    except BaseException:  # cancelled (or Ctrl+C) while waiting: the tree is not left behind
        if transport.get_returncode() is None or not proto.closed.done():
            _end_async_tree(popen, job)
        raise
    finally:
        if job is not None:
            if transport.get_returncode() is None or not proto.closed.done():
                job[0].TerminateJobObject(job[1], 1)
            job[0].CloseHandle(job[1])
        transport.close()
    return BoundedResult(
        returncode=transport.get_returncode(),
        stdout=bytes(proto.out),
        stderr_tail=bytes(proto.err),
        timed_out=timed_out,
        truncated=proto.truncated,
        stdout_total=proto.total,
    )
