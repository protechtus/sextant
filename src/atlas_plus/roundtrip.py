"""Prove an install works: start the memory server over stdio and run the three-call round-trip.

Standard library only. Reachable three ways, so every kind of install has it:

    sextant verify                                   (installed command, any install)
    <python> -m atlas_plus.roundtrip --project demo  (the interpreter that has the package)
    <python> scripts/mcp_roundtrip.py                (from a clone; thin wrapper around this)

The calls are the ones the install runbook asks for:

    1. memory_health                      -> status ok
    2. memory_remember_tool               -> writes one fact under project:<name>
    3. memory_search_tool                 -> finds it again, with a confidence band

Exit code 0 when all three succeed, 1 otherwise. Prints each result as JSON so an agent or a
human can paste it into a report. Respects ATLAS_MEMORY_ROOT / ATLAS_STATE_DIR from the
environment exactly as the server does.

``--command`` runs the server through any launcher instead of ``<python> -m ...``, which is
how a Windows client reaches a WSL install (``--command "wsl -d Ubuntu --
/home/me/sextant/.venv/bin/python"``): the module and ``--stdio`` are appended.
"""
from __future__ import annotations

import argparse
import json
import os
import queue
import shlex
import subprocess
import sys
import tempfile
import threading
from datetime import date
from pathlib import Path

SERVER_MODULE = "atlas_plus.mcp.memory_server.server"


def server_command(python: str | None = None, command: str | None = None) -> list[str]:
    """The argv that starts the stdio server: a launcher prefix, or an interpreter."""
    if command:
        prefix = shlex.split(command, posix=(os.name != "nt"))
        if os.name == "nt":
            prefix = [p.strip('"') for p in prefix]
        return [*prefix, "-m", SERVER_MODULE, "--stdio"]
    return [python or sys.executable, "-m", SERVER_MODULE, "--stdio"]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="sextant verify",
        description="Start the memory server over stdio and prove a write and a search work.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--project", default="demo",
                    help="project name for the test fact (scope project:<name>)")
    ap.add_argument("--fact", default=None,
                    help="fact text to store (default mentions today's date)")
    ap.add_argument("--python", default=sys.executable,
                    help="interpreter to run the server with (default: this one)")
    ap.add_argument("--command", default=None,
                    help='launcher that reaches the interpreter, e.g. "wsl -d Ubuntu -- '
                         '/home/me/sextant/.venv/bin/python" (the module is appended)')
    ap.add_argument("--pause", type=float, default=0.0,
                    help="seconds to wait between calls (diagnostics)")
    ap.add_argument("--skip-remember", action="store_true",
                    help="diagnostics: search only, write nothing")
    args = ap.parse_args(argv)
    fact = args.fact or f"Sextant installed by an agent on {date.today().isoformat()}."

    cmd = server_command(args.python, args.command)
    # Server logs go to a file, never a pipe: a full pipe buffer would deadlock the server.
    errlog = tempfile.NamedTemporaryFile("w+", encoding="utf-8", suffix=".log", delete=False)
    try:
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=errlog,
                                text=True, encoding="utf-8", bufsize=1, env=os.environ.copy())
    except OSError as exc:
        print(json.dumps({"step": "start", "error": f"could not start {cmd[0]!r}: {exc}"}))
        print("ROUND-TRIP: FAILED")
        return 1
    next_id = 0
    lines: "queue.Queue[str]" = queue.Queue()
    skipped: list[str] = []

    def _pump() -> None:
        assert proc.stdout is not None
        while True:
            ln = proc.stdout.readline()
            if not ln:
                break
            lines.put(ln)
        lines.put("")  # EOF marker

    threading.Thread(target=_pump, daemon=True).start()

    def _stderr_tail() -> str:
        try:
            errlog.flush()
            return Path(errlog.name).read_text(encoding="utf-8", errors="replace")[-800:]
        except OSError:
            return ""

    def rpc(method: str, params: dict | None = None, notify: bool = False) -> dict | None:
        nonlocal next_id
        msg: dict = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        if not notify:
            next_id += 1
            msg["id"] = next_id
        assert proc.stdin is not None and proc.stdout is not None
        proc.stdin.write(json.dumps(msg) + "\n")
        proc.stdin.flush()
        if notify:
            return None
        timeout = int(os.environ.get("ATLAS_VERIFY_TIMEOUT", "240"))
        while True:
            try:
                line = lines.get(timeout=timeout)
            except queue.Empty:
                raise RuntimeError(f"skipped stdout lines: {skipped[-5:]}; no reply to {method} "
                                   f"within {timeout}s; server log tail: {_stderr_tail()}")
            if line == "":
                raise RuntimeError(f"server exited during {method}; log tail: {_stderr_tail()}")
            try:
                reply = json.loads(line)
            except json.JSONDecodeError:
                skipped.append(line[:200])  # a non-JSON line on stdout: a log or a stray print
                continue
            if reply.get("id") == next_id:
                return reply

    def call(tool: str, arguments: dict) -> dict:
        reply = rpc("tools/call", {"name": tool, "arguments": arguments})
        if reply is None or "error" in reply:
            raise RuntimeError(f"{tool} failed: {json.dumps(reply)[:600]}")
        content = reply["result"].get("content", [])
        text = next((c.get("text") for c in content if c.get("type") == "text"), None)
        try:
            return json.loads(text) if text else reply["result"]
        except json.JSONDecodeError:
            return {"raw": text}

    ok = True
    try:
        init = rpc("initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                                  "clientInfo": {"name": "sextant-verify", "version": "1"}})
        rpc("notifications/initialized", notify=True)
        server_name = (init or {}).get("result", {}).get("serverInfo", {}).get("name", "?")
        tools = rpc("tools/list") or {}
        names = [t["name"] for t in tools.get("result", {}).get("tools", [])]
        print(json.dumps({"step": "handshake", "server": server_name, "tool_count": len(names),
                          "command": cmd}))

        health = call("memory_health", {})
        print(json.dumps({"step": "memory_health", "result": health}))
        if health.get("status") != "ok":
            ok = False

        if not args.skip_remember:
            written = call("memory_remember_tool", {"scope": f"project:{args.project}", "fact": fact})
            print(json.dumps({"step": "memory_remember_tool", "result": written}))
            if written.get("ok") is False or written.get("error"):
                ok = False
        if args.pause:
            import time
            time.sleep(args.pause)

        found = call("memory_search_tool", {"query": fact.split(" on ")[0], "limit": 5})
        hits = found.get("results") or found.get("hits") or []
        print(json.dumps({"step": "memory_search_tool", "confidence": found.get("confidence"),
                          "hits": len(hits), "first": (hits[0] if hits else None)}))
        if not hits:
            ok = False
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"step": "error", "error": str(exc)[:800]}))
        ok = False
    finally:
        try:
            proc.stdin.close()  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001
            pass
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        errlog.close()
        try:
            os.unlink(errlog.name)
        except OSError:
            pass

    print("ROUND-TRIP: " + ("OK" if ok else "FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
