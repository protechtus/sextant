"""Bring your existing knowledge with you: structural importers (gap G-3).

    atlas-import --source obsidian --path <vault> [--scope reference/obsidian]
    atlas-import --source chatgpt --path conversations.json
    atlas-import --source claude --path claude-export.zip
    (add --yes to execute; default is a report of what WOULD land)

(The public distribution also exposes it as `sextant-import`.)

v1 is deliberately STRUCTURAL: files land readable and provenance-stamped;
no LLM touches them. Distillation into curated memories is a later, opt-in
Brain job. Import never overwrites anything: collisions get _2, _3 ...
suffixes, and the corpus's own exclusion dirs are skipped on the way in.
After a large import, rebuild the indexes:
    atlas-fts-rebuild   and   atlas-embed-index
"""

from __future__ import annotations

import os

import argparse
import json
import re
import sys

import yaml
from datetime import datetime, timezone
from pathlib import Path

from atlas_plus import paths as atlas_paths

_SKIP_DIRS = {".obsidian", ".git", ".trash", "node_modules", "__pycache__"}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _slug(text: str, max_len: int = 60) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return (slug or "untitled")[:max_len]


def _collision_free(path: Path) -> Path:
    if not path.exists():
        return path
    n = 2
    while True:
        candidate = path.with_name(f"{path.stem}_{n}{path.suffix}")
        if not candidate.exists():
            return candidate
        n += 1


_ORIGINAL_PATH_RE = re.compile(r"^original_path:\s*(.+?)\s*$", re.M)


def _import_copies(dest: Path, src: Path, listing: dict[Path, list[str]],
                   rel: str | None = None) -> list[Path]:
    """``dest`` plus every ``<stem>_N<suffix>`` collision copy beside it that was imported
    FROM ``src`` (its frontmatter ``original_path`` says so).

    One directory listing per parent per run (``listing`` cache) and a regex on the
    name: a glob pattern built from the stem broke on ``[``/``]`` and re-scanned the
    directory per note (10x slower on 2,000 notes); a sibling vault note ``a_2.md`` is
    not a copy of ``a.md`` (round C 2026-11-10, lane C F2/F3/F4).
    """
    out = [dest]
    parent = dest.parent
    names = listing.get(parent)
    if names is None:
        try:
            names = os.listdir(parent)
        except OSError:
            names = []
        listing[parent] = names
    pat = re.compile(re.escape(dest.stem) + r"_(\d+)" + re.escape(dest.suffix) + r"$")
    # Stamps are vault-relative now (the absolute form carried the local user name into a
    # corpus that sync can ship, AG14); copies stamped by older imports still match.
    want = {str(src), rel} if rel else {str(src)}
    for name in sorted(n for n in names if pat.match(n)):
        cand = parent / name
        try:
            head = cand.read_text(encoding="utf-8-sig", errors="replace")[:4096]
        except OSError:
            continue
        m = _ORIGINAL_PATH_RE.search(head)
        try:
            stamped = yaml.safe_load(m.group(1))
        except yaml.YAMLError:
            stamped = None
        if stamped in want:
            out.append(cand)
    return out


def _chat_already_imported(dest: Path, text_out: str, listing: dict[Path, list[str]]) -> bool:
    """True when ``dest`` or one of its ``_N`` collision copies already holds this
    conversation's body (frontmatter differs by ``imported_at`` only).

    Re-running a ChatGPT/Claude import doubled every conversation while the Obsidian path
    skipped (persona pass 2026-11-10, "Priya"); the documented "report first, then --yes"
    habit must never grow a corpus on its own.
    """
    parent = dest.parent
    names = listing.get(parent)
    if names is None:
        try:
            names = os.listdir(parent)
        except OSError:
            names = []
        listing[parent] = names
    pat = re.compile(re.escape(dest.stem) + r"(_\d+)?" + re.escape(dest.suffix) + r"$")
    want = _fm_body(text_out).strip()
    for name in names:
        if not pat.match(name):
            continue
        try:
            have = (parent / name).read_text(encoding="utf-8-sig", errors="replace")
        except OSError:
            continue
        if _fm_body(have).strip() == want:
            return True
    return False


def _has_frontmatter(text: str) -> bool:
    return text.startswith("---")


from atlas_plus._fsutil import is_link as _is_link   # noqa: E402

MAX_IMPORT_CHARS = 4_000_000   # one imported note; a 100 MB message made a 100 MB note (AG16)


def _read_note(src: Path) -> tuple[str | None, str | None]:
    """(text, note) for a vault file: UTF-8 (BOM tolerated) strictly, UTF-16 by its BOM,
    anything else as cp1252 with a note; a file with NUL bytes is binary and skipped.
    A latin-1 note lost its accents and a binary `.md` was indexed as garbage with no word
    said (AG14, sweep 11)."""
    try:
        raw = src.read_bytes()
    except OSError as exc:
        return None, f"unreadable: {exc}"
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        try:
            return raw.decode("utf-16"), "decoded as UTF-16"
        except UnicodeDecodeError:
            return None, "binary (undecodable UTF-16)"
    if b"\x00" in raw[:8192]:
        return None, "binary (NUL bytes)"
    try:
        return raw.decode("utf-8-sig"), None
    except UnicodeDecodeError:
        return raw.decode("cp1252", errors="replace"), "not UTF-8: decoded as cp1252"


def _write_lf(target: Path, text: str) -> None:
    """Write an imported note: LF line endings (write_text made CRLF on Windows, AG14), control
    characters and lone surrogates scrubbed (a lone surrogate left a zero-byte note and aborted
    the import, AG16), capped, and atomic (tmp + replace)."""
    from atlas_plus.mcp.memory_server.routing import _scrub_controls
    text = _scrub_controls(text)
    if len(text) > MAX_IMPORT_CHARS:
        text = text[:MAX_IMPORT_CHARS] + f"\n\n[truncated by sextant import: {len(text):,} characters]\n"
    tmp = target.with_name(f"{target.name}.{os.getpid()}.imp-tmp")
    try:
        with tmp.open("w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        os.replace(tmp, target)
    except BaseException:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _clean_role(role) -> str:
    """A role with line breaks became a heading carrying forged block markers (AG16)."""
    cleaned = re.sub(r"[^\w .-]+", " ", str(role or "?")).strip()[:40]
    return cleaned or "?"


def _fm_block(fields: dict) -> str:
    """Frontmatter block via yaml.safe_dump: quotes, colons, newlines and
    leading indicators in titles/paths can never break parsing."""
    import yaml

    body = yaml.safe_dump(
        fields, sort_keys=False, allow_unicode=True,
        default_flow_style=False, width=10**6,
    )
    return f"---\n{body}---\n\n"


def _safe_scope(scope: str) -> str:
    """Validate --scope: a plain corpus-relative path that is not absolute,
    has no '..', and is not one of the corpus's own internal directories."""
    from atlas_plus.sync.policy import is_safe_relpath

    from atlas_plus.sync import policy as _policy
    from atlas_plus import paths as _paths

    raw = scope.strip().replace("\\", "/")
    if raw.startswith("/"):
        raise ValueError(f"unsafe --scope {scope!r}: must be relative, not absolute")
    cleaned = raw.rstrip("/")
    if not is_safe_relpath(cleaned):
        raise ValueError(f"unsafe --scope {scope!r}: must be a plain relative path")
    internal = {"versions", "archive", "_atlas", "_sync"}

    def _check_parts(parts: list[str], label: str) -> None:
        # Every component as NTFS will see it: no 8.3 alias, trailing dot or space, device
        # name, dot-prefix, and no internal directory anywhere (round-B 2026-11-09, row 28).
        for part in parts:
            if not _policy.is_portable_component(part):
                raise ValueError(f"unsafe --scope {scope!r}: component {part!r} is not portable{label}")
            norm = _policy.normalize_component(part)
            if norm.startswith(".") or norm in internal:
                raise ValueError(f"unsafe --scope {scope!r}: {part!r} is an internal directory{label}")

    _check_parts(cleaned.split("/"), "")
    root = _paths.memory_root()
    try:
        real = os.path.realpath(root / cleaned)
        real_rel = Path(real).relative_to(Path(os.path.realpath(root))).as_posix()
    except (OSError, ValueError):
        raise ValueError(f"unsafe --scope {scope!r}: resolves outside the corpus")
    if real_rel != cleaned:
        _check_parts(real_rel.split("/"), f" (resolves to {real_rel!r})")
    return cleaned


def _frontmatter_for(title: str, source: str, original: str) -> str:
    return _fm_block({
        "id": _slug(title),
        "title": title,
        "type": "imported",
        "imported_from": source,
        "imported_at": _now_iso(),
        "original_path": original,
    })


def _fm_body(text: str) -> str:
    m = re.match(r"---\r?\n.*?\r?\n---[ \t]*(?:\r?\n|$)", text, re.S)
    return text[m.end():] if m else text


def _already_imported(dest_text: str, raw: str) -> bool:
    """dest is this note already imported (stamps/frontmatter differ run to run)."""
    if dest_text.endswith(raw):
        return True
    return _fm_body(dest_text).strip() == _fm_body(raw).strip()


def _stamp_existing_frontmatter(text: str, source: str, original: str) -> str:
    """Merge provenance keys into a note that already has frontmatter.

    The user's own keys are kept (and win for `source`); the provenance keys
    `imported_from`, `imported_at` and `original_path` are set. Returns None when the
    leading block is not a YAML mapping (a horizontal rule, broken YAML, a list): the caller
    then prepends its own frontmatter and the original text stays intact in the body
    (AG14, sweep 11: such notes used to get no provenance at all)."""
    import yaml

    m = re.match(r"---\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|$)", text, re.S)
    if not m:
        return None
    try:
        data = yaml.safe_load(m.group(1))
    except yaml.YAMLError:
        return None
    if data is None:
        data = {}
    if not isinstance(data, dict):
        return None
    data.setdefault("source", source)
    # Provenance is ours to state: a vault key `original_path: elsewhere` survived the merge
    # and lied about where the note came from (AG14, sweep 11).
    data["imported_from"] = source
    data["imported_at"] = _now_iso()
    data["original_path"] = original
    body = text[m.end():]
    return _fm_block(data).rstrip("\n") + "\n\n" + body.lstrip("\r\n")


# -- Obsidian ----------------------------------------------------------------

def plan_obsidian(vault: Path, scope: str) -> list[tuple[Path, Path]]:
    """[(source_file, destination)] for every importable note."""
    return _plan_obsidian(vault, scope)[0]


def _plan_obsidian(vault: Path, scope: str) -> tuple[list[tuple[Path, Path]], list[str]]:
    """The plan plus the links that were NOT followed. A file symlink pointing outside the
    vault was imported (its outside content landed in the corpus and in search) and a linked
    directory was skipped without a word (AG14, sweep 11): links are never followed and every
    one is named in the report; a file whose real path is outside the vault is skipped too."""
    scope = _safe_scope(scope)
    dest_root = atlas_paths.memory_root() / scope
    plan: list[tuple[Path, Path]] = []
    skipped_links: list[str] = []
    try:
        vault_real = vault.resolve()
    except OSError:
        vault_real = vault
    for dirpath, dirnames, filenames in os.walk(vault, followlinks=False):
        d = Path(dirpath)
        rel_dir = d.relative_to(vault)
        keep = []
        for name in sorted(dirnames):
            if name in _SKIP_DIRS or name.startswith("."):
                continue
            if _is_link(d / name):
                skipped_links.append((rel_dir / name).as_posix() + "/")
                continue
            keep.append(name)
        dirnames[:] = keep
        for name in sorted(filenames):
            if not name.lower().endswith(".md") or name.startswith("."):
                continue
            src = d / name
            if _is_link(src):
                skipped_links.append((rel_dir / name).as_posix())
                continue
            try:
                inside = src.resolve().is_relative_to(vault_real)
            except OSError:
                inside = False
            if not inside:
                skipped_links.append((rel_dir / name).as_posix())
                continue
            plan.append((src, dest_root / rel_dir / name))
    return plan, skipped_links


def _existing_bodies(dest_root: Path) -> dict[str, tuple[str, str | None]]:
    """body-hash -> (corpus-relative path, stamped original_path) for every note already under
    dest_root (one walk)."""
    import hashlib
    out: dict[str, tuple[str, str | None]] = {}
    if not dest_root.is_dir():
        return out
    root = atlas_paths.memory_root()
    for dirpath, _dirs, files in os.walk(dest_root):
        for name in files:
            if not name.lower().endswith(".md"):
                continue
            p = Path(dirpath) / name
            try:
                text = p.read_text(encoding="utf-8-sig", errors="replace")
            except OSError:
                continue
            body = _fm_body(text).strip()
            h = hashlib.sha256(body.encode("utf-8", errors="replace")).hexdigest()
            m = _ORIGINAL_PATH_RE.search(text[:4096])
            origin = None
            if m:
                try:
                    v = yaml.safe_load(m.group(1))
                    origin = v if isinstance(v, str) else None
                except yaml.YAMLError:
                    origin = None
            try:
                rel = p.relative_to(root).as_posix()
            except ValueError:
                rel = str(p)
            out.setdefault(h, (rel, origin))
    return out


def _vault_still_has(vault: Path, origin: str) -> bool:
    try:
        p = Path(origin) if os.path.isabs(origin) else vault / origin
        return p.exists()
    except OSError:
        return False


def import_obsidian(vault: Path, scope: str, execute: bool) -> dict:
    import hashlib
    plan, skipped_links = _plan_obsidian(vault, scope)
    imported = skipped = 0
    skipped_binary: list[str] = []
    decoded: list[str] = []
    moved: list[str] = []
    superseded: list[str] = []
    sample: list[str] = []
    listing: dict[Path, list[str]] = {}
    root = atlas_paths.memory_root()
    dest_root = root / _safe_scope(scope)
    existing = _existing_bodies(dest_root)
    for src, dest in plan:
        rel_src = src.relative_to(vault).as_posix()
        text, note = _read_note(src)
        if text is None:
            skipped_binary.append(f"{rel_src}: {note}")
            continue
        if note:
            decoded.append(f"{rel_src}: {note}")
        raw = text
        if len(sample) < 20:
            sample.append(dest.relative_to(root).as_posix())
        # A note renamed or moved inside the vault since the last import is the SAME note:
        # it used to land again under the new name beside the old copy (AG14, sweep 11).
        body_hash = hashlib.sha256(_fm_body(raw).strip().encode("utf-8", errors="replace")).hexdigest()
        prior = existing.get(body_hash)
        if prior is not None:
            prior_rel, prior_origin = prior
            same_slot = dest.exists() and dest.relative_to(root).as_posix() == prior_rel
            # Only a note whose ORIGINAL vault file is gone counts as moved: two different vault
            # notes with the same body are two notes (round C: a.md equal to its sibling a_2.md).
            if not same_slot and prior_origin and not _vault_still_has(vault, prior_origin):
                moved.append(f"{rel_src} is already imported as {prior_rel} (renamed or moved in the vault)")
                skipped += 1
                continue
        if not execute:
            continue
        stamped = _stamp_existing_frontmatter(raw, "obsidian", rel_src) if _has_frontmatter(raw) else None
        text = stamped if stamped is not None else _frontmatter_for(src.stem, "obsidian", rel_src) + raw
        # Idempotent re-import (edge probe, quality loop run 16): re-running
        # the same import previously duplicated EVERY note via the collision
        # suffix. A destination whose content ends with the source's current
        # text (frontmatter is prepended, and stamps differ run-to-run) is
        # this note, already imported -- skip it. Changed notes still land
        # as a collision-suffixed new copy, never overwriting.
        # ...and so does a changed note's collision copy: re-running an unchanged import
        # of an already re-imported note must not grow _2, _3, ... (wave-2 2026-11-09).
        copies = _import_copies(dest, src, listing, rel_src)
        if any(
            cand.is_file() and _already_imported(
                cand.read_text(encoding="utf-8-sig", errors="replace"), raw
            )
            for cand in copies
        ):
            skipped += 1
            continue
        target = _collision_free(dest)
        if target != dest:
            # An edited note lands as a new copy; name the stale one so the user can archive
            # it instead of finding both in every search (AG14, sweep 11).
            superseded.append(f"{dest.relative_to(root).as_posix()} is superseded by "
                              f"{target.relative_to(root).as_posix()}; archive the older copy")
        target.parent.mkdir(parents=True, exist_ok=True)
        _write_lf(target, text)
        listing.get(target.parent, []).append(target.name)
        imported += 1
    return {"source": "obsidian", "planned": len(plan), "imported": imported,
            "skipped_already_imported": skipped, "destination": scope,
            "skipped_links": skipped_links, "skipped_binary": skipped_binary,
            "decoded_non_utf8": decoded, "already_imported_under_another_name": moved,
            "superseded": superseded, "sample_destinations": sample}


# -- ChatGPT export ----------------------------------------------------------

def _chatgpt_messages(conv: dict) -> list[tuple[str, str]]:
    """Flatten a conversations.json mapping tree into (role, text) turns."""
    out: list[tuple[str, str]] = []
    mapping = conv.get("mapping")
    if not isinstance(mapping, dict):
        return []
    # The mapping is a tree: every regenerate or edit leaves an abandoned branch. Walk the
    # live branch from current_node up its parents (AG14, sweep 11: sorting every node by time
    # put the abandoned answers into the conversation). Fall back to time order without it.
    nodes: list[dict] = []
    current = conv.get("current_node")
    if isinstance(current, str) and current in mapping:
        seen: set[str] = set()
        node_id: object = current
        while isinstance(node_id, str) and node_id in mapping and node_id not in seen:
            seen.add(node_id)
            node = mapping[node_id]
            if not isinstance(node, dict):
                break
            nodes.append(node)
            node_id = node.get("parent")
        nodes.reverse()
        nodes = [n for n in nodes if isinstance(n.get("message"), dict)]
    if not nodes:
        nodes = [n for n in mapping.values() if isinstance(n, dict) and isinstance(n.get("message"), dict)]

        def _key(n):
            ts = n["message"].get("create_time")
            return ts if isinstance(ts, (int, float)) else 0

        nodes.sort(key=_key)

    for node in nodes:
        msg = node["message"]
        author = msg.get("author")
        role = _clean_role(author.get("role", "?") if isinstance(author, dict) else "?")
        if role == "system":
            continue
        content = msg.get("content")
        parts = content.get("parts") if isinstance(content, dict) else None
        if not isinstance(parts, list) or not parts:
            continue
        text = "\n".join(str(p) for p in parts if isinstance(p, str)).strip()
        if text:
            out.append((role, text))
    return out


def _load_zip_member(path: Path, suffix: str):
    """JSON of the first member whose name ends with ``suffix`` (size-capped)."""
    import zipfile
    try:
        with zipfile.ZipFile(path) as zf:
            name = next((n for n in zf.namelist() if n.endswith(suffix)), None)
            if not name:
                raise ValueError(f"no {suffix} inside the export zip")
            info = zf.getinfo(name)
            if info.file_size > _MAX_EXPORT_MEMBER_BYTES:
                raise ValueError(
                    f"{name} declares {info.file_size:,} bytes inflated "
                    f"(cap {_MAX_EXPORT_MEMBER_BYTES:,}) -- refusing a "
                    "suspected decompression bomb; if this is a genuine "
                    "export, unzip it yourself and import the JSON directly")
            return json.loads(zf.read(name).decode("utf-8-sig", errors="replace"))
    except zipfile.BadZipFile as exc:
        raise ValueError(f"not a valid zip archive: {exc}") from exc


def plan_chatgpt(export_file: Path) -> list[dict]:
    # The export arrives as a zip with conversations.json inside; it used to be detected as a
    # Claude export and import nothing (AG14, sweep 11).
    if export_file.suffix.lower() == ".zip":
        data = _load_zip_member(export_file, "conversations.json")
    else:
        data = json.loads(export_file.read_text(encoding="utf-8-sig", errors="replace"))
    if isinstance(data, dict) and "conversations" in data:
        data = data["conversations"]
    if not isinstance(data, list):
        raise ValueError("unrecognized ChatGPT export shape (expected a list of conversations)")
    return data


def _chat_dest(dest_root: Path, stamp, title: str) -> Path:
    if stamp is None:
        return dest_root / "undated" / f"undated-{_slug(title)}.md"
    return dest_root / stamp.strftime("%Y-%m") / f"{stamp.date().isoformat()}-{_slug(title)}.md"


def import_chatgpt(export_file: Path, execute: bool) -> dict:
    conversations = plan_chatgpt(export_file)
    dest_root = atlas_paths.memory_root() / "reference" / "imports" / "chatgpt"
    imported = skipped = already = would = 0
    skipped_reasons: list[str] = []
    listing: dict[Path, list[str]] = {}
    for index, conv in enumerate(conversations):
        # A wrong-shaped entry (null, a string) is skipped with a reason, never a
        # traceback after partial writes (wave-2 2026-11-09).
        if not isinstance(conv, dict):
            skipped += 1
            skipped_reasons.append(f"entry {index}: not a conversation object")
            continue
        title = (str(conv.get("title") or "untitled")).strip() or "untitled"
        created = conv.get("create_time")
        try:
            stamp = datetime.fromtimestamp(float(created), tz=timezone.utc)
        except (TypeError, ValueError, OverflowError, OSError):
            # An undated conversation used to be filed under TODAY, so a re-run on another day
            # imported it again (AG14, sweep 11): the name must not depend on the run date.
            stamp = None
        turns = _chatgpt_messages(conv)
        if not turns:
            skipped += 1
            skipped_reasons.append(f"{title!r}: no user/assistant text turns found")
            continue
        body_lines = [f"## {role}\n\n{text}\n" for role, text in turns]
        text = (
            _fm_block({
                "id": f"chatgpt-{_slug(title)}",
                "title": title,
                "type": "imported",
                "imported_from": "chatgpt",
                "imported_at": _now_iso(),
                "conversation_date": stamp.date().isoformat() if stamp else "unknown",
            })
            + f"# {' '.join(title.split())}\n\n" + "\n".join(body_lines)
        )
        dest = _chat_dest(dest_root, stamp, title)
        if _chat_already_imported(dest, text, listing):
            already += 1
            continue
        if not execute:
            would += 1
            continue
        target = _collision_free(dest)
        target.parent.mkdir(parents=True, exist_ok=True)
        _write_lf(target, text)
        listing.setdefault(target.parent, []).append(target.name)
        imported += 1
    return {"source": "chatgpt", "planned": len(conversations),
            "imported": imported, "would_import": would, "skipped_already_imported": already,
            "skipped_empty": skipped, "skipped_reasons": skipped_reasons,
            "destination": "reference/imports/chatgpt"}


# -- claude.ai export --------------------------------------------------------

# Decompression safety cap for the zip path: the declared inflated size of
# conversations.json must stay under this. A real export is tens of MB; a
# decompression bomb declares (and zipfile enforces on read) gigabytes from
# a tiny file. CPython's ZipExtFile bounds reads to the declared file_size,
# so checking it BEFORE read() is a sound guard, not advisory.
_MAX_EXPORT_MEMBER_BYTES = 1 * 1024 * 1024 * 1024  # 1 GiB


def _load_claude_export(path: Path) -> list:
    """conversations.json from a claude.ai data export; accepts the zip too."""
    if path.suffix.lower() == ".zip":
        data = _load_zip_member(path, "conversations.json")
    else:
        data = json.loads(path.read_text(encoding="utf-8-sig", errors="replace"))
    if not isinstance(data, list):
        raise ValueError("unrecognized Claude export shape (expected a list of conversations)")
    return data


def import_claude(export_file: Path, execute: bool) -> dict:
    conversations = _load_claude_export(export_file)
    dest_root = atlas_paths.memory_root() / "reference" / "imports" / "claude"
    imported = skipped = already = would = 0
    skipped_reasons: list[str] = []
    listing: dict[Path, list[str]] = {}
    for index, conv in enumerate(conversations):
        if not isinstance(conv, dict):
            skipped += 1
            skipped_reasons.append(f"entry {index}: not a conversation object")
            continue
        title = (str(conv.get("name") or "untitled")).strip() or "untitled"
        created = str(conv.get("created_at") or "")
        try:
            stamp = datetime.fromisoformat(created.replace("Z", "+00:00"))
        except ValueError:
            stamp = None
        turns = []
        chat_messages = conv.get("chat_messages")
        for msg in chat_messages if isinstance(chat_messages, list) else []:
            if not isinstance(msg, dict):
                continue
            role = _clean_role(msg.get("sender") or "?")
            text = str(msg.get("text") or "").strip()
            if not text:
                # Newer exports carry the words in content blocks with an empty `text` (AG14).
                blocks = msg.get("content")
                if isinstance(blocks, list):
                    text = "\n".join(str(b.get("text") or "") for b in blocks
                                     if isinstance(b, dict) and b.get("type", "text") == "text").strip()
            if text:
                turns.append((role, text))
        if not turns:
            skipped += 1
            skipped_reasons.append(f"{title!r}: no user/assistant text turns found")
            continue
        body_lines = [f"## {role}\n\n{text}\n" for role, text in turns]
        text_out = (
            _fm_block({
                "id": f"claude-{_slug(title)}",
                "title": title,
                "type": "imported",
                "imported_from": "claude",
                "imported_at": _now_iso(),
                "conversation_date": stamp.date().isoformat() if stamp else "unknown",
            })
            + f"# {' '.join(title.split())}\n\n" + "\n".join(body_lines)
        )
        dest = _chat_dest(dest_root, stamp, title)
        if _chat_already_imported(dest, text_out, listing):
            already += 1
            continue
        if not execute:
            would += 1
            continue
        target = _collision_free(dest)
        target.parent.mkdir(parents=True, exist_ok=True)
        _write_lf(target, text_out)
        listing.setdefault(target.parent, []).append(target.name)
        imported += 1
    return {"source": "claude", "planned": len(conversations),
            "imported": imported, "would_import": would, "skipped_already_imported": already,
            "skipped_empty": skipped, "skipped_reasons": skipped_reasons,
            "destination": "reference/imports/claude"}


def _prog() -> str:
    """Report the name the user actually typed (sextant-* alias, subcommand, or atlas-*)."""
    import sys
    from pathlib import Path as _P
    return _P(sys.argv[0]).stem if sys.argv and sys.argv[0] else "atlas-import"


def _detect_source(src: Path) -> str | None:
    """Guess the import source from the path's shape. None when ambiguous."""
    if src.is_dir():
        return "obsidian"
    if src.suffix.lower() == ".zip":
        try:
            import zipfile
            with zipfile.ZipFile(src) as zf:
                names = zf.namelist()
                member = next((n for n in names if n.endswith("conversations.json")), None)
                if member is None:
                    return None
                info = zf.getinfo(member)
                if info.file_size > _MAX_EXPORT_MEMBER_BYTES:
                    return None
                with zf.open(member) as fh:
                    head = fh.read(65536).decode("utf-8-sig", errors="replace")
            if '"mapping"' in head:
                return "chatgpt"
            if '"chat_messages"' in head:
                return "claude"
            return None
        except Exception:
            return None
    if src.suffix.lower() == ".json":
        try:
            head = json.loads(src.read_text(encoding="utf-8-sig", errors="replace"))
            if isinstance(head, dict) and "conversations" in head:
                head = head["conversations"]
            if isinstance(head, list) and head:
                first = head[0]
                if isinstance(first, dict):
                    if "mapping" in first:
                        return "chatgpt"
                    if "chat_messages" in first:
                        return "claude"
        except Exception:
            return None
    return None


def main() -> None:
    parser = argparse.ArgumentParser(
        prog=_prog(),
        description="Import existing knowledge (report first; --yes to execute).",
    )
    parser.add_argument("path_pos", nargs="?", default=None, metavar="PATH",
                        help="vault directory, conversations.json, or the export .zip as "
                             "ChatGPT or claude.ai delivered it (source auto-detected)")
    parser.add_argument("--source", choices=["obsidian", "chatgpt", "claude"],
                        help="omit to auto-detect from the path")
    parser.add_argument("--path", help="same as the positional PATH")
    parser.add_argument("--scope", default="reference/obsidian",
                        help="corpus destination for obsidian imports")
    parser.add_argument("--yes", action="store_true", help="actually write files")
    parser.add_argument("--dry-run", action="store_true",
                        help="report only -- this is already the default; "
                             "kept for habit (incompatible with --yes)")
    args = parser.parse_args()

    if args.dry_run and args.yes:
        parser.error("--dry-run and --yes contradict each other -- pick one")

    raw_path = args.path or args.path_pos
    if not raw_path:
        parser.error("give a PATH (a vault directory, conversations.json, or a claude export .zip)")
    src = Path(raw_path)
    if not src.exists():
        print(f"path not found: {src}", file=sys.stderr)
        raise SystemExit(1)

    if not args.source:
        args.source = _detect_source(src)
        if not args.source:
            print(
                f"could not auto-detect the source of {src.name!r} -- pass "
                "--source obsidian|chatgpt|claude",
                file=sys.stderr,
            )
            raise SystemExit(1)
        print(f"auto-detected source: {args.source}")

    if args.source == "obsidian":
        try:
            args.scope = _safe_scope(args.scope)
        except ValueError as e:
            parser.error(str(e))

    try:
        if args.source == "obsidian":
            if not src.is_dir():
                print("obsidian import expects a vault DIRECTORY", file=sys.stderr)
                raise SystemExit(1)
            result = import_obsidian(src, args.scope, execute=args.yes)
        elif args.source == "claude":
            result = import_claude(src, execute=args.yes)
        else:
            result = import_chatgpt(src, execute=args.yes)
    except json.JSONDecodeError as e:
        print(f"import failed: {src} is not valid JSON -- check it's the right "
              f"export file ({e})", file=sys.stderr)
        raise SystemExit(1)
    except (ValueError, OSError) as e:
        print(f"import failed: {e} -- did you pass the wrong --source for this "
              f"file?", file=sys.stderr)
        raise SystemExit(1)

    print(json.dumps(result, indent=2))
    for key, label in (("skipped_links", "links not followed (a vault link can point outside the vault)"),
                       ("skipped_binary", "binary or undecodable files skipped"),
                       ("decoded_non_utf8", "files that were not UTF-8"),
                       ("already_imported_under_another_name", "renamed or moved notes already in the corpus"),
                       ("superseded", "edited notes: the older copy is now stale")):
        items = result.get(key) or []
        if items:
            print(f"\n{label} ({len(items)}):")
            for line in items[:20]:
                print(f"  {line}")
            if len(items) > 20:
                print(f"  ... and {len(items) - 20} more")
    if result.get("planned") == 0:
        # A wrong-but-existing path (vault parent, typo'd sibling) used to
        # report a silent `planned: 0` -- say what was looked for and where.
        expected = {
            "obsidian": "markdown notes (*.md, searched recursively; dot- and system directories skipped)",
            "chatgpt": "conversations in the export JSON",
            "claude": "conversations in the export",
        }[args.source]
        print(
            f"\nNothing to import: no {expected} found under {src}. "
            "If that is unexpected, check the path -- for Obsidian, point at "
            "the vault root itself.",
            file=sys.stderr,
        )
        return
    if not args.yes:
        sample = result.get("sample_destinations") or []
        if sample:
            print(f"\nFirst {len(sample)} of {result.get('planned')} destinations:")
            for line in sample:
                print(f"  {line}")
        print("\nReport only -- nothing written. Re-run with --yes to import.")
    else:
        print("\nDone. Keyword search picks the new notes up on its own; refresh the semantic index with "
              "sextant-embed-index if you use it.")


if __name__ == "__main__":
    main()
