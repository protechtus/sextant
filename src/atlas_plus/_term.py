"""Text that reaches a terminal from the corpus (a note's path, a file name) shows its control
characters as escapes. Linux allows a newline or an ESC in a file name, so a crafted name printed
raw could fake a result line or recolour the console (MN88, SEX-048). Plain ASCII escapes."""
from __future__ import annotations

import unicodedata


# The joiners shape letters (Persian, Indic) and glue emoji sequences; they cannot reorder a line.
_KEEP_FORMAT = frozenset("\u200c\u200d")


def safe(text: object) -> str:
    """`text` with every control character (C0, DEL, C1, and the line/paragraph separators) and
    every format character (category Cf: U+202E RIGHT-TO-LEFT OVERRIDE, the U+2066-U+2069
    isolates, U+200B, U+FEFF) written as a backslash escape: a newline becomes the two characters
    `\\n`, U+202E becomes `\\u202e`, so a file name cannot reorder the line it is printed on
    (SEX-048). The zero-width joiners U+200C/U+200D stay: an emoji sequence prints whole."""
    s = str(text)
    if s.isprintable():
        return s
    out = []
    for c in s:
        cat = unicodedata.category(c)
        if cat in ("Cc", "Zl", "Zp") or (cat == "Cf" and c not in _KEEP_FORMAT):
            out.append(c.encode("unicode_escape").decode("ascii"))
        else:
            out.append(c)
    return "".join(out)
