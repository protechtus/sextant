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
    if os.environ.get("MSYSTEM"):
        return "utf-8"  # Git Bash / MSYS2 terminals decode UTF-8 whatever GetConsoleOutputCP says (MN36: every Japanese character was `?`)
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


def write_text_out(text: str) -> None:
    """Write a block of text to stdout: UTF-8 when piped (hooks, redirects), the console's code
    page when a Windows console is attached (MN36: `sextant context` printed raw UTF-8 bytes on a
    cp932 console while `search` followed the console)."""
    data = text.encode("utf-8")
    if os.name == "nt":
        try:
            if sys.stdout is not None and sys.stdout.isatty():
                enc = _console_output_encoding() or "utf-8"
                if enc.lower().replace("-", "") != "utf8":
                    data = text.encode(enc, errors="replace")
        except Exception:  # noqa: BLE001
            pass
    sys.stdout.buffer.write(data)
    sys.stdout.buffer.write(b"\n")
    sys.stdout.buffer.flush()


def fix_argv_surrogates() -> None:
    """A C/POSIX locale hands argv over as surrogate escapes; re-decode them as UTF-8 so every
    entry point gets the words the user typed (MN36: `atlas-prime` and `sextant-transcripts`
    crashed where `sextant` had been fixed)."""
    for i, a in enumerate(sys.argv):
        try:
            a.encode("utf-8")
        except UnicodeEncodeError:
            try:
                sys.argv[i] = a.encode("utf-8", "surrogateescape").decode("utf-8")
            except UnicodeDecodeError:
                pass


fix_argv_surrogates()  # at import: every console entry point imports this module


def utf8_stdout_when_piped() -> None:
    if os.name != "nt":
        # POSIX with a non-UTF-8 locale (LC_ALL=C and PYTHONUTF8=0): `sextant search` crashed with
        # UnicodeEncodeError on a Japanese note (AG33). Replace rather than raise.
        for stream in (sys.stdout, sys.stderr):
            try:
                if stream is not None and hasattr(stream, "reconfigure") and (stream.encoding or "").lower().replace("-", "") not in ("utf8",):
                    stream.reconfigure(errors="replace")
            except Exception:  # noqa: BLE001
                pass
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
