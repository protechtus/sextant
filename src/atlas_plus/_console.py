"""Console encoding for the CLI entry points.

Windows Python writes the ANSI code page (cp1252) to a pipe by default, so a captured path
with non-ASCII letters (``Jörg Müller``) is corrupted unless the reader guesses the code
page (AG1, pipeline sweep 2026-11-11). Emit UTF-8 whenever stdout is not a console, which
is what ``PYTHONUTF8=1`` would do; console output goes through the Unicode console API and
needs nothing.
"""
from __future__ import annotations

import os
import sys


def utf8_stdout_when_piped() -> None:
    if os.name != "nt":
        return
    for stream in (sys.stdout, sys.stderr):
        try:
            if stream is None or stream.isatty() or not hasattr(stream, "reconfigure"):
                continue
            if (stream.encoding or "").lower().replace("-", "") != "utf8":
                stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass
