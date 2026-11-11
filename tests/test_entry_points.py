"""Every declared console_script entry point must resolve (R3, release-critical).

A console_script target "module:attr" that doesn't resolve (typo, renamed function, bad
ref) installs fine but is DOA the moment a user runs it. These 30 entry points had no
coverage. This test imports each module and resolves the attribute WITHOUT executing the
CLI, so it catches broken refs cheaply and deterministically.
"""
from __future__ import annotations

import importlib
import tomllib
from pathlib import Path

import pytest


def _entry_points() -> dict[str, str]:
    pp = Path(__file__).resolve().parent.parent / "pyproject.toml"
    data = tomllib.loads(pp.read_text(encoding="utf-8"))
    return data.get("project", {}).get("scripts", {})


_EPS = _entry_points()

# Entry points that currently do NOT resolve. xfail(strict) keeps the suite green while
# documenting breakage and catching regressions; when one is FIXED, the strict xfail turns
# into an unexpected pass (XPASS -> failure) prompting removal from this map.
# 2026-06-29: found 4 broken (atlas-plus / atlas-transcripts / atlas-bootstrap missing
# modules; atlas-fts-rebuild bad attr). 2026-07-02: all resolved -- namesake + transcripts
# CLIs built, fts repointed to a proper main(), stale bootstrap entry removed.
KNOWN_BROKEN: dict[str, str] = {}


def _params():
    params = []
    for name, target in sorted(_EPS.items()):
        marks = ()
        if name in KNOWN_BROKEN:
            marks = (pytest.mark.xfail(reason=KNOWN_BROKEN[name], strict=True),)
        params.append(pytest.param(name, target, marks=marks, id=name))
    return params


def test_pyproject_declares_entry_points():
    assert _EPS, "no [project.scripts] entry points found in pyproject.toml"


@pytest.mark.parametrize("name,target", _params())
def test_entry_point_resolves(name, target):
    module_path, sep, attr = target.partition(":")
    assert sep == ":" and attr, f"{name}: malformed entry-point target {target!r} (need module:attr)"
    mod = importlib.import_module(module_path)
    obj = mod
    for part in attr.split("."):
        obj = getattr(obj, part)  # AttributeError here = broken entry point
    assert callable(obj), f"{name}: {target} resolves to a non-callable {type(obj).__name__}"
