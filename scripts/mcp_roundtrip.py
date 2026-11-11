"""mcp_roundtrip.py -- prove a Sextant install works (clone-side wrapper).

The round-trip itself lives in the package as ``atlas_plus.roundtrip`` so every kind of
install has it: ``sextant verify`` or ``<python> -m atlas_plus.roundtrip`` do the same thing
without a clone. This file stays for the agent runbook and for older instructions.

Usage (from the clone, with the venv's Python):

    <venv-python> scripts/mcp_roundtrip.py
    <venv-python> scripts/mcp_roundtrip.py --project demo --fact "Sextant installed by an agent"

Exit code 0 when all three calls succeed, 1 otherwise. Standard library only: when run with an
interpreter that does not have the package installed, the module is loaded from the clone's
``src/`` tree and ``--python`` names the interpreter that does.
"""
from __future__ import annotations

import sys
from pathlib import Path

try:
    from atlas_plus.roundtrip import main
except ImportError:  # an interpreter without the package: load the module from the clone
    import importlib.util

    _src = Path(__file__).resolve().parents[1] / "src" / "atlas_plus" / "roundtrip.py"
    _spec = importlib.util.spec_from_file_location("atlas_plus_roundtrip", _src)
    _mod = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]
    assert _spec and _spec.loader
    _spec.loader.exec_module(_mod)
    main = _mod.main

if __name__ == "__main__":
    sys.exit(main())
