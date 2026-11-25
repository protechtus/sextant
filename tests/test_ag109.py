"""AG109 (sweep 31, ATL-072): the MCP server runs on the SDK it finds, 1.x or 2.x.

mcp 2.x renamed FastMCP to MCPServer (the old module raises ModuleNotFoundError) and moved the
Host policy from the constructor to the HTTP app. These tests run the server's own paths on
whichever SDK is installed: the tools are listed, a call is answered, and over HTTP a request
without the key is refused while one with it gets an answer. Nothing here names an SDK version.
"""

import json

import anyio
import pytest

_KEY = "ag109-" + "k" * 12 + "-" + "z" * 6  # assembled at runtime, 25 characters


def _server():
    from atlas_plus.mcp.memory_server import server
    return server


def _tool_names(tools) -> set[str]:
    return {getattr(t, "name", None) or t.get("name") for t in tools}


def test_mcp_server_lists_its_tools_on_the_installed_sdk():
    names = _tool_names(anyio.run(_server().mcp.list_tools))
    assert {"memory_search_tool", "memory_remember_tool", "memory_health"} <= names


def test_mcp_server_answers_a_tool_call_on_the_installed_sdk():
    server = _server()

    async def _call():
        return await server.mcp.call_tool("now_state_tool", {"zones": ["UTC"]})

    result = anyio.run(_call)
    text = json.dumps(result, default=lambda o: getattr(o, "model_dump", lambda: str(o))())
    assert "as_of_utc" in text


def _rpc(client, payload, key=None, session=None):
    headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
    if key:
        headers["Authorization"] = "Bearer " + key
    if session:
        headers["mcp-session-id"] = session
    return client.post("/mcp", headers=headers, content=json.dumps(payload))


def _body(resp):
    if "text/event-stream" in resp.headers.get("content-type", ""):
        for line in resp.text.splitlines():
            if line.startswith("data:"):
                return json.loads(line[5:])
        return {}
    return resp.json() if resp.text else {}


def test_mcp_http_app_refuses_a_missing_key_and_answers_the_right_one(monkeypatch):
    starlette_testclient = pytest.importorskip("starlette.testclient")
    monkeypatch.delenv("ATLAS_MCP_HOST", raising=False)
    monkeypatch.delenv("ATLAS_MCP_ALLOWED_HOSTS", raising=False)
    monkeypatch.setenv("ATLAS_NTFY_URL", "http://127.0.0.1:9")
    server = _server()
    app = server._build_http_app(_KEY)
    # the Host a loopback client sends; the SDK's DNS-rebinding guard refuses `testserver`
    with starlette_testclient.TestClient(app, base_url="http://127.0.0.1:9651") as client:
        init = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "ag109", "version": "0"}}}
        refused = _rpc(client, init)
        assert refused.status_code == 401
        assert "Authorization: Bearer" in refused.text

        ok = _rpc(client, init, key=_KEY)
        assert ok.status_code == 200, ok.text[:300]
        assert _body(ok)["result"]["serverInfo"]["name"] == "sextant"
        session = ok.headers.get("mcp-session-id")
        _rpc(client, {"jsonrpc": "2.0", "method": "notifications/initialized"}, key=_KEY, session=session)
        listed = _rpc(client, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, key=_KEY, session=session)
        assert listed.status_code == 200
        names = {t["name"] for t in _body(listed)["result"]["tools"]}
        assert "memory_search_tool" in names
