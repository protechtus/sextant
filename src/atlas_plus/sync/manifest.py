"""Corpus manifest: content-hash inventory plus the three-way diff.

The manifest is the protocol's unit of truth: {relpath: {sha256, size,
mtime}} over every syncable file, hashed as a whole so two systems can
compare state with one string. diff3() turns (local, remote, base) into a
SyncPlan; base is the manifest recorded at the end of the last successful
sync with that particular remote, which is what lets us tell "they changed
it" from "we changed it" without timestamps or vector clocks.

Design: docs/SEXTANT-SYNC-DESIGN-2026-08.md sections 6 and 7.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from atlas_plus.sync import policy


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


# -- hash cache (measured, quality loop run 21) ------------------------------
# build_manifest re-hashed every corpus file on every call: 866ms at 2,000
# files, on BOTH ends of every sync cycle. Unchanged files -- the vast
# majority -- now reuse the cached digest, keyed by (st_mtime_ns, size), the
# same change-detection contract the FTS index uses. The cache is a pure
# optimization: missing, stale, or corrupt cache files just mean a full
# re-hash. One cache file per corpus root so different roots never clobber
# each other; rewritten with only the entries seen this build, so deleted
# files age out for free.

def _hash_cache_path(root: Path) -> Path:
    from atlas_plus import paths as atlas_paths
    tag = hashlib.sha256(str(root.resolve()).encode("utf-8")).hexdigest()[:16]
    return atlas_paths.state_dir() / "sync" / f"hash-cache-{tag}.json"


def _load_hash_cache(root: Path) -> dict:
    # except Exception, not just OSError/ValueError (expert pass 2026-10-05,
    # finding 8): state_dir() can raise SystemExit on an unfilled template
    # placeholder and resolve() can raise RuntimeError on a symlink loop --
    # a cache problem may cost speed, never a hub API call.
    try:
        raw = json.loads(_hash_cache_path(root).read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except SystemExit:
        return {}
    except Exception:
        return {}


def _save_hash_cache(root: Path, cache: dict) -> None:
    """Best-effort atomic write; a failed save only costs future speed."""
    try:
        target = _hash_cache_path(root)
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(target.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(cache, f, separators=(",", ":"))
            os.replace(tmp, target)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    except SystemExit:
        pass  # state_dir() placeholder guard must not kill a manifest build
    except Exception:
        pass


def files_hash(files: dict) -> str:
    """Stable hash of a manifest's file table (order-independent).

    surrogatepass (hardening, quality loop run 32): a POSIX filename with
    invalid UTF-8 bytes decodes to lone surrogates (PEP 383), which strict
    utf-8 encoding refuses -- ONE such filename crashed the whole manifest
    build. surrogatepass round-trips them deterministically, so the build
    survives and the hash stays stable.
    """
    # Content identity only: two corpora with the same bytes must hash the same, whatever
    # their mtimes and sizes-on-disk (`checkin` said "different corpus state" forever on an
    # identical pair; self-hoster pass 2026-11-10).
    content = {rel: (entry.get("sha256") if isinstance(entry, dict) else entry)
               for rel, entry in files.items()}
    canonical = json.dumps(content, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8", "surrogatepass")).hexdigest()


def _is_reparse_dir(entry) -> bool:
    """A Windows junction is a directory, not a symlink, to DirEntry.is_dir(follow_symlinks=False)
    on every supported Python; it must still never be entered (wave-2 2026-11-09, R2-02)."""
    try:
        st = entry.stat(follow_symlinks=False)
    except OSError:
        return True
    # IsReparseTagNameSurrogate (bit 29): junction 0xA0000003 and symlink 0xA000000C redirect
    # the NAME to another place; cloud placeholders (OneDrive/Dropbox Files On-Demand), dedup,
    # WOF and app-exec links also carry FILE_ATTRIBUTE_REPARSE_POINT but stay where they are
    # and must not be refused (round C 2026-11-10, lane C F1).
    return bool(getattr(st, "st_reparse_tag", 0) & 0x20000000)


def _iter_files(root: Path):
    """Yield (rel_posix, Path, stat) for every regular file under root,
    sorted by rel path.

    scandir instead of rglob (measured, quality loop run 21): rglob's
    per-entry is_file/is_symlink/stat cost ~4 syscalls per file on Windows;
    DirEntry carries the directory-read stat info for free. Directory
    symlinks are never followed (a loop or an out-of-corpus link must not
    enumerate into a sync manifest); symlinked files stay excluded as
    before."""
    out: list[tuple[str, Path, os.stat_result]] = []
    stack = [str(root)]
    while stack:
        d = stack.pop()
        try:
            with os.scandir(d) as it:
                for entry in it:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            # Prune excluded trees at the WALK (measured,
                            # quality loop run 35): policy denies any path
                            # with an excluded segment, so entering
                            # archive/, versions/ or .git only to discard
                            # every entry wasted 97% of the enumeration on
                            # workspace-shaped corpora. Same exact-case
                            # semantics as policy's segment check.
                            if entry.name in policy.EXCLUDED_DIR_NAMES:
                                continue
                            if _is_reparse_dir(entry):
                                continue  # a junction leads out of the corpus
                            stack.append(entry.path)
                        elif entry.is_file(follow_symlinks=False):
                            rel = Path(entry.path).relative_to(root).as_posix()
                            out.append((rel, Path(entry.path),
                                        entry.stat(follow_symlinks=False)))
                    except OSError:
                        continue  # unreadable entry: not syncable, not fatal
        except OSError:
            continue
    out.sort(key=lambda t: t[0])
    return out


def build_manifest(root: Path, principal: policy.Principal | None = None) -> dict:
    """Walk root and return {"generated_utc", "manifest_hash", "files"}.

    Exclusions and principal filtering happen HERE, so nothing a caller may
    not see is ever even enumerated into the structure handed to them.
    """
    files: dict[str, dict] = {}
    if root.exists():
        cache = _load_hash_cache(root)
        fresh: dict[str, list] = {}
        # Racy-entry guard (expert pass 2026-10-05, M3 -- git's racy-index
        # rule): a file whose mtime is within this margin of NOW may still
        # be rewritten inside the same timestamp tick (coarse mounts: FAT
        # 2s, SMB worse), which would pin a stale digest forever. Such
        # entries are hashed but never CACHED, so the next build re-hashes
        # them; once the file is older than the margin it caches normally.
        racy_margin_ns = 3_000_000_000  # 3s
        now_ns = time.time_ns()
        for rel, path, stat in _iter_files(root):
            if policy.deny_reason(rel, principal, "read") is not None:
                continue
            try:
                cached = cache.get(rel)
                is_racy = now_ns - stat.st_mtime_ns <= racy_margin_ns
                if (not is_racy
                        and isinstance(cached, list) and len(cached) == 3
                        and cached[0] == stat.st_mtime_ns
                        and cached[1] == stat.st_size):
                    digest = cached[2]
                else:
                    digest = _sha256_file(path)
                if not is_racy:
                    fresh[rel] = [stat.st_mtime_ns, stat.st_size, digest]
                files[rel] = {
                    "sha256": digest,
                    "size": stat.st_size,
                    "mtime": int(stat.st_mtime),
                }
            except OSError:
                continue  # unreadable file: not syncable, not fatal
        # A restricted principal enumerates only its visible subset; replacing
        # the cache with that subset would evict entries other callers still
        # need. Full (trusted-local) builds replace -- which ages out deleted
        # files -- while principal-filtered builds merge.
        merged = fresh if principal is None else {**cache, **fresh}
        if merged != cache:
            _save_hash_cache(root, merged)
    return {
        "generated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "manifest_hash": files_hash(files),
        "files": files,
    }


@dataclass
class SyncPlan:
    """What a sync between local and remote would do. Paths are corpus-relative."""

    to_pull: list[str] = field(default_factory=list)
    to_push: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    local_deleted: list[str] = field(default_factory=list)
    remote_deleted: list[str] = field(default_factory=list)
    in_sync: int = 0
    # A peer's unchanged proposals (awaiting the hub's review): neither pushed nor pulled.
    proposed: list[str] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return not (
            self.to_pull
            or self.to_push
            or self.conflicts
            or self.local_deleted
            or self.remote_deleted
        )

    def summary(self) -> str:
        extra = f", proposed {len(self.proposed)}" if self.proposed else ""
        return (
            f"pull {len(self.to_pull)}, push {len(self.to_push)}, "
            f"conflicts {len(self.conflicts)}, in sync {self.in_sync}{extra}"
        )


def diff3(local: dict, remote: dict, base: dict | None) -> SyncPlan:
    """Three-way diff over manifest file tables ({relpath: {sha256,...}}).

    Rules (hashes; None = absent):
    - local == remote                      -> in sync
    - local == base, remote differs        -> pull (remote deleted -> report)
    - remote == base, local differs        -> push (local deleted -> report)
    - both differ from base and each other -> conflict (incl. delete-vs-edit)

    Deletes are never propagated automatically (archive-not-delete); they are
    reported for the operator. See design doc section 7.
    """
    base = base or {}
    plan = SyncPlan()

    def h(table: dict, rel: str) -> str | None:
        entry = table.get(rel)
        return entry["sha256"] if entry else None

    for rel in sorted(set(local) | set(remote) | set(base)):
        l, r, b = h(local, rel), h(remote, rel), h(base, rel)
        if l == r:
            if l is not None:
                plan.in_sync += 1
            # l == r == None: existed only in base; both sides dropped it.
            continue
        if l == b:
            if r is None:
                plan.remote_deleted.append(rel)
            else:
                plan.to_pull.append(rel)
        elif r == b:
            if l is None:
                plan.local_deleted.append(rel)
            else:
                plan.to_push.append(rel)
        else:
            plan.conflicts.append(rel)
    return plan
