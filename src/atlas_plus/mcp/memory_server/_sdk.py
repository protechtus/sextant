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


def server_version() -> str:
    """Sextant's installed version, for serverInfo.version (sweep 33 AG114).

    Same lookup and fallback as `atlas_plus.__version__` (sextant-ai, then atlas-plus, then the
    constant), so the two never disagree.
    """
    from atlas_plus import _installed_version
    return _installed_version()


def new_server(name: str, **kwargs):
    """Build the server with serverInfo.version set to Sextant's version on both SDK majors.

    2.x takes `version=` in the constructor and reports an empty string without it; 1.x has no
    such argument and reported the SDK's own version, so the low-level server's field is set.
    """
    if SDK_MAJOR >= 2:
        return FastMCP(name, version=server_version(), **kwargs)
    server = FastMCP(name, **kwargs)
    server._mcp_server.version = server_version()
    return server


__all__ = ["FastMCP", "SDK_MAJOR", "new_server", "server_version"]
