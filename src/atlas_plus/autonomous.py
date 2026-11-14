"""X-T6: kill switch for autonomous writes.

FAIL-CLOSED (hardened 2026-05-28, ATL-013): the default when ATLAS_AUTONOMOUS is
UNSET is now "off" -- autonomous writes are blocked unless explicitly enabled with
ATLAS_AUTONOMOUS=on. This prevents a process that starts without the env file
loaded (e.g. mid-incident or a stray script) from silently writing to .memory/.
A production deployment must set ATLAS_AUTONOMOUS=on explicitly;
the test suite opts in via an autouse fixture in tests/conftest.py.

Set ATLAS_AUTONOMOUS=off (or leave unset) to prevent any Atlas component from
writing to .memory/ or updating files. Capture (transcripts) continues regardless.

ATLAS_SESSION_AUTONOMOUS is a scoped override for session-end hooks only
(summarizer + suggest-facts). Set it to "on" to allow those writes while
keeping ATLAS_AUTONOMOUS=off for Brain, tickets, and manual remember calls.

Resolution order:
  1. ATLAS_SESSION_AUTONOMOUS=on  -> session writes allowed regardless of ATLAS_AUTONOMOUS
  2. ATLAS_SESSION_AUTONOMOUS=off -> session writes blocked regardless of ATLAS_AUTONOMOUS
  3. (not set)                    -> fall back to ATLAS_AUTONOMOUS
"""

import os


# The gate is an ALLOWLIST: only these values enable writes. The original check blocked
# a denylist (off/0/false), which meant any OTHER value -- a typo like "of"/"onn", a stray
# "yes", a corrupted env -- silently failed OPEN, inverting the ATL-013 fail-closed intent
# stated above (fixed 2026-07-02; every real config uses exactly on/off/0/1).
_ENABLED_VALUES = ("on", "1", "true")

# Interactive writes (2026-08-26, the every-day-man review's launch blocker):
# a user explicitly saying "remember this" in an authenticated MCP session IS
# the consent -- it is not "autonomous", and gating it behind ATLAS_AUTONOMOUS
# made the product's first advertised interaction fail on every fresh install.
# Interactive writes are therefore allowed by default; ATLAS_INTERACTIVE_WRITES=off
# restores the old fully-gated behavior for paranoid deployments. This is a
# DENYLIST on purpose (opposite of the gate above): the feature is default-on,
# each write is an explicit user action, and a typo'd value should not silently
# brick the primary interaction.
_INTERACTIVE_OFF_VALUES = ("off", "0", "false")


def interactive_writes_enabled() -> bool:
    """True unless ATLAS_INTERACTIVE_WRITES explicitly disables user-initiated writes."""
    return os.environ.get("ATLAS_INTERACTIVE_WRITES", "").strip().lower() not in _INTERACTIVE_OFF_VALUES


def autonomous_enabled() -> bool:
    """THE canonical boolean for the background-write gate (allowlist,
    fail-closed). Every predicate/status call site must use this -- direct
    env checks drifted into denylist semantics and a helper wrote on
    ATLAS_AUTONOMOUS=yes (Codex audit, 2026-09-21)."""
    return os.environ.get("ATLAS_AUTONOMOUS", "off").strip().lower() in _ENABLED_VALUES


def require_write(source: str, interactive: bool = False) -> None:
    """Gate a memory write: interactive user actions pass by default;
    everything else falls through to the autonomous kill switch."""
    if interactive and interactive_writes_enabled():
        return
    if interactive:
        raise RuntimeError(
            f"Atlas {source} blocked: ATLAS_INTERACTIVE_WRITES is off. "
            "Set ATLAS_INTERACTIVE_WRITES=on (the default) to allow writes you ask for."
        )
    require_autonomous_writes(source)


def require_autonomous_writes(operation: str = "write") -> None:
    """Raise RuntimeError unless autonomous writes are EXPLICITLY enabled (fail-closed)."""
    value = os.environ.get("ATLAS_AUTONOMOUS", "off").lower()
    if os.environ.get("ATLAS_SESSION_AUTONOMOUS", "").strip().lower() in _ENABLED_VALUES:
        return  # the Stop hook's scoped override (rule 1 above): suggest-facts wrote nothing under it (AG40)
    if value not in _ENABLED_VALUES:
        raise RuntimeError(
            f"Sextant autonomous {operation} blocked: ATLAS_AUTONOMOUS={value!r}. "
            "Set ATLAS_AUTONOMOUS=on in the writer's own environment (an MCP registration's env block, "
            "a hook command's --env flag) to enable unattended writes."
        )


def is_session_autonomous() -> bool:
    """Return True if session-hook writes (summarizer, suggest-facts) are allowed.

    Checks ATLAS_SESSION_AUTONOMOUS first; falls back to ATLAS_AUTONOMOUS.
    """
    session_val = os.environ.get("ATLAS_SESSION_AUTONOMOUS", "").strip().lower()  # `--env ATLAS_SESSION_AUTONOMOUS=' on '` (MN39)
    if session_val in _ENABLED_VALUES:
        return True
    if session_val in ("off", "0", "false"):
        return False
    # not set (or garbage) -- fall back to the general gate (same fail-closed allowlist).
    return os.environ.get("ATLAS_AUTONOMOUS", "off").lower() in _ENABLED_VALUES
