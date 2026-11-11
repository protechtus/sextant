"""Tests for atlas_plus.branding -- brand-by-invoked-name on CLI surfaces.

Codex audits P1-C: `sextant-sync --help` (and web/brain/mcp/backup/dream)
said "Atlas" to public users. The shared helper brands by argv[0] stem;
the static gate keeps hardcoded "Atlas ..." descriptions from creeping
back into user-facing parsers.
"""
from __future__ import annotations

from pathlib import Path

from atlas_plus import branding

SRC = Path(__file__).resolve().parents[1] / "src" / "atlas_plus"

# The user-facing command modules swept in P1-C (each has a sextant-* alias).
SWEPT = [
    SRC / "web" / "app.py",
    SRC / "brain" / "server.py",
    SRC / "mcp" / "memory_server" / "server.py",
    SRC / "backup.py",
    SRC / "curation" / "dream_mode.py",
    SRC / "sync" / "cli.py",
]


class TestProductDetection:
    def test_sextant_alias(self, monkeypatch):
        # Forward-slash absolute path splits correctly on every OS (a raw
        # C:\...\x.exe literal only splits on Windows -- backslash is not a
        # separator on POSIX, which turned this test red on ubuntu/macOS CI).
        monkeypatch.setattr("sys.argv", ["/venv/Scripts/sextant-sync.exe"])
        assert branding.product() == "Sextant"
        assert branding.prog("atlas-sync") == "sextant-sync"

    def test_sextant_alias_bare_name(self, monkeypatch):
        monkeypatch.setattr("sys.argv", ["sextant-doctor"])
        assert branding.product() == "Sextant"

    def test_atlas_name(self, monkeypatch):
        monkeypatch.setattr("sys.argv", ["atlas-sync"])
        assert branding.product() == "Atlas"
        assert branding.prog("atlas-sync") == "atlas-sync"

    def test_unrelated_invoker_defaults_to_atlas(self, monkeypatch):
        monkeypatch.setattr("sys.argv", ["pytest"])
        assert branding.product() == "Atlas"

    def test_empty_argv_is_safe(self, monkeypatch):
        monkeypatch.setattr("sys.argv", [])
        assert branding.product() == "Atlas"
        assert branding.prog("atlas-x") == "atlas-x"


class TestNoHardcodedBrandGate:
    def test_swept_parsers_have_no_hardcoded_atlas_descriptions(self):
        offenders = []
        for path in SWEPT:
            for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                code = line.split("#", 1)[0]
                if ('description="Atlas' in code or "description='Atlas" in code
                        or 'help="Atlas' in code or "help='Atlas" in code
                        or 'prog="atlas-' in code):
                    offenders.append(f"{path.name}:{i}: {line.strip()}")
        assert not offenders, (
            "hardcoded brand in a user-facing parser (use atlas_plus.branding): "
            + "; ".join(offenders)
        )
