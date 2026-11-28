"""MN97 (sweep 39): an upgraded user who types an atlas-* name sees Sextant on the public install (SEX-043 a).

Before the fix, `atlas-dream --help`, `atlas-backup --help`, `atlas-doctor --help`, `atlas-memory-mcp --help`,
`atlas-web-ui --help`, `atlas-brain --help` and `python -m atlas_plus.mcp.memory_server.server --help` (the
command setup wrote into client settings) printed "Atlas ..." on the public install: branding.product() looked
at argv[0] only. The distribution decides when argv does not name a sextant command, as cli._brand already does.
"""
from __future__ import annotations

import importlib.metadata as md

import pytest

from atlas_plus import branding


@pytest.fixture
def fake_dists(monkeypatch):
    """Make importlib.metadata.distribution answer only for the names given."""
    def install(*names):
        def distribution(name):
            if name in names:
                return object()
            raise md.PackageNotFoundError(name)
        monkeypatch.setattr(md, "distribution", distribution)
        branding.public_install.cache_clear()
    yield install
    branding.public_install.cache_clear()


@pytest.mark.parametrize("argv0", ["atlas-dream", "/venv/bin/atlas-backup", "server.py", "__main__.py"])
def test_public_install_says_sextant_for_old_names(monkeypatch, fake_dists, argv0):
    fake_dists("sextant-ai")
    monkeypatch.setattr("sys.argv", [argv0])
    assert branding.product() == "Sextant"
    # the usage line still shows the name the user typed
    assert branding.prog("atlas-dream") == "atlas-dream"


def test_private_install_keeps_its_face(monkeypatch, fake_dists):
    fake_dists("atlas-plus")
    monkeypatch.setattr("sys.argv", ["atlas-dream"])
    assert branding.product() == "Atlas"


def test_no_distribution_at_all_keeps_old_answer(monkeypatch, fake_dists):
    fake_dists()
    monkeypatch.setattr("sys.argv", ["pytest"])
    assert branding.product() == "Atlas"
    monkeypatch.setattr("sys.argv", ["sextant-dream"])
    assert branding.product() == "Sextant"


def test_dream_help_description_on_public_install(monkeypatch, fake_dists, capsys):
    """The fixed path end to end: the parser a user's --help prints."""
    fake_dists("sextant-ai")
    monkeypatch.setattr("sys.argv", ["atlas-dream", "--help"])
    from atlas_plus.curation import dream_mode
    with pytest.raises(SystemExit) as exc:
        dream_mode.main()
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "Sextant Dream Mode" in out
    assert "Atlas Dream" not in out


def test_lookup_is_cached(monkeypatch, fake_dists):
    calls = []

    def distribution(name):
        calls.append(name)
        return object()
    monkeypatch.setattr(md, "distribution", distribution)
    branding.public_install.cache_clear()
    monkeypatch.setattr("sys.argv", ["atlas-backup"])
    for _ in range(5):
        assert branding.product() == "Sextant"
    assert calls == ["sextant-ai"]
