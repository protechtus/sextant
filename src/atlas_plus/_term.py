"""Text that reaches a terminal from the corpus (a note's path, a file name) shows its control
characters as escapes. Linux allows a newline or an ESC in a file name, so a crafted name printed
raw could fake a result line or recolour the console (MN88, SEX-048). Plain ASCII escapes."""
from __future__ import annotations

import unicodedata


def safe(text: object) -> str:
    """`text` with every control character (C0, DEL, C1, and the line/paragraph separators)
    written as a backslash escape: a newline becomes the two characters `\\n`."""
    s = str(text)
    if s.isprintable():
        return s
    out = []
    for c in s:
        if unicodedata.category(c) in ("Cc", "Zl", "Zp"):
            out.append(c.encode("unicode_escape").decode("ascii"))
        else:
            out.append(c)
    return "".join(out)
