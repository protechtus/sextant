"""Factory reset: wipe an area and immediately re-scaffold it.

The debug-loop command (install -> find bug -> reset -> retry) that composes
the two existing pieces instead of duplicating them: removal comes from
atlas-uninstall (identical safety gates) and the fresh skeleton comes from
atlas-init.

    atlas-reset                  # explain what each area would do; change nothing
    atlas-reset --state --yes    # clear derived caches/indexes only (transcripts, task DB,
                                 # ledger, sync, reports are kept);
                                 #   indexes rebuild on next use)
    atlas-reset --corpus --yes   # YOUR DATA: wipe + fresh init skeleton
                                 #   (asks you to type the corpus path back)
    atlas-reset --all --yes      # both

(The public distribution also exposes it as `sextant-reset`.)

Every corpus protection from atlas-uninstall applies unchanged: --yes is
required, the exact path must be typed back (or --confirm-path for VM
scripting), and a corpus with a `.sextant-protect` marker refuses the reset
unconditionally.
"""

from __future__ import annotations

from atlas_plus._fsutil import make_private_dir
import argparse

from atlas_plus import init_cmd, uninstall_cmd
from atlas_plus import paths as atlas_paths


def reset_state(yes: bool) -> str:
    """Clear ONLY the regenerable caches and indexes inside the state dir (temporal graph, logs,
    CLI history, embedding index). Transcripts, the task DB, the intake ledger, sync devices
    and research reports are kept: they are the only copy (data-safety audit 2026-11-09)."""
    outcome = uninstall_cmd.clear_derived_state(yes)
    if outcome == "absent" or outcome.startswith(("refused", "REFUSED", "failed")):
        return outcome
    make_private_dir(atlas_paths.state_dir())
    return f"reset ({outcome}; indexes rebuild on next use)"


def reset_corpus(yes: bool, confirm_path: str | None, ask=input) -> str:
    """Wipe the corpus and lay down the first-run skeleton in its place."""
    outcome = uninstall_cmd.remove_corpus(yes, confirm_path, ask=ask)
    if outcome not in ("removed", "absent"):
        return outcome
    init_cmd.run_init(dry_run=False)
    return f"reset ({outcome} -> fresh init skeleton)"


def _prog() -> str:
    """Report the name the user actually typed (sextant-* alias, subcommand, or atlas-*)."""
    import sys
    from pathlib import Path as _P
    return _P(sys.argv[0]).stem if sys.argv and sys.argv[0] else "atlas-reset"


def main() -> None:
    from atlas_plus._console import utf8_stdout_when_piped
    utf8_stdout_when_piped()
    from atlas_plus import _next_step  # usage errors name --help (LES-054)
    parser = _next_step.ArgumentParser(
        prog=_prog(),
        description="Wipe an area and immediately re-scaffold it (uninstall + init in one step).",
    )
    parser.add_argument("--state", action="store_true", help="clear derived caches and indexes in the state dir (keeps transcripts, task DB, ledger, sync, reports)")
    parser.add_argument("--corpus", action="store_true", help="reset the memory corpus (YOUR DATA)")
    parser.add_argument("--all", action="store_true", help="reset both")
    parser.add_argument("--yes", "-y", action="store_true", help="required for any reset")
    parser.add_argument(
        "--confirm-path",
        default=None,
        type=_next_step.user_path,
        help="non-interactive corpus confirmation: must name the corpus path (slash style and trailing separators are tolerated, case only where the filesystem ignores it; anything else refuses)",
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="report what each area would do and change nothing (a safe preview)")
    args = parser.parse_args()
    import sys as _sys
    _err = _sys.stderr

    def _refuse(*lines: str) -> None:  # stderr, ending with a safe next step (AG103)
        for line in lines:
            print(line, file=_err)
        print(f"Next: {uninstall_cmd.refusal_next(_prog(), uninstall_cmd.area_preview(args))}", file=_err)
        raise SystemExit(1)

    do_state = args.state or args.all
    do_corpus = args.corpus or args.all

    if args.dry_run or (not do_state and not do_corpus):
        info = uninstall_cmd.report()
        c, s = info["corpus"], info["state"]
        print(f"{_prog()}: " + ("--dry-run: nothing was changed" if args.dry_run else "nothing selected")
              + "; here is what each area would do.")
        print(f"  --state  : clear derived caches in {s['path']} ({s['files']} file(s) now; derived caches only)")
        prot = " [PROTECTED -- reset will refuse]" if c["protected"] else ""
        print(f"  --corpus : wipe {c['path']} ({c['files']} file(s) now){prot},")
        print("             then lay down the fresh first-run skeleton (atlas-init)")
        print("  --all    : both. Every deletion needs --yes; the corpus also asks")
        print("             you to type its full path (or --confirm-path).")
        return

    # TWO-PHASE (Codex artifact audit): validate the corpus confirmation
    # BEFORE resetting anything, so a wrong --confirm-path can never leave a
    # partial reset (state wiped, corpus refused).
    if do_state and atlas_paths.state_dir().exists():
        state_refusal = uninstall_cmd.validate_destructive_target("state", atlas_paths.state_dir())
        if state_refusal:
            _refuse(f"state:  {state_refusal}",
                    *(["corpus: not reset (state target unsafe; nothing was changed)"] if do_corpus else []))
    if do_state and do_corpus:
        _pre = uninstall_cmd.derived_state_refusal()
        if _pre:  # the state step would refuse: nothing is wiped (MN66: the corpus went, the state was refused, rc 1)
            _refuse(f"state:  {_pre}", "corpus: not reset (the state step would refuse; nothing was changed)")
    if do_corpus and not atlas_paths.memory_root().exists():
        # A reset never creates what was not there: `reset --corpus` with no --yes on a missing corpus
        # exited 0 after laying down a fresh one (SEX-047, AG107). Nothing to reset is a refusal.
        from atlas_plus import _next_step
        _refuse(*(["state:  not reset (no corpus to reset; nothing was changed)"] if do_state else []),
                f"corpus: there is no corpus at {atlas_paths.memory_root()}; nothing to reset, nothing was "
                f"changed. Next: {_next_step.command('init')} creates one (or set ATLAS_WORKSPACE_ROOT to "
                "the folder that holds yours).")
    if do_corpus:
        refusal = uninstall_cmd.corpus_target_refusal() or uninstall_cmd.confirm_corpus_removal(args.yes, args.confirm_path)  # (AG73)
        if refusal is not None:
            _refuse(*(["state:  not reset (corpus confirmation failed; nothing was changed)"] if do_state else []),
                    f"corpus: {refusal}")

    failed = False
    if do_state:
        outcome = reset_state(args.yes)
        print(f"state:  {outcome}", flush=True)
        failed = failed or outcome.startswith(("refused", "REFUSED", "failed"))
    if do_corpus:
        outcome = reset_corpus(args.yes, args.confirm_path)
        print(f"corpus: {outcome}", flush=True)
        failed = failed or outcome.startswith(("refused", "REFUSED", "failed"))
    if failed:
        print(f"Next: {uninstall_cmd.refusal_next(_prog(), uninstall_cmd.area_preview(args))}", file=_err)  # (SEX-051 F8)
    raise SystemExit(1 if failed else 0)


if __name__ == "__main__":
    main()
