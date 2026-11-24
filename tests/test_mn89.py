"""MN89 (sweep 27, manual wave): an everyman on the PyPI install path, Windows.

What the stranger hit that was still open: `sextant setup` printed bash-only token lines to a PowerShell
user, and after `pip install sextant` (an unrelated project) nothing said why `sextant` had changed, while
the documented way back left no `sextant` command at all. Each test runs the path and checks the outcome.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------- setup section 4 in PowerShell
def test_setup_prints_token_lines_powershell_can_run(monkeypatch):
    from atlas_plus import setup_cmd as sc
    info = sc.describe(environ={})
    monkeypatch.setattr(sc, "_powershell", lambda: True)
    text = sc.render(info)
    assert '$env:ATLAS_MCP_TOKEN = "<long random>"; & ' in text
    assert "Bearer $env:ATLAS_MCP_TOKEN" in text and "--token $env:ATLAS_MCP_TOKEN" in text
    # before: `ATLAS_MCP_TOKEN=<long random> python ...` ("not recognized") and a header that read `Bearer `
    assert "Bearer $ATLAS_MCP_TOKEN" not in text and "--token $ATLAS_MCP_TOKEN" not in text
    assert not [ln for ln in text.splitlines() if ln.strip().startswith("ATLAS_MCP_TOKEN=")]
    assert "In a container: ATLAS_MCP_HOST=0.0.0.0" in text      # the container's own shell keeps its form


def test_setup_keeps_the_posix_token_lines_elsewhere(monkeypatch):
    from atlas_plus import setup_cmd as sc
    info = sc.describe(environ={})
    monkeypatch.setattr(sc, "_powershell", lambda: False)
    text = sc.render(info)
    assert [ln for ln in text.splitlines() if ln.strip().startswith("ATLAS_MCP_TOKEN=<long random>")]
    assert "Bearer $ATLAS_MCP_TOKEN" in text and "$env:" not in text


# ---------------------------------------------------------------- doctor names a taken-over command
def _eps(*pairs):
    def entry_points(group=None, **_k):
        return [SimpleNamespace(name=n, value=v, dist=SimpleNamespace(name=d)) for n, v, d in pairs]
    return entry_points


def test_doctor_fails_when_the_command_belongs_to_another_package(monkeypatch):
    import importlib.metadata as md
    from atlas_plus.doctor import cli as doctor
    monkeypatch.setattr(md, "entry_points", _eps(("sextant", "atlas_plus.cli:main", "sextant-ai"),
                                                 ("sextant", "sextant.cli:main", "sextant"),
                                                 ("sextant-doctor", "atlas_plus.doctor.cli:main", "sextant-ai")))
    got = doctor.check_command_owner()
    assert got["ok"] is False and got["foreign"] == ["sextant"]
    assert "pip uninstall -y sextant`" in got["fix"]
    assert "pip install --force-reinstall --no-deps sextant-ai" in got["fix"]   # a plain install does not restore it
    out = doctor.render_text({"command_owner": got})
    assert "FAIL" in out and "belongs to another package (sextant)" in out and "--force-reinstall" in out


def test_doctor_is_quiet_when_the_command_is_ours(monkeypatch):
    import importlib.metadata as md
    from atlas_plus.doctor import cli as doctor
    monkeypatch.setattr(md, "entry_points", _eps(("sextant", "atlas_plus.cli:main", "sextant-ai"),
                                                 ("other-tool", "other.cli:main", "other")))
    got = doctor.check_command_owner()
    assert got == {"ok": True, "foreign": [], "fix": ""}
    assert "belongs to another package" not in doctor.render_text({"command_owner": got})


def test_doctor_survives_unreadable_package_metadata(monkeypatch):
    import importlib.metadata as md
    from atlas_plus.doctor import cli as doctor

    def boom(**_k):
        raise OSError("metadata folder unreadable")
    monkeypatch.setattr(md, "entry_points", boom)
    assert doctor.check_command_owner()["ok"] is True


# ---------------------------------------------------------------- the documented way back
def test_the_first_timer_page_gives_the_remedy_that_restores_the_command():
    page = ROOT / "docs" / "NEW-TO-GITHUB.md"
    if not page.is_file():
        return
    row = next(ln for ln in page.read_text(encoding="utf-8").splitlines() if ln.startswith("| You ran `pip install sextant`"))
    # before: "uninstall sextant, then step 3": the uninstall removed the shared launcher and step 3 said
    # "already satisfied", so no `sextant` command was left (MN89 rows 28 to 32)
    assert "pip uninstall -y sextant`" in row and "--force-reinstall --no-deps sextant-ai" in row
