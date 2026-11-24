"""One way to split a command line that a person typed into a setting or a flag.

POSIX ``shlex.split`` treats a backslash as an escape, so a Windows path loses every separator:
``C:\\Projects\\app\\run.exe`` came out as ``C:Projectsapprun.exe`` and the operator was told the program
was "not found" at a path they never typed (KL2, sweep 27; lesson LES-070). The verify command had
already learned this for ``--command``; a second caller had not. Every command line read from the
environment, a config file or an option goes through ``split_command``; guardrail G14 keeps
``shlex.split`` out of the rest of the package.
"""
from __future__ import annotations

import os
import re
import shlex

_DRIVE_PATH = re.compile(r'^\s*"?[A-Za-z]:[\\/]')


def split_windows(command: str) -> list[str]:
    """Split the way Windows itself does (CommandLineToArgvW), so a quoted name or ``--flag="a b"``
    survives; without ctypes (or off Windows), a non-POSIX shlex split with the quotes taken off."""
    try:
        import ctypes
        from ctypes import wintypes
        argc = ctypes.c_int()
        fn = ctypes.windll.shell32.CommandLineToArgvW
        fn.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_int)]
        fn.restype = ctypes.POINTER(wintypes.LPWSTR)
        argv = fn(command, ctypes.byref(argc))
        try:
            return [argv[i] for i in range(argc.value)]
        finally:
            ctypes.windll.kernel32.LocalFree(argv)
    except Exception:  # noqa: BLE001
        return [p[1:-1] if len(p) > 1 and p[0] == p[-1] == '"' else p for p in shlex.split(command, posix=False)]


def split_command(command: str, strict_quotes: bool = False) -> list[str]:
    """The argv of ``command``. On Windows, and for any text that starts with a drive path, backslashes
    are path separators and only double quotes group; elsewhere the POSIX shell rules apply. Blank text
    is an empty list (CommandLineToArgvW would return the running program for it).

    Raises ValueError on an unbalanced quote under the POSIX rules, and with ``strict_quotes`` also for
    an odd number of double quotes under the Windows rules (Windows itself accepts those in silence)."""
    if not command or not command.strip():
        return []
    windows_rules = os.name == "nt" or bool(_DRIVE_PATH.match(command))
    if not windows_rules:
        return shlex.split(command)
    if strict_quotes and command.count('"') % 2:
        raise ValueError("No closing quotation")
    if os.name == "nt":
        return split_windows(command)
    return [p[1:-1] if len(p) > 1 and p[0] == p[-1] == '"' else p for p in shlex.split(command, posix=False)]
