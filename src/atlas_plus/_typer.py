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


def _click_classes(name: str) -> tuple:
    """Typer 0.29 vendors Click as typer._click (its Abort lives in typer.exceptions); older Typer
    raises the real click's. Every class of that name found, resolved BEFORE any except clause
    needs it: a missing attribute inside an except tuple turned `sextant init`'s exit 0 into a traceback."""
    found = []
    for mod in ("typer._click.exceptions", "typer.exceptions", "click.exceptions"):
        try:
            cls = getattr(__import__(mod, fromlist=[name]), name, None)
        except ImportError:
            continue
        if isinstance(cls, type) and cls not in found:
            found.append(cls)
    return tuple(found) or (_NeverRaised,)


class _NeverRaised(Exception):
    pass


def _delegated_prog_name():
    """The program name for an app the umbrella command handed off to, when the umbrella itself was
    started as `python -m atlas_plus` (the form `sextant setup` prints). The hand-off rewrites
    argv[0] to the command's own name (`sextant review`, `sextant-transcripts`), and Click, seeing a
    `python -m` start, glued that onto the package: the usage line read `python -m
    atlas_plus.sextant review` and `python -m atlas_plus.sextant-transcripts`, modules that do not
    exist. None when there is nothing to correct (a console script, a direct `python -m` of a module,
    a test runner)."""
    import os
    import sys

    main = sys.modules.get("__main__")
    if getattr(main, "__package__", None) != "atlas_plus" or not sys.argv or not sys.argv[0]:
        return None
    name = sys.argv[0]
    if os.sep in name or "/" in name or os.path.exists(name):
        return None   # a real path: the umbrella itself, Click names it correctly
    first, _, rest = name.partition(" ")
    if first in ("sextant", "atlas-plus"):
        return "python -m atlas_plus" + (" " + rest if rest else "")
    return name


class Typer(_typer.Typer):
    def __call__(self, *args, **kwargs):
        kwargs.setdefault("windows_expand_args", False)
        if "prog_name" not in kwargs:
            _prog = _delegated_prog_name()
            if _prog:
                kwargs["prog_name"] = _prog
        if kwargs.get("standalone_mode", True) is False:
            return super().__call__(*args, **kwargs)
        # Click's usage errors ended at a bare "No such option" / "Missing argument" box, the
        # pointer to --help above it (LES-054, AG102): the last line now names the next step.
        import sys

        click_exc, usage_exc, abort_exc = (_click_classes(n) for n in ("ClickException", "UsageError", "Abort"))
        kwargs["standalone_mode"] = False
        try:
            rv = super().__call__(*args, **kwargs)
        except click_exc as e:
            try:
                from typer import rich_utils
                rich_utils.rich_format_error(e)
            except Exception:  # noqa: BLE001 - no rich: Click's own plain rendering
                e.show()
            if isinstance(e, usage_exc):
                ctx = getattr(e, "ctx", None)
                path = ctx.command_path if ctx is not None else "sextant"
                root = ctx.find_root().command_path if ctx is not None else path  # `python -m atlas_plus` stays whole
                split_fact = type(e).__name__ == "NoSuchOption" or "extra argument" in str(getattr(e, "message", "")).lower()
                if split_fact and path.split()[-1:] in (["remember"], ["add"]):
                    # PowerShell 5.1 drops the inner double quotes of `'Always run "pytest -q" first'`, so the
                    # fact arrives in pieces and `-q` reads as an option (MN89, SEX-050). The way out is stdin.
                    print(f"If that was part of the fact (a shell can split a fact that has quotes or dashes "
                          f"in it), pipe the fact in: `echo \"<fact>\" | {path} -`.", file=sys.stderr)
                if path != root:
                    print(f"Run `{path} --help` to see what it takes; `{root} --help` lists every command.", file=sys.stderr)
                else:
                    print(f"Run `{root} --help` to see every command and option.", file=sys.stderr)
            sys.exit(e.exit_code)
        except abort_exc:
            print("Aborted!", file=sys.stderr)
            sys.exit(1)
        sys.exit(rv if isinstance(rv, int) and not isinstance(rv, bool) else 0)
