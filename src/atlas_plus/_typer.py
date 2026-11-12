"""Typer without Click's Windows argument expansion.

Click 8 expands ``$VAR``, ``%VAR%``, ``~`` and glob patterns inside every argument on Windows
(``windows_expand_args``), because cmd.exe does not. For a memory tool that is data loss: a fact
typed as ``'price is $5 and %PATH% is long'`` stored the operator's whole PATH, and
``sextant search '$HOME'`` searched for the home directory (FN2, final readiness pass,
2026-11-12). Every Sextant app is built from this class so what you type is what is stored.
"""
from __future__ import annotations

import typer as _typer


class Typer(_typer.Typer):
    def __call__(self, *args, **kwargs):
        kwargs.setdefault("windows_expand_args", False)
        return super().__call__(*args, **kwargs)
