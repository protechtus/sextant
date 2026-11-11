"""P5-T5: Skills MCP tools — project .memory/runbooks/ as SKILL.md-shaped capabilities."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import yaml

from atlas_plus.mcp.memory_server.config import MEMORY_ROOT


def _parse_frontmatter(path: Path) -> dict:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
        if not text.startswith("---"):
            return {}
        end = text.find("---", 3)
        if end == -1:
            return {}
        return yaml.safe_load(text[3:end].strip()) or {}
    except Exception:
        return {}


def _first_sentence(path: Path) -> str:
    """Extract the first non-empty, non-heading line from the body as a description."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
        in_body = not text.startswith("---")
        if not in_body:
            end = text.find("---", 3)
            text = text[end + 3:] if end != -1 else text
        for line in text.splitlines():
            line = line.strip()
            if line and not line.startswith("#") and not line.startswith("---"):
                return line[:200]
    except Exception:
        pass
    return ""


def _split_body(path: Path) -> tuple[dict, str]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
        if not text.startswith("---"):
            return {}, text
        end = text.find("---", 3)
        if end == -1:
            return {}, text
        fm = yaml.safe_load(text[3:end].strip()) or {}
        body = text[end + 3:].strip()
        return fm, body
    except Exception:
        return {}, ""


def skills_list(memory_root: Optional[Path] = None) -> list[dict]:
    """List all runbooks as SKILL.md-shaped capability descriptors."""
    root = memory_root or MEMORY_ROOT
    runbooks_dir = root / "runbooks"
    if not runbooks_dir.exists():
        return []

    result = []
    for f in sorted(runbooks_dir.glob("*.md")):
        if f.name.startswith("_"):
            continue
        fm = _parse_frontmatter(f)
        result.append({
            "name": f.stem,
            "title": fm.get("title", f.stem),
            "type": "runbook",
            "description": fm.get("description") or _first_sentence(f),
            "updated": fm.get("updated"),
        })
    return result


def skills_invoke(name: str, memory_root: Optional[Path] = None) -> dict:
    """Return full runbook body for the agent to follow."""
    root = memory_root or MEMORY_ROOT
    runbooks_dir = root / "runbooks"

    # Always find via case-insensitive glob so we get the canonical filename
    # (Windows filesystem is case-insensitive; constructing the path directly
    # would give target.stem == the input casing, not the file's actual casing)
    matches = [f for f in runbooks_dir.glob("*.md") if f.stem.lower() == name.lower()]
    if not matches:
        available = [f.stem for f in runbooks_dir.glob("*.md") if not f.name.startswith("_")]
        return {"error": f"Skill '{name}' not found. Available: {available}"}
    target = matches[0].resolve()
    try:
        target.relative_to(runbooks_dir.resolve())
    except ValueError:
        return {"error": "Access denied."}

    fm, body = _split_body(target)

    return {
        "name": target.stem,
        "title": fm.get("title", target.stem),
        "body": body,
        "instruction": "Follow this runbook step by step. Do not skip steps or substitute alternatives unless the runbook explicitly permits it.",
    }
