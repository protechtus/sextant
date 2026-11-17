"""One answer to "where is Ollama": ATLAS_BRAIN_OLLAMA_URL, else the standard OLLAMA_HOST
(host:port or a URL, as the Ollama CLI reads it), else the local default."""
from __future__ import annotations

import os

DEFAULT = "http://localhost:11434"


def default_ollama_url() -> str:
    explicit = os.environ.get("ATLAS_BRAIN_OLLAMA_URL", "").strip()
    if explicit:
        return explicit.rstrip("/")
    # OLLAMA_BASE_URL is what the dashboard's Map tab reads and what the docs named for the
    # Brain too (self-hoster pass 2026-11-10): honour it before the Ollama CLI's OLLAMA_HOST.
    base = os.environ.get("OLLAMA_BASE_URL", "").strip()
    if base:
        return base.rstrip("/")
    host = os.environ.get("OLLAMA_HOST", "").strip()
    if host:
        if "://" not in host:
            host = "http://" + host
        return host.rstrip("/")
    return DEFAULT


def loopback_refused(url: str, timeout: float = 0.3) -> bool:
    """True only when ``url`` names a loopback host (``localhost``, 127.0.0.0/8, ``::1``) and no address of it
    accepts a TCP connect within ``timeout``. A refused loopback connect is not refused at once on Windows: the
    SYN is retried for about 2 s, so an HTTP probe with a 1.5 s timeout spent 1.5 s per address, 3.0 s for
    ``localhost`` (::1 then 127.0.0.1), on every Stop hook of a machine without Ollama (MN87). A listening
    loopback port accepts in well under a millisecond. Anything else (a remote host, a bad URL, a name that does
    not resolve) is False: the caller probes as before."""
    import ipaddress
    import socket
    from urllib.parse import urlsplit
    try:
        u = urlsplit(url)
        host = u.hostname
        port = u.port or (443 if u.scheme == "https" else 80)
    except ValueError:
        return False
    if not host:
        return False
    if host.lower() != "localhost":
        try:
            if not ipaddress.ip_address(host).is_loopback:
                return False
        except ValueError:
            return False
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError:
        return False
    for family, kind, proto, _canon, addr in infos:
        try:
            sock = socket.socket(family, kind, proto)
        except OSError:
            continue
        try:
            sock.settimeout(timeout)
            sock.connect(addr)
            return False  # something listens: let the caller ask it
        except OSError:
            continue
        finally:
            sock.close()
    return bool(infos)
