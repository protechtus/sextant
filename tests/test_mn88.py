"""MN88 (sweep 26): `sextant init` on a path where a folder cannot go.

The Linux stranger pointed the corpus root at a dangling symlink and got a Rich traceback (FileExistsError
from mkdir); a regular file at the path did the same. Init must end on one sentence that says what is at
the path and what to type next (LES-054). Each test runs init itself and reads what it printed.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from atlas_plus import init_cmd


def _init(monkeypatch, capsys, memory_root: Path, state_dir: Path, *args: str) -> tuple[int, str]:
    monkeypatch.setenv("ATLAS_MEMORY_ROOT", str(memory_root))
    monkeypatch.setenv("ATLAS_STATE_DIR", str(state_dir))
    monkeypatch.setattr(sys, "argv", ["sextant-init", *args])
    monkeypatch.setattr(sys.modules["__main__"], "__spec__", None, raising=False)
    with pytest.raises(SystemExit) as exc:
        raise SystemExit(init_cmd.main())
    code = exc.value.code
    captured = capsys.readouterr()
    text = captured.out + captured.err + (code if isinstance(code, str) else "")
    return (code if isinstance(code, int) else (0 if code is None else 1)), text


def _dangling_link(link: Path, target: Path) -> None:
    try:
        os.symlink(target, link, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:  # Windows without the symlink privilege
        pytest.skip(f"cannot create a symlink here: {exc}")


def _assert_names_the_way_out(rc: int, text: str, path: Path) -> None:
    assert rc != 0
    assert "Traceback" not in text
    assert str(path) in text
    assert "`sextant init`" in text.strip().splitlines()[-1]
    assert "ATLAS_MEMORY_ROOT" in text and "ATLAS_STATE_DIR" in text


@pytest.mark.parametrize("extra", [(), ("--dry-run",)])
def test_corpus_root_that_is_a_file(tmp_path, monkeypatch, capsys, extra):
    root = tmp_path / "notes"
    root.write_text("not a folder\n", encoding="utf-8")
    rc, text = _init(monkeypatch, capsys, root, tmp_path / "state", *extra)
    _assert_names_the_way_out(rc, text, root)
    assert "is a file, not a folder" in text
    assert root.read_text(encoding="utf-8") == "not a folder\n"  # the file is left alone
    assert not (tmp_path / "state").exists()                      # and nothing was half-made beside it


def test_corpus_root_that_is_a_dangling_link(tmp_path, monkeypatch, capsys):
    root = tmp_path / "deadlink"
    _dangling_link(root, tmp_path / "gone")
    rc, text = _init(monkeypatch, capsys, root, tmp_path / "state")
    _assert_names_the_way_out(rc, text, root)
    assert "link whose target does not exist" in text
    assert not (tmp_path / "gone").exists()  # init does not create the target behind the user's back


def test_state_dir_that_is_a_dangling_link(tmp_path, monkeypatch, capsys):
    state = tmp_path / "deadstate"
    _dangling_link(state, tmp_path / "gone")
    rc, text = _init(monkeypatch, capsys, tmp_path / "notes", state)
    _assert_names_the_way_out(rc, text, state)


def test_a_file_above_the_corpus_root(tmp_path, monkeypatch, capsys):
    blocker = tmp_path / "data"
    blocker.write_text("x", encoding="utf-8")
    rc, text = _init(monkeypatch, capsys, blocker / "notes", tmp_path / "state")
    _assert_names_the_way_out(rc, text, blocker)
    assert f"{blocker} is a file, not a folder" in text


def test_a_normal_init_still_works(tmp_path, monkeypatch, capsys):
    rc, text = _init(monkeypatch, capsys, tmp_path / "notes", tmp_path / "state")
    assert rc == 0, text
    assert (tmp_path / "notes" / "identity" / "user.md").is_file()
