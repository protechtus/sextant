"""atlas://memory/{path} URI resource registration for the MCP server.

Exposes every .memory/ file as a discoverable resource so clients can
drag-and-drop or reference them without calling a tool.
"""

from pathlib import Path

from mcp.server.fastmcp import FastMCP

from atlas_plus.curation.scan import is_excluded_mirror

from .config import EXCLUDED_DIRS, MEMORY_ROOT
from .tools.read import memory_read


def register_resources(mcp: FastMCP) -> None:
    """Register dynamic resources on the FastMCP instance."""

    @mcp.resource("atlas://memory/{path}")
    def get_memory_resource(path: str) -> str:
        """Read a memory file by path relative to MEMORY_ROOT and return its body, with truncation notice if needed."""
        result = memory_read(path)
        if result.get("error"):
            return f"Error: {result['error']}"
        body = result.get("body", "")
        if result.get("truncated"):
            body += "\n\n[truncated — file exceeds 256 KB]"
        return body

    @mcp.resource("atlas://memory/")
    def list_memory_root() -> str:
        """Catalog of all .memory/ files."""
        lines = [f"# .memory/ catalog\n"]
        for p in sorted(MEMORY_ROOT.rglob("*.md")):
            parts = p.relative_to(MEMORY_ROOT).parts
            if parts and (parts[0] in EXCLUDED_DIRS or is_excluded_mirror(parts)):
                continue
            rel = str(p.relative_to(MEMORY_ROOT)).replace("\\", "/")
            lines.append(f"- atlas://memory/{rel}")
        return "\n".join(lines)
