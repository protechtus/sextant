"""SEX-048: `sextant versions --prune --yes` on a history folder that refuses the delete.

Walked on 2026-11-25 on a corpus made read-only (a Windows deny entry plus read-only files): every copy
printed `[WinError 5] Access is denied: 'E:\\\\...'` with the path twice, and the last line said "close
whatever holds them open", which is the advice for a file in use, not for a read-only folder. The refusal
now names the folder once, says it is read-only or another account's, and says to check its permissions.
A file that is held open keeps the old advice.
"""
from __future__ import annotations

import errno
import os
from pathlib import Path

import pytest


@pytest.fixture()
def history(tmp_path, monkeypatch):
    root = tmp_path / "mem"
    (root / "reference").mkdir(parents=True)
    vd = root / "versions" / "reference"
    vd.mkdir(parents=True)
    for n in ("alpha", "beta"):
        (root / "reference" / f"{n}.md").write_text(f"# {n}\n", encoding="utf-8")
        for v in (1, 2, 3):
            (vd / f"{n}_2020-01-0{v}_v1.md").write_text(f"old {n} {v}\n", encoding="utf-8")
    for k in [k for k in os.environ if k.startswith("ATLAS_")]:
        monkeypatch.delenv(k)
    monkeypatch.setenv("ATLAS_MEMORY_ROOT", str(root))
    monkeypatch.setenv("ATLAS_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("ATLAS_STATE_DIR", str(tmp_path / "state"))
    return vd


PRUNE = ["--prune", "--older-than", "0", "--keep-last", "0", "--yes"]


def _refuse_unlink(monkeypatch, make_exc):
    real = Path.unlink

    def unlink(self, *a, **kw):
        if "versions" in self.parts:
            raise make_exc(self)
        return real(self, *a, **kw)
    monkeypatch.setattr(Path, "unlink", unlink)


def test_a_refused_delete_names_the_folder_and_a_next_step_that_fits(history, monkeypatch, capsys):
    from atlas_plus import versions_cmd
    _refuse_unlink(monkeypatch, lambda p: PermissionError(errno.EACCES, "Permission denied", str(p)))
    assert versions_cmd.main(PRUNE) == 1
    err = capsys.readouterr().err
    assert len(list(history.iterdir())) == 6, "nothing may be deleted"
    assert "6 copy(ies) could not be deleted" in err
    assert "read-only or belongs to another account" in err
    assert "permissions" in err and "Next:" in err
    assert "holds them open" not in err
    assert "[Errno" not in err and "WinError" not in err, err
    # the path of each copy once on its line, not again inside a quoted errno text
    for line in [ln for ln in err.splitlines() if "could not delete" in ln]:
        assert line.count("_v1.md") == 1, line
        assert line.rstrip().endswith("Permission denied"), line
    assert err.count(str(history)) >= 1


def test_a_read_only_file_system_is_a_refusal_too(history, monkeypatch, capsys):
    from atlas_plus import versions_cmd
    _refuse_unlink(monkeypatch, lambda p: OSError(errno.EROFS, "Read-only file system", str(p)))
    assert versions_cmd.main(PRUNE) == 1
    err = capsys.readouterr().err
    assert "read-only or belongs to another account" in err and "holds them open" not in err


def test_a_file_held_open_keeps_its_own_advice(history, monkeypatch, capsys):
    from atlas_plus import versions_cmd

    def busy(p):
        exc = PermissionError(errno.EACCES, "The process cannot access the file because it is being used by another process", str(p))
        exc.winerror = 32   # the Windows sharing violation; elsewhere the attribute is simply set
        return exc
    _refuse_unlink(monkeypatch, busy)
    assert versions_cmd.main(PRUNE) == 1
    err = capsys.readouterr().err
    assert "holds them open" in err
    assert "read-only or belongs to another account" not in err


def test_a_real_read_only_copy_on_this_system(history, capsys):
    """The real thing where the system allows it: read-only files (Windows refuses the delete) or a
    read-only folder (POSIX refuses it; root is not refused and skips)."""
    from atlas_plus import versions_cmd
    copies = list(history.iterdir())
    try:
        if os.name == "nt":
            for c in copies:
                os.chmod(c, 0o444)
        else:
            if os.geteuid() == 0:
                pytest.skip("root is not refused")
            os.chmod(history, 0o555)
        code = versions_cmd.main(PRUNE)
        err = capsys.readouterr().err
    finally:
        os.chmod(history, 0o755)
        for c in copies:
            if c.exists():
                os.chmod(c, 0o644)
    assert code == 1
    assert len([c for c in copies if c.exists()]) == 6
    assert "read-only or belongs to another account" in err and "holds them open" not in err
    assert "[Errno" not in err and "WinError" not in err, err
