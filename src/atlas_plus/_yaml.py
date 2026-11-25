"""One YAML loader for frontmatter: libyaml when present (about seven times faster on a
5,000-note corpus; AG32, sweep 14), the pure loader when a document trips the C loader (a tab
inside the frontmatter, sweep 12).
"""

from __future__ import annotations

import yaml

_FAST = getattr(yaml, "CSafeLoader", yaml.SafeLoader)


def fast_safe_load(text):
    """``yaml.safe_load`` with the C loader first and the pure loader as the fallback."""
    if _FAST is yaml.SafeLoader:
        return yaml.safe_load(text)
    try:
        return yaml.load(text, Loader=_FAST)
    except yaml.YAMLError:
        return yaml.safe_load(text)


def load_mapping(text, *, fast=True):
    """A note's frontmatter block as a dict (AG119, sweep 35): a top block that is not a mapping
    (a sentence under a rule line, a list, a number, ``true``) is no frontmatter, ``{}``, so the
    caller's ``.get`` cannot raise. ``fast=False`` keeps the pure loader for callers that used
    ``yaml.safe_load``. A block that does not parse raises ``yaml.YAMLError`` as before."""
    data = fast_safe_load(text) if fast else yaml.safe_load(text)
    return data if isinstance(data, dict) else {}


def load_mapping_or_none(text, *, fast=True):
    """Like ``load_mapping`` for a caller that must tell the two apart: ``None`` when the block is
    not a mapping (the caller treats it as text), ``{}`` for an empty block."""
    data = fast_safe_load(text) if fast else yaml.safe_load(text)
    if data is None:
        return {}
    return data if isinstance(data, dict) else None
