"""Central path resolution for the Atlas workspace layout.

Every module used to hardcode absolute fallbacks for the machine it was
written on, which breaks any other install. All path defaults now resolve here:

1. The specific env var (``ATLAS_MEMORY_ROOT``, ``ATLAS_STATE_DIR``,
   ``ATLAS_PROJECTS_ROOT``, ``ATLAS_UPDATES_FILE``) always wins.
2. Otherwise paths derive from the workspace root: ``ATLAS_WORKSPACE_ROOT``
   env var if set, else the user's home directory. Nothing is autodetected.

So a machine with env vars set behaves exactly as before, and a fresh clone on a clean
machine resolves everything home-relative (``~/.memory``, ``~/.atlas``,
``~/projects``, ``~/updates.txt``).

Env vars are read at call time so tests can monkeypatch them; module-level
constants in callers still freeze values at import time, as they always did.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Impatient-persona finding (2026-09-30): every command the user types is
# sextant-*, so SEXTANT_MEMORY_ROOT is the natural wrong guess for the env
# namespace -- and it was silently ignored, landing data at the default path
# with no error to notice. Warn once per process, on stderr (stdout may be
# an MCP stdio channel).
_warned_twins: set[str] = set()


def _warn_sextant_twin(var: str) -> None:
    twin = var.replace("ATLAS_", "SEXTANT_", 1)
    if twin in _warned_twins:
        return
    if os.environ.get(twin, "").strip() and not os.environ.get(var, "").strip():
        _warned_twins.add(twin)
        # Guarded print (expert pass 2026-10-05, finding 9): with stderr
        # None (pythonw) print falls back to STDOUT -- prose into an MCP
        # stdio JSON-RPC stream -- and a broken pipe would raise out of
        # every path function. A lost warning is fine; a broken path
        # resolution is not.
        if sys.stderr is not None:
            try:
                print(
                    f"warning: {twin} is set but never read -- Sextant's "
                    f"engine uses the ATLAS_* namespace. Set {var} instead "
                    f"(see docs/CONFIGURATION.md).",
                    file=sys.stderr,
                )
            except Exception:
                pass

def workspace_root() -> Path:
    """Resolve the workspace root: ATLAS_WORKSPACE_ROOT, else the home directory.

    No other fallback: an autodetect of the maintainer's historical install folder shipped
    in the public build and silently adopted a stranger's directory of the same name
    (persona pass 2026-11-10, "Dana"). The docs promise home-relative defaults; that is
    what happens.
    """
    _warn_sextant_twin("ATLAS_WORKSPACE_ROOT")
    env = os.environ.get("ATLAS_WORKSPACE_ROOT", "").strip()
    if env.startswith("<") and env.endswith(">"):
        raise SystemExit(
            f"ATLAS_WORKSPACE_ROOT is set to {env!r}, which looks like an unfilled "
            f"placeholder from a config template. Replace it with a real path."
        )
    if env:
        expanded = os.path.expanduser(env)
        if not os.path.isabs(expanded):
            raise SystemExit(
                f"ATLAS_WORKSPACE_ROOT must be an absolute path (got {env!r}); relative paths "
                f"resolve against whichever directory the client happens to start in."
            )
        return Path(os.path.abspath(expanded))
    return Path.home()


def _env_path(var: str, default: Path) -> Path:
    _warn_sextant_twin(var)
    value = os.environ.get(var, "").strip()
    if value.startswith("<") and value.endswith(">"):
        # An unfilled template placeholder (e.g. pasted MCP config with
        # <ATLAS_MEMORY_ROOT> left literal) would otherwise surface much
        # later as a cryptic OSError on first file access. Fail at the
        # source, naming the exact fix (impatient-user review 2026-08-26).
        raise SystemExit(
            f"{var} is set to {value!r}, which looks like an unfilled "
            f"placeholder from a config template. Replace it with a real "
            f"path in your MCP client config or environment."
        )
    if value:
        expanded = os.path.expanduser(value)
        if not os.path.isabs(expanded):
            # A relative root silently made one corpus per launch directory (deep install
            # lanes 2026-11-09): an MCP client started from another folder found an empty corpus.
            raise SystemExit(
                f"{var} must be an absolute path (got {value!r}); relative paths resolve against "
                f"whichever directory the client happens to start in."
            )
        return Path(os.path.abspath(expanded))
    return default


def memory_root() -> Path:
    """The .memory/ knowledge base root."""
    return _env_path("ATLAS_MEMORY_ROOT", workspace_root() / ".memory")


def state_dir() -> Path:
    """The .atlas/ derived-state directory (indexes, logs, temporal graph)."""
    return _env_path("ATLAS_STATE_DIR", workspace_root() / ".atlas")


def projects_root() -> Path:
    """The directory that holds project working copies."""
    return _env_path("ATLAS_PROJECTS_ROOT", workspace_root() / "projects")


def updates_file() -> Path:
    """The legacy chronological session log (updates.txt).

    Defaults INSIDE the state dir so the Stop hook and the nightly job never
    prepend to (or version-copy beside) a file the user created themselves,
    such as ~/updates.txt. Set ATLAS_UPDATES_FILE to opt in to a file of your
    own."""
    return _env_path("ATLAS_UPDATES_FILE", state_dir() / "updates.txt")


def env_int(var: str, default: int) -> int:
    """An integer setting from the environment, or a clear refusal naming the variable
    (`ATLAS_WEB_PORT=abc` used to be a raw ValueError traceback; persona pass 2026-11-10)."""
    raw = os.environ.get(var, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise SystemExit(f"{var} must be a whole number (got {raw!r}).")
    if var.endswith("_PORT") and not (1 <= value <= 65535):
        raise SystemExit(f"{var} must be a TCP port between 1 and 65535 (got {value}).")
    return value
