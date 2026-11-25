"""Prove an install works: start the memory server over stdio and run the three-call round-trip.

Standard library only. Reachable three ways, so every kind of install has it:

    sextant verify                                   (installed command, any install)
    <python> -m atlas_plus.roundtrip --project demo  (the interpreter that has the package)
    <python> scripts/mcp_roundtrip.py                (from a clone; thin wrapper around this)

The calls are the ones the install runbook asks for:

    1. memory_health                      -> status ok
    2. memory_remember_tool               -> writes one fact under project:<name>
    3. memory_search_tool                 -> finds THAT fact again (the hit must name the
                                             file the write returned), with a confidence band

Exit code 0 when all three succeed, 1 otherwise. Prints each result as JSON so an agent or a
human can paste it into a report. Respects ATLAS_MEMORY_ROOT / ATLAS_STATE_DIR from the
environment exactly as the server does. Each run appends one more dated fact under
project:<name>; `--skip-remember` only searches and reports the hit count without failing.

``--command`` runs the server through any launcher instead of ``<python> -m ...``, which is
how a Windows client reaches a WSL install (``--command "wsl -d Ubuntu --
/home/me/sextant/.venv/bin/python"``): the module and ``--stdio`` are appended.

``--url http://host:8765/mcp --token <token>`` proves an HTTP install from the client machine
(the same three calls over Streamable HTTP; nothing is started here).

``--env NAME=VALUE`` (repeatable) gives the server the variables the registration bakes in
with ``-e``; with a ``wsl`` launcher the names are also added to WSLENV so they cross into the
distro. Without it a WSL server started from Windows reads the distro's ``~/.memory`` and the
round-trip proves the wrong corpus (AG3, pipeline sweep 2026-11-11).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import queue
import subprocess
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from datetime import date, datetime
from pathlib import Path

SERVER_MODULE = "atlas_plus.mcp.memory_server.server"


from atlas_plus import _cmdline  # noqa: E402  the one command-line splitter (LES-070, guardrail G14)

_split_windows = _cmdline.split_windows  # kept under its old name for callers that import it


def server_command(python: str | None = None, command: str | None = None) -> list[str]:
    """The argv that starts the stdio server: a launcher prefix, or an interpreter.

    Raises ValueError (with the offending text) on an unbalanced quote.
    """
    if command is not None:
        if not command.strip():
            raise ValueError("--command is empty")  # CommandLineToArgvW turns "" into the current exe
        try:
            prefix = _cmdline.split_command(command)
        except ValueError as exc:
            raise ValueError(f"--command could not be split ({exc}): {command!r}") from exc
        if not prefix:
            raise ValueError("--command is empty")
        # The whole registered command pasted in (`<python> -m <module> --stdio`) is accepted:
        # the module is appended once, not twice (sweep 13, AG29).
        for _ in range(4):
            if prefix and prefix[-1] == "--stdio":
                prefix = prefix[:-1]
            elif len(prefix) >= 2 and prefix[-2:] == ["--transport", "stdio"]:
                prefix = prefix[:-2]
            elif len(prefix) >= 2 and prefix[-2:] == ["-m", SERVER_MODULE]:
                prefix = prefix[:-2]
            else:
                break
        if not prefix:
            raise ValueError("--command needs the interpreter or launcher in front of the server module")
        launcher = os.path.basename(prefix[-1]).lower()
        if launcher.startswith(("sextant-mcp", "atlas-mcp")):
            return [*prefix, "--stdio"]  # a console script already is the server
        return [*prefix, "-m", SERVER_MODULE, "--stdio"]
    return [python or sys.executable, "-m", SERVER_MODULE, "--stdio"]


def _is_wsl_launcher(exe: str) -> bool:
    return exe.replace("\\", "/").rsplit("/", 1)[-1].lower() in ("wsl", "wsl.exe")


def _wslenv_with(existing: str, names: list[str]) -> str:
    """WSLENV with ``names`` appended: empty entries dropped (a trailing colon is common),
    nothing listed twice (an entry may carry a flag suffix such as ``NAME/u``)."""
    parts = [p for p in existing.split(":") if p]
    present = {p.split("/", 1)[0] for p in parts}
    for n in names:
        if n not in present:
            parts.append(n)
            present.add(n)
    return ":".join(parts)


def _decode_log(data: bytes) -> str:
    """wsl.exe writes its own errors as UTF-16; everything else is UTF-8."""
    if b"\x00" in data[:200]:
        return data.decode("utf-16-le", errors="replace")
    return data.decode("utf-8", errors="replace")


def _sse_payloads(content_type: str, body: str) -> list[str]:
    """The JSON texts in an HTTP reply: one per `data:` line of an event stream, else the body."""
    if "text/event-stream" in (content_type or ""):
        return [ln[5:].strip() for ln in body.splitlines() if ln.startswith("data:")]
    return [body] if body.strip() else []


def _cert_file_problem(path: str) -> str:
    """Why SSL_CERT_FILE cannot be a CA bundle, or '' when it looks like one."""
    p = Path(path)
    if not p.exists():
        return "does not exist"
    if p.is_dir():
        return "is a directory"
    try:
        head = p.read_bytes()[:1_000_000]
    except OSError as exc:
        return f"cannot be read ({type(exc).__name__})"
    if not head.strip():
        return "is empty"
    if (b"-----BEGIN " + b"CERTIFICATE") not in head:
        return "holds no PEM certificate"
    return ""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        return None  # a 3xx surfaces as HTTPError and is named; the token is never sent elsewhere (MN53)


_OPENER = urllib.request.build_opener(_NoRedirect())


def _http_rpc(url: str, token: str, timeout: int, skipped: list[str]):
    """A JSON-RPC sender over Streamable HTTP (one POST per message, the session id echoed),
    so an HTTP install can be proven from the client machine (AG4, pipeline sweep 2026-11-11)."""
    import urllib.error
    import urllib.request
    state = {"next_id": 0, "session": None}

    def _headers(extra: dict | None = None) -> dict:
        h = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
        if token:
            h["Authorization"] = f"Bearer {token}"
        if state["session"]:
            h["mcp-session-id"] = state["session"]
        h.update(extra or {})
        return h

    def rpc(method: str, params: dict | None = None, notify: bool = False) -> dict | None:
        msg: dict = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        if not notify:
            state["next_id"] += 1
            msg["id"] = state["next_id"]
        req = urllib.request.Request(url, data=json.dumps(msg).encode("utf-8"), headers=_headers(),
                                     method="POST")
        try:
            with _OPENER.open(req, timeout=timeout) as resp:  # never follows a redirect: urllib carried the bearer token to the redirect target (MN53)
                sid = resp.headers.get("mcp-session-id")
                if sid:
                    state["session"] = sid
                ctype = resp.headers.get("Content-Type", "")
                body = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            try:
                detail = exc.read().decode("utf-8", errors="replace")[:300]
            except OSError:
                detail = ""  # the server closed after the status line: macOS raised ConnectionResetError past this handler (CI 43655783457)
            hint = ""
            if exc.code == 401:
                hint = " (token missing or wrong: --token or ATLAS_MCP_TOKEN)"
            elif exc.code == 429:
                hint = " (this source is locked out after failed attempts; wait, or see ATLAS_MCP_TRUSTED_PEERS)"
            elif exc.code == 404:
                hint = " (the MCP path is /mcp, without a trailing slash)" if url.rstrip("/") != url else " (the MCP path is /mcp)"
            elif exc.code in (301, 302, 303, 307, 308) and url.startswith("http://") and (exc.headers.get("Location") or "").startswith("https://"):
                _loc = exc.headers.get("Location") or ""
                hint = f" (the server redirects plain HTTP to HTTPS ({_loc[:120]}); the token was not sent there: use the https:// URL of this server, with its TLS port)"  # MN65: the port was kept from the http URL
            elif exc.code in (301, 302, 303, 307, 308):
                _loc = (exc.headers.get("Location") or "?")[:120]
                hint = f" (the server redirected to {_loc}; the token was not sent there: check the URL, the MCP path is /mcp without a trailing slash)"
            elif exc.code == 405 and __import__("urllib.parse").parse.urlsplit(url).path in ("", "/"):
                hint = f" (this is the server root: the MCP endpoint is {url.rstrip('/')}/mcp)"  # AG71
            elif exc.code >= 500 and "<html" in detail.lower():
                hint = " (this does not look like an MCP server: the reply is an HTML page)"
                detail = detail.split("\n", 1)[0][:80]
            raise RuntimeError(f"{method}: HTTP {exc.code}{hint}" + (f": {detail}" if detail.strip() else "")) from exc  # no dangling colon on an empty body (AG65)
        except (urllib.error.URLError, OSError) as exc:
            _s = str(exc)
            if "WRONG_VERSION_NUMBER" in _s or "record layer failure" in _s:  # OpenSSL words differ per build (MN53: Windows says record layer failure)
                raise RuntimeError(f"{method}: {url} answered plain HTTP to a TLS handshake: use http:// for this server, or put it behind a TLS proxy") from exc  # AG56
            if isinstance(exc, (FileNotFoundError, PermissionError, IsADirectoryError)) or "NO_CERTIFICATE_OR_CRL_FOUND" in _s or "PEM lib" in _s:
                _cf = os.environ.get("SSL_CERT_FILE", "")
                raise RuntimeError(f"{method}: the certificate bundle {_cf!r} (SSL_CERT_FILE) cannot be read as a PEM certificate file ({type(exc).__name__}): point it at the hub's CA certificate, or unset it") from exc  # MN59: a missing bundle was called an untrusted certificate
            if "UNEXPECTED_EOF" in _s or "EOF occurred" in _s:
                raise RuntimeError(f"{method}: {url} closed the connection during the TLS handshake: the port may not speak TLS at all (a plain-HTTP server or a proxy without TLS on that port), or it is the wrong port") from exc  # MN55: an EOF is not an alert
            if "[SSL" in _s and "CERTIFICATE" not in _s.upper():
                from urllib.parse import urlsplit as _us
                _host = _us(url).hostname or ""
                _advice = "use the host name the server's certificate names, not its IP address" if _host.replace(".", "").replace(":", "").isdigit() or ":" in _host else "the server refused this client's handshake: check the host name against the certificate and the TLS versions it allows"
                raise RuntimeError(f"{method}: {url} refused the TLS handshake with an alert ({_s.splitlines()[0][:100]}): {_advice}") from exc  # AG65: an alert was called plain HTTP; MN55: IP advice only for an IP
            if "CERTIFICATE_VERIFY_FAILED" in _s or "certificate verify failed" in _s.lower():
                _cf = os.environ.get("SSL_CERT_FILE", "")
                if _cf:
                    _why = _cert_file_problem(_cf)
                    if _why:  # the variable was set to a broken file and verify said to set it (AG71)
                        raise RuntimeError(f"{method}: SSL_CERT_FILE is {_cf!r}, which {_why}: point it at the CA certificate (a PEM file), or unset it") from exc
                raise RuntimeError(f"{method}: {url} presented a certificate this client does not trust (a private CA or `tls internal`): point SSL_CERT_FILE at the CA certificate") from exc
            if isinstance(exc, ConnectionResetError) or "10054" in _s or "forcibly closed" in _s or "Connection reset" in _s:
                _alt = ("https://" + url[len("http://"):]) if url.startswith("http://") else url
                raise RuntimeError(f"{method}: {url} reset the connection: if this port is a TLS proxy (Caddy, nginx), use {_alt}") from exc  # a raw WinError 10054 (AG71)
            raise RuntimeError(f"{method}: could not reach {url}: {exc} (is the server running there? in a container it must "
                               f"bind ATLAS_MCP_HOST=0.0.0.0; it takes about 8 s to start)") from exc
        if notify:
            return None
        for text in _sse_payloads(ctype, body):
            try:
                reply = json.loads(text)
            except json.JSONDecodeError:
                skipped.append(text[:200])
                continue
            if (isinstance(reply, dict) and reply.get("id") == state["next_id"] and "method" not in reply
                    and ("result" in reply or "error" in reply)):
                return reply
            skipped.append(text[:200])
        raise RuntimeError(f"no reply to {method} in the HTTP response ({ctype!r}); skipped: {skipped[-3:]}")

    def close() -> None:
        if not state["session"]:
            return
        try:
            req = urllib.request.Request(url, headers=_headers(), method="DELETE")
            urllib.request.urlopen(req, timeout=10).close()
        except Exception:  # noqa: BLE001
            pass

    return rpc, close


_JSON_LINES = False
"""True under `--json-lines` (and inside `--json`): each step as the raw JSON object, one per line, the
output before SEX-052 item 3. The default is one plain line per step."""


def _plain(obj: dict) -> str:
    """One readable line for a step object (the default output; `--json-lines` keeps the raw objects)."""
    def show(v) -> str:
        return v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)

    step = obj.get("step", "?")
    result = obj.get("result") if isinstance(obj.get("result"), dict) else None
    if step == "handshake":
        where = (f"url {obj['url']}" if "url" in obj
                 else "command " + " ".join(show(c) for c in obj.get("command") or []))
        return f"handshake: server {obj.get('server')}, {obj.get('tool_count')} tools, {where}"
    if step == "memory_health" and result is not None:
        extra = {k: v for k, v in result.items() if k not in ("status", "memory_root", "memory_root_exists")}
        return (f"memory_health: status {result.get('status')}, memory_root {result.get('memory_root')}"
                + (f", {show(extra)}" if extra and result.get("status") != "ok" else ""))
    if step == "memory_remember_tool" and result is not None:
        if result.get("ok") is False or result.get("error"):
            return f"memory_remember_tool: FAILED, {show(result.get('error') or result)}"
        again = " (already remembered)" if result.get("already_remembered") else ""
        return f"memory_remember_tool: ok, file {result.get('file')}{again}"
    if step == "memory_search_tool":
        first = obj.get("first") or {}
        found = {True: ", found this write", False: ", this write NOT found", None: ""}[obj.get("found_this_write")]
        return (f"memory_search_tool: confidence {obj.get('confidence')}, {obj.get('hits')} hit(s){found}"
                + (f", first {first.get('path')}" if first.get("path") else ""))
    return f"{step}: " + ", ".join(f"{k} {show(v)}" for k, v in obj.items() if k != "step")


def _emit(obj: dict) -> None:
    print(json.dumps(obj) if _JSON_LINES else _plain(obj))


def _run_calls(rpc, args, fact: str, where: dict) -> bool:
    """The handshake and the three calls; prints one line per step; True when all passed."""
    ok = True

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

    init = rpc("initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                              "clientInfo": {"name": "sextant-verify", "version": "1"}})
    rpc("notifications/initialized", notify=True)
    server_name = (init or {}).get("result", {}).get("serverInfo", {}).get("name", "?")
    tools = rpc("tools/list") or {}
    names = [t["name"] for t in tools.get("result", {}).get("tools", [])]
    _emit({"step": "handshake", "server": server_name, "tool_count": len(names), **where})

    health = call("memory_health", {})
    _emit({"step": "memory_health", "result": health})
    if health.get("status") != "ok":
        ok = False

    written_file = None
    if not args.skip_remember:
        written = call("memory_remember_tool", {"scope": f"project:{args.project}", "fact": fact})
        _emit({"step": "memory_remember_tool", "result": written})
        if written.get("ok") is False or written.get("error"):
            ok = False
        written_file = written.get("file")
    if args.pause:
        import time
        time.sleep(args.pause)

    found = call("memory_search_tool", {"query": fact.split(" on ")[0], "limit": 10})
    hits = found.get("results") or found.get("hits") or []
    # The proof is that THIS write came back: a hit whose path is the file the write named
    # (an older identical fact from a previous run must not pass for it).
    this_hit = None
    if written_file:
        want = str(written_file).replace("\\", "/").lower()
        for h in hits:
            p = str(h.get("path", "")).replace("\\", "/").lower()
            if p and want.endswith(p):
                this_hit = h
                break
    _emit({"step": "memory_search_tool", "confidence": found.get("confidence"),
                      "hits": len(hits), "found_this_write": bool(this_hit) if written_file else None,
                      "first": (this_hit or (hits[0] if hits else None))})
    if written_file and not this_hit:
        ok = False
    return ok


def _timeout_seconds() -> int:
    raw = os.environ.get("ATLAS_VERIFY_TIMEOUT", "").strip()
    try:
        value = int(float(raw)) if raw else 240
    except ValueError:
        value = 240
    return value if value > 0 else 240


def main(argv: list[str] | None = None) -> int:
    """`--json`: one object {"ok": bool, "steps": [...]} instead of JSON lines plus a prose
    line, for installer agents (AG24, sweep 12). `--json-lines`: one JSON object per step, then the
    verdict line; the default is one plain line per step, then the verdict (SEX-052 item 3)."""
    global _JSON_LINES
    args_list = list(sys.argv[1:] if argv is None else argv)
    if "--json" in args_list:
        import contextlib
        import io
        args_list = [a for a in args_list if a not in ("--json", "--json-lines")]
        buf = io.StringIO()
        _JSON_LINES = True
        try:
            with contextlib.redirect_stdout(buf):
                rc = _main_lines(args_list)
        finally:
            _JSON_LINES = False
        steps = []
        for line in buf.getvalue().splitlines():
            line = line.strip()
            if line.startswith("{"):
                try:
                    steps.append(json.loads(line))
                except ValueError:
                    steps.append({"step": "text", "text": line})
            elif line:
                steps.append({"step": "text", "text": line})
        print(json.dumps({"ok": rc == 0, "steps": steps}, indent=2))
        return _say_next(rc, args_list)
    _JSON_LINES = "--json-lines" in args_list
    try:
        return _say_next(_main_lines([a for a in args_list if a != "--json-lines"]), args_list)
    finally:
        _JSON_LINES = False


def _say_next(rc: int, argv: list[str]) -> int:
    """`ROUND-TRIP: FAILED` was the last line, its cause in a JSON line above it (LES-054, AG102):
    end on stderr with what to type next."""
    if rc != 1:
        return rc
    from atlas_plus import _next_step
    from atlas_plus import paths as _paths

    # Only this machine's own default server reads this corpus: --url, --command or an --env root point elsewhere.
    local = not any(a.split("=")[0] in ("--url", "--command") or "ATLAS_MEMORY_ROOT=" in a for a in argv)
    if local and not _paths.memory_root().is_dir():
        step = (f"no corpus yet at {_paths.memory_root()}: run {_next_step.command('init')}, "
                f"then {_next_step.command('verify')} again.{_next_step.corpus_elsewhere()}")
    else:
        step = (f"the step lines above say what failed; {_next_step.command('doctor')} checks the install "
                "and gives a Fix line for each row that fails.")  # the doc section it named does not exist (SEX-047 row 7)
    _next_step.say("verify: the round trip did not complete.", step)
    return rc


def _main_lines(argv: list[str]) -> int:
    from atlas_plus import _next_step  # usage errors name --help (LES-054)
    ap = _next_step.ArgumentParser(
        prog="sextant verify",
        description="Start the memory server over stdio and prove a write and a search work.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--project", default="demo",
                    help="project name for the test fact (scope project:<name>); each run appends one fact")
    ap.add_argument("--fact", default=None,
                    help="fact text to store (default mentions today's date)")
    ap.add_argument("--python", default=sys.executable,
                    help="interpreter to run the server with (default: this one)")
    ap.add_argument("--command", default=None,
                    help='launcher that reaches the interpreter, e.g. "wsl -d Ubuntu -- '
                         '/home/me/sextant/.venv/bin/python" (the module is appended)')
    ap.add_argument("--url", default=None,
                    help="prove an HTTP install instead: the MCP endpoint, e.g. http://host:8765/mcp "
                         "(no server is started here)")
    ap.add_argument("--token", default=None,
                    help="bearer token for --url (default: ATLAS_MCP_TOKEN from the environment)")
    ap.add_argument("--env", action="append", default=[], metavar="NAME=VALUE",
                    help="variable for the server, repeatable (the registration's -e values); with a "
                         "wsl launcher the names are added to WSLENV so they cross into the distro")
    ap.add_argument("--pause", type=float, default=0.0,
                    help="seconds to wait between calls (diagnostics)")
    ap.add_argument("--json", action="store_true",
                    help="one JSON object {ok, steps} instead of the step lines and the verdict")
    ap.add_argument("--json-lines", action="store_true",
                    help="each step as one JSON object per line, then the ROUND-TRIP verdict line "
                         "(the default prints one plain line per step)")
    ap.add_argument("--skip-remember", action="store_true",
                    help="diagnostics: search only, write nothing, report the hit count without failing")
    args = ap.parse_args(argv)
    fact = args.fact or f"Sextant install verified on {date.today().isoformat()} at {datetime.now():%H:%M:%S} (sextant verify)."

    if args.url and not args.url.lower().startswith(("http://", "https://")):
        if "://" not in args.url:
            print(f"verify: the URL has no scheme: use https://{args.url} behind a TLS proxy, or http://{args.url} on a trusted network", file=sys.stderr)  # MN53, AG65
        else:
            print(f"verify: only http:// and https:// URLs are supported (got {args.url.split('://', 1)[0]}://)", file=sys.stderr)  # an ftp:// URL hung (AG56)
        return 2
    if args.url:
        skipped_http: list[str] = []
        token = args.token if args.token is not None else os.environ.get("ATLAS_MCP_TOKEN", "")
        rpc_http, close_http = _http_rpc(args.url, token, _timeout_seconds(), skipped_http)
        try:
            ok = _run_calls(rpc_http, args, fact, {"url": args.url, "auth": bool(token)})
        except Exception as exc:  # noqa: BLE001
            _emit({"step": "error", "error": str(exc)[:800]})
            ok = False
        finally:
            close_http()
        print("ROUND-TRIP: " + ("OK" if ok else "FAILED"))
        return 0 if ok else 1

    try:
        cmd = server_command(args.python, args.command)
    except ValueError as exc:
        _emit({"step": "start", "error": str(exc)})
        print("ROUND-TRIP: FAILED")
        return 1
    env = os.environ.copy()
    extra: dict[str, str] = {}
    for item in args.env:
        if "=" not in item:
            _emit({"step": "start", "error": f"--env needs NAME=VALUE, got {item!r}"})
            print("ROUND-TRIP: FAILED")
            return 1
        k, v = item.split("=", 1)
        extra[k] = v
    env.update(extra)
    if extra and _is_wsl_launcher(cmd[0]):
        env["WSLENV"] = _wslenv_with(env.get("WSLENV", ""), list(extra))

    # Server logs go to a file, never a pipe: a full pipe buffer would deadlock the server.
    errlog = tempfile.NamedTemporaryFile("w+", encoding="utf-8", suffix=".log", delete=False)

    def _discard_log() -> None:
        try:
            errlog.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            os.unlink(errlog.name)
        except OSError:
            pass

    try:
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=errlog,
                                text=True, encoding="utf-8", errors="replace", bufsize=1,
                                env=env)
    except OSError as exc:
        _discard_log()
        _emit({"step": "start", "error": f"could not start {cmd[0]!r}: {exc}", "command": cmd,
                          "hint": "an interpreter path with spaces goes in --python, or inside double quotes "
                                  "within --command; single quotes do not group on Windows"})
        print("ROUND-TRIP: FAILED")
        return 1
    next_id = 0
    lines: "queue.Queue[str]" = queue.Queue()
    skipped: list[str] = []

    def _pump() -> None:
        try:
            assert proc.stdout is not None
            while True:
                ln = proc.stdout.readline()
                if not ln:
                    break
                lines.put(ln)
        except Exception as exc:  # noqa: BLE001
            skipped.append(f"<stdout reader stopped: {exc}>")
        finally:
            lines.put("")  # EOF marker, always

    threading.Thread(target=_pump, daemon=True).start()

    def _stderr_tail() -> str:
        try:
            errlog.flush()
            return _decode_log(Path(errlog.name).read_bytes())[-800:]
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
        timeout = _timeout_seconds()
        while True:
            try:
                line = lines.get(timeout=timeout)
            except queue.Empty:
                raise RuntimeError(f"no reply to {method} within {timeout}s (ATLAS_VERIFY_TIMEOUT); "
                                   f"skipped stdout lines: {skipped[-5:]}; server log tail: {_stderr_tail()}")
            if line == "":
                raise RuntimeError(f"server exited during {method}; skipped stdout lines: {skipped[-5:]}; "
                                   f"log tail: {_stderr_tail()}")
            try:
                reply = json.loads(line)
            except json.JSONDecodeError:
                skipped.append(line[:200])  # a non-JSON line on stdout: a log or a stray print
                continue
            # Only a RESPONSE to our id counts: a server-initiated request (it carries "method")
            # or a bare value on stdout is not the reply.
            if (isinstance(reply, dict) and reply.get("id") == next_id and "method" not in reply
                    and ("result" in reply or "error" in reply)):
                return reply
            skipped.append(line[:200])

    try:
        ok = _run_calls(rpc, args, fact, {"command": cmd})
    except Exception as exc:  # noqa: BLE001
        _emit({"step": "error", "error": str(exc)[:800]})
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
        try:  # MN78: the documented post-update path runs verify first, so the one-time index rebuild happened in
            # this server, and its notice went to a log nobody read; pass those lines on
            errlog.flush()
            for _ln in _decode_log(Path(errlog.name).read_bytes()).splitlines():
                if _ln.startswith(("Sextant: building the search index", "Sextant: search index built",
                                   "Sextant: re-indexing")):
                    print(_ln, file=sys.stderr)
        except OSError:
            pass
        _discard_log()

    print("ROUND-TRIP: " + ("OK" if ok else "FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
