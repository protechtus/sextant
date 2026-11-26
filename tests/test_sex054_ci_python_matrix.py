"""SEX-054 item 1 (lesson LES-009): every Python version the package claims is run by CI.

The public pyproject carries `Programming Language :: Python :: 3.x` classifiers. On 2026-11-25 it
claimed 3.13 while the test job ran 3.11 and 3.12 only. A claimed version with no job is a promise
nobody checks; this test fails the day a classifier is added without its matrix entry.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
_CLASSIFIER = re.compile(r"Programming Language :: Python :: (3\.\d+)")


def _claimed_versions() -> set[str]:
    """The classifiers of this tree: the pyproject's own, or (source tree) the ones the public
    build writes into it."""
    found = set(_CLASSIFIER.findall((_REPO / "pyproject.toml").read_text(encoding="utf-8")))
    if not found:
        builder = _REPO / "scripts" / "build_public_repo.py"
        if builder.exists():
            found = set(_CLASSIFIER.findall(builder.read_text(encoding="utf-8")))
    return found


def _test_job_text() -> str:
    wf = _REPO / ".github" / "workflows" / "ci.yml"
    if not wf.exists():
        pytest.skip("workflows are not part of this tree")
    lines = wf.read_text(encoding="utf-8").splitlines()
    start = lines.index("  test:")
    out = []
    for line in lines[start + 1:]:
        if re.match(r"  \S", line) and not line.lstrip().startswith("#"):
            break  # the next job
        if not line.lstrip().startswith("#"):
            out.append(line)
    return "\n".join(out)


def _matrix_versions(job: str) -> set[str]:
    return set(re.findall(r"(?<![\d.])(3\.\d{1,2})(?![\d.])", job.split("steps:", 1)[0]))


def test_every_claimed_python_version_is_run_by_the_test_job():
    claimed = _claimed_versions()
    assert claimed, "no Python classifier found: the test lost its source"
    ran = _matrix_versions(_test_job_text())
    missing = sorted(claimed - ran, key=lambda v: int(v.split(".")[1]))
    assert not missing, (
        f"the package claims Python {missing} but the CI test job's matrix runs only "
        f"{sorted(ran)}: add the version to .github/workflows/ci.yml or drop the classifier"
    )


def test_the_floor_version_is_run():
    m = re.search(r'requires-python\s*=\s*">=\s*(3\.\d+)"', (_REPO / "pyproject.toml").read_text(encoding="utf-8"))
    assert m, "requires-python not found"
    assert m.group(1) in _matrix_versions(_test_job_text())


def test_the_matrix_reader_sees_a_missing_version():
    job = """    strategy:
      matrix:
        python-version: ["3.11"]
        include:
          - os: ubuntu-latest
            python-version: "3.12"
    steps:
      - run: echo 3.13
"""
    assert _matrix_versions(job) == {"3.11", "3.12"}
