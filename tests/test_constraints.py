"""constraints.txt stays honest (ROADMAP item 8, loop run 13).

The file offers known-good exact pins as a fallback install path. These
tests keep it from rotting: every direct dependency must be pinned, and
every line must be an exact pin (no ranges -- ranges belong in
pyproject).
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
CONSTRAINTS = REPO / "constraints.txt"


def _norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _pinned_names() -> set[str]:
    names = set()
    for line in CONSTRAINTS.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        assert "==" in line, f"constraints line is not an exact pin: {line!r}"
        names.add(_norm(line.split("==")[0]))
    return names


class TestConstraints:
    def test_every_direct_dependency_is_pinned(self):
        if not CONSTRAINTS.exists():
            pytest.skip("constraints.txt not in this tree")
        import tomllib

        with open(REPO / "pyproject.toml", "rb") as f:
            proj = tomllib.load(f)["project"]
        direct = list(proj.get("dependencies", []))
        for extra, deps in proj.get("optional-dependencies", {}).items():
            if extra in ("kernel", "trading"):  # kernel: machine-specific path dep; trading: private-only extra the public build drops (its ccxt pin made the public constraints unresolvable, MN4 2026-11-11)
                continue
            direct += deps
        pinned = _pinned_names()
        missing = []
        for spec in direct:
            name = _norm(re.split(r"[\[<>=!~; ]", spec, 1)[0])
            if name not in pinned:
                missing.append(spec)
        assert not missing, f"direct deps missing from constraints.txt: {missing}"

    def test_no_editable_or_local_paths(self):
        if not CONSTRAINTS.exists():
            pytest.skip("constraints.txt not in this tree")
        # Pin lines only -- the header comment legitimately shows a
        # `pip install -e` usage example.
        pins = [
            l for l in CONSTRAINTS.read_text(encoding="utf-8").splitlines()
            if l.strip() and not l.strip().startswith("#")
        ]
        for line in pins:
            assert not line.startswith("-e "), line
            assert "file:///" not in line.lower(), line
