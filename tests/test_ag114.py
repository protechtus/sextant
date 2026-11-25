"""Sweep 33 AG114 (ATL-072): serverInfo.version is Sextant's version on MCP SDK 1.x and 2.x.

On 2.x the server reported an empty serverInfo.version (the SDK never substitutes its own); on
1.x it reported the SDK's version. Both tests run the real server: one over stdio in a child
interpreter (sys.executable, the way `sextant verify` starts it), one in-process.
"""

import json
import os
import sys
from pathlib import Path

from atlas_plus import _installed_version
from atlas_plus._proc import run_bounded

_SRC = str(Path(__file__).resolve().parents[1] / "src")


def _env(tmp_path: Path) -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith("ATLAS_")}
    for name in ("workspace", "state", "memory"):
        (tmp_path / name).mkdir(exist_ok=True)
    env["ATLAS_WORKSPACE_ROOT"] = str(tmp_path / "workspace")
    env["ATLAS_STATE_DIR"] = str(tmp_path / "state")
    env["ATLAS_MEMORY_ROOT"] = str(tmp_path / "memory")
    env["ATLAS_SEMANTIC_SEARCH"] = "off"
    env["ATLAS_EMBED_URL"] = "http://127.0.0.1:9"
    env["ATLAS_BRAIN_URL"] = "http://127.0.0.1:9"
    env["PYTHONPATH"] = _SRC + os.pathsep + env.get("PYTHONPATH", "")
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def test_stdio_initialize_reports_sextant_version(tmp_path):
    init = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                       "clientInfo": {"name": "ag114-test", "version": "0"}}}
    lines = json.dumps(init) + "\n"
    res = run_bounded([sys.executable, "-m", "atlas_plus.mcp.memory_server.server", "--stdio"],
                      timeout=90, max_bytes=1_000_000, env=_env(tmp_path), cwd=str(tmp_path),
                      input=lines)
    out = res.stdout_text()
    replies = [json.loads(x) for x in out.splitlines() if x.strip().startswith("{")]
    info = next(r["result"]["serverInfo"] for r in replies if r.get("id") == 1 and "result" in r)
    assert info["name"] == "sextant"
    assert info["version"], "serverInfo.version is empty"
    assert info["version"] == _installed_version()


def test_in_process_server_version_matches_package(monkeypatch, tmp_path):
    for key, value in _env(tmp_path).items():
        if key.startswith("ATLAS_"):
            monkeypatch.setenv(key, value)
    from atlas_plus.mcp.memory_server import _sdk
    server = _sdk.new_server("sextant-ag114-probe")
    low = getattr(server, "_lowlevel_server", None) or server._mcp_server
    assert low.create_initialization_options().server_version == _installed_version()
    assert _sdk.server_version() == _installed_version() != ""
