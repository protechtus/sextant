"""Keel (engine codename) extensions to the atlas-memory kernel.

This package layers transcript capture, persona dispatch, MCP gateway, and a
single-pane HTMX UI on top of `atlas-memory`. Kernel internals are not modified
from here; the only kernel files touched live in atlas-memory itself and are
versioned per the workspace version-before-edit rule.

See `README.md`, `docs/ARCHITECTURE.md` and `docs/RELEASING.md` (versions, builds, releases).
"""

# Every SEXTANT_<NAME> is honoured as ATLAS_<NAME> (SEX-043, AG132): adopted once here, before any submodule
# reads a setting, so children and hooks inherit it. Only-ATLAS environments are left untouched.
from atlas_plus import _envnames as _envnames

_envnames.adopt()


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
    return "0.1.1"


def __getattr__(name: str):
    # Lazy: the metadata lookup cost 75-90 ms in EVERY process, hooks included (AG11, sweep 10).
    if name == "__version__":
        value = _installed_version()
        globals()["__version__"] = value
        return value
    raise AttributeError(name)
