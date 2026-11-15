"""Typer without Click's Windows argument expansion.

Click 8 expands ``$VAR``, ``%VAR%``, ``~`` and glob patterns inside every argument on Windows
(``windows_expand_args``), because cmd.exe does not. For a memory tool that is data loss: a fact
typed as ``'price is $5 and %PATH% is long'`` stored the operator's whole PATH, and
``sextant search '$HOME'`` searched for the home directory (FN2, final readiness pass,
2026-11-12). Every Sextant app is built from this class so what you type is what is stored.
"""
from __future__ import annotations

import typer as _typer


def _hide_flag_types() -> None:
    """Typer 0.29's rich help prints a type for every option, so `--help`, `--version` and each
    on/off switch read `--help <bool>` as if they took a value (sweep 22 manual wave). A flag takes
    no value: show no type for it. Guarded, so another Typer that lacks the hook is untouched."""
    try:
        from typer import core as _core
    except Exception:  # noqa: BLE001
        return
    opt = getattr(_core, "TyperOption", None)
    orig = getattr(opt, "display_type_rich", None)
    if orig is None or getattr(orig, "_sextant_flags", False):
        return

    def display_type_rich(self, ctx):
        if getattr(self, "is_flag", False) and getattr(self, "metavar", None) is None:
            return None
        return orig(self, ctx)

    display_type_rich._sextant_flags = True  # type: ignore[attr-defined]
    opt.display_type_rich = display_type_rich


_hide_flag_types()


class Typer(_typer.Typer):
    def __call__(self, *args, **kwargs):
        kwargs.setdefault("windows_expand_args", False)
        return super().__call__(*args, **kwargs)
