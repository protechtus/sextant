"""YAML frontmatter reader for intake classification.

Only the head of the file is read (``MAX_HEAD_BYTES``): a 400 MB markdown
export must not be loaded into memory just to learn its ``project`` key.
Anything that is not a mapping, or that fails to parse, yields ``{}`` so a
malformed header degrades to extension-based routing instead of failing
the whole run.
"""

from __future__ import annotations

from pathlib import Path

import yaml

MAX_HEAD_BYTES = 64 * 1024
FRONTMATTER_EXTENSIONS = {".md", ".markdown", ".mdx", ".txt", ".yaml", ".yml"}


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """Split ``text`` into (frontmatter mapping, body).

    The block must start on the first line with ``---`` and end with a line
    that is exactly ``---`` or ``...``. Returns ``({}, text)`` when absent or
    unparseable.
    """
    if not text.startswith("---"):
        return {}, text
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return {}, text
    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() in ("---", "..."):
            end = i
            break
    if end is None:
        return {}, text
    block = "".join(lines[1:end])
    try:
        data = yaml.safe_load(block)
    except yaml.YAMLError:
        return {}, text
    if not isinstance(data, dict):
        return {}, text
    # Keys are normalized to str so templates can address them predictably.
    return {str(k): v for k, v in data.items()}, "".join(lines[end + 1 :])


def read_frontmatter(path: Path) -> dict:
    """Frontmatter of ``path`` if its extension is text-like, else ``{}``."""
    if path.suffix.lower() not in FRONTMATTER_EXTENSIONS:
        return {}
    try:
        with path.open("rb") as f:
            head = f.read(MAX_HEAD_BYTES)
    except OSError:
        return {}
    text = head.decode("utf-8", errors="replace").lstrip("﻿")
    fm, _ = parse_frontmatter(text)
    return fm


def scalar(value, limit: int = 200) -> str:
    """Render a frontmatter value as a short, single-line string."""
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        text = ", ".join(scalar(v, limit) for v in value)
    elif isinstance(value, dict):
        text = ", ".join(f"{k}={scalar(v, limit)}" for k, v in value.items())
    else:
        text = str(value)
    text = " ".join(text.split())
    return text[:limit]
