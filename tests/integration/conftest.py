"""Integration test conftest: real-service tests are opt-in.

Tests under tests/integration/ marked @pytest.mark.ollama / @pytest.mark.claude
hit live services (Ollama, the Anthropic API) and can block for a long time on a
real model call. They are skipped by default so a plain ``pytest`` run never
hangs on them. Opt in with:

    pytest tests/integration -m ollama                 # the Ollama-backed tests
    pytest tests/integration -m claude                 # the Claude-backed tests (needs ANTHROPIC_API_KEY)
    ATLAS_RUN_INTEGRATION=1 pytest tests/integration   # all of them

Unmarked integration tests (the fact-persistence checks) are fast and run by
default. The previous version skipped the real-service tests only when *no* -m
expression was given, so an unrelated marker expression (e.g. ``-m "not slow"``)
would accidentally un-skip them and could hang on a real call. This version
gates on the specific marker instead, so the real-service tests run only when
their own marker is positively selected (or the env opt-in is set).
"""
from __future__ import annotations

import os
import re

import pytest

_REAL_SERVICE_MARKERS = ("ollama", "claude")


def _run_all_integration() -> bool:
    return os.environ.get("ATLAS_RUN_INTEGRATION", "").strip().lower() in ("1", "true", "yes", "on")


def _marker_requested(markexpr: str, name: str) -> bool:
    """True if ``name`` is positively selected in a -m expression (not negated).

    "ollama" / "ollama and not slow" -> requested; "not ollama" -> not requested;
    "" / "not slow" -> not requested.
    """
    if not markexpr or name not in markexpr:
        return False
    return re.search(rf"\bnot\s+{re.escape(name)}\b", markexpr) is None


def pytest_collection_modifyitems(config, items):
    """Skip real-service integration tests unless their marker is opted in."""
    if _run_all_integration():
        return
    markexpr = config.option.markexpr or ""
    for item in items:
        for marker in _REAL_SERVICE_MARKERS:
            if item.get_closest_marker(marker) and not _marker_requested(markexpr, marker):
                item.add_marker(
                    pytest.mark.skip(
                        reason=(
                            f"requires real {marker}; opt in with -m {marker} "
                            "or ATLAS_RUN_INTEGRATION=1"
                        )
                    )
                )
                break
