import importlib
import sys


def test_memory_server_package_import_does_not_preload_server_module():
    sys.modules.pop("atlas_plus.mcp.memory_server.server", None)
    sys.modules.pop("atlas_plus.mcp.memory_server", None)
    # Restore the live package graph afterwards: leaving a fresh package object beside the old
    # submodules desynchronised every later test that patches config/routing/fts (2026-11-09).
    saved_modules = {k: v for k, v in list(sys.modules.items()) if k.startswith("atlas_plus.mcp.memory_server")}

    importlib.import_module("atlas_plus.mcp.memory_server")

    assert "atlas_plus.mcp.memory_server.server" not in sys.modules


import pytest as _pytest


@_pytest.fixture(autouse=True, scope="module")
def _restore_memory_server_modules():
    """Snapshot the memory_server module graph before this file runs and put it back after."""
    import sys as _sys
    before = {k: v for k, v in list(_sys.modules.items()) if k.startswith("atlas_plus.mcp.memory_server")}
    yield
    for k in [k for k in _sys.modules if k.startswith("atlas_plus.mcp.memory_server")]:
        _sys.modules.pop(k, None)
    _sys.modules.update(before)
    # re-attach submodules as attributes of the restored package object
    pkg = before.get("atlas_plus.mcp.memory_server")
    if pkg is not None:
        for k, v in before.items():
            if k.count(".") == 3 and k.startswith("atlas_plus.mcp.memory_server."):
                setattr(pkg, k.rsplit(".", 1)[1], v)
