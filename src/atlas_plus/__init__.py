"""Keel (engine codename) extensions to the atlas-memory kernel.

This package layers transcript capture, persona dispatch, MCP gateway, and a
single-pane HTMX UI on top of `atlas-memory`. Kernel internals are not modified
from here; the only kernel files touched live in atlas-memory itself and are
versioned per the workspace version-before-edit rule.

See `README.md` and `docs/HOWTO-development.md` for the build map and rules.
"""


def _installed_version() -> str:
    """The version pip installed (sextant-ai publicly, atlas-plus privately), so an upgrade reports
    itself instead of a constant (AG2, pipeline sweep 2026-11-11)."""
    try:
        from importlib.metadata import PackageNotFoundError, version
        for dist in ("sextant-ai", "atlas-plus"):
            try:
                return version(dist)
            except PackageNotFoundError:
                continue
    except Exception:  # noqa: BLE001
        pass
    return "0.1.0"


def __getattr__(name: str):
    # Lazy: the metadata lookup cost 75-90 ms in EVERY process, hooks included (AG11, sweep 10).
    if name == "__version__":
        value = _installed_version()
        globals()["__version__"] = value
        return value
    raise AttributeError(name)
