"""MN90 finding 2 (and MN88 item 4, SEX-048): day 2 in a terminal without the location variables.

A corpus made in a terminal that had ATLAS_WORKSPACE_ROOT set is invisible to a new terminal without it.
The "no corpus yet" messages said "run init", which starts a second, empty corpus. They now say where the
first one may be, but only when the location comes from the defaults.
"""
from __future__ import annotations

import sys

import pytest

LOCATION_VARS = ("ATLAS_WORKSPACE_ROOT", "ATLAS_MEMORY_ROOT", "ATLAS_STATE_DIR")


def _defaults_into(monkeypatch, tmp_path):
    """No location variable; the home folder (where the defaults point) is an empty sandbox folder."""
    for var in LOCATION_VARS:
        monkeypatch.delenv(var, raising=False)
    home = tmp_path / "home"
    home.mkdir()
    for var in ("HOME", "USERPROFILE"):
        monkeypatch.setenv(var, str(home))
    return home


def _search(monkeypatch, capsys):
    from atlas_plus import cli
    monkeypatch.setattr(sys, "argv", ["sextant", "search", "kestrel"])
    with pytest.raises(SystemExit) as exc:
        cli.app(args=["search", "kestrel"], prog_name="sextant")
    return exc.value.code, " ".join(capsys.readouterr().err.split())


def test_search_without_the_variables_says_the_corpus_may_be_elsewhere(monkeypatch, tmp_path, capsys):
    from atlas_plus import paths
    home = _defaults_into(monkeypatch, tmp_path)
    if not str(paths.memory_root()).startswith(str(home)):
        pytest.skip("the default corpus path does not follow HOME on this platform")
    code, err = _search(monkeypatch, capsys)
    assert code == 1 and "No corpus yet" in err and "init" in err
    assert "ATLAS_WORKSPACE_ROOT" in err and "second, empty corpus" in err
    assert not paths.memory_root().exists()                           # still nothing created


def test_search_with_a_chosen_location_keeps_the_short_message(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("ATLAS_WORKSPACE_ROOT", str(tmp_path / "ws"))
    monkeypatch.setenv("ATLAS_MEMORY_ROOT", str(tmp_path / "ws" / ".memory"))
    monkeypatch.setenv("ATLAS_STATE_DIR", str(tmp_path / "ws" / ".atlas"))
    code, err = _search(monkeypatch, capsys)
    assert code == 1 and "No corpus yet" in err
    assert "somewhere else" not in err and "second, empty corpus" not in err


def test_a_failed_verify_before_init_says_it_too(monkeypatch, tmp_path, capsys):
    from atlas_plus import paths, roundtrip
    home = _defaults_into(monkeypatch, tmp_path)
    if not str(paths.memory_root()).startswith(str(home)):
        pytest.skip("the default corpus path does not follow HOME on this platform")
    assert roundtrip._say_next(1, []) == 1
    err = capsys.readouterr().err
    assert "no corpus yet" in err and "ATLAS_WORKSPACE_ROOT" in err


def test_the_first_screen_mentions_it_only_when_the_location_is_the_default(monkeypatch, tmp_path, capsys):
    from atlas_plus import cli, paths
    home = _defaults_into(monkeypatch, tmp_path)
    if not str(paths.memory_root()).startswith(str(home)):
        pytest.skip("the default corpus path does not follow HOME on this platform")
    monkeypatch.setattr(sys, "argv", ["sextant"])
    cli._overview()
    out = " ".join(capsys.readouterr().out.split())
    assert "1. " in out and "Not your first time?" in out and "ATLAS_WORKSPACE_ROOT" in out
    monkeypatch.setenv("ATLAS_WORKSPACE_ROOT", str(tmp_path / "ws"))
    monkeypatch.setenv("ATLAS_MEMORY_ROOT", str(tmp_path / "ws" / ".memory"))
    cli._overview()
    out = " ".join(capsys.readouterr().out.split())
    assert "1. " in out and "Not your first time?" not in out
