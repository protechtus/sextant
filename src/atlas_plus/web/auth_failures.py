"""Compat shim: auth-failure tracking moved to ``atlas_plus.auth``.

The classes lived here from ATL-014 (2026-05-28 tunnel incident) until
the auth consolidation (ROADMAP item 6, 2026-09-24). Import from
``atlas_plus.auth`` in new code; this module keeps existing imports and
tests working unchanged.
"""

from atlas_plus.auth import (  # noqa: F401
    DEFAULT_MAX_FAILURES_PER_WINDOW,
    DEFAULT_MAX_RECORDS,
    DEFAULT_WINDOW_SECONDS,
    AuthFailure,
    AuthFailureLog,
)

__all__ = [
    "AuthFailure",
    "AuthFailureLog",
    "DEFAULT_MAX_RECORDS",
    "DEFAULT_WINDOW_SECONDS",
    "DEFAULT_MAX_FAILURES_PER_WINDOW",
]
