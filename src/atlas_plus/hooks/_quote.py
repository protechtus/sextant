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
# (ZWNJ/ZWJ and the LRM/RLM marks stay: Persian and Arabic text and emoji sequences need them; MN20)
_CONTROL_RE = re.compile("[\\x00-\\x08\\x0b\\x0c\\x0e-\\x1f\\x7f-\\x9f\u200b\u202a-\u202e\u2060-\u2064\u2066-\u2069\ufeff]")  # WJ and the invisible operators too (MN30)


def comment_gap(m: "re.Match[str]") -> str:
    """What replaces a stripped provenance comment: nothing at the end of a line, a space inline
    (sweep 13, AG29: `this <!-- source: x --> and` primed as `thisand`)."""
    s, b = m.string, m.end()
    at_line_end = b >= len(s) or s[b - 1] == "\n" or s[b] in "\r\n"
    return "" if at_line_end else " "


_TEMPLATE_LINES = (
    "# Your Name", "Introduce yourself to your own memory", "so write it the way you would brief",
    "- What you go by", "- How you like to work", "- Current focus:", "Replace this file's contents freely",
    "your display name in session context",
)


def identity_beyond_template(text: str) -> str:
    """What identity/user.md says once the sextant-init scaffold's own lines are dropped: the
    facts `remember --scope identity` appended under the template (MN30: the hook kept saying
    'not filled in' and never primed them)."""
    body = text or ""
    if body.lstrip().startswith("---"):
        end = body.find("\n---", 3)
        if end != -1:
            body = body[end + 4:]
    keep = [ln for ln in body.splitlines() if ln.strip() and not any(t in ln for t in _TEMPLATE_LINES)]
    return "\n".join(keep).strip()


def identity_is_template(text: str, name: str = "") -> bool:
    """True while identity/user.md still holds the sextant-init scaffold (one rule for the hook
    and for `sextant context`; MN20 found the two disagreeing)."""
    t = (text or "").lstrip()
    return (
        "Introduce yourself to your own memory" in t
        or t.startswith("# Your Name")
        or (name or "").strip() == "Your Name"
    )


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
