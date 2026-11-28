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
import re
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


_VALIDATOR_TIMEOUT = 120  # seconds for the brand-kit validator, its whole process tree included

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
    sidecar = scan.sidecar_for(cand.path)
    if sidecar.is_file():
        # The sidecar belongs to this drop: it is archived beside the original.
        shutil.move(str(sidecar), str(parked.with_name(parked.name + scan.SIDECAR_SUFFIX)))
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


BRAND_INCOMING_DIRNAME = "_incoming"
BRAND_ZIP_MAX_ENTRIES = 5000
BRAND_ZIP_MAX_BYTES = 500 * 1024 * 1024
# A design export is a web bundle: HTML, CSS and browser JS are its content
# (Claude Design ships site/config.js, _ds_bundle.js, build.mjs). Those are
# extracted; OS executables, shell and Windows script hosts never are.
BRAND_ZIP_ALLOWED_SCRIPTS: frozenset[str] = frozenset({".js", ".mjs"})
BRAND_ZIP_REFUSED_SUFFIXES: frozenset[str] = rules_mod.EXECUTABLE_SUFFIXES - BRAND_ZIP_ALLOWED_SCRIPTS


def _brand_slug(record: store.Record) -> str:
    """Brand slug for an imported kit: sidecar ``project:`` wins, else the zip stem."""
    fm = record.frontmatter
    if fm.get("project"):
        return rules_mod.project_slug(fm["project"])
    stem = Path(record.source_rel).stem
    stem = re.sub(r"\s*\(\d+\)$", "", stem)          # "Sextant assets (2)" -> "Sextant assets"
    return rules_mod.project_slug(stem)


def _safe_zip_members(zf, root: Path) -> tuple[list, str | None]:
    """Members safe to extract under ``root``, or (``[]``, why) when the zip is refused.

    Refused outright: a member escaping the root (zip-slip), an absolute or drive
    path, an executable or script suffix (a kit is images, fonts, HTML, CSS, text),
    too many entries, or too many declared bytes.
    """
    members = []
    total = 0
    for info in zf.infolist():
        name = info.filename.replace("\\", "/")
        if info.is_dir():
            continue
        if name.startswith("/") or re.match(r"^[A-Za-z]:", name) or ".." in name.split("/"):
            return [], f"refused: unsafe member path {name!r}"
        if Path(name).suffix.lower() in BRAND_ZIP_REFUSED_SUFFIXES:
            return [], f"refused: executable member {name!r}"
        if not (root / name).resolve().is_relative_to(root.resolve()):
            return [], f"refused: member escapes extraction root {name!r}"
        total += info.file_size
        members.append(info)
        if len(members) > BRAND_ZIP_MAX_ENTRIES:
            return [], f"refused: more than {BRAND_ZIP_MAX_ENTRIES} entries"
        if total > BRAND_ZIP_MAX_BYTES:
            return [], f"refused: declared size over {BRAND_ZIP_MAX_BYTES // (1024 * 1024)} MB"
    return members, None


def _action_brand_import(record: store.Record) -> str:
    """Extract an archived kit zip to ``brand/_incoming/<date>/<slug>/``, validate, document.

    This is the automated half of brand/README.md's intake checklist (steps 1 to 3).
    Promotion into ``brand/<slug>/`` stays a human act: a draft is not a decision.
    """
    import subprocess
    import sys
    import zipfile

    zip_path = Path(record.dest)
    if not zip_path.is_file():
        return f"skipped: archived zip not found at {zip_path}"
    workspace = atlas_paths.workspace_root()
    day = datetime.now(timezone.utc).date().isoformat()
    slug = _brand_slug(record)
    target = _collision_free_dir(workspace / "brand" / BRAND_INCOMING_DIRNAME / day / slug)
    target.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as zf:
        members, why = _safe_zip_members(zf, target)
        if why:
            store.atomic_write_text(target / "REFUSED.md", f"# Refused\n\n{why}\n\nzip: {zip_path}\nsha256: {record.sha256}\n")
            return why
        for info in members:
            zf.extract(info, target)
    svgs = sum(1 for p in target.rglob("*.svg"))
    files = sum(1 for p in target.rglob("*") if p.is_file())
    # Validator: the same tool the manual checklist runs. Best effort; its report is kept beside the kit.
    validator = workspace / "brand" / "tools" / "validate_brand_svgs.py"
    verdict = "validator not found"
    if validator.is_file():
        try:
            from atlas_plus import _proc  # KL4: the validator is a workspace script; its tree ends at the timeout
            res = _proc.run_bounded([sys.executable, "-I", str(validator), str(target)],
                                    timeout=_VALIDATOR_TIMEOUT, max_bytes=1024 * 1024, cwd=str(workspace),
                                    env=_proc.other_party_env())  # a workspace script, not this package (AG127)
            if res.timed_out:
                raise subprocess.TimeoutExpired([str(validator)], _VALIDATOR_TIMEOUT)
            proc = subprocess.CompletedProcess([str(validator)], res.returncode,
                                               stdout=res.stdout_text().replace("\r\n", "\n"),
                                               stderr=res.stderr_text().replace("\r\n", "\n"))
            store.atomic_write_text(target / "VALIDATION.txt",
                                    f"exit={proc.returncode}\n\n{proc.stdout}\n{proc.stderr}")
            verdict = f"validator exit {proc.returncode}"
        except (OSError, subprocess.SubprocessError) as exc:
            verdict = f"validator failed: {exc.__class__.__name__}"
    store.atomic_write_text(target / "PROVENANCE.md", "\n".join([
        f"# Brand kit intake {day}",
        "",
        f"- source: {record.source_root} / {record.source_rel}",
        f"- archived zip: {zip_path}",
        f"- sha256: {record.sha256}",
        f"- rule: {record.rule}",
        f"- slug (proposed): {slug}",
        f"- files extracted: {files} ({svgs} svg)",
        f"- validation: {verdict} (see VALIDATION.txt)",
        "",
        "Next (human): review VALIDATION.txt, fix the master, then promote to brand/<slug>/",
        "(archive the previous kit dir with a PROVENANCE.md first). Strip C2PA from anything embedded.",
        "",
    ]))
    return f"extracted {files} files to {target} ({verdict})"


def brand_import_zip(zip_path: Path, project: str | None = None, source_note: str = "manual") -> str:
    """Run the brand-import action on an already-archived zip (a re-run after a refusal,
    or a kit that reached the archive by hand). Returns the action's summary line."""
    rec = store.Record(ts=store.now_utc(), status="imported", source_root=source_note,
                       source_rel=zip_path.name, size=zip_path.stat().st_size,
                       sha256=scan.sha256_file(zip_path), rule="manual", dest=str(zip_path),
                       frontmatter={"project": project} if project else {})
    return _action_brand_import(rec)


def _collision_free_dir(path: Path) -> Path:
    if not path.exists():
        return path
    n = 1
    while True:
        candidate = path.with_name(f"{path.name}_v{n}")
        if not candidate.exists():
            return candidate
        n += 1


ACTIONS: dict[str, Callable[[store.Record], str]] = {
    "ticket": _action_ticket,
    "brand-import": _action_brand_import,
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
    skip_executables: bool = False,
    date_from_mtime: bool = False,
    recursive: bool = True,
) -> store.RunReport:
    """Process every stable candidate in the source folder once.

    ``skip_executables`` leaves installers and scripts where they are (the
    Downloads sweep, 2026-11-15: an .exe in Downloads is something to run, not
    something to file). ``date_from_mtime`` makes ``{date}`` the file's own
    modification date, so a backlog lands under the day it was downloaded.
    """
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
    # recursive=False is the Downloads sweep: only loose files at the top. A
    # Downloads folder holds extracted tool trees (75k-file ELK dumps, site
    # exports); those are never intake (2026-11-15).
    candidates, skipped = scan.discover(src, recursive=recursive)
    report.skipped = skipped
    stable_kw = {"settle_seconds": settle_seconds, "min_age_seconds": min_age_seconds}
    if sleeper is not None:
        stable_kw["sleeper"] = sleeper

    for cand in candidates:
        if skip_executables and cand.path.suffix.lower() in rules_mod.EXECUTABLE_SUFFIXES:
            report.skipped.append((cand.rel, "executable-left-in-place"))
            continue
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
            today = None
            if date_from_mtime:
                today = datetime.fromtimestamp(cand.mtime_ns / 1e9, tz=timezone.utc).date().isoformat()
            decision = rules_mod.resolve(Path(cand.rel).name, fm, effective_rules, today=today, path=cand.path)
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
