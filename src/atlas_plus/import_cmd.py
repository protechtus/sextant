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


def _import_copies(dest: Path, src: Path, listing: dict[Path, list[str]]) -> list[Path]:
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
    want = str(src)
    for name in sorted(n for n in names if pat.match(n)):
        cand = parent / name
        try:
            head = cand.read_text(encoding="utf-8-sig", errors="replace")[:4096]
        except OSError:
            continue
        m = _ORIGINAL_PATH_RE.search(head)
        if m and yaml.safe_load(m.group(1)) == want:
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

    The user's own keys are kept (and win for `source`); `imported_from`,
    `imported_at` and `original_path` are added when absent. If the existing
    block does not parse as a YAML mapping the text is returned unchanged
    (never rewrite what we cannot round-trip)."""
    import yaml

    m = re.match(r"---\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|$)", text, re.S)
    if not m:
        return text
    try:
        data = yaml.safe_load(m.group(1))
    except yaml.YAMLError:
        return text
    if data is None:
        data = {}
    if not isinstance(data, dict):
        return text
    data.setdefault("source", source)
    data.setdefault("imported_from", source)
    data.setdefault("imported_at", _now_iso())
    data.setdefault("original_path", original)
    body = text[m.end():]
    return _fm_block(data).rstrip("\n") + "\n\n" + body.lstrip("\r\n")


# -- Obsidian ----------------------------------------------------------------

def plan_obsidian(vault: Path, scope: str) -> list[tuple[Path, Path]]:
    """[(source_file, destination)] for every importable note."""
    scope = _safe_scope(scope)
    dest_root = atlas_paths.memory_root() / scope
    plan: list[tuple[Path, Path]] = []
    for src in sorted(vault.rglob("*.md")):
        rel = src.relative_to(vault)
        if any(part in _SKIP_DIRS or part.startswith(".") for part in rel.parts):
            continue
        plan.append((src, dest_root / rel))
    return plan


def import_obsidian(vault: Path, scope: str, execute: bool) -> dict:
    plan = plan_obsidian(vault, scope)
    imported = skipped = 0
    listing: dict[Path, list[str]] = {}
    for src, dest in plan:
        if not execute:
            continue
        raw = src.read_text(encoding="utf-8-sig", errors="replace")
        text = raw
        if not _has_frontmatter(text):
            text = _frontmatter_for(src.stem, "obsidian", str(src)) + text
        else:
            text = _stamp_existing_frontmatter(text, "obsidian", str(src))
        # Idempotent re-import (edge probe, quality loop run 16): re-running
        # the same import previously duplicated EVERY note via the collision
        # suffix. A destination whose content ends with the source's current
        # text (frontmatter is prepended, and stamps differ run-to-run) is
        # this note, already imported -- skip it. Changed notes still land
        # as a collision-suffixed new copy, never overwriting.
        # ...and so does a changed note's collision copy: re-running an unchanged import
        # of an already re-imported note must not grow _2, _3, ... (wave-2 2026-11-09).
        if any(
            cand.is_file() and _already_imported(
                cand.read_text(encoding="utf-8-sig", errors="replace"), raw
            )
            for cand in _import_copies(dest, src, listing)
        ):
            skipped += 1
            continue
        target = _collision_free(dest)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        listing.get(target.parent, []).append(target.name)
        imported += 1
    return {"source": "obsidian", "planned": len(plan), "imported": imported,
            "skipped_already_imported": skipped, "destination": scope}


# -- ChatGPT export ----------------------------------------------------------

def _chatgpt_messages(conv: dict) -> list[tuple[str, str]]:
    """Flatten a conversations.json mapping tree into (role, text) turns."""
    out: list[tuple[str, str]] = []
    mapping = conv.get("mapping")
    if not isinstance(mapping, dict):
        return []
    # Follow create_time order; the mapping is a tree but linear chats dominate.
    nodes = [n for n in mapping.values() if isinstance(n, dict) and isinstance(n.get("message"), dict)]

    def _key(n):
        ts = n["message"].get("create_time")
        return ts if isinstance(ts, (int, float)) else 0

    for node in sorted(nodes, key=_key):
        msg = node["message"]
        author = msg.get("author")
        role = author.get("role", "?") if isinstance(author, dict) else "?"
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


def plan_chatgpt(export_file: Path) -> list[dict]:
    data = json.loads(export_file.read_text(encoding="utf-8-sig", errors="replace"))
    if isinstance(data, dict) and "conversations" in data:
        data = data["conversations"]
    if not isinstance(data, list):
        raise ValueError("unrecognized ChatGPT export shape (expected a list of conversations)")
    return data


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
        except (TypeError, ValueError):
            stamp = datetime.now(timezone.utc)
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
                "conversation_date": stamp.date().isoformat(),
            })
            + f"# {' '.join(title.split())}\n\n" + "\n".join(body_lines)
        )
        dest = dest_root / stamp.strftime("%Y-%m") / f"{stamp.date().isoformat()}-{_slug(title)}.md"
        if _chat_already_imported(dest, text, listing):
            already += 1
            continue
        if not execute:
            would += 1
            continue
        target = _collision_free(dest)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
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
        import zipfile

        try:
            with zipfile.ZipFile(path) as zf:
                name = next(
                    (n for n in zf.namelist() if n.endswith("conversations.json")), None
                )
                if not name:
                    raise ValueError("no conversations.json inside the export zip")
                info = zf.getinfo(name)
                if info.file_size > _MAX_EXPORT_MEMBER_BYTES:
                    raise ValueError(
                        f"{name} declares {info.file_size:,} bytes inflated "
                        f"(cap {_MAX_EXPORT_MEMBER_BYTES:,}) -- refusing a "
                        "suspected decompression bomb; if this is a genuine "
                        "export, unzip it yourself and import the JSON directly"
                    )
                data = json.loads(zf.read(name).decode("utf-8-sig", errors="replace"))
        except zipfile.BadZipFile as exc:
            raise ValueError(f"not a valid zip archive: {exc}") from exc
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
            stamp = datetime.now(timezone.utc)
        turns = []
        chat_messages = conv.get("chat_messages")
        for msg in chat_messages if isinstance(chat_messages, list) else []:
            if not isinstance(msg, dict):
                continue
            role = str(msg.get("sender") or "?")
            text = str(msg.get("text") or "").strip()
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
                "conversation_date": stamp.date().isoformat(),
            })
            + f"# {' '.join(title.split())}\n\n" + "\n".join(body_lines)
        )
        dest = dest_root / stamp.strftime("%Y-%m") / f"{stamp.date().isoformat()}-{_slug(title)}.md"
        if _chat_already_imported(dest, text_out, listing):
            already += 1
            continue
        if not execute:
            would += 1
            continue
        target = _collision_free(dest)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text_out, encoding="utf-8")
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
        return "claude"
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
                        help="vault directory or export file (source auto-detected)")
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
        print("\nReport only -- nothing written. Re-run with --yes to import.")
    else:
        print("\nDone. Keyword search picks the new notes up on its own; refresh the semantic index with "
              "sextant-embed-index if you use it.")


if __name__ == "__main__":
    main()
