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
