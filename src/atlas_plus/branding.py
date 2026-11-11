"""Brand detection for user-facing CLI surfaces (Codex audits P1-C).

The same package ships privately as Keel and publicly as Sextant; the
console-script aliases decide which face the user sees. Every --help /
--version / header surface should brand by the name the user actually
typed, so `sextant-sync --help` never says "Atlas". Centralized here
because per-file copies of the argv-stem check drifted (doctor header,
uninstall _prog, cli _brand all reimplemented it).
"""
from __future__ import annotations

import sys
from pathlib import Path


def invoked_stem(default: str = "") -> str:
    """The console-script name the user typed (lowercased stem of argv[0])."""
    if sys.argv and sys.argv[0]:
        stem = Path(sys.argv[0]).stem.lower()
        if stem:
            return stem
    return default


def product() -> str:
    """'Sextant' when invoked via a sextant-* alias, else 'Atlas'."""
    return "Sextant" if invoked_stem().startswith("sextant") else "Atlas"


def prog(atlas_name: str) -> str:
    """argparse prog: the sextant-* alias when used, else the atlas-* name."""
    stem = invoked_stem()
    return stem if stem.startswith("sextant") else atlas_name
