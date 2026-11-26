"""Atlas Web UI v0.5.4 -- operations dashboard, Brain chat, Memory browser.

Tabs: Dashboard | Tickets | Tasks | Brain Chat | Agents | Memory | Sessions

Run:
  .venv\\Scripts\\python.exe -m atlas_plus.web.app
  uv run atlas-web-ui
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import sys
import re
import re as _re
import signal
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

import html as _html_mod
import httpx
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect, status
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel, Field, field_validator

from atlas_plus.web.auth_failures import AuthFailureLog
try:  # optional SIEM panel; the integration module is a private extension
    from atlas_plus.web import watchtower
except ImportError:  # pragma: no cover - public build
    watchtower = None  # type: ignore[assignment]


from atlas_plus._yaml import fast_safe_load as _fast_yaml_load


def _js_arg(value: str) -> str:
    """Embed a string as a JS literal inside an HTML event attribute.

    html.escape alone is NOT enough there: the browser decodes entities
    BEFORE the JS parses, so onclick="fn('&#x27;+alert(1)+&#x27;')" runs the
    attacker's code -- a corpus FILENAME like a'+alert(1)+'.md (legal on
    every OS) was a stored XSS via the tree panel (quality loop run 22).
    JSON gives a valid double-quoted JS literal with its own escaping;
    html.escape then survives the attribute context."""
    return _html_mod.escape(json.dumps(value), quote=True)
from atlas_plus import __version__ as _PKG_VERSION
from atlas_plus import paths as atlas_paths

log = logging.getLogger("atlas.web")

WEB_PORT      = atlas_paths.env_int("ATLAS_WEB_PORT", 8766)
WEB_HOST      = os.environ.get("ATLAS_WEB_HOST", "127.0.0.1")
API_KEY       = os.environ.get("ATLAS_WEB_API_KEY", "")
BRAIN_URL     = os.environ.get("ATLAS_BRAIN_URL", "http://127.0.0.1:8767").rstrip("/")
BRAIN_API_KEY = os.environ.get("ATLAS_BRAIN_API_KEY", "")
MCP_TOKEN     = os.environ.get("ATLAS_MCP_TOKEN", "")
MEMORY_ROOT   = atlas_paths.memory_root()
_ALLOW_NO_AUTH = False
# HttpOnly auth cookie set on the authed page load. It carries a RANDOM
# per-browser session token (never the API key itself) that maps to an entry
# in the in-process _SESSIONS table (audit 2026-11-09, row 13). Restarting the
# web process invalidates every session; the operator re-bootstraps with
# GET /?key=<ATLAS_WEB_API_KEY> once.
_AUTH_COOKIE = "sextant_ui"
# A custom header every cookie-authenticated mutating request must carry. A cross-site
# page cannot add one without a CORS preflight, which this app never answers; together
# with the exact-origin check this closes the raw-body (text/plain) CSRF class
# (round-B re-audit 2026-11-09, N-A).
_CSRF_HEADER = "X-Sextant-Request"
_SESSION_MAX_AGE = 43200  # seconds; matches the cookie max_age
_SESSION_LIMIT = 1000     # bound the table: a scripted bootstrap loop cannot grow it forever
_SESSIONS: dict[str, float] = {}  # token -> monotonic expiry
_LOOPBACK_HOSTS = ("127.0.0.1", "::1", "localhost")

_brain_headers = {"Authorization": f"Bearer {BRAIN_API_KEY}"} if BRAIN_API_KEY else {}
_mcp_headers   = {"Authorization": f"Bearer {MCP_TOKEN}"} if MCP_TOKEN else {}


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

# Auth-failure log + sliding-window throttle (ATL-014 / incident ATL-012).
# Before this, a leaked or guessed ATLAS_WEB_API_KEY could be retried without
# limit or trace. _check_auth now records every 401 and returns 429 once a
# source crosses the failure threshold within the window.
AUTH_FAILURES = AuthFailureLog()


from atlas_plus.auth import trusted_peer_matcher as _tpm
# a proxy in another container (compose: the Docker gateway) is trusted to name the real client
# when listed, like the MCP server (MN35)
_WEB_TRUSTED_PEER = _tpm(os.environ.get("ATLAS_WEB_TRUSTED_PEERS") or os.environ.get("ATLAS_MCP_TRUSTED_PEERS", ""))


def _client_ip(request: Request) -> tuple[str, str]:
    """Return (socket_peer_ip, forwarded_ip); either may be ''.

    Behind the Cloudflare tunnel the socket peer is the local proxy, so the real
    remote source lives in the Cf-Connecting-Ip header. That header is trusted ONLY when the socket peer is loopback: a direct
    LAN client (0.0.0.0 bind) could otherwise rotate them to dodge the throttle
    or to lock someone else out (audit 2026-11-09, row 29).
    """
    from atlas_plus.auth import forwarded_ip
    client_ip = request.client.host if request.client else ""
    return client_ip, forwarded_ip(request.headers, client_ip, trusted=_WEB_TRUSTED_PEER)


def _new_session() -> str:
    """Mint a random session token for the auth cookie."""
    import secrets
    import time as _t
    now = _t.monotonic()
    for tok, exp in list(_SESSIONS.items()):
        if exp <= now:
            _SESSIONS.pop(tok, None)
    while len(_SESSIONS) >= _SESSION_LIMIT:
        _SESSIONS.pop(next(iter(_SESSIONS)), None)  # oldest first (insertion order)
    token = secrets.token_urlsafe(32)
    _SESSIONS[token] = now + _SESSION_MAX_AGE
    return token


def _session_valid(token: str) -> bool:
    """True if ``token`` is a live session minted by this process."""
    import time as _t
    if not token:
        return False
    exp = _SESSIONS.get(token)
    if exp is None:
        return False
    if exp <= _t.monotonic():
        _SESSIONS.pop(token, None)
        return False
    return True


def _cookie_origin_ok(request) -> bool:
    """CSRF guard for cookie-authenticated mutating requests.

    SameSite=Strict already keeps the cookie off cross-site requests; this
    additionally requires an Origin (or Referer) whose host AND port equal
    the ones the request was sent to. Another loopback port is another
    application and gets no pass (round-B re-audit 2026-11-09: a page on
    localhost:3000 could otherwise post with the dashboard's cookie). A
    request with neither header is refused.
    """
    from urllib.parse import urlsplit
    src = request.headers.get("origin") or request.headers.get("referer") or ""
    if not src or src == "null":
        return False
    try:
        parts = urlsplit(src)
        host = (parts.hostname or "").lower()
        port = parts.port or (443 if parts.scheme == "https" else 80)
    except ValueError:
        return False
    if not host:
        return False
    req = request.url
    req_host = (req.hostname or "").lower()
    req_port = req.port or (443 if req.scheme in ("https", "wss") else 80)
    # Behind a reverse proxy the socket is plain HTTP but the browser's Origin is https:
    # compare the forwarded scheme's default port when the proxy says so.
    fwd = (request.headers.get("x-forwarded-proto") or "").lower()
    if fwd == "https" and req.port is None:
        req_port = 443
    return bool(req_host) and host == req_host and port == req_port


def _refuse_cross_site(request) -> None:
    """No-auth dev mode still refuses browser cross-site writes (sweep 13, AG27).

    A browser always sends Origin on a cross-origin POST; the dashboard's own pages send
    the dashboard's origin and local tools (curl, the CLI) send none. So a write that
    carries a foreign Origin can only be another site driving the user's browser.
    """
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return
    if request.headers.get("origin") and not _cookie_origin_ok(request):
        raise HTTPException(status_code=403, detail="cross-site request refused: writes need the dashboard's own Origin, even with --no-auth")


_LOCK_WARNED: dict[str, float] = {}


def _same_bytes_as_disk(content: str, safe_path: str) -> bool:
    try:
        current = (Path(MEMORY_ROOT) / safe_path).read_bytes()
    except OSError:
        return False
    _eol = lambda s: s.replace("\r\n", "\n").replace("\r", "\n")  # noqa: E731
    return content.encode("utf-8") == current or _eol(content) == _eol(current.decode("utf-8", "replace"))


_SSL_CTX = None


def _shared_ssl_context():
    """One verified TLS context for every peer client: httpx builds a certifi context per client
    (0.43 to 0.51 s here), and the dashboard made 27 of them per page cycle (AG54)."""
    global _SSL_CTX
    if _SSL_CTX is None:
        try:
            _SSL_CTX = httpx.create_ssl_context()
        except Exception:  # noqa: BLE001
            _SSL_CTX = True
    return _SSL_CTX


class _PeerClient:
    """A wrapper around ``httpx.AsyncClient(...)`` with a circuit breaker per host: after a refused or
    timed-out connect on a GET the host is skipped for PEER_DOWN_SECONDS and the caller gets the same
    ConnectError at once (AG51: thirteen panels each paid about 1.2 s per poll against a dead Brain and
    eight in parallel stalled /health). Actions (POST and the rest) are always tried. It wraps the
    client object at call time, so a test that patches ``httpx.AsyncClient`` is honoured."""
    _down: dict[str, float] = {}
    PEER_DOWN_SECONDS = 10.0  # a revived Brain showed up after 21 s at 20 (MN48); the panels poll every few seconds anyway

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("verify", _shared_ssl_context())  # building a certifi context per client cost 0.43 s on the loop (AG54)
        self._raw = httpx.AsyncClient(*args, **kwargs)
        self._inner = None

    async def __aenter__(self):
        entered = await self._raw.__aenter__()
        self._inner = entered if entered is not None else self._raw
        return self

    async def __aexit__(self, *exc):
        return await self._raw.__aexit__(*exc)

    async def aclose(self):
        close = getattr(self._raw, "aclose", None)
        if close is not None:
            return await close()

    @classmethod
    def _host(cls, url) -> str:
        from urllib.parse import urlsplit
        return urlsplit(str(url)).netloc

    async def _guard(self, method: str, url, call, *args, **kwargs):
        import time as _time
        host = self._host(url)
        until = self._down.get(host, 0.0)
        if until > _time.monotonic() and method.upper() == "GET":
            raise httpx.ConnectError(f"{host} marked down for {int(until - _time.monotonic())}s more (circuit breaker)")
        try:
            result = await call(*args, **kwargs)
        except (httpx.ConnectError, httpx.ConnectTimeout):
            self._down[host] = _time.monotonic() + self.PEER_DOWN_SECONDS
            if len(self._down) > 64:
                self._down.pop(next(iter(self._down)))
            raise
        self._down.pop(host, None)  # any answer (a POST too) clears the mark: a host stayed down 20 s after it came up (MN43)
        return result

    def _target(self):
        return self._inner if self._inner is not None else self._raw

    async def request(self, method, url, *args, **kwargs):
        return await self._guard(str(method), url, self._target().request, method, url, *args, **kwargs)

    async def get(self, url, *args, **kwargs):
        return await self._guard("GET", url, self._target().get, url, *args, **kwargs)

    async def post(self, url, *args, **kwargs):
        return await self._guard("POST", url, self._target().post, url, *args, **kwargs)

    async def put(self, url, *args, **kwargs):
        return await self._guard("PUT", url, self._target().put, url, *args, **kwargs)

    async def patch(self, url, *args, **kwargs):
        return await self._guard("PATCH", url, self._target().patch, url, *args, **kwargs)

    async def delete(self, url, *args, **kwargs):
        return await self._guard("DELETE", url, self._target().delete, url, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._target(), name)


_LOCK_NAMED: dict[str, list] = {}  # peer -> [window start, named, suppressed]
_LOCK_NAMED_PER_PEER = 5  # named lines per peer per window; a forger naming 40 sources got 40 lines (MN37/MN38)


def _warn_lockout(throttle_ip: str, client_ip: str) -> None:
    """One WARNING per locked CLIENT per window, at most five per peer, then one summary line per
    peer per window (with one proxy only the first locked client in a window was named)."""
    import time as _time
    now = _time.time()
    win = AUTH_FAILURES.window_seconds
    key = f"{client_ip}|{throttle_ip}"
    if now - _LOCK_WARNED.get(key, 0.0) <= win:
        return
    row = _LOCK_NAMED.get(client_ip)
    if row is None or now - row[0] > win:
        row = _LOCK_NAMED[client_ip] = [now, 0, 0]
        if len(_LOCK_NAMED) > 512:
            _LOCK_NAMED.pop(next(iter(_LOCK_NAMED)))
    if row[1] >= _LOCK_NAMED_PER_PEER:
        row[2] += 1
        if row[2] == 1:
            log.warning("dashboard: more than %d clients via %s are locked out this window; the rest are not named (the Security panel counts them)", _LOCK_NAMED_PER_PEER, client_ip)
        return
    row[1] += 1
    _LOCK_WARNED[key] = now
    if len(_LOCK_WARNED) > 512:
        _LOCK_WARNED.pop(next(iter(_LOCK_WARNED)))
    log.warning("dashboard: %s is locked out after repeated auth failures (via %s); refusing with 429", throttle_ip, client_ip)  # the log said nothing (MN35)


def _check_auth(request: Request, header_only: bool = False) -> None:
    if _ALLOW_NO_AUTH and not API_KEY:
        _refuse_cross_site(request)
        return
    if not API_KEY:
        # the same key-missing wording as the command line, plus the next step (SEX-048); the
        # copyable keygen lines name this interpreter's path, so they stay on the server's stderr
        from atlas_plus import _next_step
        raise HTTPException(status_code=503, detail=(
            f"{_next_step.key_missing('ATLAS_WEB_API_KEY')}. Next: stop this server and start the dashboard "
            f"with {_next_step.command('web')}: it prints how to make and set the key."))
    client_ip, cf_ip = _client_ip(request)
    throttle_ip = cf_ip or client_ip
    # Loopback is trusted: the throttle exists to stop REMOTE brute-force via the
    # tunnel (counted per real source via Cf-Connecting-Ip). A local browser tab
    # auto-refreshing with a stale key must never lock the operator out of their
    # own dashboard, so the rate-limit is skipped for loopback with no forwarded
    # source. Failures are still recorded below for the operator panel/visibility.
    _is_loopback = (not cf_ip) and client_ip in ("127.0.0.1", "::1", "localhost")
    # A source that has already failed too many times this window is blocked
    # regardless of whether this attempt carries a valid key.
    # a listed proxy whose clients rotate forged names is itself the source once ten times the
    # per-client limit has come through it (AG44)
    _peer_flood = bool(cf_ip) and client_ip not in ("127.0.0.1", "::1", "localhost") and \
        AUTH_FAILURES.failures_via_peer(client_ip) >= 10 * AUTH_FAILURES.max_failures_per_window
    if not _is_loopback and (AUTH_FAILURES.is_rate_limited(throttle_ip) or _peer_flood):
        try:
            AUTH_FAILURES.record(path=request.url.path, client_ip=client_ip, cf_ip=cf_ip, reason="locked_out")  # visible in the panel, not counted (MN29)
            import time as _time
            _warn_lockout(throttle_ip, client_ip)
        except Exception:  # noqa: BLE001
            pass
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many failed authentication attempts; slow down.",
            headers={"Retry-After": str(int(AUTH_FAILURES.window_seconds))},
        )
    from atlas_plus.auth import bearer_token, constant_time_equals

    def _deny(reason: str, count: bool = True) -> None:
        if count:
          AUTH_FAILURES.record(
            path=request.url.path,
            client_ip=client_ip,
            cf_ip=cf_ip,
            reason=reason,
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorized",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # ?key= is the one-time browser bootstrap and is accepted ONLY on GET /
    # (audit 2026-11-09, row 13): anywhere else it lands the master key in
    # access logs, proxy logs and browser history.
    query_key = request.query_params.get("key")
    is_bootstrap = request.method in ("GET", "HEAD") and request.url.path == "/"
    if query_key is not None and not is_bootstrap:
        _deny("query_key_refused")

    key = (
        bearer_token(request.headers.get("Authorization", ""))
        or request.headers.get("X-Atlas-Key", "")
        or (query_key or "")
    )
    if key:
        if not constant_time_equals(key, API_KEY):
            _deny("bad_key")
        return

    # Session cookie set by the authed page load. It holds a random token,
    # never the API key; on a mutating method it also needs a same-origin
    # (or loopback) Origin/Referer on top of SameSite=Strict.
    cookie = request.cookies.get(_AUTH_COOKIE, "")
    if cookie:
        if header_only:
            _deny("cookie_not_accepted")   # this route takes the bearer only (extension inbox)
        if not _session_valid(cookie):
            # a session cookie is 256 random bits: an unknown one is a stale tab after a restart
            # or a rotation, not a guess, so it does not feed the lockout (AG31: a stale tab
            # locked the operator out through the tunnel every minute)
            _deny("bad_session", count=False)
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            if not _cookie_origin_ok(request):
                _deny("cookie_origin")
            if not request.headers.get(_CSRF_HEADER):
                _deny("csrf_header_missing")
        return
    _deny("missing_key")


# ---------------------------------------------------------------------------
# SSE event bus
# ---------------------------------------------------------------------------

_sse_subscribers: list[asyncio.Queue] = []


async def _broadcast(event: dict) -> None:
    payload = json.dumps(event)
    dead = []
    for q in _sse_subscribers:
        try:
            q.put_nowait(payload)
        except asyncio.QueueFull:
            dead.append(q)
    for q in dead:
        _sse_subscribers.remove(q)


_POLL_STATE: dict = {"warned": False}


async def _task_poll_loop() -> None:
    """Poll Brain for task status changes and broadcast them to SSE subscribers.

    Adaptive backoff: when nothing changes the poll interval grows from 2s to 30s.
    Any status change resets the interval to 2s so the UI stays responsive during
    active task runs.  This avoids hammering Brain when the queue is quiet (e.g.
    overnight) while keeping latency low during work hours.
    """
    last_seen: dict[str, str] = {}
    poll_interval = 2.0  # resets on any change, grows during idle
    while True:
        changed = False
        try:
            # connect 2.0 s, not 0.75: under load (a test suite, a model loading) a listening Brain
            # took longer than that to accept, and the log said it was down (2026-11-21)
            poll_connect_timeout = 2.0
            async with _PeerClient(timeout=httpx.Timeout(5.0, connect=poll_connect_timeout)) as client:
                r = await client.get(
                    f"{BRAIN_URL}/tasks", params={"limit": "20"}, headers=_brain_headers
                )
            if r.status_code == 200:
                if _POLL_STATE.get("warned"):
                    _POLL_STATE["warned"] = False
                    log.info("Brain at %s is answering again", BRAIN_URL)
                for task in r.json().get("tasks", []):
                    tid = task["task_id"]
                    if last_seen.get(tid) != task["status"]:
                        last_seen[tid] = task["status"]
                        await _broadcast({"type": "task_update", "task": task})
                        changed = True
            elif not _POLL_STATE.get("warned"):
                _POLL_STATE["warned"] = True
                log.warning("Brain at %s answered HTTP %s to the task poll: check ATLAS_BRAIN_API_KEY "
                            "matches the Brain's key (the dashboard works without it; tasks stay empty)",
                            BRAIN_URL, r.status_code)
        except Exception as exc:
            if not _POLL_STATE.get("warned"):
                _POLL_STATE["warned"] = True
                if isinstance(exc, httpx.ReadTimeout) or (
                        isinstance(exc, (httpx.ConnectTimeout, httpx.PoolTimeout))
                        and await asyncio.to_thread(_brain_listening)):
                    log.warning("Brain at %s is busy and did not answer the task poll in time (it is listening); "
                                "retrying", BRAIN_URL)
                else:
                    log.warning("Brain at %s is not running or not reachable: the dashboard works without it; "
                                "start sextant-brain or set ATLAS_BRAIN_URL", BRAIN_URL)  # the exception type (ConnectTimeout on Windows) said nothing to a user (MN48)
            log.debug("Task poll error: %s", exc)
        poll_interval = 2.0 if changed else min(poll_interval * 1.5, 30.0)
        await asyncio.sleep(poll_interval)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """FastAPI lifespan: start the SSE task-poll loop on startup, cancel on shutdown."""
    poll_task = asyncio.create_task(_task_poll_loop())
    yield
    poll_task.cancel()


# No unauthenticated API map: /docs, /redoc and /openapi.json listed every route to anyone
# who could reach the port (MN4, pipeline sweep 2026-11-11).
app = FastAPI(title="Atlas Web UI", version="0.5.0", lifespan=lifespan,
              docs_url=None, redoc_url=None, openapi_url=None)

# Browser hardening (MN4, pipeline sweep 2026-11-11): htmx is pinned by subresource integrity; Tailwind is
# served by this app from the package (its CDN sent no CORS header, so it could not carry SRI: MN29, MN86),
# only this origin and unpkg.com may run script, nothing may frame the page,
# and the referrer never leaves. 'unsafe-inline' stays for the page's own inline script and
# Tailwind's generated styles.
async def _json_object(request: Request, required: tuple[str, ...] = ()) -> dict:
    """The request body as a JSON object, or a 400 that says what is wrong (AG31: the ticket
    routes answered 500 and leaked KeyError text; the deep-JSON guard covered two routes)."""
    raw = await request.body()
    if len(raw) > 2_097_152:
        raise HTTPException(status_code=413, detail="Body larger than 2 MB")  # a 30 MB body was accepted (AG38)
    if _too_deep(raw):
        raise HTTPException(status_code=400, detail="Body too deeply nested")
    try:
        body = json.loads(raw.decode("utf-8-sig") if raw else "")  # a BOM is not an error (MN25)
    except (UnicodeDecodeError, ValueError):
        raise HTTPException(status_code=400, detail="Body must be a JSON object")
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Body must be a JSON object")
    missing = [k for k in required if not body.get(k)]
    if missing:
        raise HTTPException(status_code=400, detail=f"Missing field(s): {', '.join(missing)}")
    import math as _math
    for k, v in body.items():
        # these routes take flat strings and numbers; a list or object reached the store as a 500
        # or a title of "['a']" (MN25); true/Infinity landed in a ticket body as text (AG38)
        if isinstance(v, (dict, list)) or (isinstance(v, float) and not _math.isfinite(v)):
            raise HTTPException(status_code=400, detail=f"Field {k} must be a string, a boolean or a finite number")
    for k in required:
        if not isinstance(body[k], str):
            raise HTTPException(status_code=400, detail=f"Field {k} must be a string")
    return body


def _too_deep(raw: bytes, limit: int = 200) -> bool:
    """True when the JSON text nests deeper than ``limit``. json.loads raises RecursionError on
    100,000 brackets on the main thread but segfaults inside a worker thread on musl, whose
    non-main stacks are small (AG8, sweep 10): the depth is counted before parsing."""
    depth = 0
    in_str = False
    esc = False
    for b in raw:
        if in_str:
            if esc:
                esc = False
            elif b == 0x5C:      # backslash
                esc = True
            elif b == 0x22:      # quote
                in_str = False
            continue
        if b == 0x22:
            in_str = True
        elif b in (0x5B, 0x7B):  # [ {
            depth += 1
            if depth > limit:
                return True
        elif b in (0x5D, 0x7D):  # ] }
            depth -= 1
    return False


# Tools tab (2026-11-19): local tool UIs shown as panes (iframes). The panes
# are the operator's own, so they live in <state dir>/web/tool-panes.json and
# never in the source: a JSON list of {"key", "label", "url"} rows, read once
# at start. Loopback only: the pane loads from the viewer's own machine, so
# over the tunnel these panes are blank by design. Each origin in the file
# (and only these) is allowed by the CSP frame-src below. No file, or a file
# with any bad row, means no panes, no Tools tab and frame-src 'none'.
_TOOL_PANES_MAX = 12
_TOOL_PANE_KEY = re.compile(r"[a-z0-9][a-z0-9-]{0,31}")
_TOOL_PANE_URL = re.compile(r"http://127\.0\.0\.1:([0-9]{2,5})(/[A-Za-z0-9._~/?&=%-]*)?")


def _load_tool_panes(path) -> list[dict]:
    """Read and validate the tool panes file. Fails closed: one bad row
    refuses the whole file, so a typo can never widen frame-src."""
    try:
        raw = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return []
    except OSError as exc:
        log.warning("tool panes: cannot read %s (%s); no panes", path, type(exc).__name__)
        return []
    try:
        rows = json.loads(raw)
    except ValueError:
        log.warning("tool panes: %s is not valid JSON; no panes", path)
        return []
    if not isinstance(rows, list) or len(rows) > _TOOL_PANES_MAX:
        log.warning("tool panes: %s must be a list of at most %d rows; no panes", path, _TOOL_PANES_MAX)
        return []
    panes, seen = [], set()
    for row in rows:
        key = row.get("key") if isinstance(row, dict) else None
        label = row.get("label") if isinstance(row, dict) else None
        url = row.get("url") if isinstance(row, dict) else None
        m = _TOOL_PANE_URL.fullmatch(url) if isinstance(url, str) else None
        ok = (isinstance(key, str) and _TOOL_PANE_KEY.fullmatch(key) and key not in seen
              and isinstance(label, str) and 0 < len(label.strip()) <= 60
              and m is not None and 0 < int(m.group(1)) < 65536)
        if not ok:
            log.warning("tool panes: %s has a bad row (key, label up to 60 chars, "
                        "url http://127.0.0.1:<port>/...); no panes", path)
            return []
        seen.add(key)
        panes.append({"key": key, "label": label.strip(), "url": url})
    return panes


def _origin(url: str) -> str:
    scheme, _, rest = url.partition("://")
    return f"{scheme}://{rest.split('/', 1)[0]}"


def _csp_for(panes: list[dict]) -> str:
    frame_src = " ".join(sorted({_origin(p["url"]) for p in panes})) or "'none'"
    return ("default-src 'self'; script-src 'self' 'unsafe-inline' https://unpkg.com; "
            "style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self' data:; connect-src 'self'; "
            f"frame-src {frame_src}; "
            "frame-ancestors 'none'; base-uri 'self'; form-action 'self'; object-src 'none'")


_TOOL_PANES = _load_tool_panes(atlas_paths.state_dir() / "web" / "tool-panes.json")
_CSP = _csp_for(_TOOL_PANES)


def _tools_tab_button() -> str:
    if not _TOOL_PANES:
        return ""
    return ('<div class="tab" data-tab="tools" onclick="switchTab(\'tools\')" role="tab" id="tabbtn-tools" '
            'aria-controls="tab-tools" aria-selected="false" tabindex="-1">Tools</div>')


def _tools_tab_html() -> str:
    """The Tools tab: one pane per _TOOL_PANES row. Iframes are lazy so a
    hidden tab costs nothing; each pane has open-in-new-tab and reload."""
    import html as _h
    if not _TOOL_PANES:
        return ""
    panes = []
    for p in _TOOL_PANES:
        k, label, url = _h.escape(p["key"]), _h.escape(p["label"]), _h.escape(p["url"])
        panes.append(
            f'<section class="card tool-pane" style="display:flex;flex-direction:column;min-height:0;padding:0">'
            f'<div class="flex items-center justify-between px-3 py-1.5 border-b border-slate-700">'
            f'<h2 class="text-xs font-bold text-slate-300">{label}</h2>'
            f'<span class="flex gap-3 text-xs">'
            f'<button type="button" class="text-slate-400 hover:text-white" '
            f'onclick="var f=document.getElementById(\'pane-{k}\');f.src=f.src">reload</button>'
            f'<a class="text-sky-400 hover:text-sky-300" href="{url}" target="_blank" rel="noopener noreferrer">open</a>'
            f'</span></div>'
            f'<iframe id="pane-{k}" src="{url}" title="{label}" loading="lazy" '
            f'referrerpolicy="no-referrer" style="flex:1;width:100%;border:0;min-height:60vh;background:#fff"></iframe>'
            f'</section>')
    return (
        '<div id="tab-tools" role="tabpanel" aria-labelledby="tabbtn-tools" class="tab-content block-tab h-full overflow-y-auto">'
        '<div class="flex items-center justify-between mb-2 px-1">'
        '<span class="text-slate-500 text-xs">Local tools, one pane each. Panes load from this computer '
        '(127.0.0.1); over the tunnel they stay blank.</span></div>'
        '<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,560px),1fr));gap:12px">'
        + "".join(panes) + '</div></div>')


@app.middleware("http")
async def _security_headers(request, call_next):
    response = await call_next(request)
    response.headers.setdefault("Content-Security-Policy", _CSP)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    return response


# ---------------------------------------------------------------------------
# SSE
# ---------------------------------------------------------------------------

@app.get("/events")
async def sse_events(request: Request):
    """GET /events -- Server-Sent Events stream for real-time task status updates.

    Subscribes to the internal SSE broadcast queue. Each event is JSON:
    {"type": "task_update", "task": {task dict}}.
    The poll loop broadcasts every 8s when running tasks exist.
    """
    _check_auth(request)
    queue: asyncio.Queue = asyncio.Queue(maxsize=50)
    _sse_subscribers.append(queue)

    async def generator():
        """Yield SSE events from the subscriber queue, sending pings on 15s timeout to keep the connection alive."""
        yield 'data: {"type":"connected"}\n\n'
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    payload = await asyncio.wait_for(queue.get(), timeout=15.0)
                    yield f"data: {payload}\n\n"
                except asyncio.TimeoutError:
                    yield 'data: {"type":"ping"}\n\n'
        finally:
            if queue in _sse_subscribers:
                _sse_subscribers.remove(queue)

    return StreamingResponse(generator(), media_type="text/event-stream")


# ---------------------------------------------------------------------------
# Security / auth-failure visibility (ATL-014, incident ATL-012)
# ---------------------------------------------------------------------------

@app.get("/api/security/auth-failures")
async def auth_failures_json(request: Request, limit: int = 50):
    """GET /api/security/auth-failures -- recent rejected requests + throttle stats.

    JSON: {"stats": {...}, "failures": [{ts, iso, path, client_ip, cf_ip,
    source_ip, reason}, ...]} newest first. Auth-protected (viewing the security
    panel itself requires the key; failed views are themselves recorded).
    """
    _check_auth(request)
    limit = max(1, min(limit, 500))
    return {
        "stats": AUTH_FAILURES.stats(),
        "failures": [f.as_dict() for f in AUTH_FAILURES.recent(limit)],
    }


@app.get("/api/security/auth-failures.html", response_class=HTMLResponse)
async def auth_failures_panel(request: Request, limit: int = 25):
    """GET /api/security/auth-failures.html -- compact HTMX fragment of recent 401s."""
    _check_auth(request)
    limit = max(1, min(limit, 200))
    stats = AUTH_FAILURES.stats()
    rows = AUTH_FAILURES.recent(limit)
    if not rows and not stats.get("locked_out_refusals"):
        return HTMLResponse('<p class="text-green-600">No auth failures recorded.</p>')
    cells = "".join(
        "<tr>"
        f"<td class='px-2'>{_html_mod.escape(d['source_ip'])}</td>"
        f"<td class='px-2'>{_html_mod.escape(str(d['iso'])[11:19] or str(d['iso']))}</td>"
        f"<td class='px-2'>{_html_mod.escape(d['path'])}</td>"
        f"<td class='px-2'>{_html_mod.escape(d['reason'])}</td>"
        "</tr>"
        for d in (r.as_dict() for r in rows)
    )
    body = (
        f"<p>{stats['in_window']} in last {int(stats['window_seconds'])}s "
        f"from {stats['distinct_sources_in_window']} source(s); "
        f"{stats['total']} total recorded.</p>"
        + _locked_out_html(stats)
        + "<div style='overflow-x:auto;max-width:100%;min-width:0'><table class='text-xs' style='white-space:nowrap'><thead><tr>"  # the 684 px table spilled out of its 230 px card (MN50, AG65)
        "<th class='px-2 text-left'>source</th><th class='px-2 text-left'>time (UTC)</th>"
        "<th class='px-2 text-left'>path</th><th class='px-2 text-left'>reason</th>"
        "</tr></thead><tbody>" + cells + "</tbody></table></div>"
    )
    return HTMLResponse(body)


# ---------------------------------------------------------------------------
# Watchtower -- optional SIEM panel (seam: the SIEM owns the contract, the web UI renders)
# ---------------------------------------------------------------------------

def _refuse_cross_site_read(request: Request) -> None:
    """A read that starts a program refuses another site's page (KL7): the browser says so in Sec-Fetch-Site
    (cross-site, or same-site for another port of the same host) or with a foreign Origin. The dashboard's own
    fetches are same-origin; curl and the CLI send neither header."""
    headers = getattr(request, "headers", None) or {}  # an in-process caller may pass a bare object (test_kl2)
    site = (headers.get("sec-fetch-site") or "").lower()
    if site in ("cross-site", "same-site") or (headers.get("origin") and not _cookie_origin_ok(request)):
        raise HTTPException(status_code=403, detail=(
            "cross-site request refused: open the Watchtower tab on the dashboard itself, "
            "or call this URL with the API key from a terminal"))


@app.get("/api/security/watchtower")
async def watchtower_json(
    request: Request, since: str | None = None, severity: str | None = None,
):
    """GET /api/security/watchtower -- raw SIEM state (the Watchtower contract).

    Proxies the SIEM's JSON dashboard export. Returns its state dict, or
    {"error": ...} when the SIEM is unreachable or not installed. Auth-protected.
    """
    _check_auth(request)
    _refuse_cross_site_read(request)
    if watchtower is None:
        return {"error": "SIEM integration not installed"}
    try:
        # off the event loop and single-flight: the fetch may take its full timeout (KL2; services panel does the same)
        return await asyncio.to_thread(watchtower.fetch_siem_state_cached, since=since, severity_min=severity)
    except watchtower.WatchtowerError as exc:
        return {"error": str(exc), "next_step": watchtower.NEXT_STEP}


@app.get("/api/security/watchtower.html", response_class=HTMLResponse)
async def watchtower_panel(
    request: Request, since: str | None = None, severity: str | None = None,
):
    """GET /api/security/watchtower.html -- HTMX fragment of the SIEM's live state."""
    _check_auth(request)
    _refuse_cross_site_read(request)
    if watchtower is None:
        return HTMLResponse('<div class="text-slate-500 text-xs">SIEM integration not installed.</div>')
    try:
        state = await asyncio.to_thread(watchtower.fetch_siem_state_cached, since=since, severity_min=severity)
    except watchtower.WatchtowerError as exc:
        return HTMLResponse(watchtower.render_error_html(str(exc)))
    return HTMLResponse(watchtower.render_panel_html(state))


# ---------------------------------------------------------------------------
# Status bar
# ---------------------------------------------------------------------------

@app.get("/api/status", response_class=HTMLResponse)
async def status_partial(request: Request):
    """GET /api/status -- HTMX service health bar for the top-right Dashboard.

    Pings Brain (:8767), MCP (:8765), and Ollama (:11434) concurrently (3s timeout).
    Returns a '<span>' row: up=green, degraded=yellow, down=red. Also appends
    an orange badge when exhausted tasks exist (no HTTP round-trip -- local DB query).
    """
    _check_auth(request)
    mcp_port = os.environ.get("ATLAS_MCP_PORT", "8765")
    mcp_base = (os.environ.get("ATLAS_MCP_URL") or f"http://127.0.0.1:{mcp_port}").rstrip("/")
    from atlas_plus.llm.ollama_url import default_ollama_url as _ollama_url
    ollama_base = _ollama_url().rstrip("/")  # the same endpoint the Brain uses (MN25: it showed the owner's 11434 as up)
    # Fire all three checks concurrently; worst-case latency = slowest, not sum.
    async with _PeerClient(timeout=httpx.Timeout(3.0, connect=0.75)) as client:
        brain_req  = client.get(f"{BRAIN_URL}/health")
        mcp_req    = client.get(f"{mcp_base}/health", headers=_mcp_headers)
        ollama_req = client.get(f"{ollama_base}/api/tags")
        brain_r, mcp_r, ollama_r = await asyncio.gather(
            brain_req, mcp_req, ollama_req, return_exceptions=True
        )

    def _status(r, name: str) -> str:
        if isinstance(r, Exception):
            return "down"
        if r.status_code == 200:
            if name == "ollama":
                try:
                    return f"up ({len(r.json().get('models', []))}m)"
                except Exception:
                    pass
            return "up"
        return "degraded"

    checks = {
        "brain":  _status(brain_r,  "brain"),
        "mcp":    _status(mcp_r,    "mcp"),
        "ollama": _status(ollama_r, "ollama"),
    }

    parts = []
    for name, state in checks.items():
        if name == "mcp" and state == "down" and not os.environ.get("ATLAS_MCP_URL", "").strip():
            # A stdio install has no MCP HTTP server: its client starts the server itself. The first view after
            # `ROUND-TRIP: OK` showed a red `mcp: down` (AG97, sweep 25); an explicit ATLAS_MCP_URL still goes red.
            parts.append(f'<span class="text-gray-400" title="no MCP HTTP server on port {_html_mod.escape(str(mcp_port))}: '
                         'a stdio install has none (your client starts the server itself; sextant verify proves it); '
                         'HTTP mode is optional">mcp: stdio</span>')
            continue
        c = ("text-green-400" if state.startswith("up")
             else ("text-yellow-400" if state == "degraded" else "text-red-400"))
        parts.append(f'<span class="{c}">{name}: {state}</span>')

    # Web UI self -- this process is answering, so it's up; show the bind target
    # so the operator can confirm it is loopback-only (ATL-014 health completeness).
    parts.append(f'<span class="text-green-400">web: up ({WEB_HOST}:{WEB_PORT})</span>')

    # Autonomous-write mode (ATL-013). Operator-glance: are Brain/tools allowed to
    # write to .memory right now? Fail-closed default means unset reads as "off".
    from atlas_plus.autonomous import autonomous_enabled
    _auton_on = autonomous_enabled()
    parts.append(
        f'<span class="{"text-yellow-400" if _auton_on else "text-gray-400"}" '
        'title="ATLAS_AUTONOMOUS as seen by THIS web process only; Brain, MCP and '
        'the worker read their own environment">'
        f'autonomous: {"on" if _auton_on else "off"} (web process only)</span>'
    )

    # Exhausted tasks indicator: quick in-process DB query (no HTTP round-trip).
    # Shown as an orange badge when tasks have burned all retry attempts.
    try:
        from atlas_plus.brain import tasks as _ts
        ex_count = len(_ts.list_exhausted_tasks(limit=50))
        if ex_count > 0:
            parts.append(
                f'<span class="text-orange-400" title="{ex_count} task(s) exhausted all retries">'
                f'{ex_count} exhausted</span>'
            )
    except Exception:
        pass

    # Maintenance-lane rollup (WS2): glanceable "are the nightly/weekly lanes
    # running?" signal, read from the scheduler_runs heartbeat ledger in the
    # shared state.sqlite (local read, no Brain round-trip).
    try:
        from atlas_plus.brain import tasks as _ts
        from atlas_plus.brain import maintenance as _mt
        _summ = _mt.summarize_maintenance(_ts.list_job_runs())
        _mcls = {"ok": "text-green-400", "stale": "text-yellow-400",
                 "error": "text-red-400", "unknown": "text-gray-400"}[_summ["overall"]]
        _bad = [j for j in _summ["jobs"] if j["status"] in ("error", "stale", "unknown")]
        _detail = "; ".join(f'{j["job_id"]}: {j["status"]} ({j["age_label"]})' for j in _bad) or "all lanes fresh"
        _extra = "" if _summ["overall"] == "ok" else f" ({len(_bad)})"
        if all(j["status"] == "unknown" for j in _summ["jobs"]):
            raise LookupError("no lane has ever run: not configured here, so no pill (AG97)")
        parts.append(
            f'<span class="{_mcls}" title="maintenance lanes -- {_html_mod.escape(_detail)}">'
            f'maint: {_summ["overall"]}{_extra}</span>'
        )
    except Exception:
        pass

    # Security-audit verdict rollup (WS1): latest weekly host-audit verdict +
    # freshness, read from .atlas/security/index.json (local file, no subprocess).
    try:
        from atlas_plus.web import security_status as _sec
        _ssum = _sec.summarize_security(_sec.read_audit_index())
        if _ssum.get("status") != "unknown":   # no audit has ever run: the feature is not set up here (AG97)
            parts.append(_sec.rollup_span(_ssum))
    except Exception:
        pass

    # Build-loop status rollup (LOO-002): what is running / backing off on a rate
    # limit, read from .build-logs/loop-state.json (local file, no subprocess).
    try:
        from atlas_plus.web import loop_status as _loops
        _lsum = _loops.summarize_loops(_loops.read_loop_state())
        if _lsum.get("status") != "unknown":   # no loop-state snapshot: no build loop here (AG97)
            parts.append(_loops.rollup_span(_lsum))
    except Exception:
        pass

    return HTMLResponse('<span class="sep"> &nbsp;|&nbsp; </span>'.join(parts))


@app.get("/api/maintenance", response_class=HTMLResponse)
async def maintenance_panel(request: Request):
    """GET /api/maintenance -- HTMX fragment: nightly/weekly maintenance-lane health.

    Reads the scheduler_runs heartbeat ledger (written by the Brain scheduler's
    job-event listener) from the shared state.sqlite and shows, per lane, when it
    last ran and whether it is ok / stale / errored. Cross-process local read --
    no Brain HTTP round-trip -- so it works even when the Brain API is unreachable.
    """
    _check_auth(request)
    try:
        from atlas_plus.brain import tasks as _ts
        from atlas_plus.brain import maintenance as _mt
        summary = _mt.summarize_maintenance(_ts.list_job_runs())
    except Exception as exc:
        return HTMLResponse(
            f'<div class="text-red-400 text-xs">maintenance unavailable: '
            f'{_html_mod.escape(str(exc))}</div>'
        )

    cls = {"ok": "text-green-400", "stale": "text-yellow-400",
           "error": "text-red-400", "unknown": "text-gray-500"}
    c = summary["counts"]
    head = (
        f'<div class="text-xs mb-1"><span class="{cls[summary["overall"]]}">'
        f'{summary["overall"]}</span> '
        f'<span class="text-slate-500">{c["ok"]} ok / {c["stale"]} stale / '
        f'{c["error"]} err / {c["unknown"]} new</span></div>'
    )
    rows = []
    for j in summary["jobs"]:
        tip = j.get("error") or f'{j["job_id"]} ({j["cadence"]}, {j["status"]})'
        rows.append(
            f'<div class="flex justify-between gap-2 text-xs leading-5">'
            f'<span class="text-slate-400 truncate" title="{_html_mod.escape(j["job_id"])} ({j["cadence"]})">'
            f'{_html_mod.escape(j["label"])}</span>'
            f'<span class="{cls[j["status"]]} shrink-0" title="{_html_mod.escape(tip)}">'
            f'{j["age_label"]}</span></div>'
        )
    return HTMLResponse(head + "".join(rows))


@app.get("/api/security/audit", response_class=HTMLResponse)
async def security_audit_panel(request: Request):
    """GET /api/security/audit -- HTMX fragment: latest weekly security-audit verdict.

    Reads .atlas/security/index.json (written by scripts/security-audit.ps1) and
    shows the latest verdict, finding counts, freshness, and report name. Local
    file read -- no subprocess, no Brain round-trip.
    """
    _check_auth(request)
    try:
        from atlas_plus.web import security_status as _sec
        summary = _sec.summarize_security(_sec.read_audit_index())
        return HTMLResponse(_sec.render_panel(summary))
    except Exception as exc:
        return HTMLResponse(
            f'<div class="text-red-400 text-xs">security audit unavailable: '
            f'{_html_mod.escape(str(exc))}</div>'
        )


@app.get("/api/loop/status", response_class=HTMLResponse)
async def loop_status_panel(request: Request):
    """GET /api/loop/status -- HTMX fragment: autonomous build-loop status.

    Reads .build-logs/loop-state.json (written by loop-status.ps1 -Json) and shows
    what is running, what is backing off on a rate limit, and recent throttle events.
    Local file read -- no subprocess, no Brain round-trip. See LOO-002.
    """
    _check_auth(request)
    try:
        from atlas_plus.web import loop_status as _loops
        summary = _loops.summarize_loops(_loops.read_loop_state())
        return HTMLResponse(_loops.render_panel(summary))
    except Exception as exc:
        return HTMLResponse(
            f'<div class="text-red-400 text-xs">loop status unavailable: '
            f'{_html_mod.escape(str(exc))}</div>'
        )


# ---------------------------------------------------------------------------
# Agents
# ---------------------------------------------------------------------------

@app.get("/api/agents", response_class=HTMLResponse)
async def agents_html(request: Request):
    """GET /api/agents -- HTMX Agents tab panel showing all Brain agents + quality sparklines.

    Fetches agent registry from Brain /agents. Each agent row shows: name, model,
    status (up/down from Ollama ping), quality sparkline (last 12 tasks), recent
    task stats. TTL-cached to avoid hammering Ollama on every tab open.
    """
    _check_auth(request)
    try:
        async with _PeerClient(timeout=httpx.Timeout(20.0, connect=0.75)) as client:  # /api/agents hung 20 s on a blackholed Brain (AG42)
            r = await client.get(f"{BRAIN_URL}/agents", headers=_brain_headers)
        r.raise_for_status()
        agents = r.json().get("agents", [])
    except httpx.ConnectError:
        return HTMLResponse(
            f'<div class="text-red-400 text-xs">Brain not running or not reachable at {_html_mod.escape(BRAIN_URL)}: start it (<code>sextant-brain</code>), or set ATLAS_BRAIN_URL.</div>'  # the source-tree command was wrong for a pip install (MN43)
        )
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 401:
            return HTMLResponse(
                '<div class="text-red-400 text-xs">Brain auth failed (401).'
                '<br>Set the Brain token in the environment before starting the web UI.</div>'
            )
        return HTMLResponse(
            f'<div class="text-red-400 text-xs">Brain error {exc.response.status_code}.</div>'
        )
    except Exception as exc:
        if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout)):
            return HTMLResponse(  # on Windows a refused loopback connect reads as ConnectTimeout, so the "not running" branch never fired (AG54)
                f'<div class="text-red-400 text-xs">Brain not running or not reachable at {_html_mod.escape(BRAIN_URL)}: start it (<code>sextant-brain</code>), or set ATLAS_BRAIN_URL.</div>'
            )
        return HTMLResponse(
            f'<div class="text-red-400 text-xs">Brain unreachable: {type(exc).__name__}: {_html_mod.escape(str(exc)[:160])}</div>'
        )

    online_count = sum(1 for a in agents if a.get("online"))

    # Fetch credit-exhausted agent names from Brain diagnostic
    credit_exhausted: set[str] = set()
    try:
        async with _PeerClient(timeout=httpx.Timeout(3.0, connect=0.75)) as _dc:
            _dr = await _dc.get(f"{BRAIN_URL}/diagnostic", headers=_brain_headers)
            if _dr.status_code == 200:
                credit_exhausted = set(_dr.json().get("credit_exhausted_agents", []))
    except Exception:
        pass

    from atlas_plus.brain.improvement import all_agent_recent_scores, all_agent_stats
    # Batch fetch all agents' quality data in two queries instead of 2*N.
    _scores_by_agent = await asyncio.to_thread(all_agent_recent_scores, 12)
    _stats_by_agent = {s["agent"]: s for s in await asyncio.to_thread(all_agent_stats)}
    rows = [
        f'<div class="flex items-center justify-between mb-3">'
        f'<span class="text-slate-400 text-xs">{online_count}/{len(agents)} online</span>'
        f'<span class="text-slate-600 text-xs">quality sparklines = last 12 tasks</span>'
        f'</div>',
        '<div class="grid gap-2">',
    ]
    for a in agents:
        online_c = "text-green-400" if a["online"] else "text-red-400"
        online_s = "online" if a["online"] else "offline"
        caps = _html_mod.escape(", ".join(a.get("capabilities", [])))
        credit_badge = (
            ' <span class="text-xs bg-red-900 text-red-300 px-1 rounded" title="Credit exhausted -- using fallback">credits!</span>'
            if a["name"] in credit_exhausted else ""
        )
        recent = _scores_by_agent.get(a["name"], [])
        spark = _sparkline_svg(recent, width=80, height=20) if recent else ""
        stats_d = _stats_by_agent.get(a["name"], {})
        q = stats_d.get("avg_quality")
        q_str = f"{q:.1f}" if q is not None else "n/a"
        q_color = ("text-green-400" if (q or 0) >= 7
                   else ("text-yellow-400" if (q or 0) >= 4 else "text-slate-600"))
        # Use JSON encoding for the onclick argument to prevent JS injection
        # _js_arg, not bare json.dumps: JSON's \" still carries a raw quote
        # byte, which would terminate the double-quoted onclick attribute.
        name_js = _js_arg(a["name"])
        rows.append(
            f'<div class="flex items-start justify-between p-2 border border-slate-700 rounded">'
            f'<div class="min-w-0 flex-1">'
            f'<div class="flex items-center gap-2 flex-wrap">'
            f'<span class="font-bold text-slate-200 text-sm">{_html_mod.escape(a["name"])}</span>'
            f'<span class="{online_c} text-xs">{online_s}</span>'
            f'<span class="text-slate-500 text-xs">{_html_mod.escape(a["model"])}</span>'
            f'{credit_badge}'
            f'</div>'
            f'<div class="text-slate-400 text-xs mt-1">{_html_mod.escape(a["description"])}</div>'
            f'<div class="text-slate-600 text-xs mt-0.5">{caps}</div>'
            f'<div class="flex items-center gap-2 mt-1.5">'
            f'{spark}'
            f'<span class="{q_color} text-xs font-mono">{q_str}</span>'
            f'</div>'
            f'</div>'
            + (f'<button onclick="quickDispatch({name_js})" '
               f'class="btn btn-cyan text-xs shrink-0 ml-3 mt-1">Dispatch</button>'
               if a.get("online") else
               # Dispatch was offered on an offline agent: the task queued and failed later (SEX-025)
               '<button disabled title="This agent is offline: a task sent now would fail. Start it, then dispatch." '
               'aria-disabled="true" class="btn text-xs shrink-0 ml-3 mt-1 opacity-50 cursor-not-allowed">'
               'Dispatch (offline)</button>')
            + '</div>'
        )
    rows.append('</div>')
    return HTMLResponse("\n".join(rows))


# ---------------------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------------------

class TaskSubmit(BaseModel):
    """POST /api/tasks/submit body: instruction text plus optional session ID and agent routing hint."""

    instruction: str = Field(min_length=1, max_length=4000)
    session_id: Optional[str] = Field(None, max_length=200)
    agent: Optional[str] = Field(None, max_length=100)

    @field_validator("instruction")
    @classmethod
    def _instruction_not_blank(cls, v: str) -> str:
        # An empty/whitespace instruction reaches `claude --print` with no
        # prompt and fails downstream with a confusing CLI error (seen
        # 2026-10-12 as failed webui-claude-direct tasks); reject at the door.
        if not v.strip():
            raise ValueError("instruction must not be empty or whitespace")
        return v


@app.get("/api/tasks", response_class=HTMLResponse)
async def tasks_partial(request: Request, limit: int = 20, status: Optional[str] = None,
                        agent: Optional[str] = None, since_hours: Optional[int] = None):
    """GET /api/tasks -- HTMX task list for the Tasks tab.

    Reads directly from local SQLite (no Brain HTTP round-trip).
    Query params: limit, status (pending/running/done/failed/blocked), agent, since_hours.
    Returns colored rows with status badge; click triggers task_detail endpoint.
    """
    _check_auth(request)
    try:
        from atlas_plus.brain import tasks as ts
        items = ts.list_tasks(
            status=status,
            agent=agent or None,
            since_hours=since_hours,
            limit=min(limit, 100),
        )
    except Exception as exc:
        return HTMLResponse(f'<div class="text-red-400 text-xs">Error: {exc}</div>')

    if not items:
        return HTMLResponse('<div class="text-slate-500 text-xs">No tasks yet.</div>')

    rows = []
    status_cls = {
        "done": "task-done", "running": "task-running", "failed": "task-failed",
        "pending": "task-pending", "blocked": "task-blocked",
    }
    for t in items:
        c = status_cls.get(t.status, "")
        ts_str = (t.finished_at or t.started_at or t.created_at)[:19]
        instr_short = t.instruction[:55] + ("..." if len(t.instruction) > 55 else "")
        # Full instruction in title= so it appears as a tooltip on hover.
        instr_full = _html_mod.escape(t.instruction)
        agent_tag = f'<span class="text-slate-600 text-xs w-16 truncate">{_html_mod.escape(str(t.agent or ""))}</span>' if t.agent else ""  # a task's agent and id come from the Brain API: markup in them ran (MN70)
        # Visually distinguish escalated tasks (session_id starts with "escalated-")
        is_escalated = (t.session_id or "").startswith("escalated-")
        row_extra = ' style="border-left: 2px solid #d97706;"' if is_escalated else ""
        esc_badge = '<span class="text-amber-400 text-xs shrink-0" title="Escalated to Claude Code">esc</span>' if is_escalated else ""
        rows.append(
            f'<div class="flex gap-2 py-1 border-b border-slate-700 row-hover items-center"{row_extra}'
            f' onclick="showTaskDetail({_js_arg(t.task_id)})">'
            f'<span class="text-slate-500 w-32 truncate text-xs">{_html_mod.escape(str(t.task_id))}</span>'
            f'<span class="{c} w-16 text-xs shrink-0">{_html_mod.escape(str(t.status))}</span>'
            f'<span class="text-slate-300 flex-1 text-xs truncate" title="{instr_full}">{_html_mod.escape(instr_short)}</span>'
            f'{esc_badge}'
            f'{agent_tag}'
            f'<span class="text-slate-500 w-36 text-xs shrink-0">{ts_str}</span>'
            f'</div>'
        )
    return HTMLResponse("\n".join(rows))


def _brain_listening(timeout: float = 3.0) -> bool:
    """Does the Brain's port accept a connection? A connect TIMEOUT alone cannot tell a busy Brain
    from an absent one: on Windows a refused localhost connect takes about two seconds to be
    reported, longer than our 0.75 s connect budget, so 'not running' also arrives as
    ConnectTimeout (AG97). Blocking, at most `timeout` seconds: call it from a worker thread."""
    import socket
    from urllib.parse import urlsplit
    try:
        u = urlsplit(BRAIN_URL)
        port = u.port or (443 if u.scheme == "https" else 80)
        with socket.create_connection((u.hostname or "127.0.0.1", port), timeout=timeout):
            return True
    except Exception:  # noqa: BLE001 - refused, unreachable, unresolvable, or no answer at all
        return False


def _brain_busy_detail() -> str:
    """The port answered late, not never (2026-11-21): a connect or read TIMEOUT against a listening
    Brain means its event loop was busy (a slow Ollama probe, a long chat), and telling the user to
    start a running service sent them hunting for a crash that did not happen."""
    return (f"Brain at {BRAIN_URL} is busy and did not answer in time (it is running; a slow model "
            "backend usually explains it). Try again in a few seconds.")


def _brain_error_detail(exc: Exception) -> str:
    """A 503 from a Brain call says why (MN85): a refused connect has an empty str() on Windows,
    so Run, Ask Claude, Quick Run and Retry showed a bare "Error: HTTP 503" while the Agents tab
    and the chat (MN56) named the fix. A timeout is "busy", not "not running" (2026-11-21)."""
    if isinstance(exc, httpx.ReadTimeout):   # it accepted the connection: it is there
        return _brain_busy_detail()
    if isinstance(exc, (httpx.ConnectTimeout, httpx.PoolTimeout)) and _brain_listening():
        return _brain_busy_detail()
    if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout)) or not str(exc).strip():
        return (f"Brain not running or not reachable at {BRAIN_URL}: start it (sextant-brain), "
                "or set ATLAS_BRAIN_URL.")
    return str(exc)


@app.post("/api/tasks", status_code=202)
async def submit_task(req: TaskSubmit, request: Request):
    """POST /api/tasks -- submit a new task to the Brain worker (proxies to Brain /task).

    Body: TaskSubmit {instruction, session_id, agent (optional)}.
    Broadcasts task_submitted SSE event on success. Returns 503 when Brain is unreachable.
    """
    _check_auth(request)
    body: dict = {"instruction": req.instruction, "session_id": req.session_id}
    if req.agent:
        body["agent"] = req.agent
    try:
        async with _PeerClient(timeout=httpx.Timeout(10.0, connect=0.75)) as client:
            r = await client.post(f"{BRAIN_URL}/task", json=body, headers=_brain_headers)
        r.raise_for_status()
        task = r.json()
        await _broadcast({"type": "task_submitted", "task": task})
        return task
    except Exception as exc:
        log.error("POST /api/tasks failed: %s", exc)
        raise HTTPException(status_code=503, detail=_brain_error_detail(exc))


@app.post("/api/tasks/claude", status_code=202)
async def submit_claude_task(req: TaskSubmit, request: Request):
    """Submit a task routed directly to Claude Code at priority 1, bypassing smart routing.

    This is the Web UI equivalent of the Telegram /claude command. Use it for
    anything that needs the full Claude context window: complex coding, architecture,
    multi-file edits, or tasks where Qwen3 has been struggling.
    """
    _check_auth(request)
    body: dict = {
        "instruction": req.instruction,
        "agent": "claude-code",
        "priority": 1,
        "session_id": req.session_id or "webui-claude-direct",
    }
    try:
        async with _PeerClient(timeout=httpx.Timeout(10.0, connect=0.75)) as client:
            r = await client.post(f"{BRAIN_URL}/task", json=body, headers=_brain_headers)
        r.raise_for_status()
        task = r.json()
        await _broadcast({"type": "task_submitted", "task": task})
        return task
    except Exception as exc:
        log.error("POST /api/tasks/claude failed: %s", exc)
        raise HTTPException(status_code=503, detail=_brain_error_detail(exc))


@app.get("/api/tasks/exhausted-badge", response_class=HTMLResponse)
async def tasks_exhausted_badge(request: Request):
    """HTMX badge: orange warning when tasks have exhausted all retry attempts.

    Returns an empty string (no badge) when no exhausted tasks exist, so the
    Tasks tab header stays clean in healthy operation.
    """
    _check_auth(request)
    try:
        from atlas_plus.brain import tasks as ts
        count = len(ts.list_exhausted_tasks(limit=50))
    except Exception:
        return HTMLResponse("")
    if count == 0:
        return HTMLResponse("")
    return HTMLResponse(
        f'<span class="text-xs bg-orange-900 text-orange-300 px-2 py-0.5 rounded font-semibold" '
        f'title="{count} task(s) exhausted all retries -- use /exhausted in Brain CLI">'
        f'{count} exhausted</span>'
    )


@app.get("/api/tasks/{task_id}")
async def get_task_json(task_id: str, request: Request):
    """GET /api/tasks/{id} -- fetch a single task by ID (proxies to Brain /task/{id}).

    Returns the full task dict. 404 when not found, 503 when Brain is unreachable.
    """
    _check_auth(request)
    try:
        async with _PeerClient(timeout=httpx.Timeout(5.0, connect=0.75)) as client:
            r = await client.get(f"{BRAIN_URL}/task/{task_id}", headers=_brain_headers)
        if r.status_code == 404:
            raise HTTPException(status_code=404, detail="Task not found")
        return r.json()
    except HTTPException:
        raise
    except Exception as exc:
        log.error("GET /api/tasks/%s failed: %s", task_id, exc)
        raise HTTPException(status_code=503, detail=_brain_error_detail(exc))


@app.get("/api/tasks/{task_id}/detail", response_class=HTMLResponse)
async def task_detail(task_id: str, request: Request):
    """GET /api/tasks/{id}/detail -- HTMX task detail panel with result + attempt history.

    Reads task directly from SQLite (no Brain HTTP round-trip). Shows status, agent,
    result preview, attempt history table, and artifact link when present.
    """
    _check_auth(request)
    from atlas_plus.brain import tasks as ts
    task = ts.get_task(task_id)
    if not task:
        return HTMLResponse(f'<div class="text-red-400 text-xs">Task {_html_mod.escape(task_id)} not found</div>')  # reflected markup ran in the page (AG73)
    sc = {
        "done": "text-green-400", "running": "text-yellow-400", "failed": "text-red-400",
        "pending": "text-blue-400", "blocked": "text-orange-400",
    }.get(task.status, "text-slate-300")
    result_html = ""
    if task.error:
        # Error takes precedence over result -- show it prominently in red.
        escaped = _html_mod.escape(task.error)
        result_html = (
            f'<pre class="mt-2 text-xs text-red-400 bg-slate-900 p-2 rounded overflow-auto '
            f'max-h-64 whitespace-pre-wrap">{escaped}</pre>'
        )
    elif task.result:
        # Show AI-generated digest above the full result when available
        digest_html = ""
        if getattr(task, "result_digest", None):
            digest_esc = _html_mod.escape(task.result_digest)
            digest_html = (
                f'<div class="mt-2 text-xs text-cyan-300 italic bg-slate-800 border border-slate-600 '
                f'p-2 rounded" title="AI-generated digest">'
                f'<span class="text-slate-500 font-semibold not-italic">Digest: </span>{digest_esc}</div>'
            )
        escaped = _html_mod.escape(task.result)
        result_html = (
            digest_html +
            f'<pre class="mt-2 text-xs text-slate-300 bg-slate-900 p-2 rounded overflow-auto '
            f'max-h-64 whitespace-pre-wrap">{escaped}</pre>'
        )
    retry_btn = ""
    if task.status in ("failed", "blocked"):
        retry_btn = (
            f'<button onclick="retryTask({_js_arg(task.task_id)})" '
            f'class="btn btn-yellow text-xs mt-2">Retry</button>'
        )
    escalate_btn = ""
    if task.agent != "claude-code" and task.status not in ("running",):
        escaped_instr = _html_mod.escape(task.instruction[:200]).replace("'", "\\'")
        escalate_btn = (
            f'<button onclick="escalateTask({_js_arg(task.task_id)})" '
            f'class="btn btn-amber text-xs mt-2 ml-2" title="Escalate to Claude Code">Escalate CC</button>'
        )
    bump_btn = ""
    if task.status == "pending" and task.priority > 1:
        bump_btn = (
            f'<button onclick="bumpTask({_js_arg(task.task_id)})" '
            f'class="btn btn-purple text-xs mt-2 ml-2">Bump to urgent</button>'
        )
    stop_btn = ""
    if task.status in ("running", "pending"):
        stop_btn = (
            f'<button onclick="stopTask({_js_arg(task.task_id)})" '
            f'class="btn btn-red text-xs mt-2 ml-2">Stop</button>'
        )
    # Attempt history section -- only rendered when at least one attempt was recorded
    attempts_html = ""
    try:
        attempts = ts.list_task_attempts(task_id)
        if attempts:
            _oc_class = {"done": "text-green-400", "failed": "text-red-400", "blocked": "text-orange-400"}
            rows = ""
            for a in attempts:
                oc_cls = _oc_class.get(a.get("outcome", ""), "text-slate-300")
                summary = _html_mod.escape((a.get("result_summary") or "")[:80])
                rows += (
                    f'<tr class="border-t border-slate-700">'
                    f'<td class="py-0.5 pr-2 text-slate-500">#{a.get("attempt_num","?")}</td>'
                    f'<td class="py-0.5 pr-2 {oc_cls}">{a.get("outcome","")}</td>'
                    f'<td class="py-0.5 pr-2 text-slate-400">{a.get("agent") or "(none)"}</td>'
                    f'<td class="py-0.5 pr-2 text-slate-500">{(a.get("finished_at") or "")[:16]}</td>'
                    f'<td class="py-0.5 text-slate-400 truncate max-w-xs" title="{summary}">{summary}</td>'
                    f'</tr>'
                )
            attempts_html = (
                f'<div class="mt-3 border-t border-slate-700 pt-2">'
                f'<div class="text-slate-500 text-xs font-semibold mb-1">Attempt History ({len(attempts)})</div>'
                f'<table class="text-xs w-full"><thead>'
                f'<tr class="text-slate-500"><th class="text-left pr-2">#</th>'
                f'<th class="text-left pr-2">outcome</th><th class="text-left pr-2">agent</th>'
                f'<th class="text-left pr-2">finished</th><th class="text-left">summary</th></tr>'
                f'</thead><tbody>{rows}</tbody></table></div>'
            )
    except Exception:
        pass

    return HTMLResponse(
        f'<div class="text-xs space-y-1">'
        f'<div class="flex gap-2"><span class="text-slate-500 w-20">task</span>'
        f'<span class="text-slate-300 font-mono">{_html_mod.escape(str(task.task_id))}</span></div>'
        f'<div class="flex gap-2"><span class="text-slate-500 w-20">status</span>'
        f'<span class="{sc} font-bold">{_html_mod.escape(str(task.status))}</span></div>'
        f'<div class="flex gap-2"><span class="text-slate-500 w-20">agent</span>'
        f'<span class="text-slate-400">{_html_mod.escape(str(task.agent or "auto"))}</span></div>'
        f'<div class="flex gap-2"><span class="text-slate-500 w-20">session</span>'
        f'<span class="text-slate-400 truncate">{_html_mod.escape(str(task.session_id or ""))}</span></div>'
        f'<div class="flex gap-2"><span class="text-slate-500 w-20">attempts</span>'
        f'<span class="text-slate-400">{task.attempts}/{task.max_attempts}</span></div>'
        f'<div class="flex gap-2"><span class="text-slate-500 w-20">started</span>'
        f'<span class="text-slate-400">{(task.started_at or "")[:19]}</span></div>'
        f'<div class="flex gap-2"><span class="text-slate-500 w-20">finished</span>'
        f'<span class="text-slate-400">{(task.finished_at or "")[:19]}</span></div>'
        f'<div class="mt-2 text-slate-400 border-t border-slate-700 pt-2">{_html_mod.escape(task.instruction)}</div>'
        f'{result_html}'
        f'{attempts_html}'
        f'<div class="flex gap-2 mt-2 flex-wrap">{retry_btn}{escalate_btn}{bump_btn}{stop_btn}</div>'
        f'</div>'
    )


@app.patch("/api/tasks/{task_id}/priority")
async def bump_task_priority(task_id: str, request: Request):
    """Proxy to Brain PATCH /task/{id}/priority."""
    _check_auth(request)
    body = await _json_object(request)
    try:
        async with _PeerClient(timeout=httpx.Timeout(10.0, connect=0.75)) as client:
            r = await client.patch(
                f"{BRAIN_URL}/task/{task_id}/priority",
                json=body,
                headers=_brain_headers,
            )
        r.raise_for_status()
        return r.json()
    except Exception as exc:
        log.error("PATCH /api/tasks/%s/priority failed: %s", task_id, exc)
        raise HTTPException(status_code=503, detail=_brain_error_detail(exc))


@app.post("/api/tasks/retry-failed", status_code=202)
async def retry_all_failed(request: Request):
    """Resubmit all failed tasks as new tasks. Returns count submitted."""
    _check_auth(request)
    from atlas_plus.brain import tasks as ts
    failed = ts.list_tasks(status="failed", limit=50)
    submitted = 0
    async with _PeerClient(timeout=httpx.Timeout(10.0, connect=0.75)) as client:
        for task in failed:
            body: dict = {"instruction": task.instruction, "session_id": task.session_id}
            if task.agent:
                body["agent"] = task.agent
            try:
                r = await client.post(f"{BRAIN_URL}/task", json=body, headers=_brain_headers)
                r.raise_for_status()
                submitted += 1
            except Exception:
                pass
    return {"submitted": submitted, "total_failed": len(failed)}


@app.post("/api/tasks/{task_id}/retry", status_code=202)
async def retry_task(task_id: str, request: Request):
    """POST /api/tasks/{id}/retry -- resubmit a single task as a new pending task.

    Copies instruction, session_id, and agent from the existing task.
    Used by the Tasks tab 'retry' button. Returns 404 when task not found.
    """
    _check_auth(request)
    from atlas_plus.brain import tasks as ts
    task = ts.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    body: dict = {"instruction": task.instruction, "session_id": task.session_id}
    if task.agent:
        body["agent"] = task.agent
    try:
        async with _PeerClient(timeout=httpx.Timeout(10.0, connect=0.75)) as client:
            r = await client.post(f"{BRAIN_URL}/task", json=body, headers=_brain_headers)
        r.raise_for_status()
        new_task = r.json()
        await _broadcast({"type": "task_submitted", "task": new_task})
        return new_task
    except Exception as exc:
        log.error("POST /api/tasks/%s/retry failed: %s", task_id, exc)
        raise HTTPException(status_code=503, detail=_brain_error_detail(exc))


@app.post("/api/tasks/{task_id}/stop")
async def stop_task(task_id: str, request: Request):
    """Cancel a running or pending task."""
    _check_auth(request)
    from atlas_plus.brain import tasks as ts
    task = ts.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    if task.status in ("done", "failed", "blocked"):
        return {"status": task.status, "detail": "already done"}
    ts.mark_failed(task_id, "Cancelled via Web UI.")
    return {"status": "cancelled"}


@app.get("/api/tasks/{task_id}/live")
async def task_live_stream(task_id: str, request: Request):
    """SSE stream: polls task status every 2s, sends updates until terminal state."""
    _check_auth(request)

    async def generator():
        """Poll task status every 2s and yield SSE events until the task reaches a terminal state or 5 minutes elapse."""
        from atlas_plus.brain import tasks as ts
        last_status = None
        for _ in range(150):  # max 5 minutes
            if await request.is_disconnected():
                break
            task = ts.get_task(task_id)
            if not task:
                yield f'data: {json.dumps({"type":"error","detail":"not found"})}\n\n'
                break
            if task.status != last_status:
                last_status = task.status
                yield f'data: {json.dumps({"type":"status","status":task.status,"task_id":task_id})}\n\n'
            if task.status in ("done", "failed", "blocked"):
                result = task.result or task.error or ""
                yield f'data: {json.dumps({"type":"complete","status":task.status,"result":result[:3000],"agent":task.agent or "auto"})}\n\n'
                break
            await asyncio.sleep(2)

    return StreamingResponse(generator(), media_type="text/event-stream")


@app.post("/api/scheduler/trigger/{job_id}")
async def scheduler_trigger_proxy(job_id: str, request: Request):
    """Proxy to Brain POST /scheduler/trigger/{job_id}."""
    _check_auth(request)
    try:
        async with _PeerClient(timeout=httpx.Timeout(10.0, connect=0.75)) as client:
            r = await client.post(
                f"{BRAIN_URL}/scheduler/trigger/{job_id}", headers=_brain_headers
            )
        r.raise_for_status()
        return r.json()
    except httpx.HTTPStatusError as exc:
        raise HTTPException(status_code=exc.response.status_code, detail=exc.response.text)
    except Exception as exc:
        log.error("POST /api/scheduler/trigger/%s failed: %s", job_id, exc)
        raise HTTPException(status_code=503, detail=_brain_error_detail(exc))


@app.get("/api/scheduler/jobs")
async def scheduler_jobs_proxy(request: Request):
    """Proxy to Brain GET /scheduler/jobs."""
    _check_auth(request)
    try:
        async with _PeerClient(timeout=httpx.Timeout(5.0, connect=0.75)) as client:
            r = await client.get(f"{BRAIN_URL}/scheduler/jobs", headers=_brain_headers)
        r.raise_for_status()
        return r.json()
    except Exception as exc:
        return {"running": False, "jobs": [], "error": str(exc)}


@app.post("/api/credit/reset")
async def credit_reset_proxy(request: Request):
    """Proxy to Brain POST /credit/reset -- clear credit exhaustion state."""
    _check_auth(request)
    try:
        async with _PeerClient(timeout=httpx.Timeout(5.0, connect=0.75)) as client:
            r = await client.post(f"{BRAIN_URL}/credit/reset", headers=_brain_headers)
        r.raise_for_status()
        return r.json()
    except Exception as exc:
        log.error("POST /api/credit/reset failed: %s", exc)
        raise HTTPException(status_code=503, detail=_brain_error_detail(exc))


@app.get("/api/tasks/export")
async def export_tasks(request: Request, fmt: str = "csv", limit: int = 500, status: Optional[str] = None):
    """Download task history as CSV or JSON."""
    _check_auth(request)
    from atlas_plus.brain import tasks as ts
    import csv, io

    items = ts.list_tasks(status=status, limit=min(limit, 2000))

    if fmt == "json":
        import json as _json
        content = _json.dumps([t.as_dict() for t in items], indent=2)
        return StreamingResponse(
            iter([content]),
            media_type="application/json",
            headers={"Content-Disposition": "attachment; filename=tasks.json"},
        )

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["task_id", "status", "agent", "instruction", "created_at",
                     "started_at", "finished_at", "result", "error"])
    def _trunc(s: str, n: int) -> str:
        return s[:n] + " [truncated]" if len(s) > n else s

    for t in items:
        writer.writerow([
            t.task_id, t.status, t.agent or "",
            _trunc(t.instruction or "", 200),
            t.created_at or "", t.started_at or "", t.finished_at or "",
            _trunc(t.result or "", 300), _trunc(t.error or "", 200),
        ])
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=tasks.csv"},
    )


@app.post("/api/memory/capture")
async def memory_capture(request: Request):
    """Quick capture: write a fact to memory via memory_remember tool."""
    _check_auth(request)
    raw = await request.body()
    if len(raw) > 1_000_000:
        raise HTTPException(status_code=413, detail="Capture too large (1 MB cap).")
    if _too_deep(raw):
        raise HTTPException(status_code=400, detail="Body nesting too deep.")
    try:
        body = json.loads(raw)
    except (ValueError, RecursionError):
        raise HTTPException(status_code=400, detail="Body must be JSON: {\"fact\": ..., \"scope\": ...}")
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Body must be a JSON object with a 'fact'.")
    scope = str(body.get("scope") or "preference:quick-capture")
    fact = str(body.get("fact") or "").strip()
    if not fact:
        raise HTTPException(status_code=400, detail="fact is required")
    try:
        from atlas_plus.mcp.memory_server.tools.remember import memory_remember
        # A bearer-authenticated capture is the user acting (like `sextant remember`), not a
        # background job: interactive, so ATLAS_AUTONOMOUS=off does not refuse it
        # (self-hoster pass 2026-11-10).
        result = await asyncio.to_thread(memory_remember, scope=scope, fact=fact,
                                         source="human-direct", interactive=True)
        return result
    except Exception as exc:
        log.error("POST /api/memory/capture failed: %s", exc)
        raise HTTPException(status_code=503, detail=str(exc))


# ---------------------------------------------------------------------------
# Memory file browser
# ---------------------------------------------------------------------------

@app.get("/api/memory/files", response_class=HTMLResponse)
async def memory_files(request: Request):
    """Collapsible directory tree of .memory/ as HTML."""
    _check_auth(request)
    _SKIP_DIRS = {"_inbox", "__pycache__", ".git", "versions", "_atlas", ".locks", "_sync"}  # copies and state are not notes (MN29: 86% of the tree was versions/)

    _FILE_SUFFIXES = (".md", ".txt", ".json", ".yaml", ".yml")
    from atlas_plus._fsutil import inside as _inside, entry_is_link as _entry_is_link

    def _render_tree(directory: str, root: str) -> str:
        # os.scandir: entry types come from the directory read (no per-entry
        # stat), the same measured pattern as the FTS sweep (quality loop
        # run 17: 263ms -> see commit for after, at 2,000 files).
        parts: list[str] = []
        try:
            with os.scandir(directory) as it:
                entries = sorted(
                    it, key=lambda e: (not e.is_dir(follow_symlinks=False), e.name.lower())
                )
        except (PermissionError, OSError):
            return ""
        for entry in entries:
            if entry.name in _SKIP_DIRS:
                continue
            if _entry_is_link(entry) and not _inside(Path(entry.path), Path(root)):
                continue  # a link out of the corpus is refused for reading and search; it was still listed by name (MN86)
            if entry.is_dir(follow_symlinks=False):
                inner = _render_tree(entry.path, root)
                parts.append(
                    f'<details open><summary class="text-slate-400 text-xs cursor-pointer py-0.5">'
                    f'{_html_mod.escape(entry.name)}/</summary>'
                    f'<div class="pl-3">{inner}</div></details>'
                )
            elif entry.name.endswith(_FILE_SUFFIXES):
                rel = os.path.relpath(entry.path, root).replace("\\", "/")
                parts.append(
                    f'<div class="text-cyan-400 text-xs py-0.5 cursor-pointer hover:text-cyan-300"'
                    f' onclick="readMemoryFile({_js_arg(rel)})">'
                    f'{_html_mod.escape(entry.name)}</div>'
                )
        return "\n".join(parts)

    if not MEMORY_ROOT.exists():
        return HTMLResponse('<div class="text-red-400 text-xs">No corpus yet: run sextant-init (the memory root does not exist).</div>')
    # to_thread: this walk previously ran synchronously INSIDE the async
    # handler, blocking the event loop for the whole render. The tree is cached per corpus signature
    # like the other slow panels (ATLAS_WEB_PANEL_CACHE=off recomputes) and sent gzip-compressed to a
    # client that accepts it: 3.5 MB and 3 s on every load at 20,000 notes (AG100).
    def _build() -> tuple[bytes, bytes]:
        import gzip as _gzip
        body = f'<div class="space-y-0.5">{_render_tree(str(MEMORY_ROOT), str(MEMORY_ROOT))}</div>'.encode("utf-8")
        return body, _gzip.compress(body, 6)
    plain, packed = await asyncio.to_thread(lambda: _cached_panel(f"memory-files:{MEMORY_ROOT}", _build))
    from fastapi.responses import Response as _Resp
    if "gzip" in (request.headers.get("accept-encoding") or "").lower():
        return _Resp(packed, media_type="text/html; charset=utf-8",
                     headers={"Content-Encoding": "gzip", "Vary": "Accept-Encoding"})
    return _Resp(plain, media_type="text/html; charset=utf-8", headers={"Vary": "Accept-Encoding"})


# Tailwind CSS 3.4.17 (Play CDN build), vendored unmodified from https://cdn.tailwindcss.com/3.4.17
# (MIT License, Copyright (c) Tailwind Labs, Inc.; see NOTICE). The browser fetched it from that host
# with no integrity check (MN86, sweep 25); it is now part of the package and pinned:
# sha384-igm5BeiBt36UU4gqwWS7imYmelpTsZlQ45FZf+XBn9MuJbn4nQr7yx1yFydocC/K
_TAILWIND_SRI = "sha384-igm5BeiBt36UU4gqwWS7imYmelpTsZlQ45FZf+XBn9MuJbn4nQr7yx1yFydocC/K"
_TAILWIND_BYTES: bytes | None = None


@app.api_route("/static/tailwind-3.4.17.js", methods=["GET", "HEAD"])
async def tailwind_asset():
    """A public library, no corpus data: served without a session like /expired (the tag loads before sign-in)."""
    global _TAILWIND_BYTES
    if _TAILWIND_BYTES is None:
        from importlib import resources as _res
        _TAILWIND_BYTES = (_res.files("atlas_plus.web") / "static" / "tailwind-3.4.17.js").read_bytes()
    from fastapi.responses import Response as _Resp
    return _Resp(_TAILWIND_BYTES, media_type="text/javascript; charset=utf-8",
                    headers={"Cache-Control": "public, max-age=31536000, immutable"})


@app.api_route("/expired", methods=["GET", "HEAD"], response_class=HTMLResponse)  # link checkers probe with HEAD (MN25)
async def session_expired_page():
    """Where a stale tab lands after a 401: sessions live in memory, so a restart, a key rotation
    or the 12-hour limit ends them (AG31, sweep 14)."""
    return HTMLResponse(
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width, initial-scale=1\"><title>Sextant: sign in</title>"  # unreadable at 0.33 scale on a phone (MN64)
        "<style>body{font-family:system-ui,sans-serif;background:#0f172a;color:#cbd5e1;padding:3rem;max-width:40rem}"
        "code{color:#67e8f9}</style></head><body><h1>Sign in</h1>"  # MN60: a first visit is not an expired session; the body explains both
        "<p>You have not signed in from this browser yet, or the dashboard restarted, its key was rotated, or the session reached its 12-hour limit.</p>"
        "<p>Open it again with your key: <code>http://&lt;host&gt;:&lt;port&gt;/?key=&lt;ATLAS_WEB_API_KEY&gt;</code></p>"
        "<p>Sessions are held in memory on purpose. Saved notes and tickets are untouched; text typed in the editor but not saved is gone with the page.</p></body></html>"
    )



from starlette.exceptions import HTTPException as _StarletteHTTPException  # noqa: E402
from fastapi.exception_handlers import http_exception_handler as _default_http_exception_handler  # noqa: E402


@app.exception_handler(_StarletteHTTPException)
async def _html_401_for_browsers(request: Request, exc: _StarletteHTTPException):
    """A browser navigating to `/` with a dead cookie sees the expired page (status 401), not
    `{"detail": "Unauthorized"}` (AG45, MN34); API callers keep the JSON."""
    if exc.status_code == 401 and request.method == "GET" and request.url.path == "/" and "text/html" in (request.headers.get("accept") or ""):
        page = session_expired_page()
        if hasattr(page, "__await__"):
            page = await page
        body = page.body
        if request.query_params.get("key"):  # a wrong key got the same "not signed in yet" text (MN64)
            body = body.replace(b"<h1>Sign in</h1>", b"<h1>Sign in</h1><p><strong>That key is not this dashboard's key.</strong> Check ATLAS_WEB_API_KEY in the dashboard's environment.</p>", 1)
        return HTMLResponse(body, status_code=401)
    return await _default_http_exception_handler(request, exc)

@app.get("/api/memory/file", response_class=HTMLResponse)
async def memory_file_read(request: Request, path: str = "", raw: str = ""):
    """Read a single memory file. ?path=identity/user.md ; &raw=1 returns the whole file as text
    (the editor loads that, so a save never truncates a long note: AG31, sweep 14)."""
    _check_auth(request)
    from starlette.responses import PlainTextResponse
    if not path or ".." in path:
        if raw:
            return PlainTextResponse("Invalid path.", status_code=400)
        return HTMLResponse('<div class="text-red-400 text-xs">Invalid path.</div>')
    # Normalise separators and resolve inside MEMORY_ROOT
    safe_path = path.replace("\\", "/").lstrip("/")
    target = (MEMORY_ROOT / safe_path).resolve()
    try:
        target.relative_to(MEMORY_ROOT.resolve())
    except ValueError:
        if raw:
            return PlainTextResponse("Path traversal rejected.", status_code=400)
        return HTMLResponse('<div class="text-red-400 text-xs">Path traversal rejected.</div>')
    if not target.exists():
        if raw:
            return PlainTextResponse(f"File not found: {safe_path}", status_code=404)  # never 200 HTML the editor could save (MN25)
        return HTMLResponse(f'<div class="text-red-400 text-xs">File not found: {_html_mod.escape(safe_path)}</div>')
    if raw:
        # bytes as they are: read_text turned CRLF into LF and a save rewrote every line (MN25)
        data = target.read_bytes()
        try:
            text_out = data.decode("utf-8")
        except UnicodeDecodeError:
            return PlainTextResponse("This note is not UTF-8; the editor would replace its bytes. Edit it in your editor.", status_code=415)  # MN29
        import hashlib as _hl
        return PlainTextResponse(text_out, headers={
            "X-Sextant-Etag": _hl.sha256(data).hexdigest(),
            # the textarea folds CRLF to LF and fetch().text() drops a BOM: the editor re-applies both
            # on save, so a no-edit Save is a no-op and a real edit keeps the note's shape (AG41)
            "X-Sextant-Eol": ("mixed" if ((b"\r\n" in data and _re.search(rb"(?<!\r)\n", data)) or _re.search(rb"\r(?!\n)", data)) else ("crlf" if b"\r\n" in data else "lf")),  # a lone CR counts as mixed too (AG44)  # a mixed note is normalised on save, and the editor says so (MN31/MN32)
            "X-Sextant-Bom": "1" if data.startswith(b"\xef\xbb\xbf") else "0",
        })
    full = target.read_text(encoding="utf-8", errors="replace")
    content = full[:8000]
    cut = (f'<div class="text-amber-400 text-xs mb-1">showing the first 8,000 of {len(full):,} characters; '
           f'Edit loads the whole file</div>') if len(full) > 8000 else ''
    return HTMLResponse(
        cut +
        f'<div class="flex items-center justify-between mb-1">'
        f'<span class="text-cyan-400 text-xs font-mono font-bold truncate">{_html_mod.escape(safe_path)}</span>'
        f'<button class="btn btn-slate text-xs shrink-0 ml-2" onclick="editMemoryFile({_js_arg(safe_path)})">Edit</button>'
        f'</div>'
        f'<pre id="mf-content" class="text-xs text-slate-300 whitespace-pre-wrap overflow-auto leading-relaxed">'
        f'{_html_mod.escape(content)}</pre>'
    )


@app.post("/api/memory/file")
async def memory_file_write(request: Request):
    """Write (overwrite) a .memory/ file. Versions before writing."""
    _check_auth(request)
    body = await _json_object(request, required=("path",))
    path = body.get("path", "")
    content = body.get("content", "")
    if not isinstance(content, str):
        raise HTTPException(status_code=400, detail="content must be a string")  # it said "Invalid path" (AG38)
    if not isinstance(path, str) or not path or ".." in path:
        raise HTTPException(status_code=400, detail="Invalid path")
    safe_path = path.replace("\\", "/").lstrip("/")
    # Same confinement as the MCP read path (audit 2026-11-09, row 14):
    # versions/, archive/, _sync/, _atlas/ and every dot-dir (.git) are
    # off limits, as are drive prefixes and encoded traversal.
    from atlas_plus.mcp.memory_server.config import EXCLUDED_DIRS, safe_resolve_within
    target = safe_resolve_within(MEMORY_ROOT, safe_path, exclude=set(EXCLUDED_DIRS) | {"_atlas"})
    if target is None or target == MEMORY_ROOT.resolve():
        raise HTTPException(status_code=400, detail="Path rejected")
    if not target.is_file():
        raise HTTPException(status_code=404, detail=f"File not found: {safe_path}")
    if len(content.encode("utf-8")) > 1_048_576 and not _same_bytes_as_disk(content, safe_path):  # 1 MB cap prevents accidental large writes; an unchanged note is "No change" first (AG51; the clause sat inside the comment until MN43)
        raise HTTPException(status_code=413, detail="Content exceeds 1 MB limit for memory files.")
    from atlas_plus.mcp.memory_server.routing import _atomic_write_text, _write_lock
    from atlas_plus.mcp.memory_server.versioning import version_before_write
    import hashlib as _hl
    base_etag = str(body.get("base_etag") or "").strip().lower()
    try:
        with _write_lock(target):
            try:
                current = target.read_bytes()
            except OSError:
                current = b""
            if base_etag and _hl.sha256(current).hexdigest() != base_etag:
                # another writer (a CLI remember, a sync) changed the note since the editor loaded
                # it; a save would silently drop that write (MN29)
                raise HTTPException(status_code=409, detail="The note changed since you opened it (another writer). Reload it and re-apply your edit.")
            _eol = lambda s: s.replace("\r\n", "\n").replace("\r", "\n")  # noqa: E731
            if content.encode("utf-8") == current or _eol(content) == _eol(current.decode("utf-8", "replace")):
                return {"ok": True, "unchanged": True, "path": safe_path}  # line endings alone are not an edit (MN34: a mixed note was rewritten)
            # A failed version copy aborts the write: never overwrite without
            # a way back.
            try:
                version_before_write(target, root=MEMORY_ROOT.resolve())
            except Exception as exc:
                raise HTTPException(
                    status_code=500, detail=f"Version copy failed; file not written: {exc}"
                ) from exc
            _atomic_write_text(target, content)
    except RuntimeError as exc:  # lock contention
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {"ok": True, "path": safe_path, "bytes": len(content.encode())}


# ---------------------------------------------------------------------------
# Config API
# ---------------------------------------------------------------------------

_TOGGLEABLE_VARS = {
    "ATLAS_AUTONOMOUS",
    "ATLAS_SMART_ROUTING",
    "ATLAS_SESSION_AUTONOMOUS",
    "ATLAS_CLAUDE_ESCALATION",
    "ATLAS_CLAUDE_QUALITY_ESCALATION",
    "ATLAS_TICKET_DISPATCH",
}


_CONFIG_SCOPE_NOTE = (
    "Toggles apply to this web process only. They do not change Brain, MCP "
    "or the worker, which read their own environment; set the variable in "
    "each service's environment and restart it to change those."
)


@app.get("/api/config", response_class=HTMLResponse)
async def config_get(request: Request):
    """Show current toggle state for key env vars."""
    _check_auth(request)
    rows: list[str] = []
    for var in sorted(_TOGGLEABLE_VARS):
        # unset = OFF for every gate, matching the canonical fail-closed
        # allowlist (the old "unset ATLAS_AUTONOMOUS shows on" contradicted it)
        raw = os.environ.get(var, "off").lower()
        is_on = raw in ("on", "1", "true")
        btn_cls = "btn-green" if is_on else "btn-slate"
        label = "on" if is_on else "off"
        rows.append(
            f'<div class="flex items-center justify-between py-0.5">'
            f'<span class="text-slate-400 text-xs">{var.replace("ATLAS_","").lower()}</span>'
            f'<button class="btn {btn_cls} text-xs" onclick="toggleConfig({_js_arg(var)})" '
            f'title="{_CONFIG_SCOPE_NOTE}">{label} (web process only)</button>'
            f'</div>'
        )
    # Scope disclaimer (audit 2026-11-09, row 12): these toggles flip the
    # env of THIS web process only; Brain, MCP and the worker keep their own.
    rows.append(
        f'<div class="text-amber-400 text-xs py-0.5">{_CONFIG_SCOPE_NOTE}</div>'
    )
    # Read-only rows
    brain_model = os.environ.get("ATLAS_BRAIN_MODEL", "qwen3:8b")
    ntfy_url = os.environ.get("ATLAS_NTFY_URL", "")
    ntfy_s = ntfy_url[:24] + "..." if len(ntfy_url) > 24 else (ntfy_url or "off")
    rows.append(
        f'<div class="flex items-center justify-between py-0.5">'
        f'<span class="text-slate-400 text-xs">brain model</span>'
        f'<span class="text-slate-500 text-xs">{_html_mod.escape(brain_model)}</span>'
        f'</div>'
    )
    rows.append(
        f'<div class="flex items-center justify-between py-0.5">'
        f'<span class="text-slate-400 text-xs">ntfy</span>'
        f'<span class="text-slate-500 text-xs truncate max-w-24">{_html_mod.escape(ntfy_s)}</span>'
        f'</div>'
    )
    sla_minutes = os.environ.get("ATLAS_SLA_MINUTES", "15")
    rows.append(
        f'<div class="flex items-center justify-between py-0.5">'
        f'<span class="text-slate-400 text-xs">sla_minutes</span>'
        f'<span class="text-slate-500 text-xs">{_html_mod.escape(sla_minutes)}</span>'
        f'</div>'
    )
    quality_threshold = os.environ.get("ATLAS_TICKET_QUALITY_THRESHOLD", "4.0")
    rows.append(
        f'<div class="flex items-center justify-between py-0.5">'
        f'<span class="text-slate-400 text-xs">ticket_quality_threshold</span>'
        f'<span class="text-slate-500 text-xs">{_html_mod.escape(quality_threshold)}</span>'
        f'</div>'
    )
    return HTMLResponse("\n".join(rows))


class ConfigToggle(BaseModel):
    """POST /api/config body: env var name to toggle (allowed vars checked against TOGGLE_VARS allowlist)."""

    var: str


@app.post("/api/config")
async def config_toggle(body: ConfigToggle, request: Request):
    """Toggle an env var on/off."""
    _check_auth(request)
    var = body.var
    if var not in _TOGGLEABLE_VARS:
        raise HTTPException(status_code=400, detail=f"Var {var!r} not toggleable.")
    raw = os.environ.get(var, "off").lower()  # unset = OFF (canonical)
    is_on = raw in ("on", "1", "true")
    new_val = "off" if is_on else "on"
    os.environ[var] = new_val
    # Side-effect: update in-process smart routing flag
    if var == "ATLAS_SMART_ROUTING":
        try:
            import atlas_plus.brain.agents as _ag
            _ag._SMART_ROUTING = (new_val == "on")
        except Exception:
            pass
    return {
        "var": var,
        "value": new_val,
        "scope": "web-process-only",
        "affects_other_services": False,
        "note": _CONFIG_SCOPE_NOTE,
    }


# ---------------------------------------------------------------------------
# Chat history
# ---------------------------------------------------------------------------

@app.get("/api/chat/history", response_class=HTMLResponse)
async def chat_history(request: Request, session_id: str = ""):
    """Fetch chat history from Brain and format as chat bubbles."""
    _check_auth(request)
    if not session_id:
        return HTMLResponse('<div class="text-slate-500 text-xs">No session_id provided.</div>')
    try:
        async with _PeerClient(timeout=httpx.Timeout(10.0, connect=0.75)) as client:
            r = await client.get(
                f"{BRAIN_URL}/session/{session_id}/history",
                params={"limit": "40"},
                headers=_brain_headers,
            )
        r.raise_for_status()
        data = r.json()
        messages = data.get("messages", [])
    except httpx.ConnectError:
        return HTMLResponse(f'<div class="text-red-400 text-xs">Brain not running or not reachable at {_html_mod.escape(BRAIN_URL)}: start it (<code>sextant-brain</code>), or set ATLAS_BRAIN_URL.</div>')
    except Exception as exc:
        return HTMLResponse(f'<div class="text-red-400 text-xs">Error: {_html_mod.escape(str(exc))}</div>')
    if not messages:
        return HTMLResponse('<div class="text-slate-500 text-xs">No history for this session.</div>')
    rows: list[str] = [f'<div class="text-slate-500 text-xs mb-2">{len(messages)} messages &mdash; session: {_html_mod.escape(session_id)}</div>']
    for m in messages:
        role = m.get("role", "")
        content = m.get("content", "")
        if role == "user":
            cls = "msg-user"
            prefix = "You"
        else:
            cls = "msg-brain"
            prefix = "Brain"
        rows.append(
            f'<div class="{cls} text-xs leading-relaxed mb-1">'
            f'<span class="font-bold">{prefix}:</span> {_html_mod.escape(str(content)[:500])}'
            f'</div>'
        )
    return HTMLResponse("\n".join(rows))


# ---------------------------------------------------------------------------
# Sparkline helper
# ---------------------------------------------------------------------------

def _sparkline_svg(scores: list[float], width: int = 64, height: int = 18) -> str:
    """Render a tiny inline SVG quality sparkline (oldest-first scores)."""
    if len(scores) < 2:
        return '<svg width="{}" height="{}"></svg>'.format(width, height)
    pts = []
    for i, s in enumerate(scores):
        x = round(i * (width - 1) / (len(scores) - 1))
        y = round(height - max(0.0, min(10.0, s)) / 10.0 * height)
        pts.append(f"{x},{y}")
    last = scores[-1]
    color = "#4ade80" if last >= 7 else ("#fbbf24" if last >= 4 else "#f87171")
    return (
        f'<svg width="{width}" height="{height}" viewBox="0 0 {width} {height}" '
        f'style="vertical-align:middle;flex-shrink:0;opacity:0.85">'
        f'<polyline points="{" ".join(pts)}" fill="none" stroke="{color}" '
        f'stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>'
        f"</svg>"
    )


# ---------------------------------------------------------------------------
# Improvement stats
# ---------------------------------------------------------------------------

@app.get("/api/improvement/stats", response_class=HTMLResponse)
async def improvement_stats_partial(request: Request):
    """GET /api/improvement/stats -- HTMX agent quality sparklines for Dashboard sidebar.

    Reads recent observations from the improvement DB. Each row shows: agent name,
    recent avg quality (0-10), sparkline SVG of last 12 scores (green/orange/red).
    Refreshes every 60s via HTMX. Falls back to 'no data' when improvement DB is empty.
    """
    _check_auth(request)
    try:
        from atlas_plus.brain import improvement as _imp
        stats = await asyncio.to_thread(_imp.all_agent_stats)
    except Exception as exc:
        return HTMLResponse(f'<div class="text-red-400 text-xs">Error: {exc}</div>')
    if not stats:
        return HTMLResponse('<div class="text-slate-500 text-xs">No observations yet.</div>')
    # Batch fetch all sparkline data in one query instead of N.
    _scores_by_agent = await asyncio.to_thread(_imp.all_agent_recent_scores, 10)
    rows = []
    for s in sorted(stats, key=lambda x: x.get("avg_quality") or 0, reverse=True):
        q = s.get("avg_quality")
        trend = s.get("trend")
        q_str = f"{q:.1f}" if q is not None else "n/a"
        trend_str = ""
        if trend is not None:
            trend_str = f"{'+'if trend>0 else ''}{trend:.1f}"
        color = ("text-green-400" if (q or 0) >= 7
                 else ("text-yellow-400" if (q or 0) >= 4 else "text-red-400"))
        recent = _scores_by_agent.get(s["agent"], [])
        spark = _sparkline_svg(recent)
        rows.append(
            f'<div class="flex gap-2 text-xs py-0.5 items-center">'
            f'<span class="text-slate-400 w-16 truncate">{s["agent"]}</span>'
            f'{spark}'
            f'<span class="{color} w-12 font-mono text-right">{q_str}</span>'
            f'<span class="text-slate-600 text-xs">{trend_str}</span>'
            f'</div>'
        )
    return HTMLResponse("\n".join(rows))


# ---------------------------------------------------------------------------
# Routing stats panel
# ---------------------------------------------------------------------------

@app.get("/api/routing/stats", response_class=HTMLResponse)
async def routing_stats_panel(request: Request):
    """Routing configuration and classification cache panel for the Agents tab."""
    _check_auth(request)
    try:
        async with _PeerClient(timeout=httpx.Timeout(5.0, connect=0.75)) as client:
            r = await client.get(f"{BRAIN_URL}/routing/stats", headers=_brain_headers)
        r.raise_for_status()
        data = r.json()
    except Exception as exc:
        return HTMLResponse(f'<div class="text-slate-500 text-xs">Routing stats unavailable: Brain not running or not reachable at {_html_mod.escape(BRAIN_URL)} (start it with <code>sextant-brain</code>, or set ATLAS_BRAIN_URL).</div>')  # the bare exception name said nothing (MN50)

    smart = data.get("smart_routing", False)
    local = data.get("local_only", False)
    cache_size = data.get("cache_size", 0)
    capacity = data.get("cache_capacity", 500)
    dist = data.get("category_distribution", {})

    smart_c = "text-green-400" if smart else "text-slate-500"
    local_c = "text-yellow-400" if local else "text-slate-500"
    lines = [
        f'<div class="text-xs space-y-1">',
        f'<div class="flex gap-4">',
        f'<span class="{smart_c}">smart routing: {"on" if smart else "off"}</span>',
        f'<span class="{local_c}">local only: {"on" if local else "off"}</span>',
        f'<span class="text-slate-500">cache: {cache_size}/{capacity}</span>',
        f'</div>',
    ]
    if dist:
        cats = " | ".join(f'{k}:{v}' for k, v in sorted(dist.items(), key=lambda x: -x[1]))
        lines.append(f'<div class="text-slate-600">categories: {cats}</div>')
    lines.append('</div>')
    return HTMLResponse("\n".join(lines))


# ---------------------------------------------------------------------------
# Dashboard summary stats
# ---------------------------------------------------------------------------

# Cache the memory file count -- rglob("*.md") is O(files) and called every 5s
# from the dashboard header poll. Cache for 60s; stale by at most one minute.
_mem_file_count_cache: tuple[float, int] = (float("-inf"), 0)  # (monotonic timestamp, count); -inf, not 0.0: CLOCK_MONOTONIC is seconds since boot, so a fresh host (a CI runner) read the empty sentinel as fresh for 60 s (CI on 434bb67)
_MEM_FILE_CACHE_TTL = 60.0


_TTL_CACHE: dict[str, tuple[float, object]] = {}


def _ttl_cached(key: str, build, ttl: float):
    import time as _time
    if os.environ.get("ATLAS_WEB_PANEL_CACHE", "on").strip().lower() not in ("on", "1", "true"):
        return build()  # the switch that turns the panel cache off turns this one off too (MN48 fold)
    hit = _TTL_CACHE.get(key)
    if hit and _time.monotonic() - hit[0] < ttl:
        return hit[1]
    value = build()
    _TTL_CACHE[key] = (_time.monotonic(), value)
    return value


def _locked_out_html(stats: dict) -> str:
    """Refusals made while a source was locked (counted outside the failure log since AG38; the HTML
    panel never showed them: MN31/MN32)."""
    ref = stats.get("locked_out_refusals") or {}
    if not ref:
        return ""
    items = ", ".join(f"{_html_mod.escape(str(ip))}: {int(n)}" for ip, n in list(ref.items())[:20])
    return f'<p class="text-amber-400">Refused while locked: {items}</p>'


def _get_mem_file_count() -> int:
    import time
    global _mem_file_count_cache
    ts, count = _mem_file_count_cache
    if time.monotonic() - ts < _MEM_FILE_CACHE_TTL:
        return count
    _skip = {"versions", "_atlas", "archive", "_sync"}
    count = (sum(1 for p in MEMORY_ROOT.rglob("*.md")
                 if p.relative_to(MEMORY_ROOT).parts[0] not in _skip  # top-level only: a user's own projects/archive/ is notes (MN25)
                 and not any(part.startswith(".") for part in p.relative_to(MEMORY_ROOT).parts))
             if MEMORY_ROOT.exists() else 0)  # whole-file version copies are not notes (AG31)
    _mem_file_count_cache = (time.monotonic(), count)
    return count


@app.get("/api/stats/summary", response_class=HTMLResponse)
async def stats_summary(request: Request):
    """Quick-glance counts for the dashboard header."""
    _check_auth(request)

    def _gather() -> dict:
        from atlas_plus.brain import tasks as ts
        from atlas_plus.tickets.store import list_tickets
        from datetime import date, datetime, timezone, timedelta

        today = date.today().isoformat()
        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%S")
        all_tasks = ts.list_tasks(limit=5000)  # 200 hid every older task: "0 done today" with 200 newer pending (MN70)
        pending   = sum(1 for t in all_tasks if t.status == "pending")
        running   = sum(1 for t in all_tasks if t.status == "running")
        stale     = sum(
            1 for t in all_tasks
            if t.status == "running" and t.heartbeat_at and t.heartbeat_at < cutoff
        )
        def _local_day(ts: str) -> str:  # finished_at is UTC: west of Greenwich the evening's work counted as 0 (MN64)
            try:
                return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone().date().isoformat()
            except (ValueError, TypeError):
                return (ts or "")[:10]
        done_today = sum(1 for t in all_tasks if t.status == "done" and t.finished_at and _local_day(t.finished_at) == today)
        failed_today = sum(1 for t in all_tasks if t.status == "failed" and t.finished_at and _local_day(t.finished_at) == today)
        open_tickets = sum(
            1 for t in list_tickets()
            if t.status in ("todo", "in-progress", "blocked")
        )
        mem_files = _get_mem_file_count()
        return {
            "pending": pending, "running": running, "stale": stale,
            "done_today": done_today, "failed_today": failed_today,
            "open_tickets": open_tickets, "mem_files": mem_files,
        }

    try:
        s = await asyncio.to_thread(_gather)
    except Exception as _stats_exc:
        log.warning("Stats summary failed: %s", _stats_exc)
        return HTMLResponse('<span class="text-slate-600 text-xs">stats unavailable</span>')

    failed_c = "text-red-400" if s["failed_today"] > 0 else "text-slate-600"
    running_c = "text-yellow-400" if s["running"] > 0 else "text-slate-600"
    stale_badge = (
        f'<span class="text-slate-600">|</span>'
        f'<span class="text-red-400" title="{s["stale"]} running task(s) with heartbeat older than 30 min -- worker may be stuck">'
        f'{s["stale"]} stale!</span>'
    ) if s.get("stale", 0) > 0 else ""
    return HTMLResponse(
        f'<div class="flex gap-3 text-xs flex-wrap">'
        f'<span class="{running_c}">{s["running"]} running</span>'
        f'<span class="text-slate-600">|</span>'
        f'<span class="text-green-400">{s["done_today"]} done today</span>'
        f'<span class="text-slate-600">|</span>'
        f'<span class="{failed_c}">{s["failed_today"]} failed today</span>'
        f'<span class="text-slate-600">|</span>'
        f'<span class="text-slate-400">{s["pending"]} pending</span>'
        f'{stale_badge}'
        f'<span class="text-slate-600">|</span>'
        f'<span class="text-slate-500">{s["open_tickets"]} open tickets</span>'
        f'<span class="text-slate-600">|</span>'
        f'<span class="text-slate-500">{s["mem_files"]} memory files</span>'
        f'</div>'
    )


# ---------------------------------------------------------------------------
# Research Quality panel (Dashboard left sidebar)
# ---------------------------------------------------------------------------

@app.get("/api/research-quality-panel", response_class=HTMLResponse)
async def research_quality_panel(request: Request):
    """HTMX panel: research report quality overview for the Dashboard sidebar.

    Shows: total reports, avg quality score (color-coded), low-quality count,
    and a 'view gaps' link. Refreshes every 60s. Uses check_research_quality()
    from doctor.cli and check_untagged_by_scope() for context.
    """
    _check_auth(request)

    def _gather() -> dict:
        from atlas_plus.doctor.cli import check_research_quality
        return check_research_quality()

    try:
        rq = await asyncio.to_thread(_gather)
    except Exception as exc:
        return HTMLResponse(f'<div class="text-slate-600 text-xs">unavailable</div>')

    total = rq.get("total_reports", 0)
    scored = rq.get("scored_reports", 0)
    avg = rq.get("avg_score", 0.0)
    low = rq.get("low_quality_count", 0)
    min_file = rq.get("min_score_file", "")

    if total == 0:
        return HTMLResponse('<div class="text-slate-600 text-xs">No reports yet.</div>')

    avg_c = ("text-green-400" if avg >= 0.7
             else ("text-yellow-400" if avg >= 0.4 else "text-red-400"))
    low_badge = (
        f'<span class="text-orange-400 ml-1" title="{low} reports scored below 0.5">'
        f'{low} low</span>'
    ) if low > 0 else ""
    unscored_note = (
        f'<div class="text-slate-600 text-xs mt-0.5">{total - scored} unscored</div>'
    ) if scored < total else ""

    min_line = ""
    if min_file:
        esc_file = _html_mod.escape(min_file)
        esc_short = _html_mod.escape(min_file[:20])
        min_line = f'<div class="text-slate-600 truncate" title="{esc_file}">lowest: {esc_short}</div>'

    return HTMLResponse(
        f'<div class="text-xs space-y-0.5">'
        f'<div><span class="text-slate-400">{scored}/{total} reports scored</span>{low_badge}</div>'
        f'<div>avg: <span class="{avg_c} font-mono">{avg:.2f}</span></div>'
        f'{unscored_note}'
        f'{min_line}'
        f'</div>'
    )


# ---------------------------------------------------------------------------
# Pending facts (task-to-memory pipeline)
# ---------------------------------------------------------------------------

@app.get("/api/pending-facts", response_class=HTMLResponse)
async def pending_facts_panel(request: Request):
    """Render the pending facts review panel for the Memory tab."""
    _check_auth(request)
    try:
        async with _PeerClient(timeout=httpx.Timeout(5.0, connect=0.75)) as client:
            r = await client.get(f"{BRAIN_URL}/pending-facts?status=pending&limit=20", headers=_brain_headers)
        r.raise_for_status()
        facts = r.json().get("facts", [])
    except (httpx.ConnectError, httpx.TimeoutException):
        return HTMLResponse('<div class="text-slate-500 text-xs">Brain not reachable (optional: this panel fills when the Brain runs).</div>')  # a bare `Error:` under compose (AG37)
    except Exception as exc:
        return HTMLResponse(f'<div class="text-red-400 text-xs">Error: {_html_mod.escape(str(exc) or exc.__class__.__name__)}</div>')

    if not facts:
        return HTMLResponse('<div class="text-slate-500 text-xs">No pending fact candidates. Candidates appear here after substantial Brain tasks complete.</div>')

    rows = [
        f'<div class="text-slate-500 text-xs mb-2">{len(facts)} candidate(s) pending review -- from completed Brain tasks</div>',
        '<div class="space-y-1">',
    ]
    for f in facts:
        fid = f["id"]
        text = _html_mod.escape(f["fact_text"][:180])
        conf = f.get("confidence", 0)
        conf_c = "text-green-400" if conf >= 0.7 else ("text-yellow-400" if conf >= 0.5 else "text-slate-400")
        task_short = f.get("task_id", "")[:16]
        fid_js = _js_arg(fid) if isinstance(fid, str) else json.dumps(fid)
        scope_hint = _html_mod.escape(f.get("scope_hint", "reference:task-result"))
        rows.append(
            f'<div class="border border-slate-700 rounded p-2 text-xs">'
            f'<div class="flex items-start justify-between gap-2">'
            f'<div class="flex-1 min-w-0">'
            f'<div class="text-slate-300 leading-relaxed">{text}</div>'
            f'<div class="flex gap-3 mt-1 text-slate-600">'
            f'<span class="{conf_c}">conf: {conf:.2f}</span>'
            f'<span>task: {task_short}</span>'
            f'<span>scope: {scope_hint}</span>'
            f'</div></div>'
            f'<div class="flex gap-1 shrink-0">'
            f'<button onclick="approveFact({fid_js})" class="btn btn-green text-xs">Save</button>'
            f'<button onclick="rejectFact({fid_js})" class="btn btn-red text-xs">Reject</button>'
            f'</div></div></div>'
        )
    rows.append('</div>')
    return HTMLResponse("\n".join(rows))


@app.post("/api/pending-facts/{fact_id}/approve")
async def approve_fact_proxy(fact_id: str, request: Request):
    """POST /api/pending-facts/{id}/approve -- approve a pending fact (proxies to Brain).

    Writes the approved fact to .memory/ when ATLAS_AUTONOMOUS=on.
    Body is forwarded to Brain /pending-facts/{id}/approve.
    """
    _check_auth(request)
    body = {}
    try:
        body = await _json_object(request)
    except Exception:
        pass
    try:
        async with _PeerClient(timeout=httpx.Timeout(10.0, connect=0.75)) as client:
            r = await client.post(
                f"{BRAIN_URL}/pending-facts/{fact_id}/approve",
                json=body, headers=_brain_headers,
            )
        r.raise_for_status()
        return r.json()
    except Exception as exc:
        raise HTTPException(status_code=503, detail=_brain_error_detail(exc))


@app.post("/api/pending-facts/{fact_id}/reject")
async def reject_fact_proxy(fact_id: str, request: Request):
    """POST /api/pending-facts/{id}/reject -- reject and discard a pending fact.

    Records rejection in the anti-memory log so the agent won't re-propose it.
    Proxies to Brain /pending-facts/{id}/reject.
    """
    _check_auth(request)
    try:
        async with _PeerClient(timeout=httpx.Timeout(5.0, connect=0.75)) as client:
            r = await client.post(
                f"{BRAIN_URL}/pending-facts/{fact_id}/reject",
                headers=_brain_headers,
            )
        r.raise_for_status()
        return r.json()
    except Exception as exc:
        raise HTTPException(status_code=503, detail=_brain_error_detail(exc))


@app.get("/api/proposals", response_class=HTMLResponse)
async def proposals_panel(request: Request):
    """The proposals desk (ATL-073): what the weekly supervisor and the reflexion loop propose,
    one click each (approve / edit / reject). Proxies Brain /proposals."""
    _check_auth(request)
    try:
        async with _PeerClient(timeout=httpx.Timeout(5.0, connect=0.75)) as client:
            r = await client.get(f"{BRAIN_URL}/proposals?status=pending&limit=30", headers=_brain_headers)
        r.raise_for_status()
        rows_in = r.json().get("proposals", [])
    except (httpx.ConnectError, httpx.TimeoutException):
        return HTMLResponse('<div class="text-slate-500 text-xs">Brain not reachable (optional: this panel fills when the Brain runs).</div>')
    except Exception as exc:
        return HTMLResponse(f'<div class="text-red-400 text-xs">Error: {_html_mod.escape(str(exc) or exc.__class__.__name__)}</div>')

    if not rows_in:
        return HTMLResponse('<div class="text-slate-500 text-xs">No proposals waiting. The Monday supervisor and the Sunday reflexion loop file them here.</div>')

    kind_c = {"lesson": "text-violet-300", "ticket": "text-sky-300", "fact": "text-emerald-300"}
    rows = [
        f'<div class="text-slate-500 text-xs mb-2">{len(rows_in)} proposal(s) waiting for your click</div>',
        '<div class="space-y-1">',
    ]
    for p in rows_in:
        pid = str(p.get("id", ""))
        if not re.fullmatch(r"[0-9a-f]{32}", pid):
            continue
        kind = str(p.get("kind", ""))
        title = _html_mod.escape(str(p.get("title", ""))[:200])
        body = _html_mod.escape(str(p.get("body", ""))[:280])
        source = _html_mod.escape(str(p.get("source", ""))[:40])
        created = _html_mod.escape(str(p.get("created_at", ""))[:10])
        rows.append(
            f'<div class="border border-slate-700 rounded p-2 text-xs" id="prop-{pid}">'
            f'<div class="flex items-start justify-between gap-2">'
            f'<div class="flex-1 min-w-0">'
            f'<div class="text-slate-200"><span class="{kind_c.get(kind, "text-slate-400")} font-bold mr-1">{_html_mod.escape(kind)}</span>{title}</div>'
            f'<div class="text-slate-400 leading-relaxed mt-1 whitespace-pre-wrap">{body}</div>'
            f'<div class="flex gap-3 mt-1 text-slate-600"><span>from: {source}</span><span>{created}</span></div>'
            f'</div>'
            f'<div class="flex gap-1 shrink-0">'
            f'<button onclick="approveProposal({_js_arg(pid)})" class="btn btn-green text-xs">Approve</button>'
            f'<button onclick="editProposal({_js_arg(pid)})" class="btn btn-slate text-xs">Edit</button>'
            f'<button onclick="rejectProposal({_js_arg(pid)})" class="btn btn-red text-xs">Reject</button>'
            f'</div></div></div>'
        )
    rows.append('</div>')
    return HTMLResponse("\n".join(rows))


@app.get("/api/proposals/{proposal_id}/body")
async def proposal_body_proxy(proposal_id: str, request: Request):
    """The full body of one pending proposal (the Edit button loads it)."""
    _check_auth(request)
    if not re.fullmatch(r"[0-9a-f]{32}", proposal_id):
        raise HTTPException(status_code=400, detail="bad proposal id")
    async with _PeerClient(timeout=httpx.Timeout(5.0, connect=0.75)) as client:
        r = await client.get(f"{BRAIN_URL}/proposals?status=pending&limit=200", headers=_brain_headers)
    r.raise_for_status()
    for p in r.json().get("proposals", []):
        if p.get("id") == proposal_id:
            return {"id": proposal_id, "body": p.get("body", ""), "title": p.get("title", "")}
    raise HTTPException(status_code=404, detail="proposal not pending")


@app.post("/api/proposals/{proposal_id}/approve")
async def approve_proposal_proxy(proposal_id: str, request: Request):
    """Approve (optionally with an edited body); proxies Brain /proposals/{id}/approve."""
    _check_auth(request)
    if not re.fullmatch(r"[0-9a-f]{32}", proposal_id):
        raise HTTPException(status_code=400, detail="bad proposal id")
    body = {}
    try:
        body = await request.json()
    except Exception:
        pass
    async with _PeerClient(timeout=httpx.Timeout(20.0, connect=0.75)) as client:
        r = await client.post(f"{BRAIN_URL}/proposals/{proposal_id}/approve", headers=_brain_headers,
                              json=body if isinstance(body, dict) else {})
    if r.status_code >= 400:
        try:
            detail = r.json().get("detail", r.text)
        except Exception:
            detail = r.text
        raise HTTPException(status_code=r.status_code, detail=str(detail)[:400])
    return r.json()


@app.post("/api/proposals/{proposal_id}/reject")
async def reject_proposal_proxy(proposal_id: str, request: Request):
    _check_auth(request)
    if not re.fullmatch(r"[0-9a-f]{32}", proposal_id):
        raise HTTPException(status_code=400, detail="bad proposal id")
    async with _PeerClient(timeout=httpx.Timeout(5.0, connect=0.75)) as client:
        r = await client.post(f"{BRAIN_URL}/proposals/{proposal_id}/reject", headers=_brain_headers)
    r.raise_for_status()
    return r.json()


@app.get("/api/pending-facts/count")
async def pending_facts_count_proxy(request: Request):
    """Return {count} of pending facts -- used by Memory tab badge."""
    _check_auth(request)
    try:
        async with _PeerClient(timeout=httpx.Timeout(3.0, connect=0.75)) as client:
            r = await client.get(f"{BRAIN_URL}/pending-facts/count", headers=_brain_headers)
        if r.status_code == 200:
            return r.json()
        return {"count": 0}
    except Exception:
        return {"count": 0}


# ---------------------------------------------------------------------------
# Memory gaps (ATL-N06)
# ---------------------------------------------------------------------------

@app.post("/api/memory/rebuild")
async def memory_rebuild(request: Request):
    """Rebuild the FTS5 full-text search index from scratch."""
    _check_auth(request)

    def _do_rebuild() -> int:
        from atlas_plus.mcp.memory_server.fts import rebuild
        return rebuild(force=True)

    try:
        count = await asyncio.to_thread(_do_rebuild)
        return {"ok": True, "indexed": count}
    except Exception as exc:
        log.error("POST /api/memory/rebuild failed: %s", exc)
        raise HTTPException(status_code=503, detail=str(exc))


@app.post("/api/memory/suggest-tags")
async def memory_suggest_tags(request: Request):
    """Scan .memory/ for untagged files and apply deterministic tags.

    Body: {write: bool} -- write=false returns candidates without applying tags;
    write=true injects tags into frontmatter (requires ATLAS_AUTONOMOUS=on).
    Returns {candidates: [{path, tags}], applied: int, dry_run: bool}.
    """
    _check_auth(request)
    body = {}
    try:
        body = await _json_object(request)
    except Exception:
        pass
    write = bool(body.get("write", False))

    def _do_suggest() -> dict:
        from atlas_plus.curation.tag_suggest import suggest_tags_for_file, inject_tags
        from atlas_plus._fsutil import inside as _inside
        _excluded = {"archive", "_sync", "versions", "sessions"}
        candidates = []
        for md_file in sorted(MEMORY_ROOT.rglob("*.md")):
            rel_parts = md_file.relative_to(MEMORY_ROOT).parts
            if rel_parts and rel_parts[0] in _excluded:
                continue
            if not _inside(md_file, MEMORY_ROOT):
                continue  # a link out of the corpus: never read, never tagged (MN86, sweep 25)
            tags = suggest_tags_for_file(md_file, memory_root=MEMORY_ROOT, max_tags=4)
            if tags:
                candidates.append({
                    "path": str(md_file.relative_to(MEMORY_ROOT)).replace("\\", "/"),
                    "tags": tags,
                })

        applied = 0
        if write:
            from atlas_plus.autonomous import autonomous_enabled
            if not autonomous_enabled():
                raise RuntimeError("ATLAS_AUTONOMOUS is not enabled -- cannot write tags")
            for item in candidates:
                md_file = MEMORY_ROOT / item["path"]
                if inject_tags(md_file, item["tags"]):
                    applied += 1

        return {
            "candidates": candidates[:100],
            "total": len(candidates),
            "applied": applied,
            "dry_run": not write,
        }

    try:
        result = await asyncio.to_thread(_do_suggest)
        return result
    except Exception as exc:
        log.error("POST /api/memory/suggest-tags failed: %s", exc)
        raise HTTPException(status_code=503, detail=str(exc))


@app.post("/api/memory/suggest-facts")
async def memory_suggest_facts(request: Request):
    """Run suggest-facts to extract candidate facts from recent session transcripts.

    Body: {since: str, auto: bool, skip_similar: float, confidence_threshold: float}
      since:                time window (default "today", accepts "4h", "2026-05-21", etc.)
      auto:                 if true, auto-approve facts with confidence >= 0.95 (requires ATLAS_AUTONOMOUS)
      skip_similar:         Jaccard similarity cutoff for auto-skip (0.0 = disabled, e.g. 0.6)
      confidence_threshold: override minimum confidence (0.0 = use env default 0.70)
    Returns {candidates: int, approved: int, written: int, rejected_low_confidence: int, dry_run: bool}.
    """
    _check_auth(request)
    body = {}
    try:
        body = await _json_object(request)
    except Exception:
        pass
    since = str(body.get("since", "today"))
    auto = bool(body.get("auto", False))
    skip_similar = float(body.get("skip_similar", 0.0))
    confidence_threshold_raw = body.get("confidence_threshold", 0.0)
    confidence_threshold: float | None = float(confidence_threshold_raw) if confidence_threshold_raw else None

    if auto:
        from atlas_plus.autonomous import autonomous_enabled
        if not autonomous_enabled():
            raise HTTPException(status_code=503, detail="ATLAS_AUTONOMOUS is not enabled -- cannot write facts")

    def _do_suggest() -> dict:
        from atlas_plus.curation.suggest_facts import suggest_facts
        return suggest_facts(
            since=since,
            source="claude-code",
            dry_run=not auto,
            auto=auto,
            skip_similar_threshold=skip_similar,
            confidence_threshold=confidence_threshold,
        )

    try:
        stats = await asyncio.to_thread(_do_suggest)
        return {
            "candidates": stats.get("candidates", 0),
            "approved": stats.get("approved", 0),
            "written": stats.get("written", 0),
            "rejected_low_confidence": stats.get("rejected_low_confidence", 0),
            "dry_run": not auto,
            "skip_similar": skip_similar,
        }
    except Exception as exc:
        log.error("POST /api/memory/suggest-facts failed: %s", exc)
        raise HTTPException(status_code=503, detail=str(exc))


@app.get("/api/memory/gaps", response_class=HTMLResponse)
async def memory_gaps(request: Request, since_hours: float = 168.0):
    """Top miss queries from the MCP search log (last 7 days by default)."""
    _check_auth(request)

    def _get_gaps() -> list[dict]:
        from atlas_plus.mcp.memory_server.tools.search_log import top_miss_queries
        return top_miss_queries(since_hours=since_hours, limit=20)

    try:
        gaps = await asyncio.to_thread(_get_gaps)
    except Exception as exc:
        return HTMLResponse(f'<div class="text-red-400 text-xs">Error: {exc}</div>')

    if not gaps:
        return HTMLResponse(
            '<div class="text-slate-500 text-xs">No miss data yet -- run some memory searches first.</div>'
        )
    days = int(since_hours // 24)
    rows = [
        f'<div class="text-slate-500 text-xs mb-2">Top miss queries (last {days}d) -- '
        f'things Brain searched for but didn\'t find</div>',
        '<div class="space-y-0.5">',
    ]
    for g in gaps:
        pct = round(g["miss_count"] / g["total"] * 100) if g["total"] else 0
        bar_w = max(2, pct // 2)  # 50px max bar
        color = "bg-red-500" if pct >= 80 else ("bg-yellow-500" if pct >= 50 else "bg-slate-500")
        qc = "text-red-400" if pct >= 80 else ("text-yellow-400" if pct >= 50 else "text-slate-400")
        rows.append(
            f'<div class="flex items-center gap-2 py-0.5">'
            f'<div class="flex-1 min-w-0">'
            f'<div class="text-slate-300 text-xs truncate">{_html_mod.escape(g["query"][:50])}</div>'
            f'<div class="flex items-center gap-1 mt-0.5">'
            f'<div class="{color} h-1 rounded" style="width:{bar_w}px"></div>'
            f'</div>'
            f'</div>'
            f'<span class="{qc} text-xs font-mono w-16 text-right shrink-0">{g["miss_count"]}/{g["total"]} {pct}%</span>'
            f'</div>'
        )
    rows.append('</div>')
    return HTMLResponse("\n".join(rows))


# ---------------------------------------------------------------------------
# Memory next-actions (prioritized to-do list)
# ---------------------------------------------------------------------------

@app.get("/api/memory/next-actions")
async def memory_next_actions(request: Request, limit: int = 5):
    """Return prioritized memory maintenance to-do list as JSON.

    Categories (in priority order):
      decayed   -- files past their half_life: threshold
      orphans   -- unreferenced low-health old files
      untagged  -- files missing tags (quick fix)
      gaps      -- recurring miss queries with no memory coverage
    """
    _check_auth(request)

    def _gather() -> dict:
        from atlas_plus.curation.decay import find_decayed_facts
        from atlas_plus.curation.orphan import find_orphan_candidates
        from atlas_plus.curation.scan import is_excluded_mirror
        from atlas_plus.curation.tag_suggest import suggest_tags_for_file
        from atlas_plus.mcp.memory_server.tools.search_log import top_miss_queries
        from atlas_plus.mcp.memory_server.config import MEMORY_ROOT

        result: dict = {}

        try:
            result["decayed"] = [c.as_dict() for c in find_decayed_facts(threshold_days=0)[:limit]]
        except Exception:
            result["decayed"] = []

        try:
            result["orphans"] = find_orphan_candidates(
                MEMORY_ROOT, min_age_days=90, max_health=0.5, limit=limit
            )
        except Exception:
            result["orphans"] = []

        try:
            from atlas_plus._fsutil import inside as _fs_inside
            untagged = []
            for md_file in sorted(MEMORY_ROOT.rglob("*.md")):
                if len(untagged) >= limit:
                    break
                rel_parts = md_file.relative_to(MEMORY_ROOT).parts
                if rel_parts and rel_parts[0] in {"archive", "_sync", "versions", "sessions", "anti"}:
                    continue
                if is_excluded_mirror(rel_parts):
                    continue
                if md_file.name.startswith("_") or not _fs_inside(md_file, MEMORY_ROOT):
                    continue  # a link out of the corpus (MN86, sweep 25)
                tags = suggest_tags_for_file(md_file, memory_root=MEMORY_ROOT, max_tags=4)
                if tags:
                    untagged.append({
                        "path": str(md_file.relative_to(MEMORY_ROOT)).replace("\\", "/"),
                        "suggested_tags": tags,
                    })
            result["untagged"] = untagged
        except Exception:
            result["untagged"] = []

        try:
            result["gaps"] = top_miss_queries(since_hours=168, limit=limit)
        except Exception:
            result["gaps"] = []

        return result

    try:
        data = await asyncio.to_thread(lambda: _cached_panel(f"next-actions:{max(1, min(int(limit), 50))}", _gather))
        return data
    except Exception as exc:
        return {"error": str(exc), "decayed": [], "orphans": [], "untagged": [], "gaps": []}


@app.get("/api/memory/next-actions-panel", response_class=HTMLResponse)
async def memory_next_actions_panel(request: Request):
    """HTMX panel: compact next-actions summary for the Memory tab.

    Shows item counts + top entry per category. Fast: decay and gaps only for
    speed, orphan/untagged checked with small limits.
    """
    _check_auth(request)

    def _fast_gather() -> dict:
        from atlas_plus.curation.decay import find_decayed_facts
        from atlas_plus.mcp.memory_server.tools.search_log import top_miss_queries
        from atlas_plus.mcp.memory_server.config import MEMORY_ROOT
        from atlas_plus.curation.orphan import find_orphan_candidates

        decayed = _cached_panel("decayed", lambda: find_decayed_facts(threshold_days=0))
        orphans = _cached_panel("orphans:5", lambda: find_orphan_candidates(MEMORY_ROOT, min_age_days=90, max_health=0.5, limit=5))
        gaps = top_miss_queries(since_hours=168, limit=5)
        return {"decayed": decayed, "orphans": orphans, "gaps": gaps}

    try:
        data = await asyncio.to_thread(_fast_gather)
    except Exception as exc:
        return HTMLResponse(f'<div class="text-red-400 text-xs">Error: {exc}</div>')

    decayed = data["decayed"]
    orphans = data["orphans"]
    gaps = data["gaps"]
    total = len(decayed) + len(orphans) + len(gaps)

    if total == 0:
        return HTMLResponse(
            # Says what it counts: "Memory in good shape" sat beside a curation panel with 1000 duplicates (MN93 F4).
            '<div class="text-green-500 text-xs">No urgent actions: no decayed facts, orphan notes or search gaps '
            '(duplicates and stale files are in the curation scan).</div>'
        )

    rows = ['<div class="space-y-1">']

    if decayed:
        rows.append(f'<div class="text-red-400 text-xs font-semibold">{len(decayed)} decayed</div>')
        for c in decayed[:3]:
            p = _html_mod.escape(c.path[:50])
            rows.append(
                f'<div class="text-slate-400 text-xs truncate pl-2" title="{p}">'
                f'{p} <span class="text-red-500">{c.days_overdue}d overdue</span></div>'
            )

    if orphans:
        rows.append(f'<div class="text-yellow-400 text-xs font-semibold mt-1">{len(orphans)} orphans</div>')
        for o in orphans[:2]:
            p = _html_mod.escape(o["path"][:50])
            rows.append(
                f'<div class="text-slate-400 text-xs truncate pl-2" title="{p}">'
                f'{p} <span class="text-slate-500">h={o["score"]:.2f}</span></div>'
            )

    if gaps:
        rows.append(f'<div class="text-blue-400 text-xs font-semibold mt-1">{len(gaps)} gaps</div>')
        for g in gaps[:2]:
            q = _html_mod.escape(g["query"][:45])
            rows.append(
                f'<div class="text-slate-400 text-xs truncate pl-2" title="{q}">'
                f'{q} <span class="text-slate-500">{g["miss_count"]}x</span></div>'
            )

    rows.append('</div>')
    return HTMLResponse("\n".join(rows))


# ---------------------------------------------------------------------------
# Memory convergence (contradiction flags)
# ---------------------------------------------------------------------------

@app.get("/api/memory/convergence", response_class=HTMLResponse)
async def memory_convergence(request: Request):
    """List .memory/ files with unresolved contradiction flags."""
    _check_auth(request)

    def _get_convergence() -> list[dict]:
        from atlas_plus.curation.weekly_report import _scan_convergence
        return _ttl_cached("convergence", lambda: _scan_convergence(MEMORY_ROOT), ttl=300.0)  # the one Memory panel that was recomputed on every poll (MN48)

    try:
        flagged = await asyncio.to_thread(_get_convergence)
    except Exception as exc:
        return HTMLResponse(f'<div class="text-red-400 text-xs">Error: {exc}</div>')

    if not flagged:
        return HTMLResponse(
            '<div class="text-slate-500 text-xs">No contradiction flags found -- memory is clean.</div>'
        )

    rows = [
        f'<div class="text-slate-500 text-xs mb-2">{len(flagged)} file(s) with unresolved contradiction flags</div>',
        '<div class="space-y-0.5">',
    ]
    for item in flagged[:20]:
        path_escaped = _html_mod.escape(item["path"])
        contras = ", ".join(_html_mod.escape(c) for c in item["contradictions"])
        rows.append(
            f'<div class="flex items-start gap-1 py-0.5 row-hover" onclick="readMemoryFile({_js_arg(item["path"])})" title="Click to open">'
            f'<div class="flex-1 min-w-0">'
            f'<div class="text-yellow-300 text-xs font-mono truncate">{path_escaped}</div>'
            f'<div class="text-slate-500 text-xs truncate">contradicts: {contras}</div>'
            f'</div>'
            f'</div>'
        )
    if len(flagged) > 20:
        rows.append(f'<div class="text-slate-600 text-xs mt-1">+{len(flagged) - 20} more. Run atlas-review-memory convergence for full report.</div>')
    rows.append('</div>')
    return HTMLResponse("\n".join(rows))


# ---------------------------------------------------------------------------
# Memory decisions (stale decision review panel)
# ---------------------------------------------------------------------------

@app.get("/api/memory/decisions", response_class=HTMLResponse)
async def memory_decisions(request: Request):
    """List decision files that have not been reviewed in 90+ days."""
    _check_auth(request)

    def _get_decisions() -> list[dict]:
        from atlas_plus.curation.weekly_report import _scan_decisions
        return _scan_decisions(MEMORY_ROOT)

    try:
        stale = await asyncio.to_thread(_get_decisions)
    except Exception as exc:
        return HTMLResponse(f'<div class="text-red-400 text-xs">Error: {exc}</div>')

    if not stale:
        return HTMLResponse(
            '<div class="text-slate-500 text-xs">No stale decisions -- all reviewed within 90 days.</div>'
        )

    rows = [
        f'<div class="text-slate-500 text-xs mb-2">{len(stale)} decision(s) not reviewed in 90+ days</div>',
        '<div class="space-y-0.5">',
    ]
    for item in stale[:20]:
        slug = _html_mod.escape(item["slug"])
        title = _html_mod.escape(item["title"])
        project = _html_mod.escape(item["project"] or "workspace")
        age = item.get("age_days", "?")
        rows.append(
            f'<div class="flex items-start gap-1 py-0.5 row-hover" '
            f'onclick="readMemoryFile({_js_arg("decisions/" + item["slug"] + ".md")})" title="Click to open">'
            f'<div class="flex-1 min-w-0">'
            f'<div class="text-yellow-300 text-xs font-mono truncate">{slug}</div>'
            f'<div class="text-slate-500 text-xs truncate">{title} &mdash; {project} &mdash; {age}d old</div>'
            f'</div>'
            f'</div>'
        )
    if len(stale) > 20:
        rows.append(f'<div class="text-slate-600 text-xs mt-1">+{len(stale) - 20} more. Run atlas-review-memory decisions.</div>')
    rows.append('</div>')
    return HTMLResponse("\n".join(rows))


# ---------------------------------------------------------------------------
# Memory re-derivable facts panel
# ---------------------------------------------------------------------------

@app.get("/api/memory/rederivable", response_class=HTMLResponse)
async def memory_rederivable(request: Request):
    """List .memory/ files that heavily overlap with project doc files (threshold=0.40)."""
    _check_auth(request)

    def _get_rederivable() -> list[dict]:
        from atlas_plus.curation.weekly_report import _scan_rederivable
        return _scan_rederivable(MEMORY_ROOT)

    try:
        candidates = await asyncio.to_thread(_get_rederivable)
    except Exception as exc:
        return HTMLResponse(f'<div class="text-red-400 text-xs">Error: {exc}</div>')

    if not candidates:
        return HTMLResponse(
            '<div class="text-slate-500 text-xs">No re-derivable facts found -- memory is clean.</div>'
        )

    rows = [
        f'<div class="text-slate-500 text-xs mb-2">{len(candidates)} file(s) overlap heavily with project docs</div>',
        '<div class="space-y-0.5">',
    ]
    for item in candidates[:20]:
        path_esc = _html_mod.escape(item["path"])
        doc_esc = _html_mod.escape(item["matched_doc"])
        sim = item.get("similarity", 0)
        rows.append(
            f'<div class="flex items-start gap-1 py-0.5 row-hover" '
            f'onclick="readMemoryFile({_js_arg(item["path"])})" title="Click to open">'
            f'<div class="flex-1 min-w-0">'
            f'<div class="text-yellow-300 text-xs font-mono truncate">{path_esc}</div>'
            f'<div class="text-slate-500 text-xs truncate">matches: {doc_esc} &mdash; sim={sim:.2f}</div>'
            f'</div>'
            f'</div>'
        )
    if len(candidates) > 20:
        rows.append(f'<div class="text-slate-600 text-xs mt-1">+{len(candidates) - 20} more. Run atlas-review-memory rederive.</div>')
    rows.append('</div>')
    return HTMLResponse("\n".join(rows))


# ---------------------------------------------------------------------------
# Memory curation report (full scan: duplicates/stale/convergence/decisions)
# ---------------------------------------------------------------------------

@app.get("/api/memory/curation-report")
@app.post("/api/memory/curation-report")
async def memory_curation_report(request: Request, dry_run: bool = True):
    """Run the weekly curation report and return counts as JSON.

    Query params:
      dry_run (bool, default true) -- when false, writes the report to .memory/_atlas/
      (a write needs POST: a GET that writes skips the cookie CSRF checks; N-L 2026-11-09)

    Returns {duplicates, stale, convergence, decisions, rederivable, dry_run, report_path}.
    """
    if not dry_run and request.method != "POST":
        raise HTTPException(status_code=405, detail="writing the report requires POST (GET is report-only)")
    _check_auth(request)

    def _run() -> dict:
        import re as _re
        from atlas_plus.curation.weekly_report import generate_report
        if dry_run:
            report_text, output_path = _cached_panel("curation-report", _generate_report_isolated)  # the panel's worker and cache (AG48: this sibling ran in-process and stalled search)
        else:
            report_text, output_path = generate_report(dry_run=dry_run)

        from atlas_plus.curation.weekly_report import curation_counts
        counts = curation_counts(report_text)

        return {
            **counts,
            "dry_run": dry_run,
            "report_path": str(output_path) if not dry_run else None,
        }

    try:
        return await asyncio.to_thread(_run)
    except Exception as exc:
        log.error("GET /api/memory/curation-report failed: %s", exc)
        raise HTTPException(status_code=503, detail=str(exc))


@app.get("/api/memory/curation-report-panel", response_class=HTMLResponse)
async def memory_curation_report_panel(request: Request):
    """HTMX panel: compact curation health summary for the Memory tab.

    Runs generate_report() in dry-run mode and shows counts for each category.
    Returns a green 'clean' message when all counts are zero.
    """
    _check_auth(request)

    def _get_counts() -> dict:
        import re as _re
        from atlas_plus.curation.weekly_report import generate_report
        report_text, _ = _cached_panel("curation-report", _generate_report_isolated)

        from atlas_plus.curation.weekly_report import curation_counts
        counts = curation_counts(report_text)
        counts["_banded"] = "came from MinHash bands" in report_text  # MN73: the CLI and report said it, the panel did not
        # A capped section reads "(500+ found (capped at 500)": the panel showed the cap as the total (MN93 F4).
        counts["_caps"] = [
            (label, m.group(1))
            for line in report_text.splitlines()
            for needle, label in (("Repeated facts (", "repeated facts"), ("Potential Duplicates (", "note pairs"))
            if needle in line
            for m in [_re.search(r"capped at (\d+)", line)] if m
        ]
        return counts

    try:
        counts = await asyncio.to_thread(_get_counts)
    except Exception as exc:
        return HTMLResponse(f'<div class="text-red-400 text-xs">Curation report error: {_html_mod.escape(str(exc))}</div>')

    banded = bool(counts.pop("_banded", False))
    caps = counts.pop("_caps", [])
    total = sum(counts.values())
    more = " or more" if caps else ""

    if total == 0:
        return HTMLResponse(
            '<div class="text-green-500 text-xs">Curation health: clean -- no issues found.</div>'
        )

    category_labels = [
        ("duplicates",  counts["duplicates"],  "red-400",    "Duplicates"),
        ("stale",       counts["stale"],        "yellow-400", "Stale files"),
        ("convergence", counts["convergence"],  "yellow-400", "Convergence flags"),
        ("decisions",   counts["decisions"],    "blue-400",   "Stale decisions"),
        ("rederivable", counts["rederivable"],  "slate-400",  "Re-derivable"),
    ]

    rows = [
        '<div class="space-y-0.5">',
        f'<div class="text-slate-500 text-xs mb-1">Curation scan: {total}{more} item(s) need attention</div>',
    ]
    for key, count, color, label in category_labels:
        if count:
            capped = key == "duplicates" and caps
            rows.append(
                f'<div class="text-{color} text-xs">'
                f'{label}: <span class="font-semibold">{count}{" or more" if capped else ""}</span>'
                + (" (the scan stops at " + ", ".join(f"{n} {what}" for what, n in caps) + ")" if capped else "")
                + '</div>'
            )
    if banded and counts.get("duplicates"):
        rows.append('<div class="text-slate-500 text-xs">Duplicate candidates came from MinHash bands '
                    '(shared vocabulary); ATLAS_DUPLICATES_EXACT=on forces the exact scan.</div>')
    rows.append('</div>')
    return HTMLResponse("\n".join(rows))


# ---------------------------------------------------------------------------
# Memory topic index (tag cloud)
# ---------------------------------------------------------------------------

_PANEL_CACHE: dict[str, tuple[str, float, object]] = {}
_PANEL_LOCKS: dict[str, object] = {}
_PANEL_TTL = 300.0


def _signature_walk(top: str, skip: set):
    """(dirpath, DirEntry) for every `.md` non-directory under ``top``, in os.walk's top-down order with
    os.walk's rules (a dir that cannot be listed yields nothing; links to directories are not entered)."""
    stack = [top]
    while stack:
        top = stack.pop()
        try:
            it = os.scandir(top)
        except OSError:
            continue
        dirs, files = [], []
        with it:
            try:
                for entry in it:
                    try:
                        is_dir = entry.is_dir()
                    except OSError:
                        is_dir = False
                    (dirs if is_dir else files).append(entry)
            except OSError:  # a listing that fails part-way yields nothing for that dir, like os.walk
                continue
        for entry in files:
            if entry.name.endswith(".md"):
                yield top, entry
        for entry in reversed(dirs):  # reversed onto the stack: popped in listing order
            name = entry.name
            if name in skip or name.startswith("."):
                continue
            new_path = os.path.join(top, name)
            if not os.path.islink(new_path):
                stack.append(new_path)


def _corpus_signature() -> str:
    """Note count and newest mtime under the corpus (history copies and state excluded): one
    directory walk, so a panel that costs seconds is recomputed only when a note changed."""
    import zlib as _zlib
    count = 0
    newest = 0.0
    names_crc = 0
    skip = {"versions", "_atlas", ".git", "_sync", "__pycache__"}
    try:
        for dirpath, entry in _signature_walk(os.fspath(MEMORY_ROOT), skip):
            count += 1
            names_crc = _zlib.crc32((dirpath + "/" + entry.name).encode("utf-8", "replace"), names_crc)
            try:
                # the listing's own stat (follows a link like os.stat did): os.stat opened every note,
                # 675 ms per panel poll at 20,000 notes on Windows, cache hit or not (AG92, sweep 24)
                m = entry.stat().st_mtime
            except OSError:
                continue
            if m > newest:
                newest = m
    except OSError:
        pass
    return f"{MEMORY_ROOT}|{count}:{newest:.3f}:{names_crc & 0xFFFFFFFF:08x}"  # two empty corpora are not the same corpus; a rename changes the names (MN31)


def _cached_panel(key: str, build, ttl: float = _PANEL_TTL):
    """``build()`` once per corpus signature (and at most once per ``ttl``), one computation in
    flight per key: repeated loads of a slow panel piled up threads (670 MB RSS at 5,000 notes)."""
    import threading as _th
    import time as _time
    if os.environ.get("ATLAS_WEB_PANEL_CACHE", "on").strip().lower() not in ("on", "1", "true"):  # anything else is off, like every other switch (MN44)
        return build()
    sig = _corpus_signature()
    hit = _PANEL_CACHE.get(key)
    if hit and hit[0] == sig and _time.monotonic() - hit[1] < ttl:
        return hit[2]
    lock = _PANEL_LOCKS.setdefault(key, _th.Lock())
    with lock:
        hit = _PANEL_CACHE.get(key)
        if hit and hit[0] == sig and _time.monotonic() - hit[1] < ttl:
            return hit[2]
        value = build()
        _PANEL_CACHE[key] = (sig, _time.monotonic(), value)
        return value


def _generate_report_isolated():
    """The dry-run curation report from a spawned worker process (`ATLAS_WEB_REPORT_SUBPROCESS=off`
    runs it in this process). CPU-bound Python in a thread holds the GIL: while a report ran, a
    search took 23 to 60 s and abandoned requests piled up (AG42, sweep 15)."""
    from atlas_plus.curation._report_worker import report_dry_run
    if os.environ.get("ATLAS_WEB_REPORT_SUBPROCESS", "on").strip().lower() in ("off", "0", "false", "no"):
        return report_dry_run()
    try:
        import concurrent.futures as _cf
        import multiprocessing as _mp
        with _cf.ProcessPoolExecutor(max_workers=1, mp_context=_mp.get_context("spawn")) as ex:
            return ex.submit(report_dry_run).result(timeout=900)
    except Exception as exc:  # noqa: BLE001 - a worker that cannot start (frozen app, sandbox) is not a lost report
        log.warning("curation report worker failed (%s); running in-process", exc)
        return report_dry_run()


def _indexed_frontmatter(memory_root) -> Optional[dict]:
    """{relative path: frontmatter dict} from the search index (the source `memory_topics_tool`
    aggregates from), brought up to date first; None when there is no index for this corpus yet (a panel
    never builds a whole index) or it cannot be read (SEX-019, AG112: the cold walk parsed every note)."""
    import json as _json
    try:
        from atlas_plus.mcp.memory_server import fts as _fts
        if Path(_fts.MEMORY_ROOT) != Path(memory_root) or not Path(_fts.FTS_DB).is_file():
            return None
        _fts.update_incremental()
        conn = _fts._connect()
        try:
            rows = conn.execute("SELECT path, frontmatter_json FROM memory_fts").fetchall()
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 - the file walk below is the answer of record
        return None
    out: dict = {}
    for rel, fm_json in rows:
        try:
            out[rel] = _json.loads(fm_json) or {}
        except ValueError:
            continue
    return out


def _build_topic_index(memory_root) -> dict[str, list[str]]:
    """Scan .memory/ for frontmatter tags: fields. Return {tag: [path, ...]} sorted by count desc.

    The walk decides which notes count and in what order; each note's frontmatter comes from the search
    index when it has the note, and only the notes it lacks are read and parsed here."""
    indexed = _indexed_frontmatter(memory_root)
    import yaml as _yaml
    from atlas_plus._fsutil import inside as _fs_inside
    from atlas_plus.curation import _corpus_pass  # shared with the other report sections (SEX-046)
    skip_dirs = {"archive", "_sync", "versions", "_atlas", "anti"}
    tag_to_paths: dict[str, list[str]] = {}
    try:
        _root_resolved = memory_root.resolve()  # once, not once per note
    except OSError:
        _root_resolved = None

    for md_file in _corpus_pass.md_files(memory_root):
        rel_parts = md_file.relative_to(memory_root).parts
        if any(p in skip_dirs for p in rel_parts):
            continue
        if md_file.name.startswith("_") or not _fs_inside(md_file, memory_root, _root_resolved):
            continue  # a link out of the corpus (MN86, sweep 25)
        rel = str(md_file.relative_to(memory_root)).replace("\\", "/")
        fm = indexed.get(rel) if indexed is not None else None
        if fm is None:
            try:
                raw = _corpus_pass.read_text(md_file)
            except OSError:
                continue
            if not raw.startswith("---"):
                continue
            end = raw.find("\n---", 3)
            if end == -1:
                continue
            try:
                fm = _corpus_pass.load_frontmatter(raw[3:end]) or {}
            except Exception:
                continue
        if not isinstance(fm, dict):
            continue
        tags = fm.get("tags") or fm.get("tag") or []
        if isinstance(tags, str):
            # Handle comma-separated or single-value strings
            tags = [t.strip() for t in tags.replace(";", ",").split(",") if t.strip()]
        if not isinstance(tags, list):
            continue
        for tag in tags:
            tag = str(tag).strip().lower()
            if tag:
                tag_to_paths.setdefault(tag, []).append(rel)

    # Sort by count descending
    return dict(sorted(tag_to_paths.items(), key=lambda kv: -len(kv[1])))


@app.get("/api/memory/topics")
async def memory_topics(request: Request):
    """Return a topic index built from tags: frontmatter across .memory/.

    Returns {tags: [{tag, count, paths}], total_tagged_files: N, total_tags: N}.
    Sorted by tag frequency descending.
    """
    _check_auth(request)

    def _scan():
        return _cached_panel("topics", lambda: _build_topic_index(MEMORY_ROOT))

    try:
        index = await asyncio.to_thread(_scan)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc))

    tag_list = [
        {"tag": tag, "count": len(paths), "paths": paths[:10]}
        for tag, paths in index.items()
    ]
    all_files = {p for paths in index.values() for p in paths}
    return {
        "tags": tag_list,
        "total_tagged_files": len(all_files),
        "total_tags": len(tag_list),
    }


@app.get("/api/memory/topics/html", response_class=HTMLResponse)
async def memory_topics_html(request: Request):
    """Return the topic index as a clickable tag cloud HTML fragment (HTMX target)."""
    _check_auth(request)

    def _scan():
        return _cached_panel("topics", lambda: _build_topic_index(MEMORY_ROOT))

    try:
        index = await asyncio.to_thread(_scan)
    except Exception as exc:
        return HTMLResponse(f'<div class="text-red-400 text-xs">Error: {exc}</div>')

    if not index:
        return HTMLResponse('<div class="text-slate-500 text-xs">No tags found in .memory/ frontmatter.</div>')

    parts = [f'<div class="text-slate-500 text-xs mb-2">{len(index)} unique tag(s) across {sum(len(v) for v in index.values())} file references</div>',
             '<div class="flex flex-wrap gap-1">']
    for tag, paths in list(index.items())[:50]:
        count = len(paths)
        tag_esc = _html_mod.escape(tag)
        # Clicking a tag runs a memory search for it
        parts.append(
            f'<button onclick="memorySearch({_js_arg(tag)})" '
            f'class="px-2 py-0.5 rounded text-xs bg-slate-700 hover:bg-slate-600 text-slate-200 cursor-pointer" '
            f'title="{count} file(s) tagged">'
            f'{tag_esc} <span class="text-slate-400">({count})</span>'
            f'</button>'
        )
    if len(index) > 50:
        parts.append(f'<div class="text-slate-600 text-xs w-full mt-1">+{len(index) - 50} more tags not shown.</div>')
    parts.append('</div>')
    return HTMLResponse("\n".join(parts))


# ---------------------------------------------------------------------------
# Untagged scope panel (Memory tab)
# ---------------------------------------------------------------------------

@app.get("/api/memory/untagged-scopes", response_class=HTMLResponse)
async def memory_untagged_scopes(request: Request):
    """HTMX panel: top untagged memory scopes for the Memory tab.

    Shows each .memory/ subdirectory alongside its untagged file count,
    sorted highest-first. Orange highlight when count is high (> 10).
    Uses check_untagged_by_scope() from doctor.cli.
    """
    _check_auth(request)

    def _gather() -> dict[str, int]:
        from atlas_plus.doctor.cli import check_untagged_by_scope
        return check_untagged_by_scope()

    try:
        scope_counts = await asyncio.to_thread(lambda: _cached_panel("untagged-scopes", _gather))  # 1.6 s per poll before (AG48)
    except Exception:
        return HTMLResponse('<div class="text-slate-600 text-xs">unavailable</div>')

    if not scope_counts:
        return HTMLResponse('<div class="text-green-400 text-xs">All scopes fully tagged.</div>')

    sorted_scopes = sorted(scope_counts.items(), key=lambda x: -x[1])
    parts = []
    for scope, count in sorted_scopes:
        c = "text-orange-400" if count > 10 else ("text-yellow-400" if count > 3 else "text-slate-400")
        parts.append(
            f'<div class="flex justify-between text-xs">'
            f'<span class="text-slate-300">{_html_mod.escape(scope)}</span>'
            f'<span class="{c}">{count}</span>'
            f'</div>'
        )
    total_untagged = sum(scope_counts.values())
    hint = ""
    if total_untagged > 0:
        hint = (
            f'<div class="text-slate-600 text-xs mt-1">'
            f'Total: {total_untagged} -- run: atlas-review-memory suggest-tags --write'
            f'</div>'
        )
    return HTMLResponse(
        f'<div class="space-y-0.5">{"".join(parts)}{hint}</div>'
    )


# ---------------------------------------------------------------------------
# Temporal graph
# ---------------------------------------------------------------------------

@app.get("/api/temporal/stats", response_class=HTMLResponse)
async def temporal_stats(request: Request):
    """GET /api/temporal/stats -- HTMX temporal graph summary for the Memory tab.

    Shows: entity count, active facts, episode count, last-built date, and staleness warning.
    """
    _check_auth(request)

    def _get_stats() -> dict:
        import json as _json
        from datetime import datetime, timezone
        from atlas_plus.temporal_graph.file_provider import FileTemporalProvider
        p = FileTemporalProvider()
        s = p.stats()
        s["last_built"] = None
        s["stale"] = True
        if p._manifest_file.exists():
            try:
                data = _json.loads(p._manifest_file.read_text(encoding="utf-8"))
                lb = data.get("last_built")
                if lb:
                    s["last_built"] = lb[:10]
                    built_dt = datetime.fromisoformat(lb.replace("Z", "+00:00"))
                    s["stale"] = (datetime.now(timezone.utc) - built_dt).days > 7
            except Exception:
                pass
        return s

    try:
        stats = await asyncio.to_thread(_get_stats)
    except Exception as exc:
        return HTMLResponse(f'<div class="text-red-400 text-xs">Temporal graph error: {exc}</div>')

    e_count = stats.get("entities", 0)
    f_count = stats.get("facts_active", 0)
    ep_count = stats.get("episodes", 0)
    last_built = stats.get("last_built") or "never"
    stale = stats.get("stale", True)

    stale_html = ""
    if stale:
        stale_html = ' &nbsp;<span class="text-yellow-400">STALE</span>'

    return HTMLResponse(
        f'<div class="text-xs text-slate-400 space-y-0.5">'
        f'<div>{e_count} entities &nbsp;|&nbsp; {f_count} active facts &nbsp;|&nbsp; {ep_count} episodes'
        f'&nbsp;|&nbsp; built: {last_built}{stale_html}</div>'
        f'</div>'
    )


@app.get("/api/temporal/query", response_class=HTMLResponse)
async def temporal_query(request: Request, q: str = "", mode: str = "current"):
    """GET /api/temporal/query -- search the temporal graph for current or historical facts."""
    _check_auth(request)
    if not q.strip():
        return HTMLResponse('<div class="text-slate-500 text-xs">Enter a query above.</div>')

    def _do_query():
        from atlas_plus.temporal_graph.file_provider import FileTemporalProvider
        p = FileTemporalProvider()
        if mode == "history":
            hist = p.history(q, limit=20)
            # Convert TemporalHistory to list of (subj, pred, obj, valid_from) tuples
            rows_out = []
            for f in hist.facts:
                ent = hist.entity
                rows_out.append({
                    "subject": ent.name if ent else f.subject_id,
                    "predicate": f.predicate,
                    "object": f.object_value,
                    "valid_from": f.valid_from[:10] if f.valid_from else "",
                    "invalid_at": f.invalid_at[:10] if f.invalid_at else "",
                })
            return rows_out, hist.warnings if hist else ([], [])
        results = p.search_current(q, limit=20)
        rows_out = []
        for r in results:
            f = r.fact
            ent = r.entity
            rows_out.append({
                "subject": ent.name if ent else f.subject_id,
                "predicate": f.predicate,
                "object": f.object_value,
                "valid_from": f.valid_from[:10] if f.valid_from else "",
                "score": round(r.score, 3),
            })
        return rows_out, []

    try:
        results, warnings = await asyncio.to_thread(_do_query)
    except Exception as exc:
        return HTMLResponse(f'<div class="text-red-400 text-xs">Query error: {exc}</div>')

    if not results:
        return HTMLResponse(f'<div class="text-slate-500 text-xs">No results for &quot;{_html_mod.escape(q)}&quot;</div>')

    rows = [
        f'<div class="text-slate-500 text-xs mb-2">{len(results)} result(s) &mdash; mode: {mode}</div>',
    ]
    if warnings:
        for w in warnings:
            rows.append(f'<div class="text-yellow-400 text-xs mb-1">{_html_mod.escape(w)}</div>')
    rows.append('<div class="space-y-1">')
    for r in results:
        score_s = f' [{r["score"]}]' if r.get("score") else ""
        inv_s = f' <span class="text-slate-600">(until {r["invalid_at"]})</span>' if r.get("invalid_at") else ""
        rows.append(
            f'<div class="border-b border-slate-800 pb-1 text-xs">'
            f'<span class="text-cyan-400">{_html_mod.escape(str(r["subject"]))}</span>'
            f'<span class="text-slate-500 mx-1">{_html_mod.escape(str(r["predicate"]))}</span>'
            f'<span class="text-slate-300">{_html_mod.escape(str(r["object"]))}</span>'
            f'<span class="text-slate-600 ml-2">{r.get("valid_from","")}{score_s}</span>'
            f'{inv_s}'
            f'</div>'
        )
    rows.append('</div>')
    return HTMLResponse("\n".join(rows))


@app.get("/api/temporal/entities")
async def temporal_entities(request: Request, limit: int = 50):
    """GET /api/temporal/entities -- list all entities in the temporal graph as JSON.

    Returns: [{name, labels, aliases, first_seen_at, last_seen_at}, ...] sorted by name.
    """
    _check_auth(request)

    def _get_entities() -> list[dict]:
        from atlas_plus.temporal_graph.file_provider import FileTemporalProvider
        p = FileTemporalProvider()
        entities = p.list_entities(limit=limit)
        return [
            {
                "name": e.name,
                "labels": list(e.labels),
                "aliases": list(e.aliases),
                "first_seen_at": e.first_seen_at,
                "last_seen_at": e.last_seen_at,
            }
            for e in entities
        ]

    try:
        result = await asyncio.to_thread(_get_entities)
        return {"entities": result, "total": len(result)}
    except Exception as exc:
        log.error("GET /api/temporal/entities failed: %s", exc)
        raise HTTPException(status_code=503, detail=str(exc))


# ---------------------------------------------------------------------------
# Temporal history
# ---------------------------------------------------------------------------

@app.get("/api/temporal/history")
async def temporal_history(request: Request, entity: str = "", limit: int = 20):
    """GET /api/temporal/history -- fact timeline for a named entity.

    Query params:
      entity (str, required) -- entity name to look up
      limit  (int, default 20) -- max facts to return

    Returns: {entity: {name, labels, aliases, ...}, facts: [{...}], warnings: [...]}.
    Returns 400 when entity is empty; 404 when entity is not found; 503 on provider error.
    """
    _check_auth(request)
    if not entity.strip():
        raise HTTPException(status_code=400, detail="entity parameter is required")

    def _get_history() -> dict:
        from atlas_plus.temporal_graph.file_provider import FileTemporalProvider
        p = FileTemporalProvider()
        hist = p.history(entity.strip(), limit=limit)
        if hist is None or hist.entity is None:
            return None
        e = hist.entity
        facts_out = []
        for f in hist.facts:
            facts_out.append({
                "fact_text": f.fact_text,
                "predicate": f.predicate,
                "object_value": f.object_value,
                "valid_from": f.valid_from,
                "invalid_at": f.invalid_at,
                "provenance": f.provenance,
                "is_current": f is hist.current_fact,
            })
        return {
            "entity": {
                "name": e.name,
                "labels": list(e.labels),
                "aliases": list(e.aliases),
                "first_seen_at": e.first_seen_at,
                "last_seen_at": e.last_seen_at,
            },
            "facts": facts_out,
            "warnings": hist.warnings or [],
        }

    try:
        result = await asyncio.to_thread(_get_history)
    except Exception as exc:
        log.error("GET /api/temporal/history failed: %s", exc)
        raise HTTPException(status_code=503, detail=str(exc))

    if result is None:
        raise HTTPException(status_code=404, detail=f"Entity not found: {entity}")

    return result


@app.get("/api/temporal/history-panel", response_class=HTMLResponse)
async def temporal_history_panel(request: Request, entity: str = "", limit: int = 10):
    """GET /api/temporal/history-panel -- HTMX fact timeline panel for the Memory tab.

    Returns an HTML fragment showing the fact timeline for an entity.
    Returns a prompt when entity is empty; an error block when not found.
    """
    _check_auth(request)
    if not entity.strip():
        return HTMLResponse('<div class="text-slate-500 text-xs">Enter an entity name above.</div>')

    def _get_history() -> dict | None:
        from atlas_plus.temporal_graph.file_provider import FileTemporalProvider
        p = FileTemporalProvider()
        h = p.history(entity.strip(), limit=limit)
        if h is None or h.entity is None:
            return None
        return {
            "entity": h.entity,
            "facts": h.facts,
            "current_fact": h.current_fact,
            "warnings": h.warnings or [],
        }

    try:
        data = await asyncio.to_thread(_get_history)
    except Exception as exc:
        return HTMLResponse(f'<div class="text-red-400 text-xs">Error: {_html_mod.escape(str(exc))}</div>')

    if data is None:
        ent_esc = _html_mod.escape(entity[:60])
        return HTMLResponse(
            f'<div class="text-slate-500 text-xs">Entity not found: &quot;{ent_esc}&quot;</div>'
        )

    e = data["entity"]
    facts = data["facts"]
    current = data["current_fact"]
    warnings = data["warnings"]

    rows = [
        f'<div class="text-cyan-400 text-xs font-semibold mb-1">{_html_mod.escape(e.name)}</div>',
        f'<div class="text-slate-600 text-xs mb-2">{_html_mod.escape(e.first_seen_at[:10])} &mdash; {_html_mod.escape(e.last_seen_at[:10])}</div>',
    ]
    if warnings:
        for w in warnings:
            rows.append(f'<div class="text-yellow-400 text-xs mb-1">{_html_mod.escape(w)}</div>')

    rows.append('<div class="space-y-1">')
    for f in facts:
        is_cur = f is current
        marker = ">" if is_cur else "-"
        color = "text-slate-200" if is_cur else "text-slate-400"
        status_span = ""
        if f.invalid_at:
            status_span = f' <span class="text-slate-600 text-xs">(until {_html_mod.escape(f.invalid_at[:10])})</span>'
        prov = _html_mod.escape(f.provenance[:20]) if f.provenance else ""
        rows.append(
            f'<div class="border-b border-slate-800 pb-1">'
            f'<span class="text-slate-600 mr-1">{marker}</span>'
            f'<span class="{color} text-xs">{_html_mod.escape(f.fact_text[:120])}</span>'
            f'{status_span}'
            f'<div class="text-slate-600 text-xs">{_html_mod.escape(f.valid_from[:10])} &middot; {prov}</div>'
            f'</div>'
        )
    rows.append('</div>')
    if not facts:
        rows.append('<div class="text-slate-500 text-xs">No facts recorded.</div>')

    return HTMLResponse("\n".join(rows))


# ---------------------------------------------------------------------------
# Tickets
# ---------------------------------------------------------------------------

@app.get("/api/tickets")
async def tickets(request: Request, project: str = "", status: str = ""):
    """GET /api/tickets -- list tickets as JSON, optionally filtered by project/status."""
    _check_auth(request)
    from atlas_plus.tickets.store import list_tickets
    return [t.as_dict() for t in list_tickets(project=project or None, status=status or None)]


@app.get("/api/tickets/projects")
async def ticket_projects(request: Request):
    """GET /api/tickets/projects -- the project names the board's `project` filter accepts (AG80).

    The board's cap sentence asks the reader to pick a project; these are the ticket
    directories that hold at least one live ticket, named exactly as list_tickets takes them.
    """
    _check_auth(request)
    from atlas_plus.tickets import store as _tstore
    root = _tstore.TICKETS_ROOT
    names: list[str] = []
    try:
        with os.scandir(root) as it:
            entries = sorted(it, key=lambda e: e.name)
    except OSError:
        return []
    for e in entries:
        n = e.name
        if n.startswith(("_", ".")) or not re.match(r'^[a-zA-Z0-9_.+-]+$', n):
            continue
        try:
            if not e.is_dir(follow_symlinks=False):
                continue
            if next(_tstore._walk_live_ticket_files(Path(e.path), recursive=False), None) is None:
                continue
        except OSError:
            continue
        names.append(n)
    return names


@app.get("/api/projects")
async def projects_list(request: Request):
    """GET /api/projects -- list project directory names under ATLAS_PROJECTS_ROOT."""
    _check_auth(request)
    root = atlas_paths.projects_root()
    if not root.exists():
        return []
    return sorted(d.name for d in root.iterdir() if d.is_dir() and not d.name.startswith("."))


@app.get("/api/inbox", response_class=HTMLResponse)
async def inbox_list(request: Request, status_filter: str = ""):
    """GET /api/inbox -- HTMX list of inbox captures for the Inbox tab (ATL-026)."""
    _check_auth(request)
    from atlas_plus.inbox.store import list_items
    try:
        items = list_items(status=status_filter or None)
    except Exception as exc:
        return HTMLResponse(f'<div class="text-red-400 text-xs">Error: {exc}</div>')
    if not items:
        return HTMLResponse('<div class="text-slate-500 text-xs">Inbox empty.</div>')
    status_colors = {
        "new": "text-blue-400", "summarized": "text-green-400",
        "researched": "text-cyan-400", "promoted": "text-purple-400",
        "archived": "text-slate-500",
    }
    rows = []
    for it in items:
        col = status_colors.get(it.status, "text-slate-300")
        label = _html_mod.escape(it.title or it.url or (it.summary[:60] if it.summary else it.id))
        flag = ' <span class="text-pink-400" title="flagged for research">R</span>' if it.flagged_research else ''
        rows.append(
            f'<div class="flex items-center gap-2 py-1 border-b border-slate-700 row-hover cursor-pointer"'
            f' onclick="showInboxDetail({_js_arg(it.id)})">'
            f'<span class="text-slate-500 text-xs w-24">{_html_mod.escape(str(it.id))}</span>'
            f'<span class="{col} text-xs w-20">{_html_mod.escape(str(it.status))}</span>'
            f'<span class="text-slate-300 text-xs flex-1 truncate">{label}{flag}</span>'
            f'</div>'
        )
    return HTMLResponse("\n".join(rows))


@app.get("/api/inbox/{item_id}/detail", response_class=HTMLResponse)
async def inbox_detail(item_id: str, request: Request):
    """GET /api/inbox/{id}/detail -- detail panel with summary, tags, and actions (ATL-026)."""
    _check_auth(request)
    from atlas_plus.inbox.store import get_item
    it = get_item(item_id)
    if not it:
        return HTMLResponse('<div class="text-red-400 text-xs">Not found.</div>')
    esc = _html_mod.escape
    parts = [f'<div class="text-slate-300 text-sm font-semibold mb-1">{esc(it.title or it.id)}</div>']
    # Every frontmatter field is data: an item synced from another device or written by hand
    # carried markup into the Inbox tab (MN86, the sibling of MN70's task and ticket escapes).
    parts.append(f'<div class="text-xs text-slate-500 mb-2">{esc(str(it.status))} | {esc(str(it.source))} | {esc(str(it.created))}</div>')
    if it.url:
        # Only http(s) becomes a link: `javascript:` runs in the dashboard's origin on click (MN86).
        if str(it.url).strip().lower().startswith(("http://", "https://")):
            parts.append(f'<div class="mb-2"><a href="{esc(it.url)}" target="_blank" rel="noopener noreferrer" class="text-blue-400 text-xs break-all">{esc(it.url)}</a></div>')
        else:
            parts.append(f'<div class="mb-2 text-slate-400 text-xs break-all">{esc(str(it.url))}</div>')
    if it.tags:
        parts.append('<div class="mb-2">' + ' '.join(f'<span class="text-cyan-400 text-xs">#{esc(t)}</span>' for t in it.tags) + '</div>')
    summ = esc(it.summary) if it.summary else '<span class="text-slate-600">(no summary yet)</span>'
    parts.append(f'<div class="text-slate-300 text-xs whitespace-pre-wrap mb-3">{summ}</div>')
    if it.promoted_ticket:
        parts.append(f'<div class="text-purple-400 text-xs mb-2">Promoted -&gt; {esc(it.promoted_ticket)}</div>')
    parts.append('<div class="flex gap-2 flex-wrap">')
    if it.status != "promoted":
        parts.append(f'<button class="btn btn-cyan text-xs" onclick="promoteInbox({_js_arg(it.id)})">Promote to ticket</button>')
    if it.status != "archived":
        parts.append(f'<button class="btn text-xs" onclick="archiveInbox({_js_arg(it.id)})">Archive</button>')
    parts.append('</div>')
    return HTMLResponse("\n".join(parts))


@app.post("/api/inbox/{item_id}/promote", status_code=201)
async def inbox_promote(item_id: str, request: Request):
    """POST /api/inbox/{id}/promote -- create a ticket from the item and mark it promoted."""
    _check_auth(request)
    try:
        body = await _json_object(request)
    except Exception:
        body = {}
    from atlas_plus.inbox.store import promote_to_ticket
    try:
        _item, ticket_id = promote_to_ticket(item_id, project=body.get("project", "inbox"))
        return {"item_id": item_id, "ticket_id": ticket_id}
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Item not found")
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/inbox/{item_id}/archive")
async def inbox_archive(item_id: str, request: Request):
    """POST /api/inbox/{id}/archive -- mark an item archived."""
    _check_auth(request)
    from atlas_plus.inbox.store import update_item
    try:
        it = update_item(item_id, status="archived")
        return it.as_dict()
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Item not found")


# ---------------------------------------------------------------------------
# Service control panel (mirrors deploy/atlas-ctl.ps1 service list)
# ---------------------------------------------------------------------------
# Map: live system topology + activity feed. Nodes are derived from env URLs,
# live /health probes, and on-disk presence -- never a hardcoded install
# shape -- so a fresh user's map shows THEIR stack, not the maintainer's.
MCP_URL = os.environ.get("ATLAS_MCP_URL", f"http://127.0.0.1:{os.environ.get('ATLAS_MCP_PORT', '8765').strip() or '8765'}").rstrip("/")  # the port variable, like the doctor (AG77)
OLLAMA_URL = os.environ.get("OLLAMA_BASE_URL", "http://127.0.0.1:11434").rstrip("/")  # import-time default; the Map reads _map_ollama_url()


def _map_ollama_url() -> str:
    """The Ollama the Brain uses, read per request (MN85): the status pill took this helper
    with MN25, the Map node kept OLLAMA_BASE_URL only and showed another install's 11434 as up."""
    from atlas_plus.llm.ollama_url import default_ollama_url
    return default_ollama_url().rstrip("/")
SYNC_HUB_URL = os.environ.get("ATLAS_SYNC_URL", f"http://127.0.0.1:{os.environ.get('ATLAS_SYNC_PORT', '8768').strip() or '8768'}").rstrip("/")

_MAP_EDGES = [
    ("clients", "mcp"), ("mcp", "corpus"), ("mcp", "statedb"),
    ("mcp", "temporal"), ("mcp", "tickets"), ("brain", "statedb"),
    ("worker", "statedb"), ("brain", "ollama"), ("helm", "brain"),
    ("helm", "mcp"), ("sync", "corpus"), ("temporal", "corpus"),
]


async def _probe_health(client: httpx.AsyncClient, url: str) -> str:
    """up | degraded | down for one service endpoint. Never raises."""
    try:
        r = await client.get(url)
        if r.status_code == 200:
            try:
                body = r.json()
            except Exception:
                body = {}
            if isinstance(body, dict) and body.get("status") == "degraded":
                return "degraded"
            return "up"
        return "degraded" if r.status_code < 500 else "down"
    except Exception:
        return "down"


def _store_node(node_id: str, label: str, path: Path, detail: str = "") -> dict:
    present = path.exists()
    return {
        "id": node_id, "label": label, "kind": "store",
        "status": "present" if present else "missing",
        "detail": detail or (path.name if present else "not created yet"),
    }


@app.get("/api/topology")
async def api_topology(request: Request):
    """GET /api/topology -- nodes + edges for the Map tab, probed live."""
    _check_auth(request)
    async with _PeerClient(timeout=httpx.Timeout(1.5, connect=0.75)) as client:
        brain_s, mcp_s, ollama_s, sync_s = await asyncio.gather(
            _probe_health(client, f"{BRAIN_URL}/health"),
            _probe_health(client, f"{MCP_URL}/health"),
            _probe_health(client, f"{_map_ollama_url()}/api/tags"),
            _probe_health(client, f"{SYNC_HUB_URL}/sync/v1/health"),
        )
    # Worker has no port: call it active when the task lifecycle pulsed
    # the activity feed in the last 10 minutes.
    worker_s = "idle"
    try:
        from atlas_plus import activity as _act
        from datetime import datetime as _dt, timedelta as _td, timezone as _tz
        cutoff = (_dt.now(_tz.utc) - _td(minutes=10)).isoformat(timespec="microseconds")
        if any(e.get("source") == "brain" for e in _act.tail(limit=50, since=cutoff)):
            worker_s = "active"
    except Exception:
        pass
    _mcp_detail = MCP_URL
    if mcp_s == "down" and not os.environ.get("ATLAS_MCP_URL", "").strip():
        mcp_s, _mcp_detail = "n/a", "stdio: your client starts it (no HTTP server)"   # the status pill's sibling (AG97)
    state = atlas_paths.state_dir()
    try:
        from atlas_plus.tickets.store import TICKETS_ROOT as _tickets_root
    except Exception:
        _tickets_root = atlas_paths.memory_root() / "tickets"
    nodes = [
        {"id": "clients", "label": "LLM clients", "kind": "client", "status": "n/a",
         "detail": "any MCP-aware model"},
        {"id": "helm", "label": "Helm (this UI)", "kind": "service", "status": "up",
         "detail": f"port {WEB_PORT}"},
        {"id": "mcp", "label": "MCP server", "kind": "service", "status": mcp_s,
         "detail": _mcp_detail},
        {"id": "brain", "label": "Brain", "kind": "service", "status": brain_s,
         "detail": BRAIN_URL},
        {"id": "worker", "label": "Worker", "kind": "service", "status": worker_s,
         "detail": "task executor"},
        {"id": "ollama", "label": "Model backend", "kind": "service", "status": ollama_s,
         "detail": _map_ollama_url()},
        {"id": "sync", "label": "Sync hub", "kind": "service", "status": sync_s,
         "detail": "on-demand" if sync_s == "down" else SYNC_HUB_URL},
        _store_node("corpus", "Memory corpus", MEMORY_ROOT, "markdown + git"),
        _store_node("statedb", "State DB", state / "state.sqlite", "tasks + search index"),
        _store_node("temporal", "Temporal graph", state / "temporal_graph" / "facts.jsonl",
                    "facts over time"),
        _store_node("tickets", "Tickets", Path(_tickets_root), "work queue"),
    ]
    return {
        "nodes": nodes,
        "edges": [{"source": a, "target": b} for a, b in _MAP_EDGES],
    }


@app.get("/api/activity")
async def api_activity(request: Request, since: Optional[str] = None, limit: int = 100):
    """GET /api/activity -- tail of the cross-component activity feed."""
    _check_auth(request)
    from atlas_plus import activity as _act
    from datetime import datetime as _dt, timezone as _tz
    limit = max(1, min(int(limit), 500))
    # `since` is compared against our own emitted timestamps; cap its length
    # so a hostile query string cannot balloon the comparison.
    events = _act.tail(limit=limit, since=(since or None) and str(since)[:40])
    return {
        "events": events,
        "now": _dt.now(_tz.utc).isoformat(timespec="microseconds"),
    }


# ---------------------------------------------------------------------------
# Allowlisted Atlas scheduled tasks only; task names are constants so they are injection-safe
# to interpolate into the PowerShell commands below. Auth is enforced by _check_auth.
_SERVICES = [
    {"task": "Atlas-Brain", "label": "Brain", "port": 8767},
    {"task": "Atlas-Brain-Worker", "label": "Worker", "port": 0},
    {"task": "Atlas-MCP", "label": "MCP", "port": 8765},
    {"task": "Atlas-Web-UI", "label": "Web UI", "port": 8766},
    {"task": "Atlas-Telegram-Bot", "label": "Telegram", "port": 0},
    {"task": "Atlas-Ntfy-Listener", "label": "Ntfy Listener", "port": 0},
    {"task": "Atlas-Job-Poller", "label": "Job Poller", "port": 0},
]
_SERVICE_TASKS = {s["task"] for s in _SERVICES}
_SERVICE_ACTIONS = {"start", "stop", "restart"}


def _install_marks() -> list[str]:
    """Paths whose presence in a task's command line makes it THIS install's: the venv, its python, the clone."""
    import sys as _sys
    marks = [_sys.prefix, os.path.dirname(_sys.executable)]
    try:
        marks.append(str(Path(__file__).resolve().parents[3]))   # the clone of an editable install (src/atlas_plus/web)
    except IndexError:
        pass
    out = []
    for m in marks:
        n = m.replace("\\", "/").rstrip("/").casefold()
        if n and len(n) > 3 and n not in out:   # never a bare drive root: it would match every task
            out.append(n)
    return out


def _task_is_this_install(cmdline: str) -> bool:
    """A scheduled task is found by its FIXED name, so another install's Atlas-Brain answered as this one's (AG97,
    sweep 25): its command line must name this install's venv, python or clone before the dashboard offers buttons."""
    c = (cmdline or "").replace("\\", "/").casefold()
    return bool(c.strip()) and any(m in c for m in _install_marks())


_SERVICE_PS_ACTION = "$a=@($t.Actions)[0];"
_SERVICE_PS_CMDLINE = "$($a.Execute) $($a.Arguments) $($a.WorkingDirectory)"


def _run_powershell(cmd: str, timeout: float = 25.0) -> tuple[int, str]:
    """Run a PowerShell command; return (returncode, combined output). Module-level for test injection."""
    from atlas_plus import _proc
    try:
        # the bounded helper: PowerShell's whole tree ends at the timeout and the output is capped (LES-072)
        res = _proc.run_bounded(["powershell", "-NoProfile", "-NonInteractive", "-Command", cmd],
                                timeout=timeout, max_bytes=2_000_000)
    except Exception as exc:
        return 1, str(exc)
    if res.timed_out:
        return 1, f"powershell timed out after {timeout:g} seconds"
    out = (res.stdout_text() + res.stderr_text()).replace("\r\n", "\n")
    return (1 if res.returncode is None else res.returncode), out


@app.get("/api/services", response_class=HTMLResponse)
async def services_list(request: Request):
    """GET /api/services -- HTMX rows of Atlas service state with start/stop/restart buttons."""
    _check_auth(request)
    names = "','".join(s["task"] for s in _SERVICES)
    cmd = (
        "$ErrorActionPreference='SilentlyContinue';@('" + names + "')|ForEach-Object{"
        "$t=Get-ScheduledTask -TaskName $_;$i=Get-ScheduledTaskInfo -TaskName $_;" + _SERVICE_PS_ACTION +
        "\"$_`t$($t.State)`t$($i.LastTaskResult)`t" + _SERVICE_PS_CMDLINE + "\"}"
    )
    # PowerShell takes seconds; run it off-loop or every SSE/poll client in
    # this single-process uvicorn stalls behind it (sweep finding #3).
    rc, out = await asyncio.to_thread(lambda: _ttl_cached("services-ps", lambda: _run_powershell(cmd, 20.0), ttl=30.0))  # 5 s per poll before (MN34)
    states: dict[str, str] = {}
    cmdlines: dict[str, str] = {}
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2 and parts[0].strip():
            states[parts[0].strip()] = parts[1].strip()
            cmdlines[parts[0].strip()] = parts[3].strip() if len(parts) >= 4 else ""
    rows = [('<div class="text-slate-500 text-xs pb-1 border-b border-slate-700">Windows Scheduled Tasks found by fixed '
             'name (Atlas-*). Buttons appear only for a task whose command runs this install '
             f'({_html_mod.escape(sys.prefix)}); a task of another install is listed, never started or stopped here.</div>')]
    for s in _SERVICES:
        state = states.get(s["task"], "?")
        dot = "text-green-400" if state == "Running" else ("text-slate-500" if state in ("Ready", "Disabled") else "text-yellow-400")
        if not _task_is_this_install(cmdlines.get(s["task"], "")):
            _why = ("no such task on this machine" if state in ("?", "") else
                    "another install&#39;s task (not this one&#39;s command): no buttons")
            rows.append(
                f'<div class="flex items-center gap-2 py-1 border-b border-slate-700">'
                f'<span class="{dot} text-xs">&#9679;</span>'
                f'<span class="text-slate-300 text-xs w-28">{_html_mod.escape(s["label"])}</span>'
                f'<span class="text-slate-500 text-xs w-20">{_html_mod.escape(state)}</span>'
                f'<span class="text-slate-500 text-xs flex-1 truncate" title="{_html_mod.escape(cmdlines.get(s["task"], ""))}">'
                f'{_why}</span>'
                f'</div>'
            )
            continue
        rows.append(
            f'<div class="flex items-center gap-2 py-1 border-b border-slate-700">'
            f'<span class="{dot} text-xs">&#9679;</span>'
            f'<span class="text-slate-300 text-xs w-28">{_html_mod.escape(s["label"])}</span>'
            f'<span class="text-slate-500 text-xs w-20">{state}</span>'
            f'<span class="flex-1"></span>'
            f'<button class="btn btn-green text-xs" onclick="svcAction({_js_arg(s["task"])},\'start\')">start</button>'
            f'<button class="btn text-xs" onclick="svcAction({_js_arg(s["task"])},\'stop\')">stop</button>'
            f'<button class="btn btn-cyan text-xs" onclick="svcAction({_js_arg(s["task"])},\'restart\')">restart</button>'
            f'</div>'
        )
    return HTMLResponse("\n".join(rows) or '<div class="text-slate-500 text-xs">No services.</div>')


@app.post("/api/services/{task}/{action}")
async def services_action(task: str, action: str, request: Request):
    """POST /api/services/{task}/{action} -- start|stop|restart an allowlisted Atlas service."""
    _check_auth(request)
    if task not in _SERVICE_TASKS:
        raise HTTPException(status_code=400, detail="Unknown service")
    if action not in _SERVICE_ACTIONS:
        raise HTTPException(status_code=400, detail="Unknown action")
    # The list hides the buttons; the endpoint refuses too (a stale page or a scripted POST): AG97
    _rc0, _cl = await asyncio.to_thread(
        _run_powershell, f"$ErrorActionPreference='SilentlyContinue';$t=Get-ScheduledTask -TaskName '{task}';"
        + _SERVICE_PS_ACTION + '"' + _SERVICE_PS_CMDLINE + '"', 20.0)
    if _rc0 != 0 or not _task_is_this_install(_cl):
        raise HTTPException(status_code=409, detail=f"{task} is not this install's scheduled task (its command does not "
                                                    "run this install's Python): start or stop it where it was installed")
    if action == "start":
        cmd = f"Enable-ScheduledTask -TaskName '{task}' -EA SilentlyContinue | Out-Null; Start-ScheduledTask -TaskName '{task}'"
    elif action == "stop":
        cmd = f"Stop-ScheduledTask -TaskName '{task}'"
    else:  # restart
        cmd = (
            f"Stop-ScheduledTask -TaskName '{task}' -EA SilentlyContinue; Start-Sleep -Seconds 2; "
            f"Enable-ScheduledTask -TaskName '{task}' -EA SilentlyContinue | Out-Null; Start-ScheduledTask -TaskName '{task}'"
        )
    rc, out = await asyncio.to_thread(_run_powershell, cmd, 30.0)
    _TTL_CACHE.pop("services-ps", None)  # the next list reads the new state, not the 30 s cache (AG44)
    return {"task": task, "action": action, "ok": rc == 0, "output": out[-500:]}


@app.get("/api/tickets/board", response_class=HTMLResponse)
async def ticket_board(request: Request, project: str = "", status_filter: str = "open"):
    """GET /api/tickets/board -- HTMX Kanban ticket board for the Tickets tab.

    Groups tickets by status (todo / in-progress / blocked / done / archived).
    status_filter=open shows todo+in-progress+blocked; all shows every status.
    Includes dispatch badge and dispatch button per ticket.
    """
    _check_auth(request)
    from atlas_plus.tickets.store import list_tickets
    open_statuses = {"todo", "in-progress", "blocked"}
    try:
        all_tickets = list_tickets(project=project or None)
        if status_filter == "open":
            tickets_list = [t for t in all_tickets if t.status in open_statuses]
        elif status_filter == "all":
            tickets_list = all_tickets
        else:
            tickets_list = [t for t in all_tickets if t.status == status_filter]
    except Exception as exc:
        return HTMLResponse(f'<div class="text-red-400 text-xs">Error: {exc}</div>')

    if not tickets_list:
        return HTMLResponse('<div class="text-slate-500 text-xs">No tickets.</div>')
    _BOARD_CAP = 500
    _board_more = max(0, len(tickets_list) - _BOARD_CAP)
    if _board_more:  # keep the most recently updated, in the board's own order: the cap cut exactly the newest tickets (MN70)
        def _recency(t):  # `updated` is a date only: 520 tickets of one day tied and the newest were cut (MN76); the id number breaks the tie
            _m = re.search(r"-(\d+)$", str(getattr(t, 'id', '') or ''))
            return (str(getattr(t, 'updated', '') or getattr(t, 'created', '') or ''), int(_m.group(1)) if _m else -1)
        _keep = {id(t) for t in sorted(tickets_list, key=_recency, reverse=True)[:_BOARD_CAP]}
        tickets_list = [t for t in tickets_list if id(t) in _keep]  # 5,000 tickets made a 3 MB board and 40,586 DOM nodes (AG75)

    # Batch-query Brain tasks linked to tickets (dispatch + verify badges).
    # Dispatch: session_id = 'ticket-{ID}'; Verify: session_id = 'verify-ticket-{ID}'
    ticket_tasks: dict[str, dict] = {}
    verify_tasks_map: dict[str, dict] = {}
    try:
        from atlas_plus.brain import tasks as _ts
        import sqlite3 as _sq3
        conn = _sq3.connect(str(_ts._DB_PATH))
        conn.row_factory = _sq3.Row
        db_rows = conn.execute(
            "SELECT task_id, session_id, status FROM brain_tasks "
            "WHERE session_id LIKE 'ticket-%' OR session_id LIKE 'verify-ticket-%' "
            "ORDER BY created_at DESC"
        ).fetchall()
        conn.close()
        import re as _re
        _ticket_re = _re.compile(r"^ticket-([A-Za-z0-9]{1,16}-\d+)", _re.IGNORECASE)
        _verify_re = _re.compile(r"^verify-ticket-([A-Za-z0-9]{1,16}-\d+)", _re.IGNORECASE)
        for r in db_rows:
            sid = r["session_id"] or ""
            m = _ticket_re.match(sid)
            if m:
                tid = m.group(1).upper()
                if tid not in ticket_tasks:  # keep only most recent
                    ticket_tasks[tid] = {"task_id": r["task_id"], "status": r["status"]}
                continue
            mv = _verify_re.match(sid)
            if mv:
                tid = mv.group(1).upper()
                if tid not in verify_tasks_map:  # keep only most recent
                    verify_tasks_map[tid] = {"task_id": r["task_id"], "status": r["status"]}
    except Exception:
        pass  # ticket dispatch/verify badges are best-effort

    status_colors = {
        "todo": "text-blue-400", "in-progress": "text-yellow-400",
        "blocked": "text-orange-400", "done": "text-green-400", "archived": "text-slate-500",
    }
    priority_colors = {
        "critical": "text-red-500", "high": "text-orange-400",
        "normal": "text-slate-300", "low": "text-slate-500",
    }
    _task_badge_colors = {
        "pending": ("text-blue-400", "..."),
        "running": ("text-yellow-400", ">"),
        "done":    ("text-green-400", "ok"),
        "failed":  ("text-red-400",   "!"),
        "blocked": ("text-orange-400", "?"),
    }
    rows = []
    for t in tickets_list:
        sc = status_colors.get(t.status, "text-slate-300")
        pc = priority_colors.get(getattr(t, "priority", "normal"), "text-slate-300")
        assignee = getattr(t, "assignee", "human")
        assignee_label = "CC" if assignee == "claude-code" else assignee[:2].upper()
        task_info = ticket_tasks.get(t.id.upper(), {})
        if task_info:
            _tc, _sym = _task_badge_colors.get(task_info["status"], ("text-slate-500", "?"))
            _tip = f"Brain task {task_info['task_id']} ({task_info['status']})"
            task_badge = (
                f'<span class="{_tc} text-xs w-6 shrink-0 font-mono" '
                f'title="{_html_mod.escape(_tip)}">{_sym}</span>'
            )
        else:
            task_badge = '<span class="text-slate-700 text-xs w-6 shrink-0">-</span>'

        # Verify badge: show verify task status (pass/fail/pending)
        verify_info = verify_tasks_map.get(t.id.upper(), {})
        if verify_info:
            _v_status = verify_info.get("status", "?")
            _v_colors = {
                "done": ("text-green-400", "V"),
                "failed": ("text-red-400", "X"),
                "pending": ("text-blue-400", "v?"),
                "running": ("text-yellow-400", "v.."),
            }
            _vc, _vsym = _v_colors.get(_v_status, ("text-slate-500", "?"))
            _vtip = f"Verify task {verify_info['task_id']} ({_v_status})"
            verify_badge = (
                f'<span class="{_vc} text-xs w-6 shrink-0 font-mono" '
                f'title="{_html_mod.escape(_vtip)}">{_vsym}</span>'
            )
        else:
            verify_badge = ""

        rows.append(
            f'<div class="flex items-center gap-2 py-1 border-b border-slate-700 row-hover group"'
            f' onclick="showTicketDetail({_js_arg(_tref(t))})">'
            f'<span class="text-slate-500 text-xs w-24 truncate font-mono tk-id">{_html_mod.escape(str(t.id))}</span>'  # ticket frontmatter arrives by sync, intake or hand: markup in it ran (MN70)
            f'<span class="{sc} text-xs w-20 tk-status">{_html_mod.escape(str(t.status))}</span>'
            f'<span class="{pc} text-xs w-4 shrink-0">{_html_mod.escape((str(getattr(t, "priority", "N")) or "N")[0].upper())}</span>'
            f'<span class="text-slate-300 text-xs flex-1 truncate">{_html_mod.escape(t.title)}</span>'
            f'<span class="text-slate-500 text-xs w-24 truncate tk-proj">{_html_mod.escape(str(t.project))}</span>'
            f'<span class="text-slate-600 text-xs w-6 shrink-0">{_html_mod.escape(str(assignee_label))}</span>'
            f'{task_badge}'
            f'{verify_badge}'
            f'</div>'
        )
    if _board_more:
        _narrow = "pick a status filter" if project else "pick a project or a status filter"  # with a project picked it still asked for one (MN76)
        rows.append(f'<div class="text-slate-500 text-xs mt-2">{_board_more} more ticket(s) not shown: {_narrow} to narrow the board.</div>')
    return HTMLResponse("\n".join(rows))


def _operator_log_append(line: str) -> None:
    """Append one operator-action line to the corpus action log.

    The log is an ordinary corpus markdown file, so completed operator
    steps become part of memory: FTS-indexed, searchable by any session,
    and synced like everything else.

    One ACTION is one LINE, enforced here at the sink (2026-10-09): a
    completion note -- or a hand-written ticket's title/project -- that
    carries newlines could otherwise forge standalone entries in a log
    that sessions read back as memory. Every control character is
    flattened to a space before the write.
    """
    from atlas_plus import paths as atlas_paths

    line = "".join(c if c >= " " else " " for c in line).strip()
    if not line:
        return

    op_dir = atlas_paths.memory_root() / "operator"
    op_dir.mkdir(parents=True, exist_ok=True)
    log = op_dir / "action-log.md"
    if not log.exists():
        log.write_text(
            "---\n"
            "id: operator-action-log\n"
            "title: Operator action log\n"
            "type: log\n"
            "---\n\n"
            "# Operator action log\n\n"
            "Append-only record of operator steps completed or skipped from\n"
            "the dashboard's Operator panel. Newest entries last.\n\n",
            encoding="utf-8",
        )
    with log.open("a", encoding="utf-8") as fh:
        fh.write(line.rstrip("\n") + "\n")
    try:
        from atlas_plus.mcp.memory_server import fts as _fts
        _fts.mark_dirty()
    except Exception:
        pass  # search freshness is best-effort here


@app.get("/api/operator/queue-html", response_class=HTMLResponse)
async def operator_queue_html(request: Request):
    """GET /api/operator/queue-html -- the Operator panel: every human-assigned
    open step across all projects in one queue, instructions inline.

    Steps carrying a `verified:` date render as READY; the rest as NEEDS
    RESEARCH, so the operator can see at a glance what is safe to execute
    without re-deriving context. Completions post back through
    /api/operator/{id}/complete and land in the corpus action log.
    """
    _check_auth(request)
    from datetime import date as _date
    from atlas_plus.tickets.store import list_tickets

    try:
        human = list_tickets(assignee="human")
    except Exception as exc:
        return HTMLResponse(
            f'<div class="text-red-400 text-xs">Error: {_html_mod.escape(str(exc))}</div>'
        )
    open_statuses = {"todo", "in-progress", "blocked"}
    queue = [t for t in human if t.status in open_statuses]
    today = _date.today().isoformat()
    done_today = [
        t for t in human if t.status in ("done", "archived") and t.updated == today
    ]

    priority_colors = {
        "critical": "text-red-500", "high": "text-orange-400",
        "normal": "text-slate-300", "low": "text-slate-500",
    }
    parts: list[str] = []
    ready = sum(1 for t in queue if t.verified)
    parts.append(
        f'<div class="text-xs text-slate-400 pb-1">{len(queue)} step(s) queued'
        f' &middot; {ready} verified ready &middot; {len(done_today)} completed today</div>'
    )
    if not queue:
        parts.append('<div class="text-slate-500 text-xs">Operator queue is empty.</div>')
    for t in queue:
        pc = priority_colors.get(t.priority, "text-slate-300")
        if t.verified:
            badge = (
                f'<span class="text-green-400 text-xs border border-green-700 rounded px-1"'
                f' title="Instructions verified {_html_mod.escape(t.verified)}">READY</span>'
            )
        else:
            badge = (
                '<span class="text-amber-400 text-xs border border-amber-700 rounded px-1"'
                ' title="Instructions not yet verified">NEEDS RESEARCH</span>'
            )
        requires = (
            f'<span class="text-orange-400 text-xs">after {_html_mod.escape(t.requires)}</span>'
            if t.requires else ""
        )
        blocked = (
            '<span class="text-orange-400 text-xs">[blocked]</span>'
            if t.status == "blocked" else ""
        )
        body = t.body.strip() or "(no instructions recorded)"
        parts.append(
            f'<details class="border border-slate-700 rounded mb-2 bg-slate-800/40">'
            f'<summary class="flex items-center gap-2 px-2 py-1 cursor-pointer">'
            f'<span class="{pc} text-xs w-4 shrink-0">{_html_mod.escape(t.priority[:1].upper())}</span>'
            f'<span class="text-slate-500 text-xs w-20 truncate font-mono">{_html_mod.escape(t.id)}</span>'
            f'<span class="text-slate-200 text-xs flex-1 truncate">{_html_mod.escape(t.title)}</span>'
            f'{badge}{blocked}{requires}'
            f'<span class="text-slate-500 text-xs w-24 truncate">{_html_mod.escape(t.project)}</span>'
            f'<span class="text-slate-600 text-xs w-6 shrink-0">{_html_mod.escape(t.effort)}</span>'
            f'</summary>'
            f'<pre class="text-xs text-slate-300 whitespace-pre-wrap px-3 py-2 border-t border-slate-700">'
            f'{_html_mod.escape(body)}</pre>'
            f'<div class="flex gap-2 px-3 pb-2">'
            f'<button class="text-xs text-green-400 border border-green-700 rounded px-2"'
            f' onclick="operatorAct({_js_arg(_tref(t))},{_js_arg("done")})">Mark done</button>'
            f'<button class="text-xs text-slate-400 border border-slate-600 rounded px-2"'
            f' onclick="operatorAct({_js_arg(_tref(t))},{_js_arg("skipped")})">Skip</button>'
            f'<button class="text-xs text-slate-500 border border-slate-700 rounded px-2"'
            f' onclick="switchTab(\'tickets\');showTicketDetail({_js_arg(_tref(t))})">Open ticket</button>'
            f'</div>'
            f'</details>'
        )
    if done_today:
        parts.append('<div class="text-xs text-slate-500 pt-2 border-t border-slate-700">Completed today</div>')
        for t in done_today:
            parts.append(
                f'<div class="flex items-center gap-2 py-1 text-xs">'
                f'<span class="text-green-500 w-4 shrink-0">&#10003;</span>'
                f'<span class="text-slate-500 w-20 truncate font-mono">{_html_mod.escape(t.id)}</span>'
                f'<span class="text-slate-400 flex-1 truncate">{_html_mod.escape(t.title)}</span>'
                f'<span class="text-slate-600 w-24 truncate">{_html_mod.escape(t.project)}</span>'
                f'</div>'
            )
    return HTMLResponse("\n".join(parts))


def _tref(t) -> str:
    """`project/ID`: the one name for a ticket when two projects minted the same id (AG45: the board's
    Done landed on the other project's ticket)."""
    try:
        from atlas_plus.tickets.store import TICKETS_ROOT as _troot
        p = getattr(t, "path", None)
        if p is not None:
            parent = Path(p).resolve().parent
            if parent == Path(_troot).resolve():
                return t.id  # a root-level ticket: the bare id, even when a folder holds the same id (AG49)
            return f"{parent.name}/{t.id}"  # the folder the file is IN, not the frontmatter's project
        proj = getattr(t, "project", "") or ""
        if proj and (_troot / proj / f"{t.id}.md").is_file():
            return f"{proj}/{t.id}"
    except Exception:  # noqa: BLE001
        pass
    return t.id  # a root-level or moved ticket keeps the bare id (MN37: `tickets/OLD-001` was not found)


def _norm_ref(ref: str) -> str:
    proj, sep, tid = ref.rpartition("/")
    return f"{proj}/{tid.upper()}" if sep else tid.upper()


@app.post("/api/operator/{ticket_id:path}/complete")
async def operator_complete(ticket_id: str, request: Request):
    """POST /api/operator/{id}/complete -- record an operator action.

    Body: {"outcome": "done"|"skipped", "note": "..."}. Flips the ticket's
    status (done, or archived for skipped) and appends the action to the
    corpus operator log so the completion feeds back into Atlas memory.
    """
    _check_auth(request)
    try:
        payload = json.loads((await request.body()) or b"{}")
    except ValueError:
        raise HTTPException(status_code=400, detail="Body is not valid JSON.")
    outcome = str(payload.get("outcome", "done"))
    status_map = {"done": "done", "skipped": "archived"}
    if outcome not in status_map:
        raise HTTPException(status_code=400, detail="outcome must be 'done' or 'skipped'.")
    note = str(payload.get("note", "") or "").strip()[:500]

    from atlas_plus.tickets.store import get_ticket, update_ticket_status

    ticket_id = _norm_ref(ticket_id)
    t = get_ticket(ticket_id)
    if t is None:
        from atlas_plus.tickets import store as _tstore
        if _tstore.ambiguous_hint():
            raise HTTPException(status_code=409, detail=_tstore.ambiguous_hint())  # MN56: a bare id acted on an arbitrary folder ticket
    if t is None:
        raise HTTPException(status_code=404, detail=f"No ticket {ticket_id!r}.")
    if t.assignee != "human":
        raise HTTPException(
            status_code=400,
            detail=f"{ticket_id} is assigned to {t.assignee!r}, not an operator step.",
        )
    updated = update_ticket_status(ticket_id, status_map[outcome])

    from datetime import datetime as _dt, timezone as _tz
    stamp = _dt.now(_tz.utc).isoformat(timespec="seconds")
    line = f"- {stamp} [{outcome}] {updated.id} {updated.title} ({updated.project})"
    if note:
        line += f" -- note: {note}"
    try:
        _operator_log_append(line)
        logged = True
    except Exception:
        logged = False  # the status change stands; the log is additive
    return {"ok": True, "id": updated.id, "status": updated.status, "logged": logged}


@app.post("/api/tickets/create", status_code=201)
async def create_ticket_endpoint(request: Request):
    """POST /api/tickets/create -- create a new ticket in the .memory/tickets/ store.

    Body: {title, project, assignee?, priority?, effort?, body?}.
    Returns the created ticket dict. 400 when validation fails (bad assignee, empty title, etc.).
    """
    _check_auth(request)
    if not MEMORY_ROOT.exists():
        raise HTTPException(status_code=400, detail=f"memory root does not exist: {MEMORY_ROOT} -- run sextant-init there")
    body = await _json_object(request, required=("title", "project"))
    for _k in ("body", "assignee", "priority", "effort", "verified", "requires"):
        if body.get(_k) is not None and not isinstance(body[_k], str):
            raise HTTPException(status_code=400, detail=f"Field {_k} must be a string")  # `body: true` became the text True (AG38)
    from atlas_plus.tickets.store import create_ticket
    try:
        t = create_ticket(
            interactive=True,  # the dashboard is the operator's own hand (MN23: it was refused with ATLAS_AUTONOMOUS off)
            title=body["title"],
            project=body["project"],
            assignee=body.get("assignee", "human"),
            priority=body.get("priority", "normal"),
            effort=body.get("effort", "M"),
            body=body.get("body", ""),
            requires=(str(body.get("requires")).strip() or None) if body.get("requires") else None,  # it was validated and dropped (MN34)
            verified=(str(body.get("verified")).strip() or None) if body.get("verified") else None,  # READY needs it (MN29)
        )
        return t.as_dict()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/tickets/{ticket_id:path}/dispatch", status_code=202)  # `project/ID` reaches it (MN64: a qualified id was 404)
async def dispatch_ticket_to_brain(ticket_id: str, request: Request):
    """Dispatch a ticket to Brain as an async task and mark it in-progress."""
    _check_auth(request)
    body = await _json_object(request)
    from atlas_plus.tickets.store import get_ticket as _get, update_ticket_status as _update_status, ambiguous_hint as _ah, LAST_AMBIGUOUS as _la

    _la.clear()
    _tid = _norm_ref(ticket_id)  # `alpha/alp-001` answered 404 here and 200 on status/detail (AG73)
    ticket = _get(_tid)
    if not ticket:
        if _ah():  # one id in two projects: the first match was dispatched and neither status changed (MN64)
            raise HTTPException(status_code=409, detail=_ah())
        raise HTTPException(status_code=404, detail=f"Ticket {ticket_id} not found")
    from atlas_plus.tickets.store import ticket_ref as _tref
    _ref = _tref(ticket)  # the folder it lives in: a renamed folder's frontmatter project set ANOTHER ticket in-progress (AG86)

    instruction = body.get("instruction") or f"{ticket.title}\n\n{getattr(ticket, 'body', '') or ''}"
    instruction = instruction.strip()[:2000]

    # Respect ticket assignee as an agent hint so routing honours the ticket's intent
    _ASSIGNEE_AGENT_MAP = {
        "claude-code": "claude-code",
        "codex": "codex",
        "local": "brain",  # explicit local = Qwen3 brain
    }
    agent_hint = body.get("agent") or _ASSIGNEE_AGENT_MAP.get(getattr(ticket, "assignee", ""))
    # A bare id that names two tickets would make the Brain's close-out fail on the ambiguity and leave the
    # dispatched ticket in progress for ever: such a ticket goes out as `ticket-<folder>/<ID>` (MN79)
    _sess = ticket.id if _get(ticket.id) is not None else _ref
    _la.clear()
    task_payload: dict = {"instruction": instruction, "session_id": f"ticket-{_sess}"}
    if agent_hint:
        task_payload["agent"] = agent_hint

    try:
        async with _PeerClient(timeout=httpx.Timeout(10.0, connect=0.75)) as client:
            r = await client.post(
                f"{BRAIN_URL}/task",
                json=task_payload,
                headers=_brain_headers,
            )
        r.raise_for_status()
        task = r.json()
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Brain unavailable: {exc}")

    status_note = "ticket set to in-progress"
    try:
        _update_status(_ref, "in-progress")
    except Exception as exc:  # noqa: BLE001 - the dispatch stands; the page says the status did NOT change (MN64: it claimed it had)
        status_note = f"the ticket's status was not changed: {exc}"

    await _broadcast({"type": "task_submitted", "task": task})
    return {"task_id": task["task_id"], "ticket_id": ticket.id, "ticket_ref": _ref, "status": "dispatched", "status_note": status_note}


@app.patch("/api/tickets/{ticket_id:path}/status")
async def update_ticket_status(ticket_id: str, request: Request):
    """PATCH /api/tickets/{id}/status -- update ticket status in .memory/tickets/.

    Body: {"status": "todo"|"in-progress"|"done"|"blocked"|"archived"}.
    Returns updated ticket dict. 400 on invalid status or unknown ticket.
    """
    _check_auth(request)
    body = await _json_object(request, required=("status",))
    from atlas_plus.tickets.store import update_ticket_status as _update, ambiguous_hint as _ah, LAST_AMBIGUOUS as _la
    _la.clear()
    try:
        t = _update(_norm_ref(ticket_id), body["status"])  # `alp-b/ALP-001` keeps its project (AG45)
        return t.as_dict()
    except FileNotFoundError as exc:
        # An ambiguous bare id was 400 here and 409 on dispatch and detail: one cause, one answer (SEX-026)
        raise HTTPException(status_code=409 if _ah() else 400, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.get("/api/tickets/dispatch-stats")
async def tickets_dispatch_stats(request: Request):
    """Proxy Brain /dispatch-stats -- ticket dispatch activity and queue depth."""
    _check_auth(request)
    brain_key = os.environ.get("ATLAS_BRAIN_API_KEY", "")
    try:
        async with _PeerClient(timeout=httpx.Timeout(5.0, connect=0.75)) as client:
            r = await client.get(
                f"{BRAIN_URL}/dispatch-stats",
                headers={"Authorization": f"Bearer {brain_key}"},
            )
            r.raise_for_status()
            return r.json()
    except Exception as exc:
        return {"enabled": False, "error": str(exc)}


@app.get("/api/tickets/dispatch-stats-html", response_class=HTMLResponse)
async def tickets_dispatch_stats_html(request: Request):
    """HTMX badge showing dispatcher state for the Tickets tab header."""
    _check_auth(request)
    brain_key = os.environ.get("ATLAS_BRAIN_API_KEY", "")
    stats: dict = {"enabled": False}
    try:
        async with _PeerClient(timeout=httpx.Timeout(5.0, connect=0.75)) as client:
            r = await client.get(
                f"{BRAIN_URL}/dispatch-stats",
                headers={"Authorization": f"Bearer {brain_key}"},
            )
            if r.status_code == 200:
                stats = r.json()
    except Exception:
        pass

    enabled = stats.get("enabled", False)
    last_run = stats.get("last_run_at")
    in_flight = stats.get("in_flight", 0)
    last_count = stats.get("last_dispatched_count", 0)

    if last_run:
        import datetime
        try:
            run_dt = datetime.datetime.fromisoformat(last_run)
            age_min = int(
                (datetime.datetime.now(datetime.timezone.utc) - run_dt).total_seconds() / 60
            )
            age_str = f"{age_min}m ago"
        except Exception:
            age_str = "?"
    else:
        age_str = "never"

    if not enabled:
        badge_html = (
            '<span class="text-xs text-slate-500 ml-1" title="Set ATLAS_TICKET_DISPATCH=on to enable">'
            'dispatch: off</span>'
        )
    elif in_flight > 0:
        badge_html = (
            f'<span class="text-xs text-yellow-400 ml-1" '
            f'title="Last run: {age_str}, dispatched {last_count} tickets">'
            f'dispatch: {in_flight} in-flight</span>'
        )
    else:
        badge_html = (
            f'<span class="text-xs text-green-400 ml-1" '
            f'title="Last run: {age_str}, dispatched {last_count} tickets">'
            f'dispatch: ok (last: {age_str})</span>'
        )
    return HTMLResponse(badge_html)


@app.get("/api/tickets/{ticket_id:path}/detail", response_class=HTMLResponse)
async def ticket_detail(ticket_id: str, request: Request):
    """GET /api/tickets/{id}/detail -- HTMX ticket detail panel for the Tickets tab.

    Shows: status badge, assignee, priority, effort, body text, dispatch button.
    Reads directly from .memory/tickets/ (local file, no HTTP round-trip).
    """
    _check_auth(request)
    from atlas_plus.tickets.store import get_ticket as _get_ticket, list_tickets
    ticket = _get_ticket(_norm_ref(ticket_id)) if "/" in ticket_id else next((t for t in list_tickets() if t.id == ticket_id.upper()), None)
    if ticket is not None and "/" not in ticket_id:
        from atlas_plus.tickets import store as _tstore
        if _get_ticket(ticket_id.upper()) is None and _tstore.ambiguous_hint():  # a bare id in several project folders showed whichever the walk met first (AG67, sibling of MN56's 409)
            return HTMLResponse(f'<div class="text-red-400 text-xs">{_html_mod.escape(_tstore.ambiguous_hint())}</div>', status_code=409)
    if not ticket:
        return HTMLResponse(f'<div class="text-red-400 text-xs">Ticket {_html_mod.escape(ticket_id)} not found</div>')  # reflected markup ran in the page (AG73)
    body_text = getattr(ticket, "body", "") or ""
    escaped_body = _html_mod.escape(body_text)
    sc = {
        "todo": "text-blue-400", "in-progress": "text-yellow-400",
        "done": "text-green-400", "blocked": "text-orange-400", "archived": "text-slate-500",
    }.get(ticket.status, "text-slate-300")
    reopen_count = getattr(ticket, "reopen_count", 0)
    reopen_html = ""
    if reopen_count > 0:
        reopen_color = "text-red-400" if reopen_count >= 3 else "text-yellow-400"
        reopen_html = (
            f'<div class="flex gap-2"><span class="text-slate-500 w-16">reopened</span>'
            f'<span class="{reopen_color} font-bold">{reopen_count}x</span>'
            f'<span class="text-slate-600 text-xs">(quality gate or verify failures)</span></div>'
        )
    return HTMLResponse(
        f'<div class="text-xs space-y-1">'
        f'<div class="flex gap-2"><span class="text-slate-500 w-16">id</span>'
        f'<span class="text-slate-300 font-mono">{_html_mod.escape(str(ticket.id))}</span></div>'  # ticket detail: the same frontmatter fields (MN70)
        f'<div class="flex gap-2"><span class="text-slate-500 w-16">status</span>'
        f'<span class="{sc} font-bold">{_html_mod.escape(str(ticket.status))}</span></div>'
        f'<div class="flex gap-2"><span class="text-slate-500 w-16">project</span>'
        f'<span class="text-slate-400">{_html_mod.escape(str(ticket.project))}</span></div>'
        f'<div class="flex gap-2"><span class="text-slate-500 w-16">assignee</span>'
        f'<span class="text-slate-400">{_html_mod.escape(str(getattr(ticket, "assignee", "human")))}</span></div>'
        f'<div class="flex gap-2"><span class="text-slate-500 w-16">priority</span>'
        f'<span class="text-slate-400">{_html_mod.escape(str(getattr(ticket, "priority", "normal")))}</span></div>'
        f'{reopen_html}'
        f'<div class="mt-2 font-bold text-slate-200">{_html_mod.escape(ticket.title)}</div>'
        f'<pre class="mt-1 text-slate-400 bg-slate-900 p-2 rounded overflow-auto max-h-48 '
        f'whitespace-pre-wrap text-xs">{escaped_body}</pre>'
        f'<div class="mt-2 flex gap-2 flex-wrap">'
        f'<button onclick="dispatchTicketToBrain({_js_arg(_tref(ticket))},{_js_arg(ticket.title)},{_js_arg(body_text[:300])})" '  # the folder the file is in, like Start/Done/Block: the frontmatter project of a renamed folder was 404 or ANOTHER ticket (MN83)
        f'class="btn btn-cyan text-xs">Dispatch to Brain</button>'
        f'<button onclick="updateTicket({_js_arg(_tref(ticket))},\'in-progress\')" '
        f'class="btn btn-yellow text-xs">Start</button>'
        f'<button onclick="updateTicket({_js_arg(_tref(ticket))},\'done\')" '
        f'class="btn btn-green text-xs">Done</button>'
        f'<button onclick="updateTicket({_js_arg(_tref(ticket))},\'blocked\')" '
        f'class="btn btn-slate text-xs">Block</button>'
        f'</div>'
        f'</div>'
    )


# ---------------------------------------------------------------------------
# Memory search
# ---------------------------------------------------------------------------

@app.get("/api/memory/search", response_class=HTMLResponse)
async def memory_search(request: Request, q: str = ""):
    """GET /api/memory/search?q= -- FTS5 memory search for the Memory tab left panel.

    When q is empty, shows the memory INDEX.md instead. Results are clickable
    file paths that trigger a browse view. Calls the MCP FTS module via asyncio.to_thread.
    """
    _check_auth(request)

    def _do_search() -> str:
        if not q.strip():
            index_file = MEMORY_ROOT / "INDEX.md"
            if not index_file.exists():
                # nothing writes INDEX.md on a fresh install (AG31): show the corpus layout instead
                if not MEMORY_ROOT.exists():
                    return '<div class="text-slate-500 text-xs">No corpus yet: run sextant-init.</div>'
                _skip = {"versions", "_atlas", "archive", "_sync"}
                rows = []
                for d in sorted(p for p in MEMORY_ROOT.iterdir() if p.is_dir() and p.name not in _skip and not p.name.startswith(".")):
                    n = sum(1 for f in d.rglob("*.md") if not (_skip & set(f.relative_to(MEMORY_ROOT).parts)))
                    rows.append(f'<div class="text-xs"><span class="text-cyan-400 font-mono">{_html_mod.escape(d.name)}/</span> <span class="text-slate-500">{n} note(s)</span></div>')
                return ('<div class="text-slate-500 text-xs mb-1">Type words to search; the corpus holds:</div>' + "".join(rows)) if rows else '<div class="text-slate-500 text-xs">The corpus is empty: remember something first.</div>'
            content = index_file.read_text(encoding="utf-8", errors="replace")[:6000]
            return (
                f'<pre class="text-xs text-slate-300 whitespace-pre-wrap overflow-auto leading-relaxed">'
                f'{_html_mod.escape(content)}</pre>'
            )

        # Try FTS5 index first (better ranking + confidence)
        try:
            from atlas_plus.mcp.memory_server.tools.search import memory_search as fts_search
            fts_results = fts_search(q, scope="", limit=30)
            if not fts_results:
                try:
                    from atlas_plus.mcp.memory_server.fts import last_connect_error as _lce
                    _err = _lce()
                except Exception:  # noqa: BLE001
                    _err = ""
                if _err:
                    return (f'<div class="text-red-400 text-xs">Search index unavailable: {_html_mod.escape(_err)}. '
                            f'Run <code>{_html_mod.escape(__import__("atlas_plus._next_step", fromlist=["script"]).script("fts-rebuild").strip("`"))}</code>; '
                            'check that the state dir is writable.</div>')  # `sextant fts-rebuild` is no command (SEX-052 item 4)
                # AG89: the index answered "nothing", so the dashboard says what `sextant search` and the MCP tool say;
                # the substring grep below answered `لله`, `רגיש` and an emoji where both said "No hits" (MN74)
                from atlas_plus.mcp.memory_server.fts import fts5_available as _fts5_ok
                if _fts5_ok():
                    from atlas_plus._next_step import EMPTY_SEARCH as _empty  # what to try next (AG107)
                    return (f'<div class="text-slate-500 text-xs">No hits for &quot;{_html_mod.escape(q)}&quot;: search '
                            f'matches whole words, the same as <code>sextant search</code> and the MCP tool. '
                            f'{_html_mod.escape(_empty.split(". ", 1)[1])}</div>')
            if fts_results:
                top_score = float(fts_results[0].get("score", 0))
                if top_score < 0.2:
                    conf, conf_cls = "starved", "text-red-400"
                elif top_score < 0.4:
                    conf, conf_cls = "low", "text-yellow-400"
                elif top_score < 0.7:
                    conf, conf_cls = "medium", "text-cyan-400"
                else:
                    conf, conf_cls = "high", "text-green-400"
                rows = [
                    f'<div class="flex items-center gap-3 mb-2">'
                    f'<span class="text-slate-500 text-xs">{len(fts_results)} results</span>'
                    f'<span class="{conf_cls} text-xs font-bold">{conf} confidence</span>'
                    f'</div>',
                    '<div class="space-y-2">',
                ]
                for r in fts_results:
                    rel = r.get("path", "")
                    snippet = r.get("snippet", "")
                    score = float(r.get("score", 0))
                    rows.append(
                        f'<div class="border-b border-slate-700 pb-2">'
                        f'<div class="flex items-center gap-2 mb-1">'
                        f'<span class="text-cyan-400 text-xs font-mono flex-1 truncate cursor-pointer hover:text-cyan-300"'
                        f' onclick="switchTab(\'memory\');readMemoryFile({_js_arg(rel)})"'
                        f' title="Open in Memory viewer">{_html_mod.escape(rel)}</span>'
                        f'<span class="text-slate-600 text-xs">{score:.2f}</span>'
                        f'</div>'
                        f'<div class="text-slate-300 text-xs pl-2" dir="auto">{_html_mod.escape(snippet[:200])}</div>'
                        f'</div>'
                    )
                rows.append('</div>')
                return "\n".join(rows)
        except Exception:
            pass  # fall through to file grep

        # Fallback: file grep, only when the keyword index cannot answer (no FTS5, or the search raised). A substring
        # match, labelled as one, folded like search (NFKC, points, the letter folds, case: `strasse` meets `Straße`)
        try:
            from atlas_plus.mcp.memory_server.fts import _fold_marks as _fm, _recompose as _rc
            import unicodedata as _ud
            _accents = dict.fromkeys(range(0x300, 0x370))   # the accents unicode61 removes: `lodz` meets `Łódź`

            def _gfold(s: str) -> str:
                s = _fm(_rc(_ud.normalize("NFKC", s))).casefold()
                return s if s.isascii() else _ud.normalize("NFC", _ud.normalize("NFD", s).translate(_accents))
        except Exception:  # noqa: BLE001
            def _gfold(s: str) -> str:
                return s.casefold()
        q_lower = _gfold(q)
        results: list[tuple[str, list[str]]] = []
        try:
            md_files = sorted(MEMORY_ROOT.rglob("*.md"))[:400]
        except Exception:
            return '<div class="text-red-400 text-xs">Could not scan memory directory.</div>'

        from atlas_plus._fsutil import is_link
        try:
            _root_real = MEMORY_ROOT.resolve()
        except OSError:
            _root_real = MEMORY_ROOT
        for md_file in md_files:
            if "_inbox" in md_file.parts:
                continue
            # a note swapped for a link (or a linked folder) is not indexed and the file viewer refuses it:
            # the grep fallback printed the target's text, even a file outside the corpus (MN74)
            if is_link(md_file) or any(is_link(a) for a in md_file.parents if a != MEMORY_ROOT and MEMORY_ROOT in a.parents):
                continue
            try:
                md_file.resolve().relative_to(_root_real)
            except (OSError, ValueError):
                continue
            try:
                rel_parts = md_file.relative_to(MEMORY_ROOT).parts
            except ValueError:
                rel_parts = ()
            if any(p in ("versions", "_atlas", "_sync", "archive") for p in rel_parts):
                continue   # the grep listed one hit per version copy (AG17, sweep 11)
            try:
                text = md_file.read_text(encoding="utf-8", errors="replace")
                if q_lower not in _gfold(text):
                    continue
                from atlas_plus.mcp.memory_server.fts import strip_snippet_comments as _ssc
                matching = [
                    _ssc(l.strip()) for l in text.split("\n")
                    if q_lower in _gfold(l) and l.strip() and not l.startswith("---")
                ][:3]
                rel = md_file.relative_to(MEMORY_ROOT).as_posix()   # it printed backslash paths on Windows (AG89)
                results.append((rel, matching))
            except Exception:
                pass

        if not results:
            from atlas_plus._next_step import EMPTY_SEARCH as _empty  # same next step as the CLI and MCP (AG107)
            return (f'<div class="text-slate-500 text-xs">No matches for &quot;{_html_mod.escape(q)}&quot;. '
                    f'{_html_mod.escape(_empty.split(". ", 1)[1])}</div>')

        rows = [f'<div class="text-slate-500 text-xs mb-2">{len(results)} file(s) matched (substring search: the keyword '
                f'index is unavailable, so a word inside a longer word counts)</div>',
                '<div class="space-y-2">']
        for rel, lines in results[:40]:
            rows.append(
                f'<div class="border-b border-slate-700 pb-2">'
                f'<div class="text-cyan-400 text-xs font-mono mb-1 cursor-pointer hover:text-cyan-300"'
                f' onclick="switchTab(\'memory\');readMemoryFile({_js_arg(rel)})"'
                f' title="Open in Memory viewer">{_html_mod.escape(rel)}</div>'
            )
            for line in lines:
                rows.append(f'<div class="text-slate-300 text-xs pl-2 truncate">{_html_mod.escape(line)}</div>')
            rows.append('</div>')
        rows.append('</div>')
        return "\n".join(rows)

    result_html = await asyncio.to_thread(_do_search)
    return HTMLResponse(result_html)


# ---------------------------------------------------------------------------
# Session viewer
# ---------------------------------------------------------------------------

@app.get("/api/sessions", response_class=HTMLResponse)
async def sessions_list(request: Request):
    """GET /api/sessions -- list session log files from .memory/sessions/ for the Sessions tab."""
    _check_auth(request)
    sessions_dir = MEMORY_ROOT / "sessions"
    if not sessions_dir.exists():
        return HTMLResponse('<div class="text-slate-500 text-xs">No sessions directory.</div>')
    dated = sorted([f for f in sessions_dir.glob("*.md") if not f.name.startswith("_")], reverse=True)[:60]
    history_file = sessions_dir / "_history.md"
    rows = ['<div class="space-y-0.5">']
    # History compact log first
    if history_file.exists():
        size = history_file.stat().st_size
        size_str = f"{size//1024}k" if size > 1024 else f"{size}b"
        rows.append(
            f'<div class="flex items-center gap-2 py-1 border-b border-slate-600 row-hover bg-slate-800"'
            f' onclick="showSession(\'_history.md\')">'
            f'<span class="text-yellow-400 text-xs font-bold">_history</span>'
            f'<span class="text-slate-600 text-xs ml-auto">{size_str}</span>'
            f'</div>'
        )
    if not dated and not history_file.exists():
        return HTMLResponse('<div class="text-slate-500 text-xs">No sessions yet.</div>')
    for f in dated:
        size = f.stat().st_size
        size_str = f"{size//1024}k" if size > 1024 else f"{size}b"
        rows.append(
            f'<div class="flex items-center gap-2 py-1 border-b border-slate-700 row-hover"'
            f' onclick="showSession({_js_arg(f.name)})">'
            f'<span class="text-cyan-400 text-xs">{f.stem}</span>'
            f'<span class="text-slate-600 text-xs ml-auto">{size_str}</span>'
            f'</div>'
        )
    rows.append('</div>')
    return HTMLResponse("\n".join(rows))


@app.get("/api/sessions/search", response_class=HTMLResponse)
async def sessions_search(q: str = "", request: Request = None):
    """GET /api/sessions/search?q= -- full-text search across all session log files."""
    _check_auth(request)
    q = q.strip()
    if not q:
        return HTMLResponse('<div class="text-slate-500 text-xs">Enter a query to search sessions.</div>')

    sessions_dir = MEMORY_ROOT / "sessions"
    if not sessions_dir.exists():
        return HTMLResponse('<div class="text-slate-500 text-xs">No sessions directory.</div>')

    q_lower = q.lower()
    results: list[tuple[str, str]] = []  # (filename, snippet)
    for f in sorted(sessions_dir.glob("*.md"), reverse=True):
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        idx = text.lower().find(q_lower)
        if idx == -1:
            continue
        start = max(0, idx - 60)
        end = min(len(text), idx + len(q) + 100)
        from atlas_plus.mcp.memory_server.fts import strip_snippet_comments as _ssc
        snippet = _ssc(text[start:end].replace("\n", " ").strip())   # `<!-- session:... -->` tags (AG97)
        results.append((f.name, snippet))
        if len(results) >= 20:
            break

    if not results:
        return HTMLResponse(f'<div class="text-slate-500 text-xs">No sessions match "{_html_mod.escape(q)}".</div>')

    rows = [f'<div class="text-slate-400 text-xs mb-1">{len(results)} match(es) for <span class="text-cyan-400">"{_html_mod.escape(q)}"</span></div>']
    for name, snippet in results:
        hl_snippet = re.sub(
            re.escape(q), lambda m: f'<mark class="bg-yellow-900 text-yellow-200">{_html_mod.escape(m.group())}</mark>',
            _html_mod.escape(snippet), flags=re.IGNORECASE,
        )
        stem = name.replace(".md", "")
        rows.append(
            f'<div class="py-1 border-b border-slate-700 row-hover cursor-pointer" onclick="showSession({_js_arg(name)})">'
            f'<div class="text-cyan-400 text-xs font-medium">{_html_mod.escape(stem)}</div>'
            f'<div class="text-slate-400 text-xs mt-0.5" dir="auto">...{hl_snippet}...</div>'
            f'</div>'
        )
    return HTMLResponse("\n".join(rows))


@app.get("/api/sessions/narrative", response_class=HTMLResponse)
async def sessions_narrative(request: Request):
    """Return the latest weekly narrative as an HTML panel."""
    _check_auth(request)
    sessions_dir = MEMORY_ROOT / "sessions"
    # Find the most recent YYYY-WNN-narrative.md
    narratives = sorted(sessions_dir.glob("????-W??-narrative.md"), reverse=True) if sessions_dir.exists() else []
    if not narratives:
        return HTMLResponse(
            '<div class="text-slate-500 text-xs italic">'
            'No weekly narrative yet. Runs Sunday 03:55 UTC or trigger via scheduler.</div>'
        )
    latest = narratives[0]
    content = latest.read_text(encoding="utf-8", errors="replace")[:5000]
    # Quick markdown-lite render: headers + bullets
    lines_html = []
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith("## "):
            lines_html.append(f'<div class="text-cyan-400 text-xs font-bold mt-2">{_html_mod.escape(stripped[3:])}</div>')
        elif stripped.startswith("# "):
            lines_html.append(f'<div class="text-slate-200 text-xs font-bold">{_html_mod.escape(stripped[2:])}</div>')
        elif stripped.startswith("- "):
            lines_html.append(f'<div class="text-slate-300 text-xs pl-2">- {_html_mod.escape(stripped[2:])}</div>')
        elif stripped.startswith("**") and stripped.endswith("**"):
            lines_html.append(f'<div class="text-slate-400 text-xs">{_html_mod.escape(stripped)}</div>')
        elif stripped:
            lines_html.append(f'<div class="text-slate-500 text-xs">{_html_mod.escape(stripped)}</div>')
    header = (
        f'<div class="text-yellow-400 text-xs font-bold mb-1">Weekly Narrative: {_html_mod.escape(latest.stem)}</div>'
    )
    return HTMLResponse(header + "\n".join(lines_html))


@app.get("/api/sessions/{name}", response_class=HTMLResponse)
async def session_read(name: str, request: Request):
    """Return session log file content as HTML (truncated at 10,000 chars with download link)."""
    _check_auth(request)
    safe_name = Path(name).name.replace("..", "")
    if not safe_name.endswith(".md"):
        safe_name += ".md"
    session_file = MEMORY_ROOT / "sessions" / safe_name
    if not session_file.exists():
        return HTMLResponse(f'<div class="text-red-400 text-xs">Session {_html_mod.escape(safe_name)} not found</div>')  # (AG73)
    content = session_file.read_text(encoding="utf-8", errors="replace")
    truncated = len(content) > 10_000
    preview = content[:10_000]
    # No ?key= in the link: the authed page load set the HttpOnly cookie
    # that GET auth accepts (ROADMAP item 3 -- keys in URLs leak into
    # browser history and proxy logs).
    download_url = f"/api/sessions/{safe_name}/download"
    truncation_notice = (
        f'<div class="text-yellow-400 text-xs mt-1">Showing first 10,000 of {len(content):,} chars. '
        f'<a href="{download_url}" class="underline">Download full file</a></div>'
    ) if truncated else ""
    return HTMLResponse(
        f'<div class="flex items-center justify-between mb-2">'
        f'<span class="text-cyan-400 text-xs font-bold">{safe_name}</span>'
        f'<a href="{download_url}" class="text-slate-500 text-xs hover:text-slate-300">download</a>'
        f'</div>'
        f'<pre class="text-xs text-slate-300 whitespace-pre-wrap overflow-auto leading-relaxed">'
        f'{_html_mod.escape(preview)}</pre>'
        f'{truncation_notice}'
    )


@app.get("/api/sessions/{name}/download")
async def session_download(name: str, request: Request):
    """Download a session log file as raw Markdown attachment.

    Supports auth via Authorization header or the session cookie (the
    latter allows direct browser links from the Sessions tab; ?key= is
    refused off GET /).
    """
    _check_auth(request)
    safe_name = Path(name).name.replace("..", "")
    if not safe_name.endswith(".md"):
        safe_name += ".md"
    session_file = MEMORY_ROOT / "sessions" / safe_name
    if not session_file.exists():
        raise HTTPException(status_code=404, detail=f"{safe_name} not found")
    content = session_file.read_text(encoding="utf-8", errors="replace")
    from fastapi.responses import Response as _Resp
    return _Resp(
        content=content.encode("utf-8"),
        media_type="text/markdown",
        headers={"Content-Disposition": f'attachment; filename="{safe_name}"'},
    )


# ---------------------------------------------------------------------------
# Brain sessions browser
# ---------------------------------------------------------------------------

@app.get("/api/brain/sessions", response_class=HTMLResponse)
async def brain_sessions_list(request: Request, limit: int = 30):
    """Return recent Brain conversation sessions as an HTML list.

    Each row shows session_id, message count, and last active time.
    Sessions from Brain CLI have a cli- prefix; Telegram sessions tg-; REST api-.
    """
    _check_auth(request)

    def _list() -> list[dict]:
        from atlas_plus.brain import history as _hist
        return _hist.list_sessions(limit=limit)

    try:
        sessions = await asyncio.to_thread(_list)
    except Exception as exc:
        return HTMLResponse(f'<div class="text-red-400 text-xs">Error: {exc}</div>')

    if not sessions:
        return HTMLResponse('<div class="text-slate-500 text-xs">No Brain sessions found.</div>')

    rows = [f'<div class="text-slate-500 text-xs mb-1">{len(sessions)} session(s)</div>',
            '<div class="space-y-0.5">']
    for s in sessions:
        sid = _html_mod.escape(s["session_id"])
        msg_count = s.get("message_count", 0)
        last = (s.get("last_active") or "")[:19]
        # Color-code by session type
        if sid.startswith("cli-"):
            label_class = "text-cyan-400"
        elif sid.startswith("tg-"):
            label_class = "text-green-400"
        elif sid.startswith("escalated-"):
            label_class = "text-yellow-400"
        else:
            label_class = "text-slate-300"
        rows.append(
            f'<div class="flex items-center gap-2 py-1 border-b border-slate-700 row-hover cursor-pointer"'
            f' onclick="loadBrainSession({_js_arg(sid)})">'
            f'<span class="{label_class} text-xs font-mono truncate flex-1" title="{sid}">{sid}</span>'
            f'<span class="text-slate-600 text-xs shrink-0">{msg_count}msg</span>'
            f'<span class="text-slate-600 text-xs shrink-0">{last[5:16] if last else ""}</span>'
            f'</div>'
        )
    rows.append('</div>')
    return HTMLResponse("\n".join(rows))


@app.get("/api/brain/sessions/{session_id}/html", response_class=HTMLResponse)
async def brain_session_messages(session_id: str, request: Request):
    """Return the conversation messages for a Brain session as HTML.

    Renders each message with role label, timestamp, and content (max 400 chars preview
    with a "show more" toggle). Used by the Brain Sessions browser panel.
    """
    _check_auth(request)

    def _get() -> list[dict]:
        from atlas_plus.brain import history as _hist
        return _hist.get_messages(session_id, limit=100)

    try:
        messages = await asyncio.to_thread(_get)
    except Exception as exc:
        return HTMLResponse(f'<div class="text-red-400 text-xs">Error: {exc}</div>')

    if not messages:
        return HTMLResponse(
            f'<div class="text-slate-500 text-xs">Session {_html_mod.escape(session_id)!r} has no messages.</div>'
        )

    lines = [
        f'<div class="text-slate-500 text-xs mb-2 border-b border-slate-700 pb-1">'
        f'Session: <span class="text-cyan-300 font-mono">{_html_mod.escape(session_id)}</span>'
        f' &mdash; {len(messages)} message(s)'
        f' &mdash; <a href="/session/{_html_mod.escape(session_id)}/export" '
        f'class="text-slate-400 hover:text-cyan-400 underline" download>export.md</a>'
        f'</div>',
        '<div class="space-y-1">',
    ]
    for i, msg in enumerate(messages):
        role = msg["role"].upper()
        content = msg.get("content", "")
        role_class = "text-green-400" if role == "USER" else "text-cyan-300"
        preview = _html_mod.escape(content[:400])
        if len(content) > 400:
            full = _html_mod.escape(content)
            preview_block = (
                f'<span id="bsm-{i}-short">{preview}<span class="text-slate-500">...</span>'
                f'<button class="text-xs text-slate-400 ml-1 hover:text-cyan-400" '
                f'onclick="document.getElementById({_js_arg(f"bsm-{i}-short")}).style.display=\'none\';'
                f'document.getElementById({_js_arg(f"bsm-{i}-full")}).style.display=\'\'">more</button></span>'
                f'<span id="bsm-{i}-full" style="display:none">{full}'
                f'<button class="text-xs text-slate-400 ml-1 hover:text-cyan-400" '
                f'onclick="document.getElementById({_js_arg(f"bsm-{i}-full")}).style.display=\'none\';'
                f'document.getElementById({_js_arg(f"bsm-{i}-short")}).style.display=\'\'">less</button></span>'
            )
        else:
            preview_block = preview
        lines.append(
            f'<div class="py-1 border-b border-slate-800">'
            f'<span class="{role_class} text-xs font-bold mr-2">{role}</span>'
            f'<span class="text-slate-400 text-xs whitespace-pre-wrap">{preview_block}</span>'
            f'</div>'
        )
    lines.append('</div>')
    return HTMLResponse("\n".join(lines))


# ---------------------------------------------------------------------------
# Chat WebSocket
# ---------------------------------------------------------------------------

@app.websocket("/ws/chat/{session_id}")
async def ws_chat(websocket: WebSocket, session_id: str):
    """WebSocket /ws/chat/{session_id} -- Brain Chat tab streaming connection.

    Accepts the auth cookie (set by the authed page load; browsers send it
    on same-origin WS handshakes automatically, and the Origin must match) or an
    Authorization: Bearer header for scripted clients; ?key= is refused. Proxies
    messages to the Brain /chat endpoint and streams
    response chunks back to the browser as Server-Sent text frames.
    """
    if API_KEY:
        # Key is configured -- always require it, even in dev mode. Accepted:
        # the session cookie (browsers send it on same-origin handshakes) or
        # an Authorization: Bearer header (scripted clients). ?key= is no
        # longer accepted here (audit 2026-11-09, row 13).
        from atlas_plus.auth import bearer_token, constant_time_equals
        # The handshake shares the HTTP failure accounting (wave-2 2026-11-09, SEC-04): a
        # source the dashboard has locked out is refused here too, and wrong WS credentials
        # count toward that lockout.
        client_ip, cf_ip = _client_ip(websocket)
        _ws_loopback = (not cf_ip) and client_ip in _LOOPBACK_HOSTS
        if not _ws_loopback and AUTH_FAILURES.is_rate_limited(cf_ip or client_ip):
            _warn_lockout(cf_ip or client_ip, client_ip)  # the WebSocket path refused silently while the HTTP path logged (MN50)
            await websocket.close(code=4429)
            return
        if "key" in websocket.query_params:
            AUTH_FAILURES.record(path=websocket.url.path, client_ip=client_ip, cf_ip=cf_ip,
                                 reason="query_key_refused")
            await websocket.close(code=4401)
            return
        bearer = bearer_token(websocket.headers.get("authorization", ""))
        if bearer:
            ok = constant_time_equals(bearer, API_KEY)
        else:
            # Cookie handshake: the browser sends the cookie on any same-site WS open, so the
            # Origin must be this dashboard's own host:port (round-B re-audit 2026-11-09).
            ok = _session_valid(websocket.cookies.get(_AUTH_COOKIE, "")) and _cookie_origin_ok(websocket)
        if not ok:
            AUTH_FAILURES.record(path=websocket.url.path, client_ip=client_ip, cf_ip=cf_ip,
                                 reason="ws_bad_credential")
            await websocket.close(code=4401)
            return
    elif not _ALLOW_NO_AUTH:
        # No key and not explicitly in no-auth dev mode -- reject.
        await websocket.close(code=4401)
        return
    elif websocket.headers.get("origin") and not _cookie_origin_ok(websocket):
        # no-auth dev mode: a page on another origin must not drive the chat socket (sweep 13, AG27)
        await websocket.close(code=4403)
        return
    await websocket.accept()
    try:
        # Use a generous timeout; Brain chat can take 30-120s for deep tool call chains.
        async with _PeerClient(timeout=httpx.Timeout(connect=10.0, read=140.0, write=30.0, pool=10.0)) as client:
            while True:
                text = await websocket.receive_text()
                if not text.strip():
                    continue
                try:
                    # A quick look first: with no Brain the chat said "Brain is thinking..." until the stream's
                    # connect gave up (AG97, sweep 25); now the MN56 sentence comes at once
                    await client.get(f"{BRAIN_URL}/health", headers=_brain_headers,
                                     timeout=httpx.Timeout(3.0, connect=0.75))
                except (httpx.ConnectError, httpx.ConnectTimeout) as _pre_exc:
                    # a connect timeout is 'busy' only when the port really accepts (Windows reports a
                    # refused localhost connect too late for the 0.75 s budget: AG97)
                    if isinstance(_pre_exc, httpx.ConnectError) or not await asyncio.to_thread(_brain_listening):
                        await websocket.send_json({"type": "error", "detail": (
                            f"Brain not running or not reachable at {BRAIN_URL}: start it (sextant-brain), "
                            "or set ATLAS_BRAIN_URL.")})
                        continue
                except Exception:  # noqa: BLE001 - a slow (timed-out) or odd health answer: the Brain is
                    pass           # listening, so go on to the stream, whose own timeouts are generous (2026-11-21)
                await websocket.send_json({"type": "thinking"})
                try:
                    # Use streaming endpoint to relay live tool-call progress to browser.
                    async with client.stream(
                        "POST",
                        f"{BRAIN_URL}/chat/stream",
                        json={"session_id": session_id, "message": text},
                        headers=_brain_headers,
                    ) as r:
                        r.raise_for_status()
                        async for line in r.aiter_lines():
                            if not line.startswith("data: "):
                                continue
                            payload = line[6:].strip()
                            if not payload:
                                continue
                            try:
                                event = json.loads(payload)
                            except Exception:
                                continue
                            if event.get("type") == "tool_call":
                                # Relay tool progress so browser can update the thinking indicator.
                                await websocket.send_json(event)
                            elif event.get("type") == "done":
                                await websocket.send_json({
                                    "type": "response",
                                    "text": event.get("text", ""),
                                    "tool_calls": event.get("tool_calls_count", 0),
                                    "tool_log": event.get("tool_log", []),
                                    "model": event.get("model", ""),
                                })
                                await _broadcast({"type": "chat_turn", "session_id": session_id})
                            elif event.get("type") == "error":
                                await websocket.send_json({"type": "error", "detail": event.get("detail", "unknown error")})
                except Exception as exc:
                    # httpx's timeout errors carry an empty message: the chat said just "Error: "
                    if isinstance(exc, httpx.ConnectError) or (
                            isinstance(exc, httpx.ConnectTimeout) and not await asyncio.to_thread(_brain_listening)):
                        _detail = f"Brain not running or not reachable at {BRAIN_URL}: start it (sextant-brain), or set ATLAS_BRAIN_URL."  # the panels name the command; the chat did not (MN56)
                    else:
                        _detail = str(exc) or f"{type(exc).__name__} (Brain at {BRAIN_URL} did not answer in time)"
                    try:
                        await websocket.send_json({"type": "error", "detail": _detail})
                    except Exception:  # the client is gone: nothing to tell it
                        break
    except (WebSocketDisconnect, RuntimeError):
        # RuntimeError: a send after the peer closed mid-stream; neither is an error worth a
        # traceback in the service log (self-hoster pass 2026-11-10).
        pass


# ---------------------------------------------------------------------------
# HTML shell
# ---------------------------------------------------------------------------

# Sextant brand favicon (2026-10-12 brand kit), embedded as data URIs so the
# single-file app needs no package data. The SVG is the kit's favicon.svg with
# its C2PA metadata blob stripped (8188 -> 509 bytes) and the missing .t/.s
# style definitions added (kit shipped them undefined); the ICO is verbatim.
# Browsers that ignore SVG favicons (Safari) fall back to the ICO link.
_FAVICON_SVG_B64 = (
    "PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHZpZXdCb3g9IjAgMCAzMiAz"
    "MiI+PHN0eWxlPi50e2ZpbGw6I0M5QTI0Qn0uc3tzdHJva2U6I0U4RTJEMn08L3N0eWxlPjxyZWN0"
    "IHdpZHRoPSIzMiIgaGVpZ2h0PSIzMiIgZmlsbD0iIzBBMEYxQyIvPjxwYXRoIGNsYXNzPSJ0IiBk"
    "PSJNMTYgMiBMMTcuMiA0LjggTDIwIDYgTDE3LjIgNy4yIEwxNiAxMCBMMTQuOCA3LjIgTDEyIDYg"
    "TDE0LjggNC44IFoiPjwvcGF0aD48cGF0aCBjbGFzcz0icyIgZD0iTTE2IDEyIEw2IDI1IE0xNiAx"
    "MiBMMjYgMjUgTTE2IDEyIEwxNiAyOS41IiBmaWxsPSJub25lIiBzdHJva2Utd2lkdGg9IjIuOCIg"
    "c3Ryb2tlLWxpbmVjYXA9InJvdW5kIiBzdHJva2UtbGluZWpvaW49InJvdW5kIj48L3BhdGg+PHBh"
    "dGggY2xhc3M9InMiIGQ9Ik02IDI1IEExMyAxMyAwIDAgMSAyNiAyNSIgZmlsbD0ibm9uZSIgc3Ry"
    "b2tlLXdpZHRoPSIzLjIiIHN0cm9rZS1saW5lY2FwPSJyb3VuZCI+PC9wYXRoPjwvc3ZnPgo="
)
_FAVICON_ICO_B64 = (
    "AAABAAMAEBAAAAEAIACNAQAANgAAACAgAAABACAA0AIAAMMBAAAwMAAAAQAgAPMDAACTBAAAiVBO"
    "Rw0KGgoAAAANSUhEUgAAABAAAAAQCAYAAAAf8/9hAAABQklEQVR4AZSQLU8DQRiE59YQGkLrQJzg"
    "I8gSTAOyAkP/AVRA6kDQoMBRJIoUAY5gGv4BFiSkhoAkOASaEOyxz7LcR8sm7SZz7+w778zurZFd"
    "pen4eLIc35XKcTIyrMdaZTArUieS6hpnWQ9egznk296Y0crSVEhWEqluQirGncaszvYXgyGRvXUw"
    "4On1Swfnb7q+/RA8dFAwAMNEZV6VuXVoEMGA1VpVN1enau81BQ8lBAPau83Uk+dp05N/AzCs1Zb1"
    "0H92gNPznkIZCmCQazOFERBED41+HoUA/pVBBjBRASFUNGbgfygE8GgIm61DbbWO3PUJWqg21L3o"
    "IbmHdcR/jK+uYASP/Re3z3+6lz2hgXzfJNK9/MII/HaooIG8YKJEaYAGFqeCgXa2TdQx35/vJ7Ik"
    "62aM00DW+WXu1taD9wcAAP//luysZAAAAAZJREFUAwAA5Hi23W32VQAAAABJRU5ErkJggolQTkcN"
    "ChoKAAAADUlIRFIAAAAgAAAAIAgGAAAAc3p69AAAAoVJREFUeAG0lrFPFUEQxue2UmLETotXaSxB"
    "C4l0Pkv5D5RCQ6eFxEo7oNPKoIl2RAv0P9DyPTsMFoql2lFoZ4wQqnfsb2Av9y673O5ybJjb2Zmd"
    "7/t2dkmekdo4da7XPz3ZG2ATk72yS1PMs70lOGqUUgmYsElTyqAQ6WPS8VDMQpbhgMvBqwAN2KQL"
    "nvhsuZTTEhltiQ1YP+vv6uUzWXViOeE2ZiQ38hBE7t46r5ZbX5SyZMpC+pI57s1dkOwOyMEwhX10"
    "B27cF8LnDy/J4OWVqgCfblSBSAdufYSR+3Xb1x//BXvz4beu+bD+9nMHN9mSBcDw9uMfwSBm/ejF"
    "LxWFn2pZAhwJIuqdcPGU+VgC6AAiUgibe48l4N3aU1m8Py/XZ6aauNHrbAGQz85My+KDeRURzdjY"
    "mCWAU0PusPCJuXXKnCwAIk7dJCFGrhlvWycJ4K4hCoGSY08o74snCXi/9qzC2Njc8vqpXYgWUAde"
    "fbUuq6/XKwH4xAjwHnig+DEWJQBy2usAN75sjb382WvTmnJdQQQ1Gmz5tAoAqE4OHlcBCT5GHmvG"
    "Yt5DuwD7fw5JjiG0re5IAZy+CUCbMXfn5JtrYs7a3kNQAOS01QFBcnvhsdxZeKLGO3A5fYT2UV6c"
    "mpO6MPJcC1j4PjO+IHdXJwcU4s+b333bx2KIaQoBC8yxjYcLr4DDnE6QA6qLhA811LaVeAVwUk5B"
    "ywFqAwnlqXU4YPr2mVJkKIERKgpsD4ZDOHB7OxBE6jhRlDI09mf5Sse40XAjI5/M3t/toZSyLImD"
    "tnK/GH5iucAJt17B7r/tFQLJILkF9sDKaetVgJ2FwKiQmwjhcRDrwhwGmBgccLn4PgAAAP//TUjL"
    "ugAAAAZJREFUAwAq4QvUs4dRpQAAAABJRU5ErkJggolQTkcNChoKAAAADUlIRFIAAAAwAAAAMAgG"
    "AAAAVwL5hwAAA6hJREFUeAHUmb9v1DAUx18yQUGUDYab+DGWslQwtiP9D6ADqBtIUDHB1naDCRUk"
    "2CoYgP8AxutYVAZ+jIjtBtgoQoWJ1B/3fPU5ycVJbN01qs/288t734/znAiRSsl17HRn/vh0pzs1"
    "3cnG2bSGU53VEpmSA5hSzghOM+kmIvMy5ktrSGQNTWhz5QwBQCvK2XWamLnS5kIMAFjQtBOjtkSI"
    "gtAb3V/WAIif6J3vizUdG601K4MGiCX+5rUzcvniSZUmwp96EkRNDQmTGA2IGHGJyZsyzRKJ9qa5"
    "tXhWP4FYTyHJZDWlniTCZe+8PQ6ZCu0HZyBQVHb6yb3z0n02K+y+CYsdWwyQYACInL1wQpeMEe72"
    "QAHo2tvMgwF8+vZHXr3/KQt3P8vLdz9ymrDdf/pdaLnFFoZgALYGQGyhiMcGpO0XYhwFAGGIpTFG"
    "PH2MFg0AsQCw+4xjtagA7DwtlnjiRgW4MjcjK7eXyBOtRQN4s/lI3m4+lpU7S8I4FkEUAHb96tyl"
    "gWbG2AaGgIPgAAhl112N2Fhz7W3nQQF0zauSKRMFBD5l603sNQCqw/vssI9PdaZDj2AACKPWD0MX"
    "j/DBt3i1vjUIAIIoD9/0+HKPr/8ov9YA1DSC7CQbz1/bUz3e3vmie/PDPdxr5k371gC8693kGy/y"
    "ADeWH7puQT5yrQCKyuD68oOcUGNw1zgPbT9yjQEQTxkYcfSmTIpKA3983PICwqyxXrc1AiChK57E"
    "iKGkaMzthj92etvOGFsRNGtVrRnAiI9VVcKydeCaQNQGaFuzZQDYebL0dVotABJQJnUS1PElNjnq"
    "3OMNQGBqdVRwDjFvmnMzizk3bKy5h9h1JAe5XHvZ3AuA2iRwURBbNO/6Dztfi9y0jTW+EcAAwr16"
    "wfkhFzkdc+HUC4ADVnQ3IqpEF92HDRDuJQZzt5HTB6ISoOxx6nIo+OK6QqrmgBCryK8st+1bCWA7"
    "mzEJKQczb9sTi5huHOyuzZ1XArBD5jFTsyTyCewmqpoT05wNfMlFbsajWiUANxOI4NQsibDFanYu"
    "nxxeAD6BxuWTZiJbckQvtKfqf2jWj6h+STLZSv/96oV/AmpHOISq03/mJaAnAX/2fvfWD85AJmsS"
    "+OIwIhyQ7Y/D/5wMkqqvWQNAQj0FCdwPwtsKiChvLiUezaTSAAz+7vYWRC0wnuTGRhvx6BwAMNEL"
    "kwyhtOmNRmy/DQFgA2Jvt5fwNKDFNs6Ghv+JLKAJba6WfQAAAP//6oMFPwAAAAZJREFUAwCv93v9"
    "HCYp9AAAAABJRU5ErkJggg=="
)


def _html_full(key: str) -> str:
    k = key.replace('"', '').replace("'", '')
    # bandit B608 note: the heuristic misfires on this f-string (it is the
    # HTML shell, not SQL), and a nosec marker cannot ride a multiline
    # string's first line without being emitted into the page -- so B608
    # stays a known-advisory finding here (triage, quality loop run 7).
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>Sextant Helm</title>
<link rel="icon" type="image/svg+xml" href="data:image/svg+xml;base64,{_FAVICON_SVG_B64}">
<link rel="icon" type="image/x-icon" href="data:image/x-icon;base64,{_FAVICON_ICO_B64}">
<script src="https://unpkg.com/htmx.org@1.9.12/dist/htmx.min.js" integrity="sha384-ujb1lZYygJmzgSwoxRggbCHcjc0rB2XoQrxeTUQyRjrOnlCoYta87iKBWq3EsdM2" crossorigin="anonymous"></script>
<script src="/static/tailwind-3.4.17.js" integrity="{_TAILWIND_SRI}"></script>
<script>
// The bootstrap key must not stay in the address bar, history or any referrer: the cookie
// carries the session from here on (MN4, pipeline sweep 2026-11-11).
(function(){{try{{if(location.search.indexOf("key=")!==-1){{history.replaceState(null,"",location.pathname);}}}}catch(e){{}}}})();
// Every request from this page carries X-Sextant-Request (CSRF guard; see _check_auth).
(function(){{const f=window.fetch;window.fetch=function(u,o){{o=o||{{}};const h=new Headers(o.headers||{{}});if(!h.has("X-Sextant-Request"))h.set("X-Sextant-Request","1");o.headers=h;return f(u,o);}};}})();
</script>
<style>
:root{{
  --bg:#0f172a;--bg-card:#1e293b;--border:#334155;
  --text:#e2e8f0;--text-m:#94a3b8;--text-d:#475569;--text-s:#cbd5e1;
  --input-bg:#0f172a;
}}
body.light{{
  --bg:#f1f5f9;--bg-card:#ffffff;--border:#e2e8f0;
  --text:#0f172a;--text-m:#64748b;--text-d:#94a3b8;--text-s:#334155;
  --input-bg:#f8fafc;
}}
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:var(--bg);color:var(--text);font-family:ui-monospace,monospace;height:100vh;display:flex;flex-direction:column;overflow:hidden}}
.card{{background:var(--bg-card);border:1px solid var(--border);border-radius:.5rem;padding:.75rem}}
.tab{{cursor:pointer;padding:.35rem .7rem;border-radius:.375rem .375rem 0 0;font-size:.7rem;color:var(--text-m);border:1px solid transparent;border-bottom:none;white-space:nowrap}}
.tab.active{{background:var(--bg-card);color:var(--text);border-color:var(--border)}}
.tab:hover:not(.active){{color:var(--text-s)}}
.tab-content{{display:none!important}}.tab-content.active{{display:flex!important}}
.tab-content.active.block-tab{{display:block!important}}
.task-pending{{color:#60a5fa}}.task-running{{color:#fbbf24}}.task-done{{color:#4ade80}}
.task-failed{{color:#f87171}}.task-blocked{{color:#fb923c}}
.msg-user{{color:#4ade80}}.msg-brain{{color:#93c5fd}}.msg-error{{color:#f87171}}
.thinking{{color:var(--text-m);font-style:italic}}
.detail-panel{{background:var(--bg);border:1px solid var(--border);border-radius:.5rem;padding:.75rem;overflow-y:auto}}
.row-hover:hover{{background:var(--bg-card);cursor:pointer}}
input,textarea,select{{background:var(--input-bg);color:var(--text);border:1px solid var(--border);border-radius:.375rem;padding:.3rem .5rem;font-size:.7rem;font-family:inherit;width:100%}}
input:focus,textarea:focus,select:focus{{outline:none;border-color:#22d3ee}}
button{{font-family:inherit;cursor:pointer}}
.btn{{padding:.3rem .6rem;border-radius:.375rem;font-size:.7rem;border:none;white-space:nowrap}}
.btn-cyan{{background:#0e7490;color:#fff}}.btn-cyan:hover{{background:#0891b2}}
.btn-slate{{background:var(--border);color:var(--text-s)}}.btn-slate:hover{{background:var(--text-d)}}
.btn-green{{background:#166534;color:#fff}}.btn-green:hover{{background:#15803d}}
.btn-yellow{{background:#854d0e;color:#fff}}.btn-yellow:hover{{background:#a16207}}
.btn-red{{background:#7f1d1d;color:#fff}}.btn-red:hover{{background:#991b1b}}
.btn-purple{{background:#581c87;color:#fff}}.btn-purple:hover{{background:#6b21a8}}
.btn-amber{{background:#92400e;color:#fff}}.btn-amber:hover{{background:#b45309}}
#chat-messages{{flex:1;overflow-y:auto;min-height:0}}
.capture-bar{{background:var(--bg);border-bottom:1px solid var(--border);padding:.25rem .75rem;display:flex;gap:.5rem;align-items:center}}
.capture-bar input{{flex:1;background:transparent;border:none;color:var(--text-m);font-size:.7rem;outline:none}}
.capture-bar input::placeholder{{color:var(--text-d)}}
.capture-bar input:focus{{color:var(--text)}}
/* AG80: the capture input and the palette input had border:none + outline:none, so focus was invisible */
.capture-bar input:focus-visible{{outline:2px solid #22d3ee;outline-offset:1px;border-radius:.25rem}}
.palette-input:focus-visible{{outline:2px solid #22d3ee;outline-offset:-2px}}
#capture-scope:focus-visible{{outline:2px solid #22d3ee;outline-offset:1px}}
.tab:focus-visible{{outline:2px solid #22d3ee;outline-offset:1px}}
header.page-head{{display:contents}}
.stream-output{{background:var(--bg);border:1px solid var(--border);border-radius:.375rem;padding:.5rem;font-size:.65rem;color:var(--text-m);white-space:pre-wrap;word-break:break-word;max-height:12rem;overflow-y:auto;margin-top:.5rem}}
.stream-done{{color:#4ade80}}.stream-failed{{color:#f87171}}.stream-blocked{{color:#fb923c}}
.toast{{position:fixed;bottom:1.5rem;right:1.5rem;padding:.5rem 1rem;border-radius:.5rem;font-size:.75rem;z-index:300;transition:opacity .3s;pointer-events:none;max-width:20rem}}
.toast-done{{background:#166534;color:#fff}}.toast-failed{{background:#7f1d1d;color:#fff}}.toast-info{{background:var(--bg-card);color:var(--text);border:1px solid var(--border)}}
.palette-overlay{{position:fixed;inset:0;background:rgba(0,0,0,.7);z-index:200;display:flex;align-items:flex-start;justify-content:center;padding-top:8vh}}
.palette-box{{background:var(--bg-card);border:1px solid var(--text-d);border-radius:.75rem;width:min(520px,90vw);overflow:hidden;box-shadow:0 25px 50px rgba(0,0,0,.6)}}
.palette-input{{width:100%;background:transparent;border:none;color:var(--text);font-size:.95rem;font-family:inherit;padding:.85rem 1rem;outline:none}}
.palette-item{{padding:.4rem 1rem;cursor:pointer;font-size:.75rem;color:var(--text-m);display:flex;align-items:center;gap:.75rem}}
.palette-item:hover,.palette-item.active{{background:var(--border);color:var(--text)}}
.palette-item .k{{color:var(--text-d);font-size:.65rem;margin-left:auto}}
/* the Memory search box shares a 288 px column with four buttons: 26 px wide on every desktop (MN50); it takes its own row */
div:has(> #mem-q){{flex-wrap:wrap}}
#mem-q{{flex:1 1 100%!important;min-width:0}}
@media(max-width:768px){{
  body{{height:100dvh}}
  #tab-dash{{flex-direction:column!important}}
  #dash-row{{flex-direction:column!important;overflow-y:auto}}  /* the main column got 0 px: Recent Tasks unreachable at 375-768 (AG71) */
  #tab-tickets{{flex-direction:column!important}}
  #tab-tasks{{flex-direction:column!important}}
  #tab-memory{{flex-direction:column!important}}
  .w-64,.w-72,.w-80,.w-96{{width:100%!important}}
  #tabbar{{flex-wrap:wrap;overflow-x:visible}}
  .tk-proj{{display:none}}
  .tk-id{{width:3.6rem!important}}
  .tk-status{{width:2.6rem!important;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}
  .tab{{padding:.3rem .5rem;font-size:.65rem}}
  .detail-panel{{height:12rem!important}}
  #dash-detail{{height:10rem!important}}
  /* phone (AG59): header 277 px (one service per row), the memory search box 89 px, the Sessions
     panes cut off at the right edge, the Map's drawing squeezed to nothing */
  div:has(> #status-bar){{min-width:0;flex:1 1 0}}
  #status-bar{{display:grid;grid-template-columns:1fr 1fr;gap:.1rem .5rem;flex:1;min-width:0}}
  #status-bar .sep{{display:none}}
  div:has(> #mem-q){{flex-wrap:wrap}}
  #mem-q{{flex:1 1 100%!important}}
  #tab-sessions{{flex-direction:column!important;overflow-y:auto}}
  #tab-sessions>div{{width:100%!important;flex-shrink:0}}
  #tab-sessions #session-list,#tab-sessions #session-content,#tab-sessions #brain-session-list{{max-height:14rem}}
  #tab-map>.flex-1{{flex-direction:column;overflow-y:auto}}
  #tab-map>.flex-1>.flex-1{{flex:0 0 18rem}}
  #tab-map .w-80{{flex:0 0 9rem}}
}}
@media(max-width:411px){{
  #map-status{{display:none}}  /* the refreshed stamp spilled past the viewport at 375 (MN56) */
}}
/* phone (AG45): the ticket, task and inbox detail panes were 320 px fixed beside a 0 px list;
   the tab bar scrolled sideways with no sign of it */
@media (max-width: 768px) {{  /* through tablet portrait: at 641-768 px the 768 block made the detail pane full width beside a 0 px list (MN50) */
  div:has(> .detail-panel), div:has(> #inbox-detail) {{ flex-wrap: wrap; }}
  .detail-panel, #inbox-detail {{ width: 100% !important; flex: 1 1 100% !important; }}
  nav, .tabs, [role=tablist] {{ flex-wrap: wrap; }}
}}
</style>
</head>
<body hx-headers='{{"X-Sextant-Request": "1"}}'>
<div id="cdn-missing" style="display:none;margin:8px;padding:10px 14px;border:1px solid #b45309;background:#431407;color:#fde68a;font:14px system-ui,sans-serif;border-radius:6px">
This dashboard loads one UI library (htmx) from unpkg.com in your browser; Tailwind is served by the dashboard itself.
<span id="cdn-missing-which"></span> Without htmx every panel stays at "loading..."; without Tailwind nothing is styled. You are offline, or that host is blocked.
The server itself is fine; its JSON endpoints (for example <code>/api/stats/summary</code>) answer without them.
</div>
<script>
// Offline, the page was a permanent "loading..." shell with no explanation (AG18, sweep 11).
window.addEventListener("DOMContentLoaded",function(){{var miss=[];if(typeof htmx==="undefined")miss.push("htmx (unpkg.com)");if(typeof tailwind==="undefined")miss.push("Tailwind (/static/tailwind-3.4.17.js on this dashboard)");if(miss.length){{var b=document.getElementById("cdn-missing");if(b){{var w=document.getElementById("cdn-missing-which");if(w)w.textContent="Not loaded: "+miss.join(", ")+".";b.style.display="block";}}}}}});
</script>

<!-- Top bar, capture bar and tab bar: one banner landmark (display:contents keeps the flex layout) -->
<header class="page-head">
<div class="flex items-center justify-between px-3 py-1.5 border-b border-slate-700 shrink-0">
  <div class="flex items-center gap-3">
    <h1 class="text-sm font-bold text-cyan-400 shrink-0 flex items-center gap-1.5"><svg width="16" height="16" viewBox="0 0 32 32" aria-hidden="true"><path d="M16 2 L17.2 4.8 L20 6 L17.2 7.2 L16 10 L14.8 7.2 L12 6 L14.8 4.8 Z" fill="#C9A24B"/><path d="M16 12 L6 25 M16 12 L26 25 M16 12 L16 29.5" stroke="currentColor" stroke-width="2.8" fill="none" stroke-linecap="round" stroke-linejoin="round"/><path d="M6 25 A13 13 0 0 1 26 25" stroke="currentColor" stroke-width="3.2" fill="none" stroke-linecap="round"/></svg>Sextant</h1>
    <div id="status-bar" class="flex gap-2 text-xs flex-wrap"
      hx-get="/api/status" hx-trigger="load, every 30s" hx-swap="innerHTML">
      <span class="text-slate-500">checking...</span>
    </div>
  </div>
  <div class="flex items-center gap-2 shrink-0">
    <button onclick="openPalette()" class="text-slate-600 hover:text-slate-300 text-xs hidden sm:block" title="Ctrl+K">⌘K</button>
    <button id="theme-btn" onclick="toggleTheme()" class="text-slate-500 hover:text-slate-300 text-xs" title="Toggle light/dark theme">dark</button>
    <span id="conn" class="text-xs text-slate-500">--</span>
    <button onclick="stopServer()" class="btn btn-red text-xs">stop</button>
  </div>
</div>
<!-- Quick capture bar -->
<div class="capture-bar">
  <label for="capture-input" class="text-slate-600 text-xs shrink-0">capture:</label>
  <input id="capture-input" type="text" placeholder="Save a fact to memory... (Enter)" autocomplete="off"
    onkeydown="if(event.key==='Enter')quickCapture()">
  <select id="capture-scope" aria-label="capture scope" style="background:var(--bg-card,#0f172a);border:none;color:var(--text-m,#475569);font-size:.65rem;width:auto">
    <option value="preference:quick-capture">pref</option>
    <option value="identity">identity</option>
    <option value="reference:misc">ref</option>
  </select>
  <span id="capture-status" class="text-xs text-slate-600 shrink-0"></span>
</div>

<!-- Tabs -->
<div id="tabbar" role="tablist" aria-label="Sections" class="flex gap-1 px-3 pt-1.5 border-b border-slate-700 shrink-0 overflow-x-auto">
  <div class="tab active" data-tab="dash" onclick="switchTab('dash')" role="tab" id="tabbtn-dash" aria-controls="tab-dash" aria-selected="true" tabindex="0">Dashboard</div>
  <div class="tab" data-tab="map" onclick="switchTab('map')" role="tab" id="tabbtn-map" aria-controls="tab-map" aria-selected="false" tabindex="-1">Map</div>
  <div class="tab" data-tab="tickets" onclick="switchTab('tickets')" role="tab" id="tabbtn-tickets" aria-controls="tab-tickets" aria-selected="false" tabindex="-1">Tickets</div>
  <div class="tab" data-tab="operator" onclick="switchTab('operator')" role="tab" id="tabbtn-operator" aria-controls="tab-operator" aria-selected="false" tabindex="-1">Operator</div>
  <div class="tab" data-tab="inbox" onclick="switchTab('inbox')" role="tab" id="tabbtn-inbox" aria-controls="tab-inbox" aria-selected="false" tabindex="-1">Inbox</div>
  <div class="tab" data-tab="tasks" onclick="switchTab('tasks')" role="tab" id="tabbtn-tasks" aria-controls="tab-tasks" aria-selected="false" tabindex="-1">Tasks<span id="tasks-badge" class="ml-1 text-yellow-400 text-xs"></span></div>
  <div class="tab" data-tab="services" onclick="switchTab('services')" role="tab" id="tabbtn-services" aria-controls="tab-services" aria-selected="false" tabindex="-1">Services</div>
  <div class="tab" data-tab="chat" onclick="switchTab('chat')" role="tab" id="tabbtn-chat" aria-controls="tab-chat" aria-selected="false" tabindex="-1">Brain Chat</div>
  <div class="tab" data-tab="agents" onclick="switchTab('agents')" role="tab" id="tabbtn-agents" aria-controls="tab-agents" aria-selected="false" tabindex="-1">Agents</div>
  <div class="tab" data-tab="memory" onclick="switchTab('memory')" role="tab" id="tabbtn-memory" aria-controls="tab-memory" aria-selected="false" tabindex="-1">Memory<span id="facts-badge" class="ml-1 text-amber-400 text-xs"></span></div>
  <div class="tab" data-tab="sessions" onclick="switchTab('sessions')" role="tab" id="tabbtn-sessions" aria-controls="tab-sessions" aria-selected="false" tabindex="-1">Sessions</div>
  <div class="tab" data-tab="watchtower" onclick="switchTab('watchtower')" role="tab" id="tabbtn-watchtower" aria-controls="tab-watchtower" aria-selected="false" tabindex="-1">Watchtower</div>
  {_tools_tab_button()}
</div>
</header>

<!-- Main content area -->
<main id="main" class="flex-1 overflow-hidden p-3">

<!-- DASHBOARD TAB -->
<div id="tab-dash" role="tabpanel" aria-labelledby="tabbtn-dash" class="tab-content active h-full gap-3" style="flex-direction:column">
  <!-- Stats bar -->
  <div class="card shrink-0 py-1.5"
    hx-get="/api/stats/summary" hx-trigger="load, every 15s" hx-swap="innerHTML">
    <span class="text-slate-600 text-xs">loading stats...</span>
  </div>
  <!-- Main row -->
  <div id="dash-row" class="flex gap-3 flex-1 min-h-0">
  <!-- Left sidebar -->
  <div class="flex flex-col gap-3 w-64 shrink-0">
    <div class="card">
      <h2 class="text-xs font-bold text-slate-400 mb-2">Submit Task</h2>
      <textarea id="ti" class="w-full resize-none text-xs" rows="3"
        placeholder="Instruction for Brain..."
        onkeydown="if(event.key==='Enter'&&event.ctrlKey)submitTask()"></textarea>
      <div class="flex gap-1.5 mt-2">
        <select id="task-agent" aria-label="Agent for this task" class="flex-1 text-xs">
          <option value="">auto-route</option>
          <option value="fast">fast (qwen3:1.7b)</option>
          <option value="brain">brain (qwen3:8b)</option>
          <option value="coder">coder</option>
          <option value="mistral">mistral</option>
          <option value="opencode">opencode (free)</option>
          <option value="research">research</option>
          <option value="codex">codex</option>
        </select>
        <button class="btn btn-cyan" onclick="submitTask()">Run</button>
      </div>
      <div id="tsr" class="mt-1 text-xs text-slate-500 truncate"></div>
    </div>
    <div class="card">
      <h2 class="text-xs font-bold text-yellow-400 mb-1">Ask Claude</h2>
      <p class="text-slate-500 text-xs mb-2">Bypasses routing. Priority 1. Full context window.</p>
      <textarea id="claude-input" class="w-full resize-none text-xs" rows="3"
        placeholder="Ask Claude Code directly..."
        onkeydown="if(event.key==='Enter'&&event.ctrlKey)submitClaudeTask()"></textarea>
      <div class="flex gap-1.5 mt-2">
        <button class="btn text-xs font-bold" style="background:#854d0e;color:#fef3c7;border-color:#92400e;" onclick="submitClaudeTask()">Ask Claude</button>
      </div>
      <div id="claude-tsr" class="mt-1 text-xs text-slate-500 truncate"></div>
    </div>
    <div class="card shrink-0">
      <h2 class="text-xs font-bold text-slate-400 mb-2">Quick Run</h2>
      <div class="flex flex-col gap-1">
        <button class="btn btn-slate text-xs text-left" onclick="quickRun('suggest-facts')">suggest-facts</button>
        <button class="btn btn-slate text-xs text-left" onclick="quickRun('temporal-build')">temporal-build</button>
        <button class="btn btn-slate text-xs text-left" onclick="quickRun('curation')">curation</button>
      </div>
    </div>
    <div class="card">
      <h2 class="text-xs font-bold text-slate-400 mb-1">Agent Quality</h2>
      <div hx-get="/api/improvement/stats" hx-trigger="load, every 60s" hx-swap="innerHTML">
        <div class="text-slate-600 text-xs">loading...</div>
      </div>
    </div>
    <div class="card">
      <h2 class="text-xs font-bold text-slate-400 mb-1">Research Quality</h2>
      <div hx-get="/api/research-quality-panel" hx-trigger="load, every 60s" hx-swap="innerHTML">
        <div class="text-slate-600 text-xs">loading...</div>
      </div>
    </div>
    <div class="card">
      <h2 class="text-xs font-bold text-slate-400 mb-1">Maintenance <span class="text-slate-600 font-normal">(nightly lanes)</span></h2>
      <div hx-get="/api/maintenance" hx-trigger="load, every 60s" hx-swap="innerHTML">
        <div class="text-slate-600 text-xs">loading...</div>
      </div>
    </div>
    <div class="card">
      <h2 class="text-xs font-bold text-slate-400 mb-1">Security audit <span class="text-slate-600 font-normal">(weekly)</span></h2>
      <div hx-get="/api/security/audit" hx-trigger="load, every 300s" hx-swap="innerHTML">
        <div class="text-slate-600 text-xs">loading...</div>
      </div>
    </div>
    <div class="card">
      <h2 class="text-xs font-bold text-slate-400 mb-1">Auth failures <span class="text-slate-600 font-normal">(per source; locked_out = refused while locked)</span></h2>
      <div hx-get="/api/security/auth-failures.html" hx-trigger="load, every 60s" hx-swap="innerHTML">
        <div class="text-slate-600 text-xs">loading...</div>
      </div>
    </div>
    <div class="card">
      <h2 class="text-xs font-bold text-slate-400 mb-1">Build loops <span class="text-slate-600 font-normal">(governor)</span></h2>
      <div hx-get="/api/loop/status" hx-trigger="load, every 30s" hx-swap="innerHTML">
        <div class="text-slate-600 text-xs">loading...</div>
      </div>
    </div>
    <div class="card shrink-0">
      <h2 class="text-xs font-bold text-slate-400 mb-1">Config</h2>
      <div id="config-panel"
        hx-get="/api/config" hx-trigger="load" hx-swap="innerHTML">
        <div class="text-slate-600 text-xs">loading...</div>
      </div>
      <button onclick="resetCredits()" class="btn btn-slate text-xs mt-2 w-full">Reset credit exhaustion</button>
    </div>
    <div class="card flex-1 min-h-0 overflow-hidden flex flex-col">
      <h2 class="text-xs font-bold text-slate-400 mb-1">Live Feed</h2>
      <div id="feed" class="text-xs text-slate-500 space-y-0.5 overflow-y-auto flex-1 min-h-0"></div>
    </div>
  </div>
  <!-- Main area -->
  <div class="flex-1 flex flex-col gap-3 min-w-0">
    <div class="card flex-1 min-h-0 overflow-hidden flex flex-col">
      <div class="flex items-center justify-between mb-2 shrink-0">
        <h2 class="text-xs font-bold text-slate-400">Recent Tasks <span class="text-slate-600 font-normal">(click to inspect)</span></h2>
        <button class="btn btn-slate text-xs"
          hx-get="/api/tasks?limit=15" hx-target="#dash-tasks" hx-swap="innerHTML">refresh</button>
      </div>
      <div id="dash-tasks" class="overflow-y-auto flex-1 min-h-0"
        hx-get="/api/tasks?limit=15" hx-trigger="load, every 8s" hx-swap="innerHTML">
        <div class="text-slate-500 text-xs">loading...</div>
      </div>
    </div>
    <div id="dash-detail" class="detail-panel h-44 shrink-0">
      <div class="text-slate-600 text-xs">Click a task to inspect</div>
    </div>
  </div>
  </div><!-- end main row -->
</div>

<!-- TICKETS TAB -->
<div id="tab-services" role="tabpanel" aria-labelledby="tabbtn-services" class="tab-content h-full flex-col gap-2">
  <div class="flex items-center gap-2 shrink-0 flex-wrap">
    <button class="btn btn-cyan" onclick="reloadServices()">Refresh</button>
    <span class="text-xs text-slate-500">Restarting Web UI briefly disconnects this page.</span>
    <span id="svc-result" class="text-xs text-slate-400"></span>
  </div>
  <div class="card flex-1 overflow-y-auto">
    <div id="svc-list"
      hx-get="/api/services"
      hx-trigger="tab-show, every 30s"
      hx-swap="innerHTML">
      <div class="text-slate-500 text-xs">loading...</div>
    </div>
  </div>
</div>

<div id="tab-inbox" role="tabpanel" aria-labelledby="tabbtn-inbox" class="tab-content h-full flex-col gap-2">
  <div class="flex items-center gap-2 shrink-0 flex-wrap">
    <select id="inbox-filter" aria-label="Inbox status filter" onchange="reloadInbox()" class="w-32">
      <option value="">all</option>
      <option value="new">new</option>
      <option value="summarized">summarized</option>
      <option value="promoted">promoted</option>
      <option value="archived">archived</option>
    </select>
    <span class="text-xs text-slate-500">Drop links from your phone via Telegram /inbox</span>
    <span id="inbox-result" class="text-xs text-slate-400"></span>
  </div>
  <div class="flex gap-3 flex-1 min-h-0">
    <div class="card flex-1 overflow-y-auto">
      <div id="inbox-list"
        hx-get="/api/inbox"
        hx-trigger="tab-show, every 30s"
        hx-swap="innerHTML">
        <div class="text-slate-500 text-xs">loading...</div>
      </div>
    </div>
    <div id="inbox-detail" class="card w-80 shrink-0 overflow-y-auto">
      <div class="text-slate-600 text-xs">Click an item to inspect</div>
    </div>
  </div>
</div>

<div id="tab-operator" role="tabpanel" aria-labelledby="tabbtn-operator" class="tab-content h-full flex-col gap-2">
  <div class="flex items-center gap-2 shrink-0">
    <h2 class="text-xs font-bold text-slate-400">Operator queue -- steps only a human can take, instructions verified where marked READY</h2>
    <button class="btn btn-cyan text-xs" onclick="reloadOperator()">refresh</button>
    <span id="op-result" class="text-xs text-slate-400"></span>
  </div>
  <div id="op-queue" class="card flex-1 overflow-y-auto"
    hx-get="/api/operator/queue-html"
    hx-trigger="tab-show, every 60s"
    hx-swap="innerHTML">
    <div class="text-slate-600 text-xs">loading queue...</div>
  </div>
</div>

<div id="tab-tickets" role="tabpanel" aria-labelledby="tabbtn-tickets" class="tab-content h-full flex-col gap-2">
  <div class="flex items-center gap-2 shrink-0 flex-wrap">
    <select id="tkt-filter" aria-label="Ticket status filter" onchange="reloadTickets()" class="w-32">
      <option value="open">open</option>
      <option value="all">all</option>
      <option value="todo">todo</option>
      <option value="in-progress">in-progress</option>
      <option value="blocked">blocked</option>
      <option value="done">done</option>
    </select>
    <select id="tkt-project" aria-label="Ticket project" onchange="reloadTickets()" class="w-36">
      <option value="">all projects</option>
    </select>
    <button class="btn btn-cyan" onclick="toggleTicketForm()">+ New</button>
    <span id="dispatch-badge"
      hx-get="/api/tickets/dispatch-stats-html"
      hx-trigger="tab-show, every 60s"
      hx-swap="innerHTML">
    </span>
    <span id="tk-result" class="text-xs text-slate-400"></span>
  </div>
  <div id="ticket-form" style="display:none" class="card shrink-0">
    <div class="flex gap-2 flex-wrap items-end">
      <div class="flex-1 min-w-40">
        <div class="text-xs text-slate-500 mb-1">Title</div>
        <input id="tk-title" type="text" placeholder="Ticket title">
      </div>
      <div class="w-28">
        <div class="text-xs text-slate-500 mb-1">Project</div>
        <input id="tk-project" type="text" placeholder="project-name">
      </div>
      <div class="w-24">
        <div class="text-xs text-slate-500 mb-1">Priority</div>
        <select id="tk-priority" aria-label="Priority">
          <option value="normal">normal</option>
          <option value="high">high</option>
          <option value="critical">critical</option>
          <option value="low">low</option>
        </select>
      </div>
      <div class="w-28">
        <div class="text-xs text-slate-500 mb-1">Assignee</div>
        <select id="tk-assignee" aria-label="Assignee">
          <option value="human">human</option>
          <option value="claude-code">claude-code</option>
        </select>
      </div>
    </div>
    <div class="mt-2">
      <div class="text-xs text-slate-500 mb-1">Body (optional)</div>
      <div class="flex gap-2 flex-wrap"><input id="tk-requires" class="flex-1 text-xs" placeholder="requires (e.g. WEB-001)"><input id="tk-verified" class="flex-1 text-xs" placeholder="verified (YYYY-MM-DD)"><select id="tk-effort" aria-label="Effort" class="text-xs"><option value="">effort</option><option>S</option><option>M</option><option>L</option><option>XL</option></select></div>
      <textarea id="tk-body" rows="2" class="w-full resize-none" placeholder="Description, context, steps..."></textarea>
    </div>
    <div class="flex gap-2 mt-2">
      <button class="btn btn-cyan" onclick="createTicket()">Create</button>
      <button class="btn btn-slate" onclick="toggleTicketForm()">Cancel</button>
    </div>
  </div>
  <div class="flex gap-3 flex-1 min-h-0">
    <div class="card flex-1 overflow-y-auto">
      <div id="tboard"
        hx-get="/api/tickets/board?status_filter=open"
        hx-trigger="tab-show, every 30s"
        hx-swap="innerHTML">
        <div class="text-slate-500 text-xs">loading...</div>
      </div>
    </div>
    <div id="ticket-detail" class="detail-panel w-80 shrink-0">
      <div class="text-slate-600 text-xs">Click a ticket to inspect</div>
    </div>
  </div>
</div>

<!-- TASKS TAB -->
<div id="tab-tasks" role="tabpanel" aria-labelledby="tabbtn-tasks" class="tab-content h-full flex-col gap-2">
  <div class="flex items-center gap-2 shrink-0 flex-wrap">
    <select id="task-filter" aria-label="Task status filter" onchange="reloadTasks()" class="w-32">
      <option value="">all status</option>
      <option value="pending">pending</option>
      <option value="running">running</option>
      <option value="done">done</option>
      <option value="failed">failed</option>
      <option value="blocked">blocked</option>
    </select>
    <select id="time-filter" aria-label="Time window" onchange="reloadTasks()" class="w-24">
      <option value="">all time</option>
      <option value="1">1h</option>
      <option value="6">6h</option>
      <option value="24">today</option>
      <option value="168">7d</option>
    </select>
    <select id="agent-filter" aria-label="Agent filter" onchange="reloadTasks()" class="w-36">
      <option value="">all agents</option>
      <option value="brain">brain</option>
      <option value="fast">fast</option>
      <option value="coder">coder</option>
      <option value="claude-code">claude-code</option>
      <option value="research">research</option>
      <option value="opencode">opencode</option>
      <option value="codex">codex</option>
    </select>
    <button class="btn btn-slate text-xs"
      hx-get="/api/tasks?limit=50" hx-target="#task-list" hx-swap="innerHTML">refresh</button>
    <button class="btn btn-yellow text-xs" onclick="retryAllFailed()" title="Resubmit all failed tasks">Retry Failed</button>
    <button class="btn btn-slate text-xs" onclick="exportTasks()" title="Download task history as CSV">Export CSV</button>
    <span id="exhausted-badge"
      hx-get="/api/tasks/exhausted-badge"
      hx-trigger="tab-show, every 60s"
      hx-swap="innerHTML">
    </span>
    <span id="retry-status" class="text-xs text-slate-500"></span>
  </div>
  <div class="flex gap-3 flex-1 min-h-0">
    <div class="card flex-1 overflow-y-auto">
      <div id="task-list"
        hx-get="/api/tasks?limit=50" hx-trigger="tab-show, every 8s" hx-swap="innerHTML">
        <div class="text-slate-500 text-xs">loading...</div>
      </div>
    </div>
    <div id="task-detail" class="detail-panel w-80 shrink-0">
      <div class="text-slate-600 text-xs">Click a task to inspect</div>
    </div>
  </div>
</div>

<!-- CHAT TAB -->
<div id="tab-chat" role="tabpanel" aria-labelledby="tabbtn-chat" class="tab-content h-full flex-col gap-2">
  <div class="card shrink-0 text-xs text-slate-500">
    Brain Chat -- local Qwen3, no token limits.
    Session: <span id="chat-sid" class="text-slate-400"></span>
    <button onclick="clearChat()" class="btn btn-slate text-xs ml-2">Clear</button>
    <button onclick="loadChatHistory()" class="btn btn-slate text-xs ml-2">History</button>
  </div>
  <div class="card flex-1 min-h-0 overflow-hidden flex flex-col">
    <div id="chat-messages" class="flex-1 overflow-y-auto mb-2 space-y-1"></div>
    <div class="flex gap-2 border-t border-slate-700 pt-2 shrink-0 items-end">
      <textarea id="ci" rows="2" class="flex-1 resize-none"
        placeholder="Message Brain... (Enter to send, Shift+Enter for newline)"
        onkeydown="if(event.key==='Enter'&&!event.shiftKey){{event.preventDefault();sendChat()}}"></textarea>
      <button class="btn btn-cyan shrink-0" onclick="sendChat()">Send</button>
    </div>
  </div>
</div>

<!-- AGENTS TAB -->
<div id="tab-agents" role="tabpanel" aria-labelledby="tabbtn-agents" class="tab-content block-tab h-full overflow-y-auto">
  <div class="card"
    hx-get="/api/agents" hx-trigger="tab-show, every 30s" hx-swap="innerHTML">
    <div class="text-slate-500 text-xs">loading agents...</div>
  </div>
  <div class="card mt-3">
    <h2 class="text-xs font-bold text-slate-400 mb-2">Routing</h2>
    <div hx-get="/api/routing/stats" hx-trigger="tab-show, every 60s" hx-swap="innerHTML">
      <div class="text-slate-600 text-xs">loading...</div>
    </div>
  </div>
</div>

<!-- MEMORY TAB -->
<div id="tab-memory" role="tabpanel" aria-labelledby="tabbtn-memory" class="tab-content h-full gap-3">
  <!-- Left panel: search + gaps -->
  <div class="flex flex-col gap-2 w-72 shrink-0 min-h-0">
    <div class="flex items-center gap-2 shrink-0">
      <input id="mem-q" type="text" class="flex-1" placeholder="Search memory... (Enter)"
        onkeydown="if(event.key==='Enter')searchMemory()">
      <button class="btn btn-cyan" onclick="searchMemory()">Search</button>
      <button class="btn btn-slate" onclick="loadMemoryIndex()">Index</button>
      <button class="btn btn-slate text-xs" onclick="rebuildMemoryIndex()" title="Rebuild FTS5 index">Rebuild</button>
      <button class="btn btn-slate text-xs" onclick="browseMemory()">Browse</button>
    </div>
    <div id="mem-results" class="card flex-1 min-h-0 overflow-y-auto">
      <div class="text-slate-600 text-xs">Enter a query or click Index to browse.</div>
    </div>
  </div>
  <!-- Right panel: pending facts + gaps + temporal -->
  <div class="flex flex-col gap-2 flex-1 min-h-0 min-w-0">
    <div class="card shrink-0 overflow-y-auto" style="max-height:32%">
      <div class="flex items-center justify-between mb-2">
        <h2 class="text-xs font-bold text-slate-400">Pending Facts <span class="text-slate-600 font-normal">(from Brain tasks)</span></h2>
        <button class="btn btn-slate text-xs"
          hx-get="/api/pending-facts" hx-target="#pending-facts-list" hx-swap="innerHTML">Refresh</button>
      </div>
      <div id="pending-facts-list"
        hx-get="/api/pending-facts" hx-trigger="tab-show" hx-swap="innerHTML">
        <div class="text-slate-600 text-xs">loading...</div>
      </div>
    </div>
    <div class="card shrink-0 overflow-y-auto" style="max-height:32%">
      <div class="flex items-center justify-between mb-2">
        <h2 class="text-xs font-bold text-slate-400">Proposals <span class="text-slate-600 font-normal">(weekly supervisor, reflexion; your click writes)</span></h2>
        <button class="btn btn-slate text-xs"
          hx-get="/api/proposals" hx-target="#proposals-list" hx-swap="innerHTML">Refresh</button>
      </div>
      <div id="proposals-list"
        hx-get="/api/proposals" hx-trigger="tab-show" hx-swap="innerHTML">
        <div class="text-slate-600 text-xs">loading...</div>
      </div>
    </div>
    <div class="card shrink-0 overflow-y-auto" style="max-height:25%">
      <div class="flex items-center justify-between mb-2">
        <h2 class="text-xs font-bold text-slate-400">Memory Gaps</h2>
        <button class="btn btn-slate text-xs"
          hx-get="/api/memory/gaps" hx-target="#gaps-list" hx-swap="innerHTML">Refresh</button>
      </div>
      <div id="gaps-list"
        hx-get="/api/memory/gaps" hx-trigger="tab-show" hx-swap="innerHTML">
        <div class="text-slate-600 text-xs">loading...</div>
      </div>
    </div>
    <div class="card shrink-0 overflow-y-auto" style="max-height:20%">
      <div class="flex items-center justify-between mb-2">
        <h2 class="text-xs font-bold text-slate-400">Convergence Flags</h2>
        <button class="btn btn-slate text-xs"
          hx-get="/api/memory/convergence" hx-target="#convergence-list" hx-swap="innerHTML">Refresh</button>
      </div>
      <div id="convergence-list"
        hx-get="/api/memory/convergence" hx-trigger="tab-show" hx-swap="innerHTML">
        <div class="text-slate-600 text-xs">loading...</div>
      </div>
    </div>
    <div class="card shrink-0 overflow-y-auto" style="max-height:20%">
      <div class="flex items-center justify-between mb-2">
        <h2 class="text-xs font-bold text-slate-400">Stale Decisions</h2>
        <button class="btn btn-slate text-xs"
          hx-get="/api/memory/decisions" hx-target="#decisions-list" hx-swap="innerHTML">Refresh</button>
      </div>
      <div id="decisions-list"
        hx-get="/api/memory/decisions" hx-trigger="tab-show" hx-swap="innerHTML">
        <div class="text-slate-600 text-xs">loading...</div>
      </div>
    </div>
    <div class="card shrink-0 overflow-y-auto" style="max-height:20%">
      <div class="flex items-center justify-between mb-2">
        <h2 class="text-xs font-bold text-slate-400">Re-derivable Facts</h2>
        <button class="btn btn-slate text-xs"
          hx-get="/api/memory/rederivable" hx-target="#rederivable-list" hx-swap="innerHTML">Refresh</button>
      </div>
      <div id="rederivable-list"
        hx-get="/api/memory/rederivable" hx-trigger="tab-show" hx-swap="innerHTML">
        <div class="text-slate-600 text-xs">loading...</div>
      </div>
    </div>
    <div class="card shrink-0 overflow-y-auto" style="max-height:15%">
      <div class="flex items-center justify-between mb-2">
        <h2 class="text-xs font-bold text-slate-400">Untagged by Scope</h2>
        <button class="btn btn-slate text-xs"
          hx-get="/api/memory/untagged-scopes" hx-target="#untagged-scopes" hx-swap="innerHTML">Refresh</button>
      </div>
      <div id="untagged-scopes"
        hx-get="/api/memory/untagged-scopes" hx-trigger="tab-show" hx-swap="innerHTML">
        <div class="text-slate-600 text-xs">loading...</div>
      </div>
    </div>
    <div class="card shrink-0 overflow-y-auto" style="max-height:18%">
      <div class="flex items-center justify-between mb-2">
        <h2 class="text-xs font-bold text-slate-400">Next Actions</h2>
        <button class="btn btn-slate text-xs"
          hx-get="/api/memory/next-actions-panel" hx-target="#next-actions-panel" hx-swap="innerHTML">Refresh</button>
      </div>
      <div id="next-actions-panel"
        hx-get="/api/memory/next-actions-panel" hx-trigger="tab-show" hx-swap="innerHTML">
        <div class="text-slate-600 text-xs">loading...</div>
      </div>
    </div>
    <div class="card shrink-0 overflow-y-auto" style="max-height:16%">
      <div class="flex items-center justify-between mb-2">
        <h2 class="text-xs font-bold text-slate-400">Curation Health</h2>
        <button class="btn btn-slate text-xs"
          hx-get="/api/memory/curation-report-panel" hx-target="#curation-report-panel" hx-swap="innerHTML">Refresh</button>
      </div>
      <div id="curation-report-panel"
        hx-get="/api/memory/curation-report-panel" hx-trigger="tab-show" hx-swap="innerHTML">
        <div class="text-slate-600 text-xs">loading...</div>
      </div>
    </div>
    <div class="card shrink-0 overflow-y-auto" style="max-height:18%">
      <div class="flex items-center justify-between mb-2">
        <h2 class="text-xs font-bold text-slate-400">Topic Index</h2>
        <button class="btn btn-slate text-xs"
          hx-get="/api/memory/topics/html" hx-target="#topics-panel" hx-swap="innerHTML">Refresh</button>
      </div>
      <div id="topics-panel"
        hx-get="/api/memory/topics/html" hx-trigger="tab-show" hx-swap="innerHTML">
        <div class="text-slate-600 text-xs">loading...</div>
      </div>
    </div>
    <div class="card flex-1 min-h-0 overflow-hidden flex flex-col" style="min-height:0">
      <div class="flex items-center justify-between mb-2 shrink-0">
        <h2 class="text-xs font-bold text-slate-400">Temporal Graph</h2>
        <div id="temporal-stats" class="text-xs text-slate-600"
          hx-get="/api/temporal/stats" hx-trigger="tab-show" hx-swap="innerHTML">loading...</div>
      </div>
      <div class="flex items-center gap-2 shrink-0 mb-1">
        <input id="tg-q" type="text" class="flex-1" placeholder="Query temporal graph..."
          onkeydown="if(event.key==='Enter')queryTemporal()">
        <select id="tg-mode" aria-label="Temporal query mode" class="w-24">
          <option value="current">current</option>
          <option value="history">history</option>
        </select>
        <button class="btn btn-cyan" onclick="queryTemporal()">Query</button>
      </div>
      <div class="flex items-center gap-2 shrink-0 mb-2">
        <input id="tg-entity" type="text" class="flex-1" placeholder="Entity history..."
          onkeydown="if(event.key==='Enter')fetchEntityHistory()">
        <button class="btn btn-slate text-xs" onclick="fetchEntityHistory()">History</button>
      </div>
      <div id="tg-results" tabindex="0" aria-label="Temporal graph results" class="flex-1 overflow-y-auto text-xs">
        <div class="text-slate-600 text-xs">Enter an entity or topic to query the temporal knowledge graph.</div>
      </div>
    </div>
  </div>
</div>

<!-- SESSIONS TAB -->
<div id="tab-sessions" role="tabpanel" aria-labelledby="tabbtn-sessions" class="tab-content h-full gap-3">
  <div class="w-56 shrink-0 flex flex-col gap-2">
    <input id="session-search-input" type="text" placeholder="Search sessions..."
      class="w-full bg-slate-800 border border-slate-600 rounded px-2 py-1 text-xs text-slate-200 focus:outline-none focus:border-cyan-500"
      oninput="sessionSearchDebounced(this.value)">
    <div id="session-list" class="card flex-1 overflow-y-auto"
      hx-get="/api/sessions" hx-trigger="tab-show" hx-swap="innerHTML">
      <div class="text-slate-500 text-xs">loading...</div>
    </div>
  </div>
  <div class="flex-1 min-h-0 flex flex-col gap-2">
    <div id="session-content" class="card flex-1 overflow-y-auto">
      <div class="text-slate-600 text-xs">Click a session to read it</div>
    </div>
    <div class="card shrink-0"
      hx-get="/api/sessions/narrative" hx-trigger="tab-show" hx-swap="innerHTML">
      <div class="text-slate-600 text-xs">loading narrative...</div>
    </div>
  </div>
  <!-- Brain Sessions browser panel -->
  <div class="w-64 shrink-0 flex flex-col gap-2">
    <div class="card shrink-0 flex items-center justify-between">
      <h2 class="text-xs font-bold text-slate-400">Brain Sessions</h2>
      <button class="btn btn-slate text-xs"
        hx-get="/api/brain/sessions" hx-target="#brain-session-list" hx-swap="innerHTML">Refresh</button>
    </div>
    <div id="brain-session-list" class="card flex-1 overflow-y-auto"
      hx-get="/api/brain/sessions" hx-trigger="tab-show" hx-swap="innerHTML">
      <div class="text-slate-500 text-xs">loading...</div>
    </div>
    <div id="brain-session-detail" class="card flex-1 overflow-y-auto" style="display:none">
      <div class="text-slate-600 text-xs">Click a Brain session above</div>
    </div>
  </div>
</div>

<!-- TOOLS TAB (local tool panes) -->
{_tools_tab_html()}

<!-- WATCHTOWER TAB (optional SIEM) -->
<div id="tab-watchtower" role="tabpanel" aria-labelledby="tabbtn-watchtower" class="tab-content block-tab h-full overflow-y-auto">
  <div class="card">
    <div class="flex items-center justify-between mb-2">
      <h2 class="text-xs font-bold text-slate-400">Watchtower &mdash; SIEM</h2>
      <span class="text-slate-600 text-xs">live single-operator SOC view</span>
    </div>
    <div hx-get="/api/security/watchtower.html" hx-trigger="tab-show, every 15s" hx-swap="innerHTML">
      <div class="text-slate-500 text-xs">loading SIEM state...</div>
    </div>
  </div>
</div>

<div id="tab-map" role="tabpanel" aria-labelledby="tabbtn-map" class="tab-content h-full flex-col gap-2">
  <div class="card flex items-center gap-3 py-2">
    <h2 class="text-xs font-bold text-slate-400">System Map</h2>
    <span class="text-slate-600 text-xs">services, stores, and who is talking &mdash; live</span>
    <span class="flex-1"></span>
    <span class="text-xs text-slate-500"><span style="color:#4ade80">&#9679;</span> up</span>
    <span class="text-xs text-slate-500"><span style="color:#facc15">&#9679;</span> degraded</span>
    <span class="text-xs text-slate-500"><span style="color:#f87171">&#9679;</span> down</span>
    <span class="text-xs text-slate-500"><span style="color:#22d3ee">&#9679;</span> store</span>
    <span id="map-status" class="text-slate-600 text-xs"></span>
  </div>
  <div class="flex-1 flex gap-2 min-h-0">
    <div class="card flex-1 min-w-0 p-1">
      <svg id="map-svg" width="100%" height="100%" viewBox="0 0 880 620" preserveAspectRatio="xMidYMid meet"></svg>
    </div>
    <div class="card w-80 flex flex-col min-h-0">
      <h3 class="text-xs font-bold text-slate-400 mb-1">Live activity</h3>
      <div id="map-ticker" tabindex="0" aria-label="Activity ticker" class="flex-1 overflow-y-auto text-xs font-mono"></div>
    </div>
  </div>
</div>

</main><!-- end main content -->

<!-- Ctrl+K Command Palette -->
<div id="palette" class="palette-overlay" style="display:none" tabindex="-1" onclick="if(event.target===this)closePalette(true)">
  <div class="palette-box" role="dialog" aria-modal="true" aria-label="Command palette">
    <input id="palette-input" class="palette-input" type="text" placeholder="Type a command..." aria-label="Command"
      autocomplete="off" oninput="filterPalette(this.value)"
      onkeydown="palettKey(event)">
    <div id="palette-list" style="border-top:1px solid #334155;max-height:320px;overflow-y:auto"></div>
  </div>
</div>

<!-- Keyboard shortcuts (`?` or the palette's "Keyboard shortcuts"; AG97: the dashboard had no help) -->
<div id="kbd-help" class="palette-overlay" style="display:none" tabindex="-1" onclick="if(event.target===this)closeShortcuts()">
  <div class="palette-box" role="dialog" aria-modal="true" aria-labelledby="kbd-help-title" style="padding:12px 16px">
    <div id="kbd-help-title" class="text-sm font-bold mb-2">Keyboard shortcuts</div>
    <table class="text-xs"><tbody>
      <tr><td class="pr-4 font-mono">Ctrl+K</td><td>command palette (every action by name)</td></tr>
      <tr><td class="pr-4 font-mono">/</td><td>search memory</td></tr>
      <tr><td class="pr-4 font-mono">g d / g t / g a</td><td>go to Dashboard / Tasks / Agents</td></tr>
      <tr><td class="pr-4 font-mono">g m / g c / g s / g k</td><td>go to Memory / Chat / Sessions / Tickets</td></tr>
      <tr><td class="pr-4 font-mono">?</td><td>this list</td></tr>
      <tr><td class="pr-4 font-mono">Esc</td><td>close a dialog</td></tr>
    </tbody></table>
    <div class="text-xs text-slate-500 mt-2">Shortcuts are off while you type in a box.</div>
  </div>
</div>

<!-- Quick Dispatch Modal -->
<div id="dispatch-modal"
  style="display:none;position:fixed;inset:0;background:rgba(0,0,0,.65);align-items:center;justify-content:center;z-index:100">
  <div class="card w-96 mx-4" role="dialog" aria-modal="true" aria-labelledby="dm-title">
    <div class="flex items-center justify-between mb-3">
      <h2 id="dm-title" class="text-sm font-bold text-slate-200">
        Dispatch to <span id="dm-agent" class="text-cyan-400"></span>
      </h2>
      <button onclick="closeDispatchModal()" aria-label="Close" class="text-slate-400 hover:text-white text-lg leading-none">&times;</button>
    </div>
    <textarea id="dm-inst" rows="4" class="w-full resize-none" aria-label="Instruction for this agent"
      placeholder="Instruction for this agent..."
      onkeydown="if(event.key==='Enter'&&event.ctrlKey)confirmDispatch()"></textarea>
    <div class="text-slate-600 text-xs mt-1">Ctrl+Enter to dispatch</div>
    <div class="flex gap-2 mt-3">
      <button class="btn btn-cyan flex-1" onclick="confirmDispatch()">Dispatch</button>
      <button class="btn btn-slate" onclick="closeDispatchModal()">Cancel</button>
    </div>
    <div id="dm-result" class="mt-2 text-xs text-slate-400 min-h-4"></div>
  </div>
</div>

<script>
const SID="web-"+new Date().toISOString().slice(0,10);
document.getElementById("chat-sid").textContent=SID;

// ---- Theme toggle ----
(function(){{
  const saved = localStorage.getItem('atlas-theme');
  if(saved === 'light') {{
    document.body.classList.add('light');
    const btn = document.getElementById('theme-btn');
    if(btn) btn.textContent = 'light';
  }}
}})();
function toggleTheme(){{
  const isLight = document.body.classList.toggle('light');
  localStorage.setItem('atlas-theme', isLight ? 'light' : 'dark');
  const btn = document.getElementById('theme-btn');
  if(btn) btn.textContent = isLight ? 'light' : 'dark';
}}

// ---- Stop server ----
async function stopServer(){{
  if(!confirm("Stop the Sextant dashboard server?"))return;
  try{{const r=await fetch("/stop",{{method:"POST",headers:{{}}}});if(!r.ok){{showToast('Stop refused (HTTP '+r.status+'): sign in again','failed');return;}}}}catch(e){{}}  // a dead socket means it stopped (MN29)
  window._sextantStopped=true;
  try{{es.close();}}catch(e){{}}  // the event stream reconnected every few seconds on the stopped page (AG59)
  document.body.innerHTML='<div style="color:#4ade80;font-family:monospace;padding:2rem;font-size:1rem">Server stopped. Close this tab.</div>';
}}

// ---- System Map: live topology + activity pulses ----
const MAP_POS={{
  clients:[100,300], helm:[290,90], mcp:[290,300], sync:[290,530],
  brain:[520,150], worker:[520,300], ollama:[750,150],
  corpus:[560,460], statedb:[750,300], temporal:[760,560], tickets:[560,580]
}};
const MAP_EDGE_FOR_SOURCE={{
  mcp:['clients','mcp'], brain:['brain','statedb'], tickets:['mcp','tickets'],
  temporal:['temporal','corpus'], sync:['sync','corpus'], hook:['clients','mcp']
}};
const MAP_STATUS_COLOR={{
  up:'#4ade80', active:'#4ade80', degraded:'#facc15', down:'#f87171',
  idle:'#64748b', present:'#22d3ee', missing:'#475569', 'n/a':'#94a3b8'
}};
let mapCursor=null, mapTimer=null, topoTimer=null;
function mapEsc(s){{return String(s==null?'':s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');}}
function mapEdgeId(a,b){{return 'edge-'+a+'-'+b;}}
function mapActive(){{const t=document.getElementById('tab-map');return t&&t.classList.contains('active');}}
async function loadTopology(){{
  if(!mapActive())return;
  try{{
    const r=await fetch('/api/topology',{{headers:{{}}}});
    if(!r.ok)return;
    const topo=await r.json();
    let out='';
    topo.edges.forEach(e=>{{
      const a=MAP_POS[e.source],b=MAP_POS[e.target];
      if(!a||!b)return;
      out+='<path id="'+mapEdgeId(e.source,e.target)+'" d="M '+a[0]+' '+a[1]+' L '+b[0]+' '+b[1]+'" stroke="#334155" stroke-width="1.5" fill="none"/>';
    }});
    topo.nodes.forEach(n=>{{
      const p=MAP_POS[n.id];if(!p)return;
      const c=MAP_STATUS_COLOR[n.status]||'#94a3b8';
      const w=150,h=46,x=p[0]-w/2,y=p[1]-h/2;
      const dash=n.kind==='store'?' stroke-dasharray="4 3"':'';
      const rx=n.kind==='client'?23:8;
      out+='<g id="node-'+mapEsc(n.id)+'">'
        +'<rect x="'+x+'" y="'+y+'" width="'+w+'" height="'+h+'" rx="'+rx+'" fill="#1e293b" stroke="#475569"'+dash+'/>'
        +'<circle cx="'+(x+15)+'" cy="'+(y+16)+'" r="4" fill="'+c+'"><title>'+mapEsc(n.status)+'</title></circle>'
        +'<text x="'+(x+27)+'" y="'+(y+20)+'" fill="#e2e8f0" font-size="12" font-family="monospace">'+mapEsc(n.label)+'</text>'
        +'<text x="'+(x+13)+'" y="'+(y+36)+'" fill="#64748b" font-size="9" font-family="monospace">'+mapEsc(String(n.detail||'').slice(0,30))+'</text>'
        +'</g>';
    }});
    document.getElementById('map-svg').innerHTML=out;
    document.getElementById('map-status').textContent='refreshed '+new Date().toLocaleTimeString();
  }}catch(e){{}}
}}
function pulseNode(id){{
  const g=document.getElementById('node-'+id);if(!g)return;
  const rect=g.querySelector('rect');if(!rect)return;
  rect.setAttribute('stroke','#22d3ee');rect.setAttribute('stroke-width','2.5');
  setTimeout(()=>{{rect.setAttribute('stroke','#475569');rect.setAttribute('stroke-width','1');}},900);
}}
function pulseEdge(a,b){{
  const p=document.getElementById(mapEdgeId(a,b))||document.getElementById(mapEdgeId(b,a));
  if(!p)return;
  p.setAttribute('stroke','#22d3ee');p.setAttribute('stroke-width','2.5');
  setTimeout(()=>{{p.setAttribute('stroke','#334155');p.setAttribute('stroke-width','1.5');}},1100);
  const svg=document.getElementById('map-svg');
  const dot=document.createElementNS('http://www.w3.org/2000/svg','circle');
  dot.setAttribute('r','4');dot.setAttribute('fill','#22d3ee');
  const mo=document.createElementNS('http://www.w3.org/2000/svg','animateMotion');
  mo.setAttribute('dur','0.9s');mo.setAttribute('path',p.getAttribute('d'));
  mo.setAttribute('fill','freeze');
  dot.appendChild(mo);svg.appendChild(dot);
  setTimeout(()=>{{if(dot.parentNode)dot.parentNode.removeChild(dot);}},1000);
}}
async function pollActivity(){{
  if(!mapActive())return;
  try{{
    const q=mapCursor?('?since='+encodeURIComponent(mapCursor)):'?limit=30';
    const r=await fetch('/api/activity'+q,{{headers:{{}}}});
    if(!r.ok)return;
    const body=await r.json();
    mapCursor=body.now;
    const ticker=document.getElementById('map-ticker');
    body.events.forEach(ev=>{{
      pulseNode(ev.source==='hook'?'clients':ev.source);
      const edge=MAP_EDGE_FOR_SOURCE[ev.source];
      if(edge)pulseEdge(edge[0],edge[1]);
      const line=document.createElement('div');
      line.className='py-0.5 border-b border-slate-800 text-slate-400';
      const detail=ev.tool||ev.task_id||ev.ticket_id||ev.device||'';
      let t='';
      try{{t=new Date(ev.ts).toLocaleTimeString();}}catch(err){{}}
      // ev.ref is where the data lives (memory path, ticket file, task
      // artifact, truncated query) -- references only, never contents.
      line.textContent=t+' '+ev.source+' '+ev.event+(detail?' '+detail:'')+(ev.ref?' → '+ev.ref:'');
      if(ev.ref)line.title=ev.ref;
      ticker.prepend(line);
    }});
    while(ticker.children.length>120)ticker.removeChild(ticker.lastChild);
  }}catch(e){{}}
}}
function mapTabShown(){{
  loadTopology();
  pollActivity();
  if(!mapTimer)mapTimer=setInterval(pollActivity,3000);
  if(!topoTimer)topoTimer=setInterval(loadTopology,30000);
}}

// ---- Tab switching with lazy HTMX loading ----
// Data-driven: bar tabs carry data-tab and panels are #tab-<name>, so the
// bar and the panel set can never drift apart (the old parallel _TABS array
// was missing operator/watchtower, which misaligned highlighting from the
// third tab on and left those two panels active when switching away).
function switchTab(name){{
  if(!document.getElementById('tab-'+name))return;  // an optional tab that is not installed (watchtower) left every pane hidden: a blank page
  document.querySelectorAll('.tab[data-tab]').forEach(t=>{{const on=t.dataset.tab===name;t.classList.toggle('active',on);t.setAttribute('aria-selected',on?'true':'false');t.setAttribute('tabindex',on?'0':'-1');}});  // a real tablist: one tab stop, the selected tab (AG80)
  if(name==='tickets')loadTicketProjects();
  document.querySelectorAll('.tab-content').forEach(el=>{{
    el.classList.remove('active');
    el.style.setProperty('display','none','important');
  }});
  const target=document.getElementById('tab-'+name);
  if(!target)return;
  target.classList.add('active');
  const dv=target.classList.contains('block-tab')?'block':'flex';
  target.style.setProperty('display',dv,'important');
  target.querySelectorAll('[hx-trigger*="tab-show"]').forEach(el=>htmx.trigger(el,'tab-show'));
  if(name==='map'&&typeof mapTabShown==='function')mapTabShown();
}}
// Tab bar keys (AG80): Left/Right (wrapping), Home and End move to a tab and show it; Enter and Space show the focused tab.
function _tabKey(ev){{
  const t=ev.target.closest&&ev.target.closest('.tab[data-tab]');
  if(!t)return;
  const tabs=[...document.querySelectorAll('#tabbar .tab[data-tab]')];
  const i=tabs.indexOf(t);let j=-1;
  if(ev.key==='ArrowRight')j=(i+1)%tabs.length;
  else if(ev.key==='ArrowLeft')j=(i-1+tabs.length)%tabs.length;
  else if(ev.key==='Home')j=0;
  else if(ev.key==='End')j=tabs.length-1;
  else if(ev.key==='Enter'||ev.key===' '){{ev.preventDefault();switchTab(t.dataset.tab);return;}}
  else return;
  ev.preventDefault();
  switchTab(tabs[j].dataset.tab);tabs[j].focus();
}}
document.getElementById('tabbar').addEventListener('keydown',_tabKey);
// Ticket board project picker (AG80): the cap sentence said "pick a project" and the tab had no way to.
async function loadTicketProjects(){{
  const sel=document.getElementById('tkt-project');if(!sel)return;
  try{{
    const r=await fetch('/api/tickets/projects');if(!r.ok)return;
    const names=await r.json();const cur=sel.value;
    while(sel.options.length>1)sel.remove(1);
    names.forEach(n=>{{const o=document.createElement('option');o.value=n;o.textContent=n;sel.appendChild(o);}});
    sel.value=names.includes(cur)?cur:'';
    if(cur&&!names.includes(cur))reloadTickets();  // the picked project is gone: show the whole board again
  }}catch(_e){{}}
}}

// ---- SSE ----
// A 401 from any panel means the session is gone (restart, rotation, 12 h): leave the page
// instead of polling 43 panels into a lockout (AG31, sweep 14).
document.body.addEventListener("htmx:responseError",function(e){{
  const s=e.detail&&e.detail.xhr?e.detail.xhr.status:0;
  if(s===401){{
    const dirty=[...document.querySelectorAll('textarea[data-dirty="1"]')].some(t=>t.value.trim());  // a draft on a hidden tab counts too (AG49)
    if(dirty){{
      // typed text is never thrown away (AG45); every poll stops, or the dead session's 401s lock a remote user out (MN37, the AG31 rule)
      window._sextantExpired=true;try{{es.close();}}catch(_){{}}
      if(!window._sextantExpiredToast){{window._sextantExpiredToast=true;showToast('Your session expired: copy any unsaved editor text, then reload and sign in with ?key=','failed');}}
      return;
    }}
    window.location.replace("/expired");
  }}
}});
document.body.addEventListener('htmx:beforeRequest',function(e){{if(window._sextantExpired)e.preventDefault();}});
(function(){{const _f=window.fetch;window.fetch=function(){{if(window._sextantExpired||window._sextantStopped)return Promise.reject(new Error('session expired'));return _f.apply(this,arguments);}};}})();  // the stopped page kept polling /api/pending-facts/count every 30 s (AG59)
(function(){{const _s=XMLHttpRequest.prototype.send;XMLHttpRequest.prototype.send=function(){{if(window._sextantStopped)return;return _s.apply(this,arguments);}};}})();  // htmx's pollers (tasks every 8 s, status every 30 s) kept asking a stopped server: 21 requests in 45 s (AG59)
document.addEventListener('input',function(e){{if(e.target&&e.target.tagName==='TEXTAREA')e.target.dataset.dirty=e.target.value.trim()?'1':'0';}});  // an emptied or abandoned box is not unsaved work (MN38)
const es=new EventSource("/events");
es.onopen=()=>document.getElementById("conn").textContent="live";
es.onerror=()=>document.getElementById("conn").textContent="off";
es.onmessage=(e)=>{{
  const d=JSON.parse(e.data);
  if(d.type==="ping"||d.type==="connected")return;
  const f=document.getElementById("feed");
  const l=document.createElement("div");
  const ts=new Date().toLocaleTimeString();
  const info=d.task?(" "+d.task.task_id.slice(-8)+" "+d.task.status):(d.session_id?" "+d.session_id:"");
  l.textContent=ts+" "+d.type+info;
  f.prepend(l);
  while(f.children.length>80)f.removeChild(f.lastChild);
  const fl=document.getElementById("feed-latest");if(fl)fl.textContent=l.textContent;
  if(d.type==="task_update"&&d.task){{
    const st=d.task.status;
    if(st==="done")showToast("Task done: "+d.task.task_id.slice(-8),"done");
    else if(st==="failed")showToast("Task failed: "+d.task.task_id.slice(-8),"failed");
    else if(st==="blocked")showToast("Task blocked: "+d.task.task_id.slice(-8),"failed");
    _updateTasksBadge();
  }}
}};

// ---- WebSocket chat ----
let ws=null;
function cwS(){{
  if(window._sextantExpired||window._sextantStopped)return;
  ws=new WebSocket((location.protocol==='https:'?'wss://':'ws://')+location.host+"/ws/chat/"+SID);  // behind TLS the page's CSP refuses ws:// (AG47)
  ws.onmessage=(e)=>{{
    const m=JSON.parse(e.data),b=document.getElementById("chat-messages");
    if(m.type==="thinking"){{
      const d=document.createElement("div");d.className="thinking text-xs";d.id="tind";d.textContent="Brain is thinking...";b.appendChild(d);
    }}else if(m.type==="tool_call"){{
      const t=document.getElementById("tind");
      if(t)t.textContent="Brain: calling "+m.tool+"...";
    }}else if(m.type==="response"){{
      const t=document.getElementById("tind");if(t)t.remove();
      const d=document.createElement("div");d.className="msg-brain text-xs leading-relaxed";
      let txt="Brain: "+m.text;
      if(m.tool_log&&m.tool_log.length>0){{
        // textContent for the LLM text, a separate element for the tool
        // badge: m.text is model output steerable by corpus content
        // (same stored-XSS class as the onclick hardening -- expert pass
        // 2026-10-05, adjacent finding; innerHTML here executed markup).
        const names=[...new Set(m.tool_log.map(e=>e.tool))].join(", ");
        d.textContent=txt+"\\n";
        const badge=document.createElement("span");
        badge.className="text-slate-500";
        badge.textContent="[tools: "+names+"]";
        d.appendChild(badge);
      }}else if(m.tool_calls){{
        txt+=' ['+m.tool_calls+' tools]';
        d.textContent=txt;
      }}else{{
        d.textContent=txt;
      }}
      b.appendChild(d);
    }}else if(m.type==="error"){{
      const t=document.getElementById("tind");if(t)t.remove();
      const d=document.createElement("div");d.className="msg-error text-xs";d.textContent="Error: "+(m.detail||"no answer from Brain");b.appendChild(d);
    }}
    b.scrollTop=b.scrollHeight;
  }};
  ws.onclose=()=>{{if(!window._sextantExpired&&!window._sextantStopped)setTimeout(cwS,3000);}};  // the stopped page too (MN44: 15 handshakes from a stopped tab locked the remote user out after a restart)  // a dead session must not retry the handshake every 3 s: each try counted as an auth failure and locked the remote user out (AG49)
}}
cwS();

function sendChat(){{
  const i=document.getElementById("ci"),t=i.value.trim();
  if(!t||!ws||ws.readyState!==1)return;
  const b=document.getElementById("chat-messages");
  const d=document.createElement("div");d.className="msg-user text-xs";d.textContent="You: "+t;
  b.appendChild(d);b.scrollTop=b.scrollHeight;ws.send(t);i.value="";
}}

function clearChat(){{
  document.getElementById("chat-messages").innerHTML="";
}}

// ---- Tasks (submitTask defined once below with streaming) ----

function reloadTasks(){{
  const s=document.getElementById("task-filter").value;
  const a=(document.getElementById("agent-filter")||{{}}).value||"";
  const h=(document.getElementById("time-filter")||{{}}).value||"";
  let url="/api/tasks?limit=50";
  if(s)url+="&status="+encodeURIComponent(s);
  if(a)url+="&agent="+encodeURIComponent(a);
  if(h)url+="&since_hours="+encodeURIComponent(h);
  const el=document.getElementById("task-list");
  el.setAttribute("hx-get",url);htmx.process(el);htmx.trigger(el,"tab-show");
}}

// Detail panes (AG80): opening a card or row moves focus into its pane; Escape inside the pane returns it.
let _detailReturn=null;
function _noteOpener(paneIds){{
  const a=document.activeElement;
  if(!a||a===document.body)return;
  if(paneIds.some(id=>{{const p=document.getElementById(id);return p&&p.contains(a);}}))return;  // a re-render from inside the pane keeps the first opener
  _detailReturn={{el:a,oc:a.getAttribute('onclick')}};
}}
function _focusPane(id){{
  const p=document.getElementById(id);
  if(!p||p.offsetParent===null)return;
  if(!p.hasAttribute('tabindex'))p.setAttribute('tabindex','-1');
  p.focus({{preventScroll:window.innerWidth>=768}});
}}
function _returnFromPane(){{
  const a=document.activeElement;
  const pane=a&&a.closest&&a.closest('#ticket-detail,#task-detail,#dash-detail,#inbox-detail');
  if(!pane||!_detailReturn)return false;
  let el=_detailReturn.el;
  if((!el||!el.isConnected)&&_detailReturn.oc){{
    el=[...document.querySelectorAll('[onclick]')].find(x=>x.getAttribute('onclick')===_detailReturn.oc&&x.offsetParent!==null)||null;  // the list re-rendered under the pane: the same row, new node
  }}
  if(!el||!el.isConnected)return false;
  el.focus();return true;
}}

function showTaskDetail(taskId){{
  _noteOpener(['task-detail','dash-detail']);
  fetch("/api/tasks/"+taskId+"/detail",{{headers:{{}}}})
    .then(r=>r.text()).then(html=>{{
      const d=document.getElementById("task-detail");if(d)d.innerHTML=html;
      const dd=document.getElementById("dash-detail");if(dd)dd.innerHTML=html;
      _focusPane('task-detail');_focusPane('dash-detail');  // only the visible one takes focus
    }});
}}

async function retryTask(taskId){{
  const r=document.getElementById("task-detail");
  if(r)r.innerHTML+='<div class="text-xs text-slate-400 mt-2">retrying...</div>';
  try{{
    const resp=await fetch("/api/tasks/"+taskId+"/retry",{{method:"POST",
      headers:{{}}}});
    const data=await jsonOrThrow(resp);
    if(r)r.innerHTML+='<div class="text-xs text-green-400 mt-1">new task: '+data.task_id+'</div>';
  }}catch(ex){{
    if(r)r.innerHTML+='<div class="text-xs text-red-400 mt-1">retry failed: '+ex.message+'</div>';
  }}
}}

// ---- Escalate to Claude Code ----
async function escalateTask(taskId){{
  const r=document.getElementById('task-detail');
  if(r)r.innerHTML+='<div class="text-xs text-amber-400 mt-1">Escalating to Claude Code...</div>';
  try{{
    const resp=await fetch('/api/tasks/claude',{{method:'POST',
      headers:{{'Content-Type':'application/json'}},
      body:JSON.stringify({{instruction:'[ESCALATION] Review and improve task '+taskId+'. See original task for context.',
        session_id:'web-escalate-'+taskId.slice(0,8)}})}});
    const data=await jsonOrThrow(resp);
    if(r)r.innerHTML+='<div class="text-xs text-amber-400 mt-1">escalated: '+data.task_id+'</div>';
  }}catch(ex){{
    if(r)r.innerHTML+='<div class="text-xs text-red-400 mt-1">escalate failed: '+ex.message+'</div>';
  }}
}}

// ---- Tickets ----
function toggleTicketForm(){{
  const f=document.getElementById("ticket-form");
  f.style.display=f.style.display==="none"?"block":"none";
}}

function reloadTickets(){{
  const s=document.getElementById("tkt-filter").value;
  const pj=(document.getElementById("tkt-project")||{{}}).value||"";
  const url="/api/tickets/board?status_filter="+encodeURIComponent(s)+(pj?"&project="+encodeURIComponent(pj):"");
  const el=document.getElementById("tboard");
  el.setAttribute("hx-get",url);htmx.process(el);htmx.trigger(el,"tab-show");
}}

function reloadOperator(){{
  const el=document.getElementById("op-queue");
  if(el)htmx.trigger(el,"tab-show");
}}

async function operatorAct(id,outcome){{
  const note=prompt(outcome==="done"?"Done -- optional note for the action log:":"Skip -- why? (lands in the action log)","");
  if(note===null)return; // cancelled
  const out=document.getElementById("op-result");
  try{{
    const resp=await fetch("/api/operator/"+encodeURIComponent(id)+"/complete",{{method:"POST",
      headers:{{"Content-Type":"application/json"}},
      body:JSON.stringify({{outcome:outcome,note:note}})}});
    if(!resp.ok){{const e=await resp.json();throw new Error(e.detail||resp.status);}}
    const data=await resp.json();
    if(out)out.textContent=data.id+" -> "+data.status+(data.logged?" (logged)":" (log write failed)");
    showToast("Operator: "+data.id+" "+outcome,"done");
    reloadOperator();
  }}catch(ex){{
    if(out)out.textContent="error: "+ex.message;
    showToast("Operator action failed: "+ex.message,"failed");
  }}
}}

function showTicketDetail(ticketId){{
  _noteOpener(['ticket-detail']);
  fetch("/api/tickets/"+ticketId+"/detail",{{headers:{{}}}})
    .then(async r=>{{if(!r.ok){{let m="HTTP "+r.status;try{{const j=await r.json();if(j&&j.detail)m=j.detail;}}catch(_e){{}}throw new Error(m);}}return r.text();}})  // the 409 names the choices (MN64)
    .then(html=>{{const d=document.getElementById("ticket-detail");d.innerHTML=html;if(window.innerWidth<768)d.scrollIntoView({{behavior:'smooth',block:'start'}});_focusPane('ticket-detail');}})  // on a phone the pane sits below the fold (AG51)
    .catch(e=>{{document.getElementById("ticket-detail").innerHTML='<div class="text-red-400 text-xs">Failed to load ticket: '+e.message+'</div>';showToast("Ticket detail failed: "+e.message,"failed");}});
}}

function reloadServices(){{
  const el=document.getElementById("svc-list");
  if(el)htmx.trigger(el,"tab-show");
}}
async function svcAction(task,action){{
  const out=document.getElementById("svc-result");
  if(out)out.textContent=action+" "+task+"...";
  try{{
    const r=await fetch("/api/services/"+task+"/"+action,{{method:"POST",headers:{{}}}});
    const d=await r.json();
    if(out)out.textContent=task+" "+action+(d.ok?" ok":" failed");
    setTimeout(reloadServices,2500);
  }}catch(ex){{
    if(out)out.textContent=action+" sent ("+task+"); if Web UI, reload the page in a moment.";
  }}
}}

function reloadInbox(){{
  const s=document.getElementById("inbox-filter").value;
  const el=document.getElementById("inbox-list");
  el.setAttribute("hx-get","/api/inbox?status_filter="+s);htmx.process(el);htmx.trigger(el,"tab-show");
}}

function showInboxDetail(id){{
  _noteOpener(['inbox-detail']);
  fetch("/api/inbox/"+id+"/detail",{{headers:{{}}}})
    .then(r=>{{if(!r.ok)throw new Error("HTTP "+r.status);return r.text();}})
    .then(html=>{{document.getElementById("inbox-detail").innerHTML=html;_focusPane('inbox-detail');}})
    .catch(e=>{{document.getElementById("inbox-detail").innerHTML='<div class="text-red-400 text-xs">Failed: '+e.message+'</div>';}});
}}

async function promoteInbox(id){{
  try{{
    const resp=await fetch("/api/inbox/"+id+"/promote",{{method:"POST",
      headers:{{"Content-Type":"application/json"}},
      body:JSON.stringify({{}})}});
    if(!resp.ok){{const e=await resp.json();throw new Error(e.detail||resp.status);}}
    const data=await resp.json();
    showToast("Promoted to "+data.ticket_id,"done");
    reloadInbox();showInboxDetail(id);
  }}catch(ex){{showToast("Promote failed: "+ex.message,"failed");}}
}}

async function archiveInbox(id){{
  try{{
    await fetch("/api/inbox/"+id+"/archive",{{method:"POST",
      headers:{{"Content-Type":"application/json"}}}});
    reloadInbox();showInboxDetail(id);
  }}catch(ex){{showToast("Archive failed: "+ex.message,"failed");}}
}}

async function createTicket(){{
  const title=document.getElementById("tk-title").value.trim();
  const project=document.getElementById("tk-project").value.trim();
  if(!title||!project){{document.getElementById("tk-result").textContent="title + project required";return;}}
  const priority=document.getElementById("tk-priority").value;
  const assignee=document.getElementById("tk-assignee").value;
  const body=document.getElementById("tk-body").value.trim();
  const requires=(document.getElementById("tk-requires")||{{}}).value||"";const verified=(document.getElementById("tk-verified")||{{}}).value||"";const effort=(document.getElementById("tk-effort")||{{}}).value||"";
  try{{
    const resp=await fetch("/api/tickets/create",{{method:"POST",
      headers:{{"Content-Type":"application/json"}},
      body:JSON.stringify(Object.assign({{title,project,priority,assignee,body}},requires.trim()?{{requires:requires.trim()}}:{{}},verified.trim()?{{verified:verified.trim()}}:{{}},effort?{{effort}}:{{}}))}});
    const data=await resp.json();
    if(!resp.ok){{document.getElementById("tk-result").textContent="not created: "+(data.detail||resp.status);return;}}
    document.getElementById("tk-result").textContent="created: "+data.id;
    document.getElementById("tk-title").value="";
    document.getElementById("tk-body").value="";
    toggleTicketForm();reloadTickets();
  }}catch(ex){{document.getElementById("tk-result").textContent="error: "+ex.message;}}
}}

async function updateTicket(id,status){{
  try{{
    const resp=await fetch("/api/tickets/"+id+"/status",{{method:"PATCH",
      headers:{{"Content-Type":"application/json"}},
      body:JSON.stringify({{status}})}});
    if(!resp.ok){{let d={{}};try{{d=await resp.json();}}catch(e){{}}showToast("Status not changed: "+(d.detail||resp.status),"failed");return;}}
    reloadTickets();showTicketDetail(id);
  }}catch(ex){{console.error(ex);}}
}}

// Keyboard reach (MN64): tabs, task rows and ticket cards were <div onclick> with no tabindex; Map, Operator, Inbox and Services
// could not be reached at all. Every clickable non-control gets a tab stop, a role and Enter/Space.
function _kbdify(root){{
  (root||document).querySelectorAll('[onclick]').forEach(el=>{{
    const tag=el.tagName;
    if(['BUTTON','A','INPUT','SELECT','TEXTAREA','OPTION','LABEL'].includes(tag)||el.hasAttribute('tabindex'))return;
    el.setAttribute('tabindex','0');
    if(!el.hasAttribute('role'))el.setAttribute('role',el.classList.contains('tab')?'tab':'button');
    el.addEventListener('keydown',ev=>{{if(ev.target!==el)return;if(ev.key==='Enter'||ev.key===' '){{ev.preventDefault();el.click();}}}});  // a Space typed in the palette input bubbled here and closed the palette (AG73)
  }});
}}
document.addEventListener('DOMContentLoaded',()=>_kbdify(document));
document.body&&document.body.addEventListener('htmx:afterSwap',ev=>_kbdify(ev.target));
document.addEventListener('htmx:afterSwap',ev=>_kbdify(ev.target));
// A polled list (tasks every 8 s, tickets and inbox every 30 s) re-rendered under a focused row and dropped focus on the
// page body (MN76): the same row, by its onclick, takes focus back; a row that is gone leaves focus on its list.
let _swapFocus=null;
document.addEventListener('htmx:beforeSwap',ev=>{{
  const a=document.activeElement,t=ev.detail&&ev.detail.target;
  _swapFocus=(a&&a!==document.body&&t&&t.contains(a))?{{oc:a.getAttribute('onclick'),t}}:null;
}});
document.addEventListener('htmx:afterSwap',ev=>{{
  const s=_swapFocus;_swapFocus=null;
  if(!s||document.activeElement!==document.body||!s.t.isConnected)return;
  _kbdify(s.t);
  const el=s.oc?[...s.t.querySelectorAll('[onclick]')].find(x=>x.getAttribute('onclick')===s.oc):null;
  if(el){{el.focus({{preventScroll:true}});return;}}
  if(!s.t.hasAttribute('tabindex'))s.t.setAttribute('tabindex','-1');
  s.t.focus({{preventScroll:true}});
}});
// content filled by fetch + innerHTML (memory search results, details) never fired htmx:afterSwap (MN70)
document.addEventListener('DOMContentLoaded',()=>{{let _kq=null;new MutationObserver(()=>{{if(_kq)return;_kq=requestAnimationFrame(()=>{{_kq=null;_kdify_all();}});}}).observe(document.body,{{childList:true,subtree:true}});}});
function _kdify_all(){{_kbdify(document);}}

async function dispatchTicketToBrain(id,title,body){{
  try{{
    const resp=await fetch("/api/tickets/"+id+"/dispatch",{{method:"POST",
      headers:{{"Content-Type":"application/json"}},
      body:JSON.stringify({{instruction:"Ticket "+id+": "+title+"\\n\\n"+body}})}});
    if(!resp.ok){{const e=await resp.json();throw new Error(e.detail||resp.status);}}
    const data=await resp.json();
    const d=document.getElementById("ticket-detail");
    if(d){{const _m=document.createElement('div');_m.className='mt-2 text-xs '+(data.status_note&&data.status_note.indexOf('not changed')>=0?'text-amber-400':'text-green-400');_m.textContent='Dispatched: '+data.task_id+' | '+(data.status_note||'');d.appendChild(_m);}}  // the server's sentence, never a fixed claim (MN64)
    showToast("Dispatched to Brain","done");
    reloadTickets();
  }}catch(ex){{showToast("Dispatch failed: "+ex.message,"failed");}}
}}

// ---- Quick Dispatch modal ----
let _dmAgent='';
function quickDispatch(agentName){{
  _dmAgent=agentName;
  document.getElementById('dm-agent').textContent=agentName;
  document.getElementById('dm-inst').value='';
  document.getElementById('dm-result').textContent='';
  document.getElementById('dm-result').className='mt-2 text-xs text-slate-400 min-h-4';
  const _dmEl=document.getElementById('dispatch-modal');
  if(_dmEl.style.display!=='flex'){{const a=document.activeElement;_dmReturn=(a&&a!==document.body)?a:null;}}
  _dmEl.style.display='flex';
  document.getElementById('dm-inst').focus();  // the palette's sibling dialog: focus at once, kept inside, given back on close (AG80)
}}
let _dmReturn=null;
function closeDispatchModal(){{
  const m=document.getElementById('dispatch-modal');
  const wasOpen=m.style.display==='flex';
  m.style.display='none';
  if(wasOpen&&_dmReturn&&_dmReturn.isConnected)_dmReturn.focus();
  if(wasOpen)_dmReturn=null;
}}
document.addEventListener('focusin',ev=>{{
  const m=document.getElementById('dispatch-modal');
  if(m&&m.style.display==='flex'&&!m.contains(ev.target))document.getElementById('dm-inst').focus();
}});
document.getElementById('dispatch-modal').addEventListener('keydown',ev=>{{
  if(ev.key!=='Tab')return;
  const f=[...ev.currentTarget.querySelectorAll('button,textarea,input,select,[tabindex]:not([tabindex="-1"])')].filter(x=>x.offsetParent!==null);
  if(!f.length)return;
  const first=f[0],last=f[f.length-1];
  if(ev.shiftKey&&document.activeElement===first){{ev.preventDefault();last.focus();}}
  else if(!ev.shiftKey&&document.activeElement===last){{ev.preventDefault();first.focus();}}
}});
async function confirmDispatch(){{
  const inst=document.getElementById('dm-inst').value.trim();
  if(!inst)return;
  const r=document.getElementById('dm-result');
  r.textContent='dispatching...';
  try{{
    const resp=await fetch('/api/tasks',{{method:'POST',
      headers:{{'Content-Type':'application/json'}},
      body:JSON.stringify({{instruction:inst,agent:_dmAgent}})}});
    const data=await jsonOrThrow(resp);
    r.textContent='queued: '+data.task_id;
    r.className='mt-2 text-xs text-green-400 min-h-4';
    setTimeout(closeDispatchModal,1800);
  }}catch(e){{
    r.textContent='error: '+e.message;
    r.className='mt-2 text-xs text-red-400 min-h-4';
  }}
}}
document.getElementById('dispatch-modal').addEventListener('click',function(e){{
  if(e.target===this)closeDispatchModal();
}});

// ---- Memory ----
async function searchMemory(){{
  const q=document.getElementById('mem-q').value;
  const r=document.getElementById('mem-results');
  r.innerHTML='<div class="text-slate-500 text-xs">searching...</div>';
  try{{
    const resp=await fetch('/api/memory/search?q='+encodeURIComponent(q),
      {{headers:{{}}}});
    r.innerHTML=await resp.text();
  }}catch(e){{r.innerHTML='<div class="text-red-400 text-xs">Error: '+e.message+'</div>';}}
}}
async function loadMemoryIndex(){{
  document.getElementById('mem-q').value='';
  await searchMemory();
}}

// ---- Bump task priority ----
async function bumpTask(taskId){{
  try{{
    const r=await fetch('/api/tasks/'+taskId+'/priority',{{method:'PATCH',
      headers:{{'Content-Type':'application/json'}},
      body:JSON.stringify({{priority:1}})}});
    if(r.ok){{
      showTaskDetail(taskId);
      htmx.trigger(document.getElementById('task-list'),'tab-show');
    }}
  }}catch(e){{console.error('bump failed:',e);}}
}}

// ---- Bulk retry failed ----
async function retryAllFailed(){{
  const st=document.getElementById('retry-status');
  st.textContent='retrying...';
  try{{
    const r=await fetch('/api/tasks/retry-failed',{{method:'POST',
      headers:{{}}}});
    const d=await r.json();
    st.textContent='submitted '+d.submitted+' of '+d.total_failed+' failed tasks';
    setTimeout(()=>{{st.textContent='';reloadTasks();}},3000);
  }}catch(e){{st.textContent='error: '+e.message;}}
}}

// ---- Proposals desk (ATL-073) ----
async function approveProposal(id, body){{
  try{{
    const r=await fetch('/api/proposals/'+id+'/approve',{{method:'POST',
      headers:{{'Content-Type':'application/json'}},
      body:JSON.stringify(body?{{body:body}}:{{}})}});
    if(!r.ok){{let d='';try{{d=(await r.json()).detail||'';}}catch(e){{}}throw new Error(d||r.status);}}
    const res=await r.json();
    showToast('Approved: '+(res.lesson_id||res.ticket_id||res.file||res.kind||'done'),'done');
    htmx.trigger('#proposals-list','load');
  }}catch(e){{showToast('Approve failed: '+e.message,'failed');}}
}}
async function editProposal(id){{
  try{{
    const r=await fetch('/api/proposals/'+id+'/body');
    if(!r.ok)throw new Error(r.status);
    const d=await r.json();
    const edited=prompt('Edit the proposal body, then OK to approve it as written (Cancel keeps it pending):', d.body||'');
    if(edited===null)return;
    if(!edited.trim()){{showToast('Empty body; nothing approved','failed');return;}}
    await approveProposal(id, edited);
  }}catch(e){{showToast('Edit failed: '+e.message,'failed');}}
}}
async function rejectProposal(id){{
  try{{
    const r=await fetch('/api/proposals/'+id+'/reject',{{method:'POST',headers:{{}}}});
    if(!r.ok)throw new Error(r.status);
    showToast('Rejected','done');
    htmx.trigger('#proposals-list','load');
  }}catch(e){{showToast('Reject failed: '+e.message,'failed');}}
}}

// ---- Pending facts approval/rejection ----
async function approveFact(id){{
  try{{
    const r=await fetch('/api/pending-facts/'+id+'/approve',{{method:'POST',
      headers:{{'Content-Type':'application/json'}},
      body:JSON.stringify({{}})}});
    if(!r.ok){{const e=await r.json();throw new Error(e.detail||r.status);}}
    showToast('Fact saved to memory','done');
    htmx.trigger('#pending-facts-list','load');
  }}catch(e){{showToast('Approve failed: '+e.message,'failed');}}
}}

async function rejectFact(id){{
  try{{
    await fetch('/api/pending-facts/'+id+'/reject',{{method:'POST',
      headers:{{}}}});
    showToast('Fact rejected','info');
    htmx.trigger('#pending-facts-list','load');
  }}catch(e){{showToast('Reject failed: '+e.message,'failed');}}
}}

// ---- Credit exhaustion reset ----
async function resetCredits(){{
  try{{
    const r=await fetch('/api/credit/reset',{{method:'POST',
      headers:{{}}}});
    const d=await r.json();
    const cleared=d.cleared||[];
    const msg=cleared.length>0?'Cleared: '+cleared.join(', '):'No agents were exhausted.';
    showToast(msg,'info');
    htmx.trigger('#config-panel','load');
  }}catch(e){{showToast('Reset failed: '+e.message,'failed');}}
}}

// ---- Task CSV export ----
function exportTasks(){{
  // Use fetch to add auth header and trigger download
  fetch('/api/tasks/export?fmt=csv&limit=500',{{
    headers:{{}}
  }}).then(r=>r.blob()).then(blob=>{{
    const link=document.createElement('a');
    link.href=URL.createObjectURL(blob);
    link.download='atlas-tasks.csv';
    link.click();
    URL.revokeObjectURL(link.href);
  }}).catch(e=>showToast('Export failed: '+e.message,'failed'));
}}

// ---- Memory index rebuild ----
async function rebuildMemoryIndex(){{
  const r=document.getElementById('mem-results');
  r.innerHTML='<div class="text-slate-500 text-xs">Rebuilding FTS5 index...</div>';
  try{{
    const resp=await fetch('/api/memory/rebuild',{{method:'POST',
      headers:{{}}}});
    const d=await resp.json();
    r.innerHTML='<div class="text-green-400 text-xs">Index rebuilt: '+d.indexed+' files indexed.</div>';
  }}catch(e){{r.innerHTML='<div class="text-red-400 text-xs">Error: '+e.message+'</div>';}}
}}

// ---- Temporal graph query ----
async function queryTemporal(){{
  const q=document.getElementById('tg-q').value.trim();
  if(!q)return;
  const mode=document.getElementById('tg-mode').value;
  const r=document.getElementById('tg-results');
  r.innerHTML='<div class="text-slate-500 text-xs">querying...</div>';
  try{{
    const resp=await fetch('/api/temporal/query?q='+encodeURIComponent(q)+'&mode='+mode,
      {{headers:{{}}}});
    r.innerHTML=await resp.text();
  }}catch(e){{r.innerHTML='<div class="text-red-400 text-xs">Error: '+e.message+'</div>';}}
}}

// ---- Temporal entity history ----
async function fetchEntityHistory(){{
  const entity=document.getElementById('tg-entity').value.trim();
  if(!entity)return;
  const r=document.getElementById('tg-results');
  r.innerHTML='<div class="text-slate-500 text-xs">loading history...</div>';
  try{{
    const resp=await fetch('/api/temporal/history-panel?entity='+encodeURIComponent(entity),
      {{headers:{{}}}});
    r.innerHTML=await resp.text();
  }}catch(e){{r.innerHTML='<div class="text-red-400 text-xs">Error: '+e.message+'</div>';}}
}}

// ---- Global keyboard shortcuts ----
let _gKey=false,_gTimer=null;
document.addEventListener('keydown',e=>{{
  if(e.key==='Escape'){{
    const palOpen=document.getElementById('palette').style.display!=='none';
    const modalOpen=(document.getElementById('dispatch-modal')||{{style:{{}}}}).style.display==='flex';
    const helpOpen=(document.getElementById('kbd-help')||{{style:{{}}}}).style.display==='flex';
    closeDispatchModal(); closePalette(palOpen); closeShortcuts();
    if(!palOpen&&!modalOpen&&!helpOpen&&_returnFromPane())e.preventDefault();
  }}
  if((e.ctrlKey||e.metaKey)&&e.key==='k'){{ e.preventDefault(); openPalette(); return; }}
  const tag=document.activeElement&&document.activeElement.tagName;
  const inInput=(tag==='INPUT'||tag==='TEXTAREA'||tag==='SELECT');
  if(!inInput&&e.key==='?'&&!e.ctrlKey&&!e.metaKey&&!e.altKey){{ e.preventDefault(); showShortcuts(); return; }}
  if(!inInput&&e.key==='/'){{
    e.preventDefault();
    switchTab('memory');
    setTimeout(()=>document.getElementById('mem-q').focus(),100);
    return;
  }}
  if(!inInput&&e.key==='g'&&!_gKey){{
    _gKey=true;
    if(_gTimer)clearTimeout(_gTimer);
    _gTimer=setTimeout(()=>{{_gKey=false;}},500);
    return;
  }}
  if(_gKey){{
    _gKey=false;if(_gTimer){{clearTimeout(_gTimer);_gTimer=null;}}
    const map={{'d':'dash','t':'tasks','a':'agents','m':'memory','c':'chat','s':'sessions','k':'tickets'}};
    if(map[e.key]){{e.preventDefault();switchTab(map[e.key]);}}
  }}
}});

// ---- Quick capture ----
async function quickCapture(){{
  const inp=document.getElementById('capture-input');
  const fact=inp.value.trim();
  if(!fact)return;
  const scope=document.getElementById('capture-scope').value;
  const st=document.getElementById('capture-status');
  st.textContent='saving...';st.className='text-xs text-slate-400 shrink-0';
  try{{
    const r=await fetch('/api/memory/capture',{{method:'POST',
      headers:{{'Content-Type':'application/json'}},
      body:JSON.stringify({{fact,scope}})}});
    const d=await r.json();
    if(d.ok){{
      st.textContent=d.already_remembered?'already remembered':'saved';st.className='text-xs text-green-400 shrink-0';
      inp.value='';
    }}else{{
      st.textContent=d.error||'error';st.className='text-xs text-red-400 shrink-0';
    }}
  }}catch(ex){{st.textContent='err: '+ex.message;st.className='text-xs text-red-400 shrink-0';}}
  setTimeout(()=>{{st.textContent='';st.className='text-xs text-slate-600 shrink-0';}},3000);
}}

// ---- Session viewer ----
function showSession(name){{
  const c=document.getElementById('session-content');
  c.innerHTML='<div class="text-slate-500 text-xs">loading...</div>';
  fetch('/api/sessions/'+encodeURIComponent(name),{{headers:{{}}}})
    .then(r=>r.text()).then(html=>{{c.innerHTML=html;}})
    .catch(e=>{{c.innerHTML='<div class="text-red-400 text-xs">'+e.message+'</div>';}});
}}

async function loadBrainSession(sessionId){{
  const detail=document.getElementById('brain-session-detail');
  if(!detail) return;
  detail.style.display='';
  detail.innerHTML='<div class="text-slate-500 text-xs">loading...</div>';
  try{{
    const r=await fetch('/api/brain/sessions/'+encodeURIComponent(sessionId)+'/html',
      {{headers:{{}}}});
    detail.innerHTML=await r.text();
  }} catch(e){{
    detail.innerHTML='<div class="text-red-400 text-xs">'+e.message+'</div>';
  }}
}}

let _sessionSearchTimer=null;
function sessionSearchDebounced(q){{
  clearTimeout(_sessionSearchTimer);
  const list=document.getElementById('session-list');
  if(!q.trim()){{
    // Restore full session list
    fetch('/api/sessions',{{headers:{{}}}})
      .then(r=>r.text()).then(html=>{{list.innerHTML=html;}});
    return;
  }}
  _sessionSearchTimer=setTimeout(()=>{{
    list.innerHTML='<div class="text-slate-500 text-xs">searching...</div>';
    fetch('/api/sessions/search?q='+encodeURIComponent(q),{{headers:{{}}}})
      .then(r=>r.text()).then(html=>{{list.innerHTML=html;}})
      .catch(e=>{{list.innerHTML='<div class="text-red-400 text-xs">'+e.message+'</div>';}});
  }},300);
}}

// ---- Task live streaming ----
let _liveEs=null;
function streamTask(taskId){{
  if(_liveEs){{_liveEs.close();_liveEs=null;}}
  const dd=document.getElementById('dash-detail');
  const td=document.getElementById('task-detail');
  const box=document.createElement('div');
  box.className='stream-output';box.id='stream-'+taskId;
  box.textContent='Waiting for Brain...';
  if(dd)dd.innerHTML='';if(dd)dd.appendChild(box.cloneNode(true));
  if(td){{td.innerHTML='';td.appendChild(box);}}
  _liveEs=new EventSource('/api/tasks/'+taskId+'/live');
  _liveEs.onmessage=(e)=>{{
    const d=JSON.parse(e.data);
    const el=document.getElementById('stream-'+taskId)||document.querySelector('.stream-output');
    if(!el)return;
    if(d.type==='status'){{
      el.textContent='Status: '+d.status+'...';
    }}else if(d.type==='complete'){{
      el.className='stream-output stream-'+d.status;
      el.textContent=(d.result||'(no output)')+'\\n\\nAgent: '+(d.agent||'?')+' | Status: '+d.status;
      _liveEs.close();_liveEs=null;
      // Refresh task list
      htmx.trigger(document.getElementById('dash-tasks'),'tab-show');
      htmx.trigger(document.getElementById('task-list'),'tab-show');
    }}else if(d.type==='error'){{
      el.className='stream-output stream-failed';
      el.textContent='Error: '+d.detail;
      _liveEs.close();_liveEs=null;
    }}
  }};
  _liveEs.onerror=()=>{{if(_liveEs){{_liveEs.close();_liveEs=null;}}}};
}}

// ---- Override submitTask to stream ----
async function submitTask(){{
  const inst=document.getElementById("ti").value.trim();if(!inst)return;
  const agent=document.getElementById("task-agent").value;
  const r=document.getElementById("tsr");r.textContent="submitting...";
  const body={{instruction:inst}};
  if(agent)body.agent=agent;
  try{{
    const resp=await fetch("/api/tasks",{{method:"POST",
      headers:{{"Content-Type":"application/json"}},
      body:JSON.stringify(body)}});
    const data=await jsonOrThrow(resp);
    r.textContent="queued: "+data.task_id;
    document.getElementById("ti").value="";
    streamTask(data.task_id);
  }}catch(ex){{r.textContent="error: "+ex.message;}}
}}

// ---- Fetch helper: surface HTTP errors readably (422 detail arrays etc.) ----
async function jsonOrThrow(resp){{
  if(!resp.ok){{
    let msg='HTTP '+resp.status;
    try{{
      const b=await resp.json();
      if(b&&b.detail)msg=typeof b.detail==='string'?b.detail
        :b.detail.map(d=>(d.msg||JSON.stringify(d))).join('; ');
    }}catch(_e){{}}
    throw new Error(msg);
  }}
  return resp.json();
}}

// ---- Toast notifications ----
function showToast(msg,type){{
  if(window._sextantStopped)return;  // the stopped page has no toast element (MN34)
  if(typeof msg==='string'&&(msg.startsWith('Saved')||msg.startsWith('No change')))document.querySelectorAll('textarea[data-dirty="1"]').forEach(t=>{{delete t.dataset.dirty;}});
  const t=document.createElement('div');
  t.className='toast toast-'+(type||'info');
  t.textContent=msg;
  if(type==='failed'){{  // a failure stays long enough to read and copy, and is in the console too (2026-11-21)
    t.style.pointerEvents='auto';t.style.userSelect='text';t.title='click to dismiss';
    t.addEventListener('click',()=>t.remove());
    try{{console.error('[helm] '+msg);}}catch(_e){{}}
  }}
  document.body.appendChild(t);
  setTimeout(()=>{{t.style.opacity='0';setTimeout(()=>t.remove(),350);}},type==='failed'?9000:2700);
}}

// ---- Global error surfacing (no silent failures) ----
window.addEventListener('unhandledrejection',function(ev){{try{{showToast('Error: '+((ev.reason&&ev.reason.message)||ev.reason||'request failed'),'failed');}}catch(_e){{}}}});
window.addEventListener('error',function(ev){{try{{showToast('Error: '+(ev.message||'script error'),'failed');}}catch(_e){{}}}});

// ---- Stop task ----
async function stopTask(taskId){{
  try{{
    const r=await fetch('/api/tasks/'+taskId+'/stop',{{method:'POST',
      headers:{{}}}});
    const d=await jsonOrThrow(r);
    if(d.status==='cancelled'){{
      showToast('Task cancelled: '+taskId.slice(-8),'info');
    }}else{{
      showToast('Already '+d.status+': '+taskId.slice(-8),'info');
    }}
    showTaskDetail(taskId);
    htmx.trigger(document.getElementById('task-list'),'tab-show');
    htmx.trigger(document.getElementById('dash-tasks'),'tab-show');
  }}catch(e){{showToast('Stop failed: '+e.message,'failed');}}
}}

// ---- Ask Claude (direct priority-1 Claude Code task) ----
async function submitClaudeTask(){{
  const inst=document.getElementById("claude-input").value.trim();if(!inst)return;
  const r=document.getElementById("claude-tsr");r.textContent="submitting to Claude...";
  try{{
    const resp=await fetch("/api/tasks/claude",{{
      method:"POST",
      headers:{{"Content-Type":"application/json"}},
      body:JSON.stringify({{instruction:inst}})
    }});
    const task=await jsonOrThrow(resp);
    r.textContent="Claude task queued: "+task.task_id;
    document.getElementById("claude-input").value="";
    showToast("Claude Code task queued: "+task.task_id+" (priority 1)","done");
    htmx.trigger(document.getElementById("task-list"),"tab-show");
    htmx.trigger(document.getElementById("dash-tasks"),"tab-show");
  }}catch(e){{r.textContent="Error: "+e.message;showToast("Claude submit failed: "+e.message,"failed");}}
}}

// ---- Quick Run ----
const _QUICK_RUN_MAP={{
  'suggest-facts':'Run suggest-facts review: search recent session transcripts for high-confidence facts not yet in .memory/. Report candidates with confidence >= 0.95.',
  'temporal-build':'Run the temporal graph build: call temporal_build to ingest recent memory and session files. Report how many entities and facts were updated.',
  'curation':'Run memory curation: check .memory/ for duplicate entries, contradictions, and stale project records. Summarize findings.'
}};
async function quickRun(jobName){{
  // Use the scheduler trigger endpoint when available -- fires the real scheduler job
  // so it appears with the correct kind/session_id in the task list.
  try{{
    const resp=await fetch('/api/scheduler/trigger/'+encodeURIComponent(jobName),{{
      method:'POST',
      headers:{{}}}});
    if(resp.ok){{
      showToast('Triggered: '+jobName,'info');
      setTimeout(reloadTasks,1000);
      return;
    }}
  }}catch(e){{/* fall through to legacy task submit */}}
  // Fallback: submit raw task instruction (for jobs not in scheduler map)
  const instruction=_QUICK_RUN_MAP[jobName];
  if(!instruction)return;
  try{{
    const resp=await fetch('/api/tasks',{{method:'POST',
      headers:{{'Content-Type':'application/json'}},
      body:JSON.stringify({{instruction:instruction,session_id:'quick-run-'+jobName}})}});
    const data=await jsonOrThrow(resp);
    showToast('Queued: '+jobName,'info');
    streamTask(data.task_id);
  }}catch(e){{showToast('quickRun error: '+e.message,'failed');}}
}}

// ---- Memory file browser ----
async function readMemoryFile(path){{
  const r=document.getElementById('mem-results');
  r.innerHTML='<div class="text-slate-500 text-xs">loading...</div>';
  try{{
    const resp=await fetch('/api/memory/file?path='+encodeURIComponent(path),
      {{headers:{{}}}});
    r.innerHTML=await resp.text();
  }}catch(e){{r.innerHTML='<div class="text-red-400 text-xs">Error: '+e.message+'</div>';}}
}}
async function browseMemory(){{
  const r=document.getElementById('mem-results');
  r.innerHTML='<div class="text-slate-500 text-xs">loading...</div>';
  try{{
    const resp=await fetch('/api/memory/files',
      {{headers:{{}}}});
    r.innerHTML=await resp.text();
  }}catch(e){{r.innerHTML='<div class="text-red-400 text-xs">Error: '+e.message+'</div>';}}
}}

// ---- Memory file editor ----
let _mfEditPath='';
let _mfBaseEtag='';
let _mfEol='lf';
let _mfBom='0';
async function editMemoryFile(path){{
  _mfEditPath=path;
  const pre=document.getElementById('mf-content');
  if(!pre)return;
  let current=pre.textContent;
  try{{const rr=await fetch('/api/memory/file?raw=1&path='+encodeURIComponent(path));if(rr.ok){{current=await rr.text();_mfBaseEtag=rr.headers.get('X-Sextant-Etag')||'';_mfEol=rr.headers.get('X-Sextant-Eol')||'lf';_mfBom=rr.headers.get('X-Sextant-Bom')||'0';if(_mfEol==='mixed')showToast('This note has line endings other than plain LF (CRLF mixed in, or lone CR); a save with edits makes them LF; a save with none says No change and leaves them','done');}}else{{showToast('Could not load the whole file (HTTP '+rr.status+'): '+(await rr.text()).slice(0,160),'failed');return;}}}}
  catch(e){{showToast('Could not load the whole file: '+e.message,'failed');return;}}
  const r=document.getElementById('mem-results');
  // Static skeleton via innerHTML; the PATH lands via textContent -- a
  // synced corpus filename can carry < > on POSIX and must never parse
  // as markup here (same stored-XSS class as the onclick hardening;
  // quality loop run 36).
  r.innerHTML='<div class="flex items-center justify-between mb-2">'
    +'<span id="mf-edit-path" class="text-cyan-400 text-xs font-mono font-bold truncate"></span>'
    +'<div class="flex gap-1">'
    +'<button class="btn btn-green text-xs" onclick="saveMemoryFile()">Save</button>'
    +'<button class="btn btn-slate text-xs" onclick="readMemoryFile(_mfEditPath)">Cancel</button>'
    +'</div></div>'
    +'<textarea id="mf-edit" dir="auto" class="w-full text-xs" style="height:70vh;resize:vertical;font-family:monospace"></textarea>';  // RTL notes read right-to-left (MN46)
  const sp=document.getElementById('mf-edit-path');
  if(sp)sp.textContent=path;
  const ta=document.getElementById('mf-edit');
  if(ta)ta.value=current;
}}
async function saveMemoryFile(){{
  const ta=document.getElementById('mf-edit');
  if(!ta||!_mfEditPath)return;
  try{{
    const r=await fetch('/api/memory/file',{{method:'POST',
      headers:{{'Content-Type':'application/json'}},
      body:JSON.stringify({{path:_mfEditPath,content:(function(){{let v=ta.value;if(_mfEol==='crlf')v=v.replace(/\\r?\\n/g,'\\r\\n');if(_mfBom==='1'&&v.charCodeAt(0)!==0xFEFF)v='\\uFEFF'+v;return v;}})(),base_etag:_mfBaseEtag}})}});
    const d=await r.json().catch(()=>({{}}));
    if(d.ok){{
      showToast((d.unchanged?'No change: ':'Saved: ')+_mfEditPath.split('/').pop(),'done');
      readMemoryFile(_mfEditPath);
    }}else{{showToast('Save failed: '+(d.detail||('HTTP '+r.status)),'failed');}}
  }}catch(e){{showToast('Save error: '+e.message,'failed');}}
}}

// ---- Config toggle ----
async function toggleConfig(varName){{
  try{{
    await fetch('/api/config',{{method:'POST',
      headers:{{'Content-Type':'application/json'}},
      body:JSON.stringify({{var:varName}})}});
    const panel=document.getElementById('config-panel');
    if(panel)htmx.trigger(panel,'load');
  }}catch(e){{showToast('Config error: '+e.message,'failed');}}
}}

// ---- Chat history ----
async function loadChatHistory(){{
  const b=document.getElementById('chat-messages');
  b.innerHTML='<div class="text-slate-500 text-xs">loading history...</div>';
  try{{
    const resp=await fetch('/api/chat/history?session_id='+encodeURIComponent(SID),
      {{headers:{{}}}});
    b.innerHTML=await resp.text();
    b.scrollTop=b.scrollHeight;
  }}catch(e){{b.innerHTML='<div class="text-red-400 text-xs">Error: '+e.message+'</div>';}}
}}

// ---- Tasks badge (running count) ----
function _updateTasksBadge(){{
  const list=document.getElementById('task-list');
  if(!list)return;
  const running=list.querySelectorAll('.task-running').length;
  const badge=document.getElementById('tasks-badge');
  if(badge)badge.textContent=running>0?'('+running+')':'';
}}
setInterval(_updateTasksBadge,5000);

// ---- Memory tab facts badge (pending count) ----
async function _updateFactsBadge(){{
  try{{
    const r=await fetch('/api/pending-facts/count',{{}});
    if(!r.ok)return;
    const d=await r.json();
    const badge=document.getElementById('facts-badge');
    if(badge)badge.textContent=(d.count>0)?'('+d.count+')':'';
  }}catch(e){{}}
}}
_updateFactsBadge();
setInterval(_updateFactsBadge,30000);

// ---- Ctrl+K palette ----
const _PALETTE_CMDS=[
  {{label:'Submit task',hint:'dash',action:()=>{{switchTab('dash');setTimeout(()=>document.getElementById('ti').focus(),100);}}}},
  {{label:'Research topic',hint:'dash',action:()=>{{switchTab('dash');const a=document.getElementById('task-agent');if(a)a.value='research';setTimeout(()=>document.getElementById('ti').focus(),100);}}}},
  {{label:'Search memory',hint:'memory',action:()=>{{switchTab('memory');setTimeout(()=>document.getElementById('mem-q').focus(),100);}}}},
  {{label:'Memory index',hint:'memory',action:()=>{{switchTab('memory');loadMemoryIndex();}}}},
  {{label:'Memory gaps',hint:'memory',action:()=>{{switchTab('memory');}}}},
  {{label:'Temporal query',hint:'temporal',action:()=>{{switchTab('memory');setTimeout(()=>document.getElementById('tg-q').focus(),200);}}}},
  {{label:'New ticket',hint:'tickets',action:()=>{{switchTab('tickets');toggleTicketForm();}}}},
  {{label:'Tickets board',hint:'tickets',action:()=>switchTab('tickets')}},
  {{label:'Brain chat',hint:'chat',action:()=>{{switchTab('chat');setTimeout(()=>document.getElementById('ci').focus(),100);}}}},
  {{label:'Agents',hint:'agents',action:()=>switchTab('agents')}},
  {{label:'Sessions',hint:'sessions',action:()=>switchTab('sessions')}},
  {{label:'Map',hint:'map',action:()=>switchTab('map')}},
  {{label:'Operator',hint:'operator',action:()=>switchTab('operator')}},
  {{label:'Inbox',hint:'inbox',action:()=>switchTab('inbox')}},
  {{label:'Services',hint:'services',action:()=>switchTab('services')}},
  {{label:'Watchtower',hint:'watchtower',action:()=>switchTab('watchtower')}},
  {{label:'Dashboard',hint:'dash',action:()=>switchTab('dash')}},
  {{label:'Capture fact',hint:'capture',action:()=>{{closePalette();document.getElementById('capture-input').focus();}}}},
  {{label:'Ask Claude',hint:'escalate',action:()=>{{closePalette();switchTab('dash');setTimeout(()=>{{const inp=document.getElementById('claude-input');if(inp)inp.focus();}},150);}}}},
  {{label:'Pending facts',hint:'memory',action:()=>{{closePalette();switchTab('memory');htmx.trigger(document.getElementById('pending-facts-list'),'load');}}}},
  {{label:'Toggle escalation',hint:'config',action:()=>{{closePalette();toggleConfig('ATLAS_CLAUDE_ESCALATION');}}}},
  {{label:'Retry all failed',hint:'tasks',action:()=>{{closePalette();switchTab('tasks');retryAllFailed();}}}},
  {{label:'Keyboard shortcuts',hint:'help ?',action:()=>{{closePalette();showShortcuts();}}}},
  {{label:'Stop server',hint:'system',action:()=>stopServer()}},
];
function showShortcuts(){{const h=document.getElementById('kbd-help');if(h){{h.style.display='flex';h.focus();}}}}
function closeShortcuts(){{const h=document.getElementById('kbd-help');if(h)h.style.display='none';}}
let _palActive=0,_palReturn=null;

function openPalette(){{
  const _dm=document.getElementById('dispatch-modal');
  if(_dm&&_dm.style.display==='flex')return;  // two focus traps fought: "Maximum call stack size exceeded" and the keys went into the hidden instruction box (MN76)
  const pal=document.getElementById('palette');
  if(pal.style.display!=='flex'){{const a=document.activeElement;_palReturn=(a&&a!==document.body)?a:null;}}
  pal.style.display='flex';
  const inp=document.getElementById('palette-input');
  inp.value='';filterPalette('');
  inp.focus();  // synchronously: keys typed right after Ctrl+K went to the page behind (AG80)
}}
function closePalette(restore){{
  const pal=document.getElementById('palette');
  const wasOpen=pal.style.display!=='none';
  pal.style.display='none';
  if(restore&&wasOpen&&_palReturn&&_palReturn.isConnected)_palReturn.focus();  // a dismissed palette gives focus back; a run command owns focus
  if(wasOpen)_palReturn=null;
}}
// Every word of the query, in any order, anywhere in the label or hint; extra spaces ignored (AG80)
function _palMatch(c,q){{
  const words=String(q||'').toLowerCase().split(/\\s+/).filter(Boolean);
  const hay=(c.label+' '+c.hint).toLowerCase();
  return words.every(w=>hay.includes(w));
}}
// Focus stays inside the open palette (AG80): Tab and Shift+Tab move the highlighted command instead
document.addEventListener('focusin',ev=>{{
  const pal=document.getElementById('palette');
  if(pal&&pal.style.display!=='none'&&!pal.contains(ev.target))document.getElementById('palette-input').focus();
}});
function renderPalette(cmds){{
  const list=document.getElementById('palette-list');
  list.innerHTML=cmds.map((c,i)=>
    '<div class="palette-item'+(i===_palActive?' active':'')+'" onclick="execPalette('+i+')" data-idx="'+i+'">'
    +'<span>'+c.label+'</span><span class="k">'+c.hint+'</span></div>'
  ).join('');
}}
function filterPalette(q){{
  _palActive=0;
  const filtered=_PALETTE_CMDS.filter(c=>_palMatch(c,q)&&(c.hint!=='watchtower'||document.getElementById('tab-watchtower')));  // no command for a tab that is not installed (MN76)
  renderPalette(filtered);
  document.getElementById('palette-list')._filtered=filtered;
}}
function execPalette(i){{
  const list=document.getElementById('palette-list');
  const filtered=list._filtered||_PALETTE_CMDS;
  if(!filtered[i]){{closePalette(true);return;}}  // Enter with no match closed the palette and dropped focus on the page body (MN76)
  closePalette();
  filtered[i].action();
  setTimeout(()=>{{if(document.activeElement===document.body){{const t=document.querySelector('#tabbar .tab[aria-selected="true"]');if(t)t.focus();}}}},250);  // a tab command left focus nowhere (MN76)
}}
function palettKey(e){{
  const list=document.getElementById('palette-list');
  const filtered=list._filtered||_PALETTE_CMDS;
  const items=list.querySelectorAll('.palette-item');
  if(e.key==='Tab'){{
    e.preventDefault();
    if(!filtered.length)return;
    _palActive=e.shiftKey?(_palActive-1+filtered.length)%filtered.length:(_palActive+1)%filtered.length;
    items.forEach((el,i)=>el.classList.toggle('active',i===_palActive));
    if(items[_palActive])items[_palActive].scrollIntoView({{block:'nearest'}});
  }}else if(e.key==='ArrowDown'){{
    _palActive=Math.min(_palActive+1,filtered.length-1);
    items.forEach((el,i)=>el.classList.toggle('active',i===_palActive));
    if(items[_palActive])items[_palActive].scrollIntoView({{block:'nearest'}});
    e.preventDefault();
  }}else if(e.key==='ArrowUp'){{
    _palActive=Math.max(_palActive-1,0);
    items.forEach((el,i)=>el.classList.toggle('active',i===_palActive));
    if(items[_palActive])items[_palActive].scrollIntoView({{block:'nearest'}});
    e.preventDefault();
  }}else if(e.key==='Enter'){{
    execPalette(_palActive);
    e.preventDefault();
  }}else if(e.key==='Escape'){{
    closePalette(true);e.stopPropagation();
  }}
}}
</script>
</body></html>"""


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


_WATCHTOWER_TAB_RE = re.compile(r'\s*<div class="tab" data-tab="watchtower"[^>]*>Watchtower</div>')
_WATCHTOWER_PANEL_RE = re.compile(
    r'\n<!-- WATCHTOWER TAB \(optional SIEM\) -->\n<div id="tab-watchtower".*?\n</div>\n', re.S  # ends at the first column-0 </div>; inner closings are indented
)


def _html(key: str) -> str:
    """Dashboard shell. Optional panels whose backing module is not installed are removed
    from the markup, so a stranger never sees a permanently dead tab (audit 2026-11-09)."""
    page = _html_full(key)
    if watchtower is None:
        page = _WATCHTOWER_TAB_RE.sub("", page, count=1)
        page = _WATCHTOWER_PANEL_RE.sub("\n", page, count=1)
    return page

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    """GET / -- serve the single-page Atlas Web UI (HTML shell + HTMX + TailwindCSS CDN)."""
    _check_auth(request)
    # The page never carries the API key (audit 2026-11-09, row 13): every
    # in-page request authenticates with the HttpOnly session cookie.
    resp = HTMLResponse(_html(""))
    if API_KEY and not _session_valid(request.cookies.get(_AUTH_COOKIE, "")):
        # HttpOnly: page JS never reads it; Strict: never sent cross-site.
        # The value is a random session token, not the API key.
        resp.set_cookie(
            _AUTH_COOKIE, _new_session(), httponly=True, samesite="strict",
            max_age=_SESSION_MAX_AGE,
            # Secure is opt-in: the default bind is plain-HTTP loopback where Secure would
            # silently break the UI. Behind TLS set ATLAS_WEB_SECURE_COOKIE=on.
            secure=os.environ.get("ATLAS_WEB_SECURE_COOKIE", "").lower() in ("on", "1", "true"),
        )
    return resp


@app.get("/health")
async def health():
    """Public no-auth health check. Returns 200 as long as the Web UI process is alive."""
    return {"status": "ok", "version": _PKG_VERSION}


@app.get("/api/workers")
async def workers_proxy(request: Request):
    """Proxy GET /workers from Brain server. Shows worker health assessment."""
    _check_auth(request)
    try:
        async with _PeerClient(timeout=httpx.Timeout(5.0, connect=0.75)) as client:
            r = await client.get(f"{BRAIN_URL}/workers", headers=_brain_headers)
        return r.json()
    except Exception as exc:
        return {"assessment": "unknown", "reason": str(exc)}


@app.post("/api/inbox/conversation")
async def inbox_conversation(request: Request):
    """Sextant Clip inbox: accept a captured web-chat conversation.

    The browser extension (or ANY tool that can POST JSON) sends
    ``{"title": ..., "source": ..., "messages": [{"role", "content"}, ...]}``;
    the conversation lands in the transcript drop directory and is ingested
    immediately -- same pipeline, same archive-not-delete behavior as
    ``sextant-transcripts ingest``. Auth is header-only by design (POSTs
    never accept the cookie), so a malicious page cannot CSRF content into
    your corpus; the extension sends the bearer key it stores locally.
    """
    _check_auth(request, header_only=True)
    raw = await request.body()
    if len(raw) > 2_000_000:
        raise HTTPException(status_code=413, detail="Conversation too large (2 MB cap).")
    if _too_deep(raw):
        raise HTTPException(status_code=400, detail="Body nesting too deep.")
    try:
        body = json.loads(raw)
    except (ValueError, RecursionError):  # 100k nested brackets is a RecursionError, not a ValueError
        raise HTTPException(status_code=400, detail="Body is not valid JSON.")
    # Valid JSON is not yet a valid request: `null`, `[]`, `42` or `"x"` 500'd on .get
    # (wave-2 2026-11-09, SEC-03). Every shape error is a bounded 400.
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Body must be a JSON object.")
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        raise HTTPException(status_code=400, detail="'messages' must be a non-empty list.")
    if len(messages) > 2000:
        raise HTTPException(status_code=413, detail="Too many messages (2000 cap).")
    if not all(isinstance(m, dict) and isinstance(m.get("content"), str) for m in messages):
        raise HTTPException(status_code=400, detail="Every message must be an object with a string content and a role.")

    import re as _re

    def _slug(s: str, fallback: str) -> str:
        s = _re.sub(r"[^a-z0-9]+", "-", str(s).lower()).strip("-")[:60]
        return s or fallback

    from datetime import datetime as _dt, timezone as _tz
    from uuid import uuid4 as _uuid4

    source = _slug(body.get("source", ""), "web")
    title = _slug(body.get("title", ""), "chat")
    # Timestamp alone collided: concurrent captures of the same page in the
    # same second overwrote each other's file and 500'd (deep-pass probe,
    # 2026-09-27). The uuid suffix makes every capture's path unique.
    stamp = f"{_dt.now(_tz.utc).strftime('%Y%m%d-%H%M%S')}-{_uuid4().hex[:8]}"

    from atlas_plus.transcripts.sources import drop as _drop

    def _write_and_ingest() -> dict:
        drop_root = _drop.drop_dir()
        drop_root.mkdir(parents=True, exist_ok=True)
        path = drop_root / f"{source}-{title}-{stamp}.jsonl"
        lines = []
        for m in messages:
            if not isinstance(m, dict):
                continue
            lines.append(json.dumps(
                {k: m[k] for k in ("role", "content", "ts", "model") if k in m},
                ensure_ascii=False,
            ))
        payload = "\n".join(lines) + "\n"
        # The same capture sent twice (double click) must not become two conversations
        # (persona rerun 2026-11-10): compare with what this source+title already ingested.
        import hashlib as _hl
        digest = _hl.sha256(payload.encode("utf-8")).hexdigest()
        archived = drop_root / "_ingested"
        if archived.is_dir():
            for prior in sorted(archived.glob(f"{source}-{title}-*.jsonl"), reverse=True)[:50]:
                try:
                    if _hl.sha256(prior.read_bytes()).hexdigest() == digest:
                        return {"files": 0, "turns": len(lines), "skipped": 0, "duplicate": True,
                                "conversation_id": prior.name.split(".")[0]}
                except OSError:
                    continue
        path.write_bytes(payload.encode("utf-8"))  # the bytes that were hashed (CRLF-proof)
        result = _drop.ingest(path)
        result["conversation_id"] = path.stem
        return result

    result = await asyncio.to_thread(_write_and_ingest)
    if result.get("duplicate"):
        return {"ok": True, **result}
    # Paths in the answer are relative to the state dir: a browser extension has no business
    # learning the machine's directory layout (claims pass 2026-11-10).
    _state = str(atlas_paths.state_dir())
    for _k, _v in list(result.items()):
        if isinstance(_v, str) and _v and os.path.normcase(_v).startswith(os.path.normcase(_state)):
            result[_k] = os.path.relpath(_v, _state).replace("\\", "/")
    if result.get("turns", 0) == 0:
        raise HTTPException(
            status_code=422,
            detail="No ingestable turns (each message needs a valid role and content).",
        )
    return {"ok": True, **result}


@app.get("/api/spend")
async def spend_proxy(request: Request, days: int = 7):
    """Proxy GET /spend from Brain: token totals + estimated cost (spend v2)."""
    _check_auth(request)
    try:
        async with _PeerClient(timeout=httpx.Timeout(5.0, connect=0.75)) as client:
            r = await client.get(f"{BRAIN_URL}/spend", params={"days": days}, headers=_brain_headers)
        return r.json()
    except Exception as exc:
        return {"available": False, "reason": str(exc)}


@app.get("/status")
async def public_status():
    """Public no-auth status page.

    Returns lightweight service reachability without exposing internal details.
    Safe for Cloudflare health checks and external uptime monitors.
    """
    import time

    services: dict[str, str] = {}
    start = time.monotonic()

    async def _probe(name: str, url: str, headers: dict | None = None) -> tuple[str, str]:
        try:
            async with _PeerClient(timeout=httpx.Timeout(2.0, connect=0.75)) as client:
                r = await client.get(url, headers=headers or {})
            # 429 is a locked-out source, not a healthy service (claims pass 2026-11-10)
            return name, "degraded" if (r.status_code >= 500 or r.status_code == 429) else "ok"
        except Exception:
            return name, "down"

    import asyncio as _aio
    results = await _aio.gather(
        _probe("brain", f"{BRAIN_URL}/health"),
        _probe("mcp", f"http://127.0.0.1:{os.environ.get('ATLAS_MCP_PORT', '8765')}/health", _mcp_headers),
        return_exceptions=True,
    )
    for item in results:
        if isinstance(item, tuple):
            services[item[0]] = item[1]
        else:
            pass  # probe exception already caught inside _probe

    overall = "ok" if all(v == "ok" for v in services.values()) else "degraded"
    elapsed_ms = round((time.monotonic() - start) * 1000)

    return {
        "status": overall,
        "version": _PKG_VERSION,
        "services": services,
        "checked_in_ms": elapsed_ms,
    }


@app.post("/stop")
async def stop(request: Request):
    """POST /stop -- graceful shutdown. Sends SIGTERM to the process after 500ms."""
    _check_auth(request)
    log.info("Shutdown requested via /stop")
    asyncio.get_event_loop().call_later(0.5, lambda: os.kill(os.getpid(), signal.SIGTERM))
    return {"status": "shutting down"}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _port_free(host: str, port: int) -> bool:
    """True when a listening socket can be bound there right now (checked before uvicorn starts: MN54)."""
    import socket
    try:
        with socket.socket(socket.AF_INET6 if ":" in host else socket.AF_INET, socket.SOCK_STREAM) as s:
            if os.name != "nt":
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)  # on Windows the option binds beside a live listener (AG61)
            elif hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)  # a listener on 0.0.0.0 was missed (MN65)
            s.bind((host, port))
        return True
    except OSError:
        return False


_START_BANNER = ""  # set by `sextant dashboard`; printed once the start checks pass (AG90)


def open_line(host: str, port: int, keyed: bool = True) -> str:
    """Where to open the dashboard: the address it really binds (ATLAS_WEB_HOST/ATLAS_WEB_PORT, not
    the default) and how the key is given; never the key itself (SEX-050 item 9)."""
    shown = {"": "127.0.0.1", "0.0.0.0": "127.0.0.1", "::": "::1"}.get(host, host)  # a wildcard: open loopback
    if ":" in shown and not shown.startswith("["):
        shown = f"[{shown}]"  # an IPv6 literal needs brackets in a URL
    url = f"http://{shown}:{port}/"
    if not keyed:
        return f"Open {url} in your browser (--no-auth: no key needed, loopback only)."
    return (f"Open {url}?key=<your ATLAS_WEB_API_KEY> in your browser once (the key becomes a sign-in "
            f"cookie); scripts send the header `Authorization: Bearer <key>`.")


def main() -> None:
    """Entry point for `python -m atlas_plus.web.app`.

    Starts the Atlas Web UI on :8766. Flags:
      --no-auth  Allow unauthenticated access (loopback-only bind enforced).
    Auth is controlled by ATLAS_WEB_API_KEY (bearer header, or the one-time ?key= bootstrap
    that becomes a session cookie).
    The SSE task-poll loop starts via FastAPI lifespan.
    """
    import argparse
    import uvicorn
    global _ALLOW_NO_AUTH

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
    )

    from atlas_plus.branding import product
    if any(a == "--port" or a.startswith("--port=") or a == "--host" or a.startswith("--host=") for a in sys.argv[1:]):
        print("--port and --host are not options here: set ATLAS_WEB_PORT=<n> and ATLAS_WEB_HOST=<addr> in the environment (GETTING-STARTED, Troubleshooting).", file=sys.stderr)  # MN60
        raise SystemExit(2)
    from atlas_plus import _next_step  # usage errors name --help (LES-054)
    parser = _next_step.ArgumentParser(description=f"{product()} Web UI (Helm dashboard)")
    parser.add_argument("--no-auth", action="store_true", help="Local dev, loopback only")
    args = parser.parse_args()

    if args.no_auth:
        if WEB_HOST not in ("127.0.0.1", "localhost", "::1"):
            print("--no-auth requires loopback bind.", file=__import__("sys").stderr)
            raise SystemExit(1)
        _ALLOW_NO_AUTH = True
        from starlette.middleware.trustedhost import TrustedHostMiddleware
        # Without a key, a DNS-rebinding page could drive the API; pin the Host header
        # to loopback names (round-B re-audit 2026-11-09, N-N).
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "[::1]", "::1"])
        log.warning("--no-auth active. Loopback only.")

    if API_KEY and len(API_KEY) < _next_step.KEY_MIN_LEN:
        # Refuse, not warn: the docs say the dashboard will not run without a real key, and a
        # 5-character one is not a key (persona rerun 2026-11-10).
        print(_next_step.key_setup_lines("ATLAS_WEB_API_KEY", headline=f"ATLAS_WEB_API_KEY is only {len(API_KEY)} "
                                         f"characters; refusing to start (it needs at least {_next_step.KEY_MIN_LEN})"),
              file=__import__("sys").stderr)  # the same copyable lines as a missing key (AG106)
        raise SystemExit(1)
    # httpx logs every Brain poll at INFO (a 401 every few seconds when the Brain key is
    # wrong or the Brain is not running); the poll loop reports that once, with a hint.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    if not API_KEY and not _ALLOW_NO_AUTH:
        print(
            _next_step.key_setup_lines("ATLAS_WEB_API_KEY") + "\n"
            'Or use --no-auth for local dev (loopback only). More: docs/GETTING-STARTED.md, section 6.',
            file=__import__("sys").stderr,
        )
        raise SystemExit(1)

    from atlas_plus import __version__ as _pkg_version
    from atlas_plus.branding import product as _product
    if not _port_free(WEB_HOST, WEB_PORT):
        print(f"Port {WEB_PORT} is already in use on {WEB_HOST}: another dashboard, or another program. Set ATLAS_WEB_PORT to a free port "
              f"(GETTING-STARTED, Troubleshooting: port already in use).", file=sys.stderr)  # the raw uvicorn bind error named nothing (MN54)
        raise SystemExit(3)
    if _START_BANNER:
        print(_START_BANNER, flush=True)
    log.info("%s web dashboard %s on %s:%d", _product(), _pkg_version, WEB_HOST, WEB_PORT)
    print(open_line(WEB_HOST, WEB_PORT, keyed=not _ALLOW_NO_AUTH), flush=True)  # SEX-050 item 9
    # access_log=False: uvicorn's access log records the path WITH its query
    # string, which put the ?key= bootstrap into service logs (audit
    # 2026-11-09, row 13).
    # The app OBJECT, not an import string: `python -m atlas_plus.web.app --no-auth` imports
    # a second module instance whose _ALLOW_NO_AUTH is still False and every route 503s
    # (wave-2 2026-11-09, SEC-02). The console script and this path serve the same instance.
    uvicorn.run(app, host=WEB_HOST, port=WEB_PORT, reload=False, access_log=False, proxy_headers=False)


if __name__ == "__main__":
    main()
