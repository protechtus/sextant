"""Brand detection for user-facing CLI surfaces (Codex audits P1-C).

The same package ships privately as Keel and publicly as Sextant; the
console-script aliases decide which face the user sees. Every --help /
--version / header surface should brand by the name the user actually
typed, so `sextant-sync --help` never says "Atlas". Centralized here
because per-file copies of the argv-stem check drifted (doctor header,
uninstall _prog, cli _brand all reimplemented it).
"""
from __future__ import annotations

import functools
import sys
from pathlib import Path


def invoked_stem(default: str = "") -> str:
    """The console-script name the user typed (lowercased stem of argv[0])."""
    if sys.argv and sys.argv[0]:
        stem = Path(sys.argv[0]).stem.lower()
        if stem:
            return stem
    return default


@functools.lru_cache(maxsize=1)
def public_install() -> bool:
    """True when the installed distribution is the public `sextant-ai` (the private build is `atlas-plus`).

    One direct lookup, cached: `packages_distributions()` reads every installed RECORD (AG11 sweep 10).
    """
    try:
        from importlib.metadata import PackageNotFoundError, distribution
    except Exception:  # noqa: BLE001
        return False
    try:
        distribution("sextant-ai")
    except PackageNotFoundError:
        return False
    except Exception:  # noqa: BLE001 - a broken metadata folder must not stop a --help
        return False
    return True


def product() -> str:
    """'Sextant' when invoked via a sextant-* alias or on the public install, else 'Atlas'.

    An upgraded 0.1.x user who keeps typing `atlas-dream` or runs `python -m atlas_plus...` from the
    settings setup wrote sees Sextant too (SEX-043 group a); the private build keeps its own face.
    """
    if invoked_stem().startswith("sextant") or public_install():
        return "Sextant"
    return "Atlas"


def prog(atlas_name: str) -> str:
    """argparse prog: the sextant-* alias when used, else the atlas-* name."""
    stem = invoked_stem()
    return stem if stem.startswith("sextant") else atlas_name
