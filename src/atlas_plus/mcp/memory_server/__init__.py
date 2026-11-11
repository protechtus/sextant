"""Atlas MCP memory server package."""

from importlib import import_module


def __getattr__(name: str):
    """Load the server module only when callers ask for it."""
    if name == "server":
        return import_module(f"{__name__}.server")
    if name == "mcp":
        return import_module(f"{__name__}.server").mcp
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = ["mcp", "server"]
