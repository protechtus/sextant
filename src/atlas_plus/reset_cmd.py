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

import argparse

from atlas_plus import init_cmd, uninstall_cmd
from atlas_plus import paths as atlas_paths


def reset_state(yes: bool) -> str:
    """Clear ONLY the regenerable caches and indexes inside the state dir (temporal graph, logs,
    CLI history, embedding index). Transcripts, the task DB, the intake ledger, sync devices
    and research reports are kept: they are the only copy (data-safety audit 2026-11-09)."""
    outcome = uninstall_cmd.clear_derived_state(yes)
    if outcome == "absent" or outcome.startswith(("refused", "REFUSED")):
        return outcome
    atlas_paths.state_dir().mkdir(parents=True, exist_ok=True)
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
    parser = argparse.ArgumentParser(
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
        help="non-interactive corpus confirmation: must name the corpus path (case and slash style are tolerated; anything else refuses)",
    )
    args = parser.parse_args()

    do_state = args.state or args.all
    do_corpus = args.corpus or args.all

    if not do_state and not do_corpus:
        info = uninstall_cmd.report()
        c, s = info["corpus"], info["state"]
        print(f"{_prog()}: nothing selected; here is what each area would do.")
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
            print(f"state:  {state_refusal}")
            if do_corpus:
                print("corpus: not reset (state target unsafe; nothing was changed)")
            raise SystemExit(1)
    if do_corpus:
        refusal = uninstall_cmd.confirm_corpus_removal(args.yes, args.confirm_path)
        if refusal is not None:
            if do_state:
                print("state:  not reset (corpus confirmation failed; nothing was changed)")
            print(f"corpus: {refusal}")
            raise SystemExit(1)

    failed = False
    if do_state:
        outcome = reset_state(args.yes)
        print(f"state:  {outcome}")
        failed = failed or outcome.startswith(("refused", "REFUSED"))
    if do_corpus:
        outcome = reset_corpus(args.yes, args.confirm_path)
        print(f"corpus: {outcome}")
        failed = failed or outcome.startswith(("refused", "REFUSED"))
    raise SystemExit(1 if failed else 0)


if __name__ == "__main__":
    main()
