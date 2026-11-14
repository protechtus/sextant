"""Shared auth primitives for every Sextant network surface (ROADMAP item 6).

The four services (web dashboard, Brain API, MCP server, sync hub) grew
auth behavior independently: each had its own token compare, its own
throttle shape, its own failure bookkeeping. The behaviors were all
reasonable -- the duplication was the risk (a fix in one place silently
not applied in another). This module is the single home for the
primitives; each service keeps its own POLICY (what to throttle, which
sources to accept) and composes it from these parts.

Contents:
- ``constant_time_equals``      -- the one true token compare
- ``bearer_token``              -- Authorization-header parsing
- ``SlidingWindowRateLimiter``  -- per-identity request throttle
  (monotonic clock: immune to NTP/DST jumps; lifted from the Brain)
- ``AuthFailure`` / ``AuthFailureLog`` -- bounded failure record +
  failure-count throttle (lifted from the web dashboard, ATL-014)

Not consolidated (deliberately): the MCP server's lockout thresholds and
the sync hub's hashed device-token store are policy, not primitives --
they now build on ``constant_time_equals`` but keep their own shapes.
"""

from __future__ import annotations

import hmac
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass
from datetime import datetime, timezone

DEFAULT_MAX_RECORDS = 500
DEFAULT_WINDOW_SECONDS = 60.0
DEFAULT_MAX_FAILURES_PER_WINDOW = 10


def constant_time_equals(candidate: str, expected: str) -> bool:
    """Timing-safe token comparison. Empty expected never matches."""
    if not expected:
        return False
    # Compare UTF-8 bytes: compare_digest(str, str) raises TypeError for a
    # non-ASCII str, which turned a junk token into a 500 instead of a
    # recorded, throttled 401 (audit 2026-11-09, row 30).
    try:
        return hmac.compare_digest(
            candidate.encode("utf-8", "surrogatepass"), expected.encode("utf-8", "surrogatepass")
        )
    except (AttributeError, UnicodeError):
        return False


def bearer_token(authorization_header: str) -> str:
    """Extract the token from an ``Authorization: Bearer ...`` value ('' if absent).

    The scheme is case-insensitive (RFC 9110 section 11.1); proxies and some clients
    send ``bearer``. The token itself is compared byte for byte by the caller."""
    if authorization_header[:7].lower() == "bearer ":
        return authorization_header[7:].strip()
    return ""


class SlidingWindowRateLimiter:
    """Per-identity sliding-window throttle on the monotonic clock.

    ``allow(identity)`` returns ``(allowed, retry_after_secs)``.
    ``limit <= 0`` disables the limiter (always allowed).
    """

    def __init__(self, limit: int, window_seconds: float = 60.0, *, now_fn=time.monotonic) -> None:
        self.limit = limit
        self.window_seconds = window_seconds
        self._now = now_fn
        self._buckets: dict[str, deque] = {}
        self._lock = threading.Lock()

    # Bucket-count ceiling: identities can be caller-supplied strings
    # (X-Atlas-Caller-Identity), so an authenticated-but-hostile client
    # could otherwise grow the dict without bound (expert review
    # 2026-09-27). At the ceiling, expired buckets are swept.
    _MAX_BUCKETS = 10_000

    def allow(self, identity: str) -> tuple[bool, float]:
        if self.limit <= 0:
            return True, 0.0
        now = self._now()
        cutoff = now - self.window_seconds
        with self._lock:
            if identity not in self._buckets and len(self._buckets) >= self._MAX_BUCKETS:
                for key in [k for k, q in self._buckets.items() if not q or q[-1] < cutoff]:
                    del self._buckets[key]
            q = self._buckets.setdefault(identity, deque())
            while q and q[0] < cutoff:
                q.popleft()
            if len(q) >= self.limit:
                retry_after = q[0] + self.window_seconds - now
                return False, max(0.1, retry_after)
            q.append(now)
        return True, 0.0


@dataclass(frozen=True)
class AuthFailure:
    """A single rejected request."""

    ts: float        # wall-clock epoch seconds
    path: str
    client_ip: str   # socket peer (often the tunnel/loopback proxy)
    cf_ip: str       # Cf-Connecting-Ip header value, "" if absent
    reason: str      # "missing_key" | "bad_key" | "throttled" | ...

    def as_dict(self) -> dict:
        d = asdict(self)
        d["iso"] = datetime.fromtimestamp(self.ts, tz=timezone.utc).isoformat()
        # The IP most useful for an operator: real source if known, else peer.
        d["source_ip"] = self.cf_ip or self.client_ip or "unknown"
        return d


class AuthFailureLog:
    """Bounded, thread-safe record of auth failures with a sliding-window throttle."""

    def __init__(
        self,
        *,
        max_records: int = DEFAULT_MAX_RECORDS,
        window_seconds: float = DEFAULT_WINDOW_SECONDS,
        max_failures_per_window: int = DEFAULT_MAX_FAILURES_PER_WINDOW,
    ) -> None:
        self._records: deque[AuthFailure] = deque(maxlen=max_records)
        # refusals while locked are counted per source here, never in the deque: a flood of
        # them evicted the ten real failures and lifted the lock (AG38, sweep 15)
        self._refused: dict[str, list] = {}
        self._lock = threading.Lock()
        self.window_seconds = window_seconds
        self.max_failures_per_window = max_failures_per_window

    def record(
        self,
        *,
        path: str,
        client_ip: str = "",
        cf_ip: str = "",
        reason: str = "",
        now: float | None = None,
    ) -> AuthFailure:
        """Append a failure record and return it."""
        ts = time.time() if now is None else now
        rec = AuthFailure(ts=ts, path=path, client_ip=client_ip, cf_ip=cf_ip, reason=reason)
        with self._lock:
            if reason == "locked_out":
                key = cf_ip or client_ip or "unknown"
                row = self._refused.get(key)
                if row is None:
                    if len(self._refused) >= 256:
                        oldest = min(self._refused, key=lambda k: self._refused[k][1])
                        self._refused.pop(oldest, None)
                    self._refused[key] = [0, ts]
                    row = self._refused[key]
                row[0] += 1
                row[1] = ts
                return rec
            self._records.append(rec)
        return rec

    def recent(self, limit: int = 50) -> list[AuthFailure]:
        """Return up to ``limit`` most-recent failures, newest first."""
        with self._lock:
            records = list(self._records)
        return records[-limit:][::-1]

    def failures_from(self, ip: str, *, now: float | None = None) -> int:
        """Count failures attributed to ``ip`` within the sliding window."""
        if not ip:
            return 0
        now = time.time() if now is None else now
        cutoff = now - self.window_seconds
        with self._lock:
            return sum(
                1
                for r in self._records
                if r.ts >= cutoff and (r.cf_ip or r.client_ip) == ip and r.reason != "locked_out"  # a failure belongs to its forwarded client only: the proxy's other clients inherited it (AG47); informational rows never extend a lockout (MN29)
            )

    def failures_via_peer(self, peer: str, *, now: float | None = None) -> int:
        """Failures that arrived through ``peer`` whatever client it named: a trusted proxy whose
        clients rotate forged identities is itself the source (AG44: unlimited guessing)."""
        if not peer:
            return 0
        now = time.time() if now is None else now
        cutoff = now - self.window_seconds
        with self._lock:
            return sum(1 for r in self._records if r.ts >= cutoff and r.client_ip == peer and r.reason != "locked_out")

    def is_rate_limited(self, ip: str, *, now: float | None = None) -> bool:
        """True once ``ip`` has reached the failure threshold within the window."""
        if not ip:
            return False
        return self.failures_from(ip, now=now) >= self.max_failures_per_window

    def stats(self, *, now: float | None = None) -> dict:
        """Summary for the operator panel."""
        now = time.time() if now is None else now
        cutoff = now - self.window_seconds
        with self._lock:
            records = list(self._records)
        in_window = [r for r in records if r.ts >= cutoff]
        sources = {(r.cf_ip or r.client_ip or "unknown") for r in in_window}
        return {
            "total": len(records),
            "in_window": len(in_window),
            "window_seconds": self.window_seconds,
            "distinct_sources_in_window": len(sources),
            "last_failure_iso": records[-1].as_dict()["iso"] if records else None,
            "locked_out_refusals": {k: v[0] for k, v in sorted(self._refused.items(), key=lambda kv: -kv[1][1])[:20]},
        }

    def clear(self) -> None:
        """Drop all records (used by tests)."""
        with self._lock:
            self._refused.clear()
        with self._lock:
            self._records.clear()


LOOPBACK_PEERS = frozenset({"127.0.0.1", "::1", "localhost"})


def trusted_peer_matcher(raw: str):
    """``trusted`` callable for ``forwarded_ip`` from a comma-separated list of IPs or CIDRs (the
    MCP server's ATLAS_MCP_TRUSTED_PEERS rule, shared with the dashboard: MN35, behind compose the
    dashboard saw every client as the Docker gateway and one bad client locked the operator out)."""
    entries = [e.strip() for e in (raw or "").split(",") if e.strip()]

    def _trusted(peer: str) -> bool:
        if not entries or not peer:
            return False
        import ipaddress
        try:
            addr = ipaddress.ip_address(peer.split("%", 1)[0])  # fe80::1%eth0 (AG44)
            if addr.version == 6 and addr.ipv4_mapped is not None:
                addr = addr.ipv4_mapped  # ::ffff:172.17.0.1 on a dual-stack bind (AG44)
        except ValueError:
            return False
        for entry in entries:
            try:
                if addr in ipaddress.ip_network(entry, strict=False):
                    return True
            except ValueError:
                continue
        return False
    return _trusted


def forwarded_ip(headers, peer: str, trusted=None) -> str:
    """The real client address a trusted local proxy reports, or '' when none applies.

    Trusted ONLY when the socket peer is loopback (the tunnel or reverse proxy runs on this
    host): a direct client could otherwise rotate the header to dodge the throttle or to lock
    someone else out. The header is ``ATLAS_FORWARDED_IP_HEADER`` (default
    ``Cf-Connecting-Ip``), then the LAST non-loopback hop of ``X-Forwarded-For`` (the one the
    proxy on this host appended; an appending proxy leaves the first hop client-chosen). Set
    the variable to ``none`` to ignore forwarded headers entirely. One definition for every
    network surface, and the ONLY one: the servers start uvicorn with proxy_headers=False
    so its middleware cannot rewrite the peer first (self-hoster passes 2026-11-10).
    """
    import os as _os
    # ``trusted`` (a callable peer -> bool) extends the trust to proxies the operator listed
    # (ATLAS_MCP_TRUSTED_PEERS): a proxy that is not on loopback can then name the real client.
    if peer not in LOOPBACK_PEERS and not (trusted is not None and trusted(peer)):
        return ""
    name = (_os.environ.get("ATLAS_FORWARDED_IP_HEADER") or "Cf-Connecting-Ip").strip()
    if name.lower() == "none":
        return ""
    value = (headers.get(name) or "").strip()
    if value:
        return _valid_ip(value.split(",")[0].strip())
    xff = (headers.get("X-Forwarded-For") or "").strip()
    if not xff:
        return ""
    hops = [h.strip() for h in xff.split(",") if h.strip()]
    for hop in reversed(hops):
        if hop not in LOOPBACK_PEERS:
            return _valid_ip(hop)
    return ""


def _valid_ip(value: str) -> str:
    """The value as a normalised IP address; a port suffix (`1.2.3.4:5678`, `[::1]:443`) is dropped.
    Anything else becomes its own BOUNDED source `bad:<40 chars>` (AG44: a 5,000-character value was
    an identity; MN37: an empty result switched the loopback exemption on and the throttle off)."""
    import ipaddress
    import re as _re
    v = value.strip()
    m = _re.match(r"^(\d{1,3}(?:\.\d{1,3}){3}):\d{1,5}$", v) or _re.match(r"^\[([0-9A-Fa-f:.]+)\](?::\d{1,5})?$", v)  # `[::1]` with or without a port (AG49)
    if m:
        v = m.group(1)
    try:
        return str(ipaddress.ip_address(v.split("%", 1)[0]))
    except ValueError:
        return "bad:" + _re.sub(r"[^\x21-\x7e]", "?", v)[:40]
