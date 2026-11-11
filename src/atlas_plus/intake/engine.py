"""One intake run, start to finish. The only module here that writes outside state.

Write discipline, same as sync/apply.py:
- the original is COPIED, never moved, until the copy is hash-verified;
- a destination holding different bytes is versioned (version_before_write)
  before it is replaced; identical bytes are left alone. Only destinations
  under the projects root are ever replaced: anywhere else an occupied path
  gets a ``_vN`` sibling (Decision.replace);
- writes are atomic (tmp + os.replace);
- the original is then ARCHIVED under <source>/_imported/<date>/ with a
  provenance sidecar, or left in place with ``leave_originals=True``;
- nothing is ever deleted.
"""

from __future__ import annotations

import json
import os
import shutil
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from atlas_plus import paths as atlas_paths
from atlas_plus.mcp.memory_server.versioning import version_before_write

from . import rules as rules_mod
from . import scan, store
from .frontmatter import read_frontmatter, scalar

QUARANTINE_DIRNAME = "_quarantine"


class IntakeConfigError(Exception):
    """The source folder is not configured or does not exist."""


def source_dir(explicit: Path | str | None = None) -> Path:
    """Resolve the drop folder: argument, then ``ATLAS_INTAKE_DIR``."""
    if explicit:
        return Path(explicit)
    env = os.environ.get("ATLAS_INTAKE_DIR", "").strip()
    if not env:
        raise IntakeConfigError(
            "no intake folder configured: pass --source or set ATLAS_INTAKE_DIR "
            "(for example the Google Drive 'Intake' folder; see docs/CONFIGURATION.md)."
        )
    return Path(env)


def _reachable(path: Path, *, attempts: int = 5, base_delay: float = 0.5,
               sleeper: Callable[[float], None] | None = None) -> tuple[bool, str]:
    """Is the drop folder a directory right now, allowing for cloud-mount hiccups?

    Google Drive for Desktop occasionally aborts a stat with WinError 995 or 3
    for a few hundred milliseconds (seen 2026-11-06, same class Ursa's
    drive_import handles). Retry with backoff before concluding anything.
    """
    import time

    sleep = sleeper or time.sleep
    last = ""
    for i in range(attempts):
        try:
            if path.is_dir():
                return True, ""
            return False, f"source folder not found: {path}"
        except OSError as exc:
            last = f"{exc.__class__.__name__}: {exc}"
            if i < attempts - 1:
                sleep(base_delay * (2 ** i))
    return False, f"source folder unreachable after {attempts} attempts: {last}"


def _collision_free(path: Path) -> Path:
    if not path.exists():
        return path
    n = 1
    while True:
        candidate = path.with_name(f"{path.stem}_v{n}{path.suffix}")
        if not candidate.exists():
            return candidate
        n += 1


def _frontmatter_summary(fm: dict) -> dict:
    keep = ("project", "artifact_type", "status", "version", "created", "owner",
            "intended_consumer", "related_systems", "region", "destination", "title", "tags")
    return {k: scalar(fm[k], 120) for k in keep if k in fm}


def _copy_verified(src: Path, dest: Path, expected_sha: str) -> None:
    """Atomic copy of src to dest, then re-hash dest; raises on mismatch."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".intake-tmp")
    try:
        shutil.copyfile(src, tmp)
        got = scan.sha256_file(tmp)
        if got != expected_sha:
            raise IOError(f"copy hash mismatch: source {expected_sha[:12]} vs copy {got[:12]}")
        os.replace(tmp, dest)
        try:
            shutil.copystat(src, dest)
        except OSError:
            pass
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def _archive_original(source_root: Path, cand: scan.Candidate, record: store.Record) -> Path:
    """Move the original under _imported/<date>/<rel> with a provenance sidecar."""
    day = datetime.now(timezone.utc).date().isoformat()
    parked = _collision_free(source_root / scan.ARCHIVE_DIRNAME / day / cand.rel)
    parked.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(cand.path), str(parked))
    meta = {
        "original_path": cand.rel,
        "sha256": record.sha256,
        "size": record.size,
        "imported_utc": record.ts,
        "destination": record.dest,
        "rule": record.rule,
        "run_id": record.run_id,
    }
    store.atomic_write_text(parked.with_name(parked.name + ".meta.json"),
                            json.dumps(meta, indent=2, sort_keys=True))
    return parked


def _quarantine(cand: scan.Candidate, sha: str, reason: str) -> Path:
    day = datetime.now(timezone.utc).date().isoformat()
    parked = _collision_free(rules_mod.landing_root() / QUARANTINE_DIRNAME / day / Path(cand.rel).name)
    _copy_verified(cand.path, parked, sha)
    store.atomic_write_text(parked.with_name(parked.name + ".meta.json"), json.dumps({
        "reason": reason, "original_path": cand.rel, "sha256": sha,
        "received_utc": store.now_utc()}, indent=2, sort_keys=True))
    return parked


# -- Post-import actions ---------------------------------------------------------

def _action_ticket(record: store.Record) -> str:
    from atlas_plus.tickets.store import create_ticket

    fm = record.frontmatter
    slug = rules_mod.project_slug(fm.get("project"))
    kind = fm.get("artifact_type") or Path(record.source_rel).suffix.lstrip(".") or "file"
    title = f"Intake: {Path(record.source_rel).name} ({kind})"
    body_lines = [
        f"Arrived via Sextant Intake on {record.ts}.",
        f"Landed at: {record.dest}",
        f"sha256: {record.sha256}",
        "",
        "Frontmatter:",
    ] + [f"- {k}: {v}" for k, v in fm.items()]
    if fm.get("intended_consumer"):
        body_lines += ["", f"Intended consumer: {fm['intended_consumer']}. Read the file, then turn it into a plan or backlog."]
    t = create_ticket(title=title, project=slug, assignee="human", priority="normal", effort="M",
                      body="\n".join(body_lines))
    tid = t.get("id") if isinstance(t, dict) else getattr(t, "id", None)
    return f"created {tid}" if tid else "created"


ACTIONS: dict[str, Callable[[store.Record], str]] = {
    "ticket": _action_ticket,
    # "notify" is a run-level action (one push per run), handled in run_once.
}


def _run_actions(record: store.Record, actions: tuple[str, ...]) -> None:
    for name in actions:
        fn = ACTIONS.get(name)
        if fn is None:
            continue
        try:
            record.actions[name] = fn(record)
        except Exception as exc:  # an action must never undo a verified import
            record.actions[name] = f"failed: {exc.__class__.__name__}: {str(exc)[:120]}"


def _notify(report: store.RunReport) -> str:
    imported = [r for r in report.records if r.status == "imported"]
    if not imported:
        return "nothing-to-announce"
    try:
        import asyncio

        from atlas_plus.telegram import notify

        names = ", ".join(Path(r.source_rel).name for r in imported[:5])
        more = f" (+{len(imported) - 5} more)" if len(imported) > 5 else ""
        ok = asyncio.run(notify.push(
            title=f"Intake: {len(imported)} file(s) landed",
            message=f"{names}{more}\n{report.summary_line()}",
            tags=["inbox_tray"],
        ))
        return "sent" if ok else "not-configured"
    except Exception as exc:
        return f"failed: {exc.__class__.__name__}"


# -- The run ----------------------------------------------------------------------

def run_once(
    source: Path | str | None = None,
    *,
    dry_run: bool = False,
    leave_originals: bool = False,
    settle_seconds: float = 2.0,
    min_age_seconds: float = 5.0,
    notify: bool = True,
    rules: list[rules_mod.Rule] | None = None,
    state_root: Path | None = None,
    sleeper: Callable[[float], None] | None = None,
) -> store.RunReport:
    """Process every stable candidate in the source folder once."""
    src = source_dir(source)
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    report = store.RunReport(run_id=run_id, started_utc=store.now_utc(), source=str(src), dry_run=dry_run)
    ok, why = _reachable(src, sleeper=sleeper)
    if not ok:
        # Not an error: an unmounted or hiccuping cloud drive is retried on
        # the next scheduled run. The note is the whole story.
        report.notes.append(why)
        report.finished_utc = store.now_utc()
        return report

    effective_rules = rules if rules is not None else rules_mod.load_rules()
    seen = store.imported_hashes(state_root)
    candidates, skipped = scan.discover(src)
    report.skipped = skipped
    stable_kw = {"settle_seconds": settle_seconds, "min_age_seconds": min_age_seconds}
    if sleeper is not None:
        stable_kw["sleeper"] = sleeper

    for cand in candidates:
        rec = store.Record(ts=store.now_utc(), status="error", source_root=str(src),
                           source_rel=cand.rel, size=cand.size, run_id=run_id)
        try:
            ok, why = scan.is_stable(cand.path, **stable_kw)
            if not ok:
                rec.status, rec.reason = "unstable", why
                report.records.append(rec)      # not ledgered: it will be retried
                continue
            rec.sha256 = scan.sha256_file(cand.path)
            fm = read_frontmatter(cand.path)
            rec.frontmatter = _frontmatter_summary(fm)
            decision = rules_mod.resolve(Path(cand.rel).name, fm, effective_rules)
            rec.rule = decision.rule.name

            prior = seen.get(rec.sha256)
            if prior is not None:
                rec.status = "duplicate"
                rec.dest = prior.get("dest", "")
                rec.reason = f"same bytes imported {prior.get('ts', '')[:19]}"
                if not dry_run:
                    store.ledger_append(rec, state_root)
                    if not leave_originals:
                        rec.archived_to = str(_archive_original(src, cand, rec))
                report.records.append(rec)
                continue

            if decision.dest is None:
                rec.reason = decision.reason
                if dry_run:
                    rec.status = "dry-run"
                    rec.dest = f"<quarantine> {decision.reason}"
                else:
                    rec.status = "quarantined"
                    rec.dest = str(_quarantine(cand, rec.sha256, decision.reason))
                    store.ledger_append(rec, state_root)
                    if not leave_originals:
                        rec.archived_to = str(_archive_original(src, cand, rec))
                report.records.append(rec)
                continue

            dest = decision.dest
            rec.dest = str(dest)
            if dest.is_file() and scan.sha256_file(dest) == rec.sha256:
                rec.status = "dry-run" if dry_run else "already-at-dest"
                rec.reason = "destination already holds identical bytes"
                if not dry_run:
                    store.ledger_append(rec, state_root)
                    seen[rec.sha256] = {"dest": rec.dest, "ts": rec.ts}
                    if not leave_originals:
                        rec.archived_to = str(_archive_original(src, cand, rec))
                report.records.append(rec)
                continue

            if dest.exists() and not decision.replace:
                # Only projects-root destinations may replace a file. Anywhere
                # else an occupied path (two drops sharing a basename on one
                # day, or an existing file the drop must never clobber) gets
                # a _vN sibling instead (audit 2026-11-09 rows 3 and 35).
                dest = _collision_free(dest)
                rec.dest = str(dest)

            if dry_run:
                rec.status = "dry-run"
                if dest.exists():
                    rec.reason = "would version the existing destination first"
                report.records.append(rec)
                continue

            if dest.exists():
                v = version_before_write(dest, root=decision.version_root)
                rec.versioned = str(v) if v else ""
            _copy_verified(cand.path, dest, rec.sha256)
            rec.status = "imported"
            _run_actions(rec, decision.rule.on_import)
            store.ledger_append(rec, state_root)
            seen[rec.sha256] = {"dest": rec.dest, "ts": rec.ts}
            try:
                from atlas_plus import activity
                activity.record("intake", "imported", rule=rec.rule, name=Path(cand.rel).name)
            except Exception:
                pass
            if not leave_originals:
                try:
                    rec.archived_to = str(_archive_original(src, cand, rec))
                except OSError as exc:
                    # The import is complete and ledgered; a locked original is
                    # retried as a duplicate (then archived) on the next run.
                    rec.actions["archive"] = f"deferred: {exc.__class__.__name__}"
            report.records.append(rec)
        except OSError as exc:
            # A transient mount abort (WinError 995/3) or a lock mid-read is
            # not a defect of the file: leave it for the next run, unledgered.
            # Nothing partial was written (copies go through tmp + re-hash).
            rec.status = "unstable"
            rec.reason = f"io: {exc.__class__.__name__}: {str(exc)[:160]}"
            report.records.append(rec)
        except Exception as exc:
            rec.status = "error"
            rec.reason = f"{exc.__class__.__name__}: {str(exc)[:200]}"
            report.notes.append(traceback.format_exc(limit=3))
            if not dry_run:
                store.ledger_append(rec, state_root)
            report.records.append(rec)

    if notify and not dry_run:
        report.notes.append(f"notify: {_notify(report)}")
    report.finished_utc = store.now_utc()
    if not dry_run:
        store.save_run(report, state_root)
        if report.records:
            write_manifest(src, state_root)
    return report


def write_manifest(source: Path, state_root: Path | None = None) -> Path:
    """Regenerate <source>/_imported/MANIFEST.md from the ledger for this source."""
    src_key = str(source)
    records = [d for d in store.ledger_load(state_root)
               if d.get("source_root") == src_key and d.get("status") != "unstable"]
    text = store.render_manifest(records, source=src_key)
    target = source / scan.ARCHIVE_DIRNAME / scan.MANIFEST_NAME
    store.atomic_write_text(target, text)
    return target
