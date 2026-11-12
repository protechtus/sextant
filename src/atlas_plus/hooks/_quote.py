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
    for line in text.splitlines():
        line = _MARKER_RE.sub(lambda m: "= " * (len(m.group(0)) - 1) + "=", line)
        line = _TAG_RE.sub("< ", line)
        out.append("> " + line if line.strip() else ">")
    return "\n".join(out)
