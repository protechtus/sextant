"""Quote corpus text before it is printed into a priming block.

A note is the user's own data. Printed raw, a note could close the hook's block with its own
`=== END ATLAS CONTEXT ===` line, open a fake one, or carry `<system>`-shaped tags that read
like the tool speaking (AG16, sweep 11). Every line of corpus-derived text is therefore
prefixed as a quotation, block markers inside it are defused, and tag-shaped text gets a space
after the angle bracket so it cannot parse as a tag. The words stay readable.
"""
from __future__ import annotations

import re

_MARKER_RE = re.compile(r"={3,}")
_TAG_RE = re.compile(r"<(?=/?[A-Za-z!?])")
# C0/C1 controls except tab (an ESC[2J in a decision cleared the screen under `sextant context`).
# ... plus the invisible format characters a note can use to reorder or hide what the human
# reads: zero-width space/joiners, bidi embeddings and overrides, isolates, BOM (sweep 13, AG29).
_CONTROL_RE = re.compile("[\\x00-\\x08\\x0b\\x0c\\x0e-\\x1f\\x7f-\\x9f\u200b-\u200f\u202a-\u202e\u2060-\u2064\u2066-\u2069\ufeff]")


def comment_gap(m: "re.Match[str]") -> str:
    """What replaces a stripped provenance comment: nothing at the end of a line, a space inline
    (sweep 13, AG29: `this <!-- source: x --> and` primed as `thisand`)."""
    s, a, b = m.string, m.start(), m.end()
    at_line_start = a == 0 or s[a - 1] == "\n"
    at_line_end = b >= len(s) or s[b - 1] == "\n" or s[b] == "\n"
    return "" if at_line_end else " "


def one_line(text: str) -> str:
    """One defused line without the quote prefix, for list items (ticket titles, timestamps)."""
    parts = []
    for line in str(text).splitlines():
        q = quote_note(line)
        parts.append(q[2:] if q.startswith("> ") else q.lstrip(">"))
    return " ".join(" ".join(parts).split())


def quote_note(text: str) -> str:
    """Prefix every line with `> ` and defuse block markers and tag-shaped text."""
    if not text:
        return text
    out = []
    for line in _CONTROL_RE.sub("", text).splitlines():
        line = _MARKER_RE.sub(lambda m: "= " * (len(m.group(0)) - 1) + "=", line)
        line = _TAG_RE.sub("< ", line)
        out.append("> " + line if line.strip() else ">")
    return "\n".join(out)
