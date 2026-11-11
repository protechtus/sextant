"""memory.frontmatter — return only the YAML frontmatter of a .memory/ file."""

import yaml

from ..config import MEMORY_ROOT
from .read import _safe_resolve


def memory_frontmatter(path: str) -> dict:
    """Return parsed YAML frontmatter for a .memory/ file.

    Returns {path, frontmatter, error}.
    """
    target = _safe_resolve(path)
    if target is None:
        return {"path": path, "frontmatter": {}, "error": "invalid path"}
    if not target.exists():
        return {"path": path, "frontmatter": {}, "error": "not found"}

    try:
        raw = target.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return {"path": path, "frontmatter": {}, "error": str(e)}

    if not raw.startswith("---"):
        return {"path": path, "frontmatter": {}, "error": None}

    end = raw.find("\n---", 3)
    if end == -1:
        return {"path": path, "frontmatter": {}, "error": None}

    try:
        fm = yaml.safe_load(raw[3:end].strip()) or {}
    except Exception as e:
        return {"path": path, "frontmatter": {}, "error": f"yaml parse error: {e}"}

    return {"path": path, "frontmatter": fm, "error": None}
