"""SEX-053 item 5: the last progress line of a sync that stopped before its first file.

A push of 300 notes to a hub that went away ended with "0/300 files transferred (300 not: see below)":
true, and read as 300 failures. It now says the sync stopped before any file was transferred. A sync
that moved some files keeps the count line, and a complete one is unchanged.
"""
from __future__ import annotations

from atlas_plus.sync import cli


def _end_line(capsys, done, total):
    show = cli._progress_printer()
    show("start", 0, total, 0, total)
    capsys.readouterr()
    show("end", done, total, 0, total)
    return capsys.readouterr().err.strip()


def test_nothing_transferred_reads_as_stopped(capsys):
    line = _end_line(capsys, 0, 300)
    assert line == "stopped before any file was transferred (0 of 300: see below)"
    assert "300 not" not in line


def test_a_partial_sync_keeps_the_count(capsys):
    assert _end_line(capsys, 120, 300) == "120/300 files transferred (180 not: see below)"


def test_a_complete_sync_is_unchanged(capsys):
    assert _end_line(capsys, 300, 300) == "300/300 files"


def test_a_small_sync_still_prints_nothing(capsys):
    assert _end_line(capsys, 0, cli.PROGRESS_MIN - 1) == ""
