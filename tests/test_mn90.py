"""MN90 (sweep 27): doctor's Permissions Fix line must be one that can work on the folder's filesystem.

On WSL a corpus under /mnt/<drive> (drvfs, 9p) is always mode 777 and chmod is a no-op there, so
`chmod 700 <path>` never clears the warning. The fix line has to say why and what works instead.
"""
from atlas_plus import _fsutil

_DRVFS = (
    "rootfs / ext4 rw 0 0\n"
    "/dev/sdc / ext4 rw,relatime 0 0\n"
    "E:\\134 /mnt/e 9p rw,noatime,dirsync,aname=drvfs;path=E:\\;uid=1000;gid=1000 0 0\n"
)
_OLD_WSL = "/dev/sdc / ext4 rw 0 0\nC: /mnt/c drvfs rw,noatime 0 0\n"
_EXT4 = "/dev/sdc / ext4 rw,relatime 0 0\n/dev/sdd /home ext4 rw 0 0\n"


def test_drvfs_9p_fix_is_not_chmod():
    fix = _fsutil.mode_fix("/mnt/e/data/sextant/.memory", _DRVFS)
    assert not fix.startswith("chmod")
    assert "/mnt/e" in fix and "ATLAS_WORKSPACE_ROOT" in fix


def test_legacy_drvfs_type_fix_is_not_chmod():
    assert not _fsutil.mode_fix("/mnt/c/Projects/.memory", _OLD_WSL).startswith("chmod")


def test_ext4_keeps_chmod_fix_quoted():
    assert _fsutil.mode_fix("/home/user/my data/.memory", _EXT4) == "chmod 700 '/home/user/my data/.memory'"


def test_prefix_is_not_a_mount_match():
    # /mnt/e2 is not under /mnt/e: the root ext4 wins, chmod is the right fix
    assert _fsutil.mode_fix("/mnt/e2/.memory", _DRVFS).startswith("chmod 700 ")


def test_unreadable_mounts_falls_back_to_chmod():
    assert _fsutil.mode_fix("/srv/x", "").startswith("chmod 700 ")


def test_a_windows_drive_mounted_with_metadata_keeps_the_chmod_fix():
    mounts = "/dev/sdc / ext4 rw 0 0\nE:\\134 /mnt/e 9p rw,noatime,aname=drvfs;path=E:\\;metadata;uid=1000 0 0\n"
    assert _fsutil.mode_fix("/mnt/e/data/.memory", mounts) == "chmod 700 /mnt/e/data/.memory"
    legacy = "C: /mnt/c drvfs rw,noatime,metadata 0 0\n"
    assert _fsutil.mode_fix("/mnt/c/x", legacy).startswith("chmod 700 ")
