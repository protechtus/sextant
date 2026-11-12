"""Console encoding for the CLI entry points.

Windows Python writes the ANSI code page (cp1252) to a pipe by default and raises on a
character it cannot encode (six console scripts died on a Japanese fact piped through
``Select-Object``: AG17). Console output goes through the Unicode console API and needs nothing;
a PIPE is decoded by the parent shell with the console's output code page, so that is what we
emit (UTF-8 on a 65001 console or with no console at all), replacing what the page cannot hold.
"""
from __future__ import annotations

import os
import sys


def _console_output_encoding() -> str:
    """The encoding the parent shell decodes a pipe with.

    PowerShell 5.1 and cmd.exe decode a native command's output with the console's OUTPUT code
    page (``[Console]::OutputEncoding``), so a Japanese console (cp932) given UTF-8 bytes shows
    mojibake for every search hit and the pasted priming text (FN5, final readiness pass). With a
    console attached, emit its code page; 65001 is UTF-8. With no console at all (a hook run by
    an editor, a subprocess, CI) there is no code page to match and UTF-8 is the only sane answer.
    ``PYTHONIOENCODING`` still wins over both, as in any Python.
    """
    if os.environ.get("PYTHONIOENCODING"):
        return ""
    try:
        import ctypes
        cp = int(ctypes.windll.kernel32.GetConsoleOutputCP())  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        cp = 0
    if cp == 0:
        return "utf-8"
    if cp == 65001:
        return "utf-8"
    try:
        import codecs
        codecs.lookup(f"cp{cp}")
        return f"cp{cp}"
    except LookupError:
        return "utf-8"


def utf8_stdout_when_piped() -> None:
    if os.name != "nt":
        return
    enc = _console_output_encoding()
    if not enc:
        return
    for stream in (sys.stdout, sys.stderr):
        try:
            if stream is None or stream.isatty() or not hasattr(stream, "reconfigure"):
                continue
            if (stream.encoding or "").lower().replace("-", "") != enc.replace("-", ""):
                stream.reconfigure(encoding=enc, errors="replace")
        except Exception:  # noqa: BLE001
            pass
