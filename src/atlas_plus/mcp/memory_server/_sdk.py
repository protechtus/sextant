"""The one place that imports the MCP SDK's server class (ATL-072, sweep 31 AG109).

mcp 2.x renamed `mcp.server.fastmcp.FastMCP` to `mcp.server.mcpserver.MCPServer`, and the old
module now raises ModuleNotFoundError on import. The server runs on both majors: the class is
imported here, and the few call sites whose arguments moved branch on SDK_MAJOR.
"""

try:
    from mcp.server.fastmcp import FastMCP  # mcp 1.x
    SDK_MAJOR = 1
except ModuleNotFoundError:
    from mcp.server.mcpserver import MCPServer as FastMCP  # mcp 2.x
    SDK_MAJOR = 2

__all__ = ["FastMCP", "SDK_MAJOR"]
