"""Read-only source adapters for the Atlas temporal graph.

Adapters:
  FixtureSource         -- hand-authored fixture bundles (first slice, always available)
  MemoryMarkdownSource  -- reads .memory/**/*.md, extracts frontmatter facts
  SessionLogSource      -- reads .memory/sessions/*.md, creates session entities + worked_on edges

Path guard: all adapters reject paths outside Atlas allowed roots.
  Allowed: ATLAS_MEMORY_ROOT, ATLAS_STATE_DIR/transcripts, atlas-memory+, atlas-memory
  Rejected: paths outside those roots (other projects, system paths)

Incremental builds: each adapter checks a Manifest and skips files with unchanged content hash.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import yaml

from atlas_plus import paths as atlas_paths
from atlas_plus.temporal_graph.models import (
    TemporalEpisode,
    TemporalEntity,
    TemporalFact,
    _now_iso,
    _sha256_short,
)
from atlas_plus.temporal_graph.provider import TemporalGraphProvider

# ---------------------------------------------------------------------------
# Path guard
# ---------------------------------------------------------------------------

_ATLAS_ALLOWED_ROOTS = [
    atlas_paths.memory_root(),
    atlas_paths.state_dir() / "transcripts",
    atlas_paths.projects_root() / "atlas-memory+",
    atlas_paths.projects_root() / "atlas-memory",
]


def _is_allowed_path(p: Path) -> bool:
    try:
        resolved = p.resolve()
    except (OSError, ValueError):
        return False
    return any(
        resolved == root.resolve() or resolved.is_relative_to(root.resolve())
        for root in _ATLAS_ALLOWED_ROOTS
    )


# ---------------------------------------------------------------------------
# Manifest -- incremental build tracking
# ---------------------------------------------------------------------------

@dataclass
class FileRecord:
    """Per-file ingestion record stored inside a Manifest (hash + timestamp + fact count)."""

    content_hash: str
    last_indexed: str
    facts_added: int = 0


# Bump when the rules that turn a file into facts change: a plain `build` then re-indexes every
# file once and retires facts the new rules no longer emit, so an upgrade finishes itself
# without --force (upgrade persona 2026-11-10: 36 template-default facts stayed live).
RULES_VERSION = 2


@dataclass
class Manifest:
    """Incremental build manifest: tracks per-file hashes so unchanged files are skipped on re-index."""

    path: Path
    last_built: Optional[str] = None
    files: dict[str, FileRecord] = field(default_factory=dict)
    rules: int = 0

    @classmethod
    def load(cls, manifest_path: Path) -> "Manifest":
        """Load or create a Manifest from a JSON file. Returns empty manifest when file missing."""
        m = cls(path=manifest_path)
        if manifest_path.exists():
            try:
                data = json.loads(manifest_path.read_text(encoding="utf-8"))
                m.last_built = data.get("last_built")
                m.rules = int(data.get("rules", 0) or 0)
                for k, v in data.get("files", {}).items():
                    m.files[k] = FileRecord(**v)
            except Exception:
                pass
        return m

    def save(self) -> None:
        """Persist manifest to JSON file, updating last_built timestamp."""
        self.last_built = _now_iso()
        data = {
            "last_built": self.last_built,
            "rules": self.rules,
            "files": {
                k: {"content_hash": v.content_hash, "last_indexed": v.last_indexed, "facts_added": v.facts_added}
                for k, v in self.files.items()
            },
        }
        self.path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def is_changed(self, key: str, content_hash: str) -> bool:
        """Return True when file is new or content hash differs from last build."""
        rec = self.files.get(key)
        return rec is None or rec.content_hash != content_hash

    def record(self, key: str, content_hash: str, facts_added: int = 0) -> None:
        """Store a file's hash and fact count after successful ingestion."""
        self.files[key] = FileRecord(
            content_hash=content_hash,
            last_indexed=_now_iso(),
            facts_added=facts_added,
        )


def _file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Fixture source (unchanged from first slice, kept here for co-location)
# ---------------------------------------------------------------------------

@dataclass
class FixtureBundle:
    """Container for pre-built test/fixture data: episodes, entities, and facts to load directly."""

    episodes: list[TemporalEpisode] = field(default_factory=list)
    entities: list[TemporalEntity] = field(default_factory=list)
    facts: list[TemporalFact] = field(default_factory=list)


class FixtureSource:
    """Ingestion source that loads a FixtureBundle directly into a TemporalGraphProvider."""

    def __init__(self, bundle: FixtureBundle) -> None:
        self._bundle = bundle

    def ingest(self, provider: TemporalGraphProvider, manifest: Optional[Manifest] = None) -> dict:
        """Load a FixtureBundle (episodes + entities + facts) into the provider. Returns summary dict."""
        warnings: list[str] = []
        for ep in self._bundle.episodes:
            provider.add_episode(ep)
        for ent in self._bundle.entities:
            provider.upsert_entity(ent)
        for fact in self._bundle.facts:
            _, ws = provider.add_fact(fact)
            warnings.extend(ws)
        return {
            "episodes": len(self._bundle.episodes),
            "entities": len(self._bundle.entities),
            "facts": len(self._bundle.facts),
            "warnings": warnings,
        }


# ---------------------------------------------------------------------------
# Helpers shared by real source adapters
# ---------------------------------------------------------------------------

# Named exclusions plus the underscore convention: any directory whose name
# starts with "_" is not live memory (the tickets store archives to
# tickets/_archive/, matching its own "underscore directories are not live
# tickets" rule). Before 2026-10-08 only the literal names below were skipped,
# so tickets/_archive/** leaked into the graph: 81% of live facts were
# archived-ticket residue.
_MEMORY_EXCLUDED = {"archive", "_sync", "versions", "_atlas", "sessions"}

_MEMORY_ROOT = atlas_paths.memory_root()


def _date_to_iso(date_str: str) -> str:
    if not date_str:
        return _now_iso()
    s = str(date_str).strip()
    if len(s) == 10:
        return s + "T00:00:00+00:00"
    return s if "T" in s else _now_iso()


def _mtime_iso(path: Path) -> str:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%S+00:00"
        )
    except OSError:
        return _now_iso()


def _ascii_safe(text: str) -> str:
    """Replace non-ASCII and control characters with a space. Keeps facts clean."""
    return "".join(c if ord(c) < 128 and c >= " " else " " for c in text).strip()


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end == -1:
        return {}, text
    try:
        fm = yaml.safe_load(text[3:end]) or {}
    except Exception:
        fm = {}
    return fm, text[end + 4:].strip()


# ---------------------------------------------------------------------------
# MemoryMarkdownSource
# ---------------------------------------------------------------------------

# Only frontmatter the user actually stated becomes a fact. `status`, `priority` and `type` are
# filled in by the routing template on every write ("X priority is normal" is not a memory);
# a project link is a statement (persona rerun 2026-11-10: prime presented template defaults as
# current state).
_FACT_PREDICATES = {
    "status":   ("has_status",   "{name} status is {value}."),
    "priority": ("has_priority", "{name} priority is {value}."),
    "project":  ("belongs_to",   "{name} belongs to project {value}."),
}
# Values the routing template writes on every note; stating them is not remembering them.
_TEMPLATE_DEFAULTS = {"status": {"active"}, "priority": {"normal"}}


def _upsert_project_entity(
    provider: TemporalGraphProvider,
    project_name: str,
    valid_from: str,
    now: str,
    evidence_refs: list[str],
) -> None:
    """Upsert a Project entity so project names are first-class graph nodes.

    Enables temporal queries like 'what is atlas-memory+?' to resolve the project
    entity and show all memory files, sessions, and conversations belonging to it.
    """
    safe_name = _ascii_safe(project_name)
    if not safe_name:
        return
    provider.upsert_entity(TemporalEntity(
        entity_id=f"project:{safe_name}",
        name=safe_name,
        labels=["Project"],
        aliases=[safe_name.lower().replace("+", "-plus"), safe_name],
        summary=f"Project {safe_name} referenced in .memory/ files.",
        first_seen_at=valid_from,
        last_seen_at=valid_from,
        source_refs=evidence_refs,
    ))


class MemoryMarkdownSource:
    """Indexes .memory/**/*.md files as MemoryFile entities with frontmatter-derived facts.

    Does not perform LLM extraction -- only deterministic frontmatter fields are indexed.
    Skips: archive, versions, sessions (handled by SessionLogSource), and every
    underscore-prefixed directory or file (_archive, _sync, _atlas, ...).
    Uses Manifest for incremental builds -- skips unchanged files.
    """

    def __init__(
        self,
        memory_root: Optional[Path] = None,
        excluded_dirs: Optional[set[str]] = None,
        _extra_allowed_roots: Optional[list[Path]] = None,
    ) -> None:
        self._root = memory_root or _MEMORY_ROOT
        self._excluded = excluded_dirs if excluded_dirs is not None else _MEMORY_EXCLUDED
        self._allowed_roots = _ATLAS_ALLOWED_ROOTS + (list(_extra_allowed_roots) if _extra_allowed_roots else [])

    def _check_allowed(self, p: Path) -> bool:
        try:
            resolved = p.resolve()
        except (OSError, ValueError):
            return False
        return any(
            resolved == root.resolve() or resolved.is_relative_to(root.resolve())
            for root in self._allowed_roots
        )

    def ingest(self, provider: TemporalGraphProvider, manifest: Optional[Manifest] = None) -> dict:
        """Index .memory/**/*.md files as MemoryFile entities with frontmatter-derived facts.

        Extracts type, confidence, half_life, tags, last_updated, last_confirmed.
        Skips excluded dirs (archive, versions, sessions) and any underscore-prefixed
        path component (_archive, _sync, _atlas, ...).
        Manifest-incremental: unchanged files skipped. Returns {files_indexed, files_skipped, total_facts, warnings}.
        """
        if not self._check_allowed(self._root):
            raise PermissionError(f"Memory root {self._root} is not in Atlas allowed roots.")

        files_indexed = 0
        files_skipped = 0
        total_facts = 0
        all_warnings: list[str] = []
        seen_keys: set[str] = set()
        rules_changed = manifest is not None and manifest.rules != RULES_VERSION
        provider_facts = getattr(provider, "_facts", None)

        for path in sorted(self._root.rglob("*.md")):
            rel = path.relative_to(self._root)
            parts = rel.parts

            if any(p in self._excluded or p.startswith("_") for p in parts):
                continue
            if not self._check_allowed(path):
                continue

            key = rel.as_posix()
            seen_keys.add(key)
            try:
                fhash = _file_hash(path)
            except OSError:
                continue

            if manifest and not rules_changed and not manifest.is_changed(key, fhash):
                files_skipped += 1
                continue
            emitted: set[str] = set()  # fact ids this pass produced for the file

            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue

            fm, body = _parse_frontmatter(text)
            name = _ascii_safe(str(fm.get("title") or rel.stem))
            updated_str = str(fm.get("updated", "")).strip()
            valid_from = _date_to_iso(updated_str) if updated_str else _mtime_iso(path)
            entity_id = f"memory:{key}"
            now = _now_iso()

            # Episode: the file content
            ep_id = f"ep:{key}"
            provider.add_episode(TemporalEpisode(
                episode_id=ep_id,
                source_system="memory",
                source_path=key,
                source_type="markdown",
                body=text[:2000],
                body_hash=_sha256_short(text),
                reference_time=valid_from,
                ingested_at=now,
            ))

            # Entity: the memory file itself
            provider.upsert_entity(TemporalEntity(
                entity_id=entity_id,
                name=name,
                labels=["MemoryFile", str(fm.get("type", "unknown")).title()],
                aliases=[rel.stem, key],
                summary=body[:200] if body else "",
                first_seen_at=_mtime_iso(path),
                last_seen_at=valid_from,
                source_refs=[ep_id],
            ))

            # Facts from frontmatter
            facts_added = 0
            for fm_key, (predicate, template) in _FACT_PREDICATES.items():
                value = str(fm.get(fm_key, "")).strip()
                if value.lower() in _TEMPLATE_DEFAULTS.get(fm_key, set()):
                    continue  # a template default, not a statement
                if not value:
                    continue
                fact_id = f"f:{key}:{predicate}"
                emitted.add(fact_id)
                fact_text = template.format(name=name, value=value)
                _, ws = provider.add_fact(TemporalFact(
                    fact_id=fact_id,
                    subject_id=entity_id,
                    predicate=predicate,
                    object_id=value,
                    fact_text=fact_text,
                    valid_from=valid_from,
                    observed_at=now,
                    ingested_at=now,
                    last_verified_at=now,
                    confidence=0.95,
                    authority="memory",
                    evidence_refs=[ep_id],
                ))
                all_warnings.extend(ws)
                facts_added += 1
                # Upsert a Project entity when the project: field is present
                if fm_key == "project" and value:
                    _upsert_project_entity(provider, value, valid_from, now, [ep_id])

            # applies_to: list field -- create belongs_to facts and upsert Project entities
            applies_to = fm.get("applies_to", [])
            if isinstance(applies_to, list):
                for proj in applies_to:
                    proj = str(proj).strip()
                    if not proj:
                        continue
                    fact_id = f"f:{key}:applies_to:{proj}"
                    emitted.add(fact_id)
                    _, ws = provider.add_fact(TemporalFact(
                        fact_id=fact_id,
                        subject_id=entity_id,
                        predicate="applies_to_project",
                        object_id=proj,
                        fact_text=f"{name} applies to project {proj}.",
                        valid_from=valid_from,
                        observed_at=now,
                        ingested_at=now,
                        last_verified_at=now,
                        confidence=0.9,
                        authority="memory",
                        evidence_refs=[ep_id],
                    ))
                    all_warnings.extend(ws)
                    facts_added += 1
                    _upsert_project_entity(provider, proj, valid_from, now, [ep_id])

            # Tags as individual facts
            tags = fm.get("tags", [])
            if isinstance(tags, list):
                for tag in tags:
                    tag = str(tag).strip()
                    if tag:
                        fact_id = f"f:{key}:has_tag:{tag}"
                        emitted.add(fact_id)
                        _, ws = provider.add_fact(TemporalFact(
                            fact_id=fact_id,
                            subject_id=entity_id,
                            predicate="has_tag",
                            object_id=tag,
                            fact_text=f"{name} is tagged {tag}.",
                            valid_from=valid_from,
                            observed_at=now,
                            ingested_at=now,
                            last_verified_at=now,
                            confidence=0.9,
                            authority="memory",
                            evidence_refs=[ep_id],
                        ))
                        all_warnings.extend(ws)
                        facts_added += 1

            # A re-indexed file retires the facts it no longer states (a changed status, a
            # dropped tag, a template default the rules stopped counting).
            if provider_facts is not None:
                prefix = f"f:{key}:"
                for fact_id, fact in list(provider_facts.items()):
                    if fact_id.startswith(prefix) and fact_id not in emitted and not fact.invalid_at:
                        try:
                            provider.invalidate_fact(fact_id, now, f"{key} no longer states it")
                        except KeyError:
                            pass

            if manifest:
                manifest.record(key, fhash, facts_added)

            total_facts += facts_added
            files_indexed += 1

        # Retract-on-missing (2026-10-09): a memory file the manifest knows
        # but this walk no longer sees -- deleted, or moved under _archive/ --
        # must stop asserting its facts. Before this, ghost refs persisted
        # indefinitely (tickets/test/TES-001.md haunted the live graph for
        # months after archival). Append-only honored: facts are invalidated
        # (gone from search_current, kept for as-of history), never deleted.
        # Scope guard: other sources share this manifest under their own key
        # prefixes; only keys in OUR namespace are considered, and a key is
        # only retracted when its file is truly gone from disk OR now lands
        # in an excluded location (so pre-fix graphs heal on their next build).
        files_retracted = 0
        facts_invalidated = 0
        if manifest:
            _foreign = ("sessions/", "transcripts/", "mining/")
            provider_facts = getattr(provider, "_facts", None)
            for key in list(manifest.files):
                if key in seen_keys or key.startswith(_foreign):
                    continue
                key_parts = Path(key).parts
                now_excluded = any(
                    p in self._excluded or p.startswith("_") for p in key_parts
                )
                if not now_excluded and (self._root / key).exists():
                    continue  # still a live file; a glob hiccup must not retract
                now = _now_iso()
                if provider_facts is not None:
                    prefix = f"f:{key}:"
                    for fact_id, fact in list(provider_facts.items()):
                        if fact_id.startswith(prefix) and not fact.invalid_at:
                            try:
                                provider.invalidate_fact(
                                    fact_id, now,
                                    f"source file {key} removed from corpus",
                                )
                                facts_invalidated += 1
                            except KeyError:
                                pass
                del manifest.files[key]
                files_retracted += 1

        if manifest:
            manifest.rules = RULES_VERSION
            manifest.save()

        return {
            "files_indexed": files_indexed,
            "files_skipped": files_skipped,
            "files_retracted": files_retracted,
            "facts_invalidated": facts_invalidated,
            "facts": total_facts,
            "warnings": all_warnings,
        }


# ---------------------------------------------------------------------------
# SessionLogSource
# ---------------------------------------------------------------------------

_SESSION_PROJECT_RE = re.compile(
    r"##\s+([a-zA-Z0-9_+.-]+)\s+-",         # ## atlas-memory+ - description
)
_SESSION_ACTIVE_RE = re.compile(
    r"active\s+project[:\s]+([a-zA-Z0-9_+.-]+)",
    re.IGNORECASE,
)


def _parse_session_log(path: Path) -> dict:
    text = path.read_text(encoding="utf-8", errors="replace")
    date_stem = path.stem  # YYYY-MM-DD
    fm, body = _parse_frontmatter(text)
    title = _ascii_safe(str(fm.get("title", date_stem)))

    # Extract project names from section headers and active-project lines
    projects: set[str] = set()
    for line in body.splitlines():
        m = _SESSION_PROJECT_RE.search(line)
        if m:
            name = m.group(1).strip()
            if name not in ("Atlas", "atlas") and len(name) > 3:
                projects.add(name)
        m2 = _SESSION_ACTIVE_RE.search(line)
        if m2:
            projects.add(m2.group(1).strip())

    return {
        "date": date_stem,
        "title": title,
        "body": body,
        "fm": fm,
        "projects": sorted(projects),
    }


class SessionLogSource:
    """Indexes .memory/sessions/*.md as Session entities with worked_on edges.

    Each session file becomes:
      - One Session entity (entity_id: session:YYYY-MM-DD)
      - One episode per session
      - One worked_on fact per project mentioned in the session
    """

    def __init__(
        self,
        memory_root: Optional[Path] = None,
        _extra_allowed_roots: Optional[list[Path]] = None,
    ) -> None:
        self._root = memory_root or _MEMORY_ROOT
        self._sessions_dir = self._root / "sessions"
        self._allowed_roots = _ATLAS_ALLOWED_ROOTS + (list(_extra_allowed_roots) if _extra_allowed_roots else [])

    def _check_allowed(self, p: Path) -> bool:
        try:
            resolved = p.resolve()
        except (OSError, ValueError):
            return False
        return any(
            resolved == root.resolve() or resolved.is_relative_to(root.resolve())
            for root in self._allowed_roots
        )

    def ingest(self, provider: TemporalGraphProvider, manifest: Optional[Manifest] = None) -> dict:
        """Index .memory/sessions/*.md as Session entities with worked_on edges.

        Each session file: Session entity + episode + worked_on facts per project mentioned.
        Manifest-incremental: unchanged files skipped.
        Returns {files_indexed, files_skipped, facts, warnings}.
        """
        if not self._check_allowed(self._root):
            raise PermissionError(f"Memory root {self._root} is not in Atlas allowed roots.")

        if not self._sessions_dir.exists():
            return {"files_indexed": 0, "files_skipped": 0, "facts": 0, "warnings": []}

        files_indexed = 0
        files_skipped = 0
        total_facts = 0
        all_warnings: list[str] = []

        for path in sorted(self._sessions_dir.glob("*.md")):
            if path.name.startswith("_"):
                continue
            # Only parse YYYY-MM-DD.md files
            if not re.match(r"^\d{4}-\d{2}-\d{2}\.md$", path.name):
                continue

            key = f"sessions/{path.name}"
            try:
                fhash = _file_hash(path)
            except OSError:
                continue

            if manifest and not manifest.is_changed(key, fhash):
                files_skipped += 1
                continue

            try:
                parsed = _parse_session_log(path)
            except Exception:
                continue

            date_str = parsed["date"]
            valid_from = _date_to_iso(date_str)
            entity_id = f"session:{date_str}"
            now = _now_iso()
            ep_id = f"ep:session:{date_str}"

            provider.add_episode(TemporalEpisode(
                episode_id=ep_id,
                source_system="session_log",
                source_path=key,
                source_type="summary",
                body=parsed["body"][:2000],
                body_hash=_sha256_short(parsed["body"]),
                reference_time=valid_from,
                ingested_at=now,
            ))

            provider.upsert_entity(TemporalEntity(
                entity_id=entity_id,
                name=f"Session {date_str}",
                labels=["Session"],
                aliases=[date_str, parsed["title"][:60]],
                summary=parsed["title"],
                first_seen_at=valid_from,
                last_seen_at=valid_from,
                source_refs=[ep_id],
            ))

            facts_added = 0
            # Session title fact
            _, ws = provider.add_fact(TemporalFact(
                fact_id=f"f:session:{date_str}:title",
                subject_id=entity_id,
                predicate="has_title",
                object_id=parsed["title"][:80],
                fact_text=f"Session {date_str} was titled: {parsed['title'][:80]}.",
                valid_from=valid_from,
                observed_at=now,
                ingested_at=now,
                last_verified_at=now,
                confidence=1.0,
                authority="session_log",
                evidence_refs=[ep_id],
            ))
            all_warnings.extend(ws)
            facts_added += 1

            # worked_on facts per project
            for project in parsed["projects"]:
                fact_id = f"f:session:{date_str}:worked_on:{project}"
                _, ws = provider.add_fact(TemporalFact(
                    fact_id=fact_id,
                    subject_id=entity_id,
                    predicate="worked_on",
                    object_id=project,
                    fact_text=f"Session {date_str} included work on {project}.",
                    valid_from=valid_from,
                    observed_at=now,
                    ingested_at=now,
                    last_verified_at=now,
                    confidence=0.85,
                    authority="session_log",
                    evidence_refs=[ep_id],
                ))
                all_warnings.extend(ws)
                facts_added += 1

            if manifest:
                manifest.record(key, fhash, facts_added)

            total_facts += facts_added
            files_indexed += 1

        if manifest:
            manifest.save()

        return {
            "files_indexed": files_indexed,
            "files_skipped": files_skipped,
            "facts": total_facts,
            "warnings": all_warnings,
        }


# ---------------------------------------------------------------------------
# ClaudeCodeTranscriptSource (Direction E)
# ---------------------------------------------------------------------------

def _claude_project_dir(claude_root: Path) -> str:
    """The Claude Code project folder for THIS workspace (`D:\\Work\\notes` -> `D--Work-notes`), matched
    case-insensitively on disk; `ATLAS_CLAUDE_PROJECT_DIR` overrides. It was the maintainer's own
    slug, so mining indexed nothing for anyone else (AG34)."""
    override = os.environ.get("ATLAS_CLAUDE_PROJECT_DIR", "").strip()
    if override:
        return override
    try:
        from atlas_plus import paths as _paths
        root = str(_paths.workspace_root())
    except Exception:  # noqa: BLE001
        root = ""
    slug = re.sub(r"[^A-Za-z0-9]", "-", root) if root else ""
    try:
        for d in claude_root.iterdir():
            if d.is_dir() and slug and d.name.lower() == slug.lower():
                return d.name
    except OSError:
        pass
    return slug


_CLAUDE_CODE_ROOT = Path(os.environ.get(
    "ATLAS_CLAUDE_CODE_ROOT",
    str(Path.home() / ".claude" / "projects"),
))


def _extract_transcript_metadata(path: Path) -> dict:
    """Read a Claude Code JSONL file and return metadata-only dict. No content stored."""
    meta = {
        "session_id": path.stem,
        "slug": None,
        "model": None,
        "cwd": None,
        "first_ts": None,
        "last_ts": None,
        "turn_count": 0,
        "tokens_in": 0,
        "tokens_out": 0,
    }
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue

            ts = record.get("timestamp")
            if ts:
                if meta["first_ts"] is None:
                    meta["first_ts"] = ts
                meta["last_ts"] = ts

            sid = record.get("sessionId")
            if sid and meta["session_id"] == path.stem:
                meta["session_id"] = sid

            if not meta["slug"]:
                meta["slug"] = record.get("slug")
            if not meta["cwd"]:
                meta["cwd"] = record.get("cwd")

            msg = record.get("message", {})
            role = record.get("type") or msg.get("role", "")
            if role == "assistant":
                if not meta["model"] and msg.get("model"):
                    meta["model"] = msg["model"]
                usage = msg.get("usage", {})
                meta["tokens_in"] += (
                    usage.get("input_tokens", 0)
                    + usage.get("cache_read_input_tokens", 0)
                    + usage.get("cache_creation_input_tokens", 0)
                )
                meta["tokens_out"] += usage.get("output_tokens", 0)

            meta["turn_count"] += 1
    except OSError:
        pass
    return meta


def _project_from_cwd(cwd: Optional[str]) -> Optional[str]:
    """Extract a project name from a cwd path string. E.g. '<workspace>/projects/myproj' -> 'myproj'.

    Transcript cwd strings keep the RECORDING machine's separators, so a
    Windows path must parse correctly on a POSIX host (and vice versa).
    ntpath.basename handles both separator styles on every platform.
    """
    if not cwd:
        return None
    try:
        import ntpath
        return ntpath.basename(cwd.rstrip("/\\")) or None
    except (ValueError, OSError):
        return None


def _ts_to_iso(ts: Optional[str]) -> str:
    """Normalise a Claude Code timestamp (ISO with Z or without) to +00:00 format."""
    if not ts:
        return _now_iso()
    ts = ts.strip()
    if ts.endswith("Z"):
        ts = ts[:-1] + "+00:00"
    elif "T" in ts and "+" not in ts and ts[-6] not in ("+", "-"):
        ts = ts + "+00:00"
    return ts


class ClaudeCodeTranscriptSource:
    """Indexes Claude Code session transcripts as Conversation entities.

    Reads ~/.claude/projects/<target_dir>/*.jsonl files.
    Indexes METADATA ONLY: timestamps, model, cwd, token counts, turn counts.
    No conversation content is stored in the temporal graph.

    Each JSONL file becomes:
      - One Conversation entity (entity_id: transcript:{session_id})
      - One episode (body = metadata JSON summary, NOT conversation text)
      - Facts: has_model, worked_in_project (from cwd), used_tokens, turn_count

    Incremental: Manifest tracks file content hashes, skips unchanged files.
    """

    def __init__(
        self,
        claude_root: Optional[Path] = None,
        target_dir: str | None = None,
    ) -> None:
        _base = claude_root or _CLAUDE_CODE_ROOT
        self._root = _base / (target_dir or _claude_project_dir(_base))
        self._claude_root = claude_root or _CLAUDE_CODE_ROOT

    def ingest(self, provider: TemporalGraphProvider, manifest: Optional[Manifest] = None) -> dict:
        """Index Claude Code JSONL transcripts as Conversation entities (METADATA ONLY).

        Reads ~/.claude/projects/<target_dir>/*.jsonl. Creates Conversation entity +
        episode + facts (has_model, worked_in_project, used_tokens, turn_count).
        No conversation content stored. Manifest-incremental: unchanged files skipped.
        Returns {files_indexed, files_skipped, facts, warnings}.
        """
        if not self._root.exists():
            return {"files_indexed": 0, "files_skipped": 0, "facts": 0, "warnings": [
                f"Transcript dir not found: {self._root}"
            ]}

        files_indexed = 0
        files_skipped = 0
        total_facts = 0
        all_warnings: list[str] = []

        for path in sorted(self._root.glob("*.jsonl")):
            if path.name.startswith("_"):
                continue

            key = f"transcripts/{path.name}"
            try:
                fhash = _file_hash(path)
            except OSError:
                continue

            if manifest and not manifest.is_changed(key, fhash):
                files_skipped += 1
                continue

            meta = _extract_transcript_metadata(path)
            session_id = meta["session_id"]
            entity_id = f"transcript:{session_id}"
            now = _now_iso()
            first_ts = _ts_to_iso(meta["first_ts"])
            last_ts = _ts_to_iso(meta["last_ts"])
            ep_id = f"ep:transcript:{session_id}"
            slug = _ascii_safe(meta["slug"] or session_id[:20])
            project = _project_from_cwd(meta["cwd"])

            # Episode: metadata summary (not content)
            meta_body = json.dumps({
                "session_id": session_id,
                "model": meta["model"],
                "project": project,
                "turns": meta["turn_count"],
                "tokens_in": meta["tokens_in"],
                "tokens_out": meta["tokens_out"],
                "first_ts": first_ts,
                "last_ts": last_ts,
            })
            provider.add_episode(TemporalEpisode(
                episode_id=ep_id,
                source_system="transcript",
                source_path=key,
                source_type="turn",
                body=meta_body,
                body_hash=_sha256_short(meta_body),
                reference_time=first_ts,
                ingested_at=now,
                metadata={"session_id": session_id},
            ))

            provider.upsert_entity(TemporalEntity(
                entity_id=entity_id,
                name=f"Conversation {slug}",
                labels=["Conversation"],
                aliases=[session_id, slug],
                summary=f"Claude Code session with {meta['turn_count']} turns.",
                first_seen_at=first_ts,
                last_seen_at=last_ts,
                source_refs=[ep_id],
            ))

            facts_added = 0

            # Model fact
            if meta["model"]:
                safe_model = _ascii_safe(meta["model"])
                _, ws = provider.add_fact(TemporalFact(
                    fact_id=f"f:{entity_id}:has_model",
                    subject_id=entity_id,
                    predicate="has_model",
                    object_id=safe_model,
                    fact_text=f"Conversation {slug} used model {safe_model}.",
                    valid_from=first_ts,
                    observed_at=now,
                    ingested_at=now,
                    last_verified_at=now,
                    confidence=1.0,
                    authority="transcript",
                    evidence_refs=[ep_id],
                ))
                all_warnings.extend(ws)
                facts_added += 1

            # Project fact (from cwd)
            if project:
                _, ws = provider.add_fact(TemporalFact(
                    fact_id=f"f:{entity_id}:worked_in_project",
                    subject_id=entity_id,
                    predicate="worked_in_project",
                    object_id=project,
                    fact_text=f"Conversation {slug} was in project {project}.",
                    valid_from=first_ts,
                    observed_at=now,
                    ingested_at=now,
                    last_verified_at=now,
                    confidence=0.95,
                    authority="transcript",
                    evidence_refs=[ep_id],
                ))
                all_warnings.extend(ws)
                facts_added += 1

            # Token count fact
            total_tokens = meta["tokens_in"] + meta["tokens_out"]
            if total_tokens > 0:
                _, ws = provider.add_fact(TemporalFact(
                    fact_id=f"f:{entity_id}:total_tokens",
                    subject_id=entity_id,
                    predicate="used_tokens",
                    object_id=str(total_tokens),
                    fact_text=(
                        f"Conversation {slug} used {total_tokens} tokens "
                        f"({meta['tokens_in']} in, {meta['tokens_out']} out)."
                    ),
                    valid_from=first_ts,
                    observed_at=now,
                    ingested_at=now,
                    last_verified_at=now,
                    confidence=1.0,
                    authority="transcript",
                    evidence_refs=[ep_id],
                ))
                all_warnings.extend(ws)
                facts_added += 1

            if manifest:
                manifest.record(key, fhash, facts_added)

            total_facts += facts_added
            files_indexed += 1

        if manifest:
            manifest.save()

        return {
            "files_indexed": files_indexed,
            "files_skipped": files_skipped,
            "facts": total_facts,
            "warnings": all_warnings,
        }


# ---------------------------------------------------------------------------
# TranscriptMiningSource
# ---------------------------------------------------------------------------

_EDIT_TOOLS = frozenset({"Edit", "Write", "NotebookEdit"})
_READ_TOOLS  = frozenset({"Read", "Glob", "Grep"})
_EXEC_TOOLS  = frozenset({"Bash", "PowerShell"})


def _extract_tool_patterns(path: Path) -> dict:
    """Parse a JSONL transcript and return tool call statistics.

    Reads ONLY tool_use content blocks (type, name, file_path from input).
    No conversation text or secret values are extracted.
    Returns tool_counts, files_touched, and total_tool_calls.
    """
    tool_counts: dict[str, int] = {}
    files_touched: set[str] = set()

    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue

            msg = record.get("message", {})
            role = record.get("type") or msg.get("role", "")
            if role != "assistant":
                continue

            for block in msg.get("content", []):
                if not isinstance(block, dict):
                    continue
                if block.get("type") != "tool_use":
                    continue
                name = block.get("name", "unknown")
                tool_counts[name] = tool_counts.get(name, 0) + 1
                # Collect file paths from Read/Edit/Write inputs (not content)
                inp = block.get("input", {})
                for key in ("file_path", "path"):
                    fp = inp.get(key)
                    if isinstance(fp, str) and fp:
                        # Transcript paths keep the recording machine's
                        # separators; parse both styles on any host.
                        import ntpath
                        files_touched.add(ntpath.basename(str(fp))[:80])
    except OSError:
        pass

    return {
        "tool_counts": tool_counts,
        "files_touched": sorted(files_touched),
        "total_tool_calls": sum(tool_counts.values()),
    }


def _classify_session_type(tool_counts: dict[str, int]) -> str:
    """Classify session work type from tool call distribution.

    Returns one of: edit-heavy, read-heavy, exec-heavy, mixed, empty.
    """
    total = sum(tool_counts.values())
    if total == 0:
        return "empty"

    edit_n = sum(tool_counts.get(t, 0) for t in _EDIT_TOOLS)
    read_n = sum(tool_counts.get(t, 0) for t in _READ_TOOLS)
    exec_n = sum(tool_counts.get(t, 0) for t in _EXEC_TOOLS)

    dominant_frac = max(edit_n, read_n, exec_n) / total
    if dominant_frac < 0.40:
        return "mixed"
    if edit_n >= read_n and edit_n >= exec_n:
        return "edit-heavy"
    if read_n >= edit_n and read_n >= exec_n:
        return "read-heavy"
    return "exec-heavy"


class TranscriptMiningSource:
    """Adds tool-pattern facts to existing Conversation entities.

    Supplements ClaudeCodeTranscriptSource (metadata) with tool usage stats:
      - top_tool_used predicate: which tool was called most in the session
      - distinct_tools predicate: how many unique tool types were used
      - session_type predicate: edit-heavy | read-heavy | exec-heavy | mixed
      - files_touched_count predicate: number of distinct file names accessed

    Reads the same ~/.claude/projects/<target_dir>/*.jsonl files.
    No conversation content is stored -- only aggregate tool statistics.

    Can run independently or after ClaudeCodeTranscriptSource; it upserts the
    same Conversation entity so the facts are co-located.

    Incremental: uses the same Manifest keys as ClaudeCodeTranscriptSource
    (prefixed "mining/") so unchanged files are skipped.
    """

    def __init__(
        self,
        claude_root: Optional[Path] = None,
        target_dir: str | None = None,
    ) -> None:
        _base = claude_root or _CLAUDE_CODE_ROOT
        self._root = _base / (target_dir or _claude_project_dir(_base))

    def ingest(self, provider: TemporalGraphProvider, manifest: Optional[Manifest] = None) -> dict:
        """Mine tool-usage statistics from Claude Code JSONL files (TranscriptMiningSource).

        Supplements ClaudeCodeTranscriptSource with per-session tool-usage facts:
        top_tool_used, distinct_tools, session_type, files_touched_count.
        Manifest keys prefixed 'mining/' to avoid collision with metadata pass.
        Manifest-incremental: unchanged files skipped.
        Returns {files_indexed, files_skipped, facts, warnings}.
        """
        if not self._root.exists():
            return {"files_indexed": 0, "files_skipped": 0, "facts": 0, "warnings": [
                f"Transcript dir not found: {self._root}"
            ]}

        files_indexed = 0
        files_skipped = 0
        total_facts = 0
        all_warnings: list[str] = []

        for path in sorted(self._root.glob("*.jsonl")):
            if path.name.startswith("_"):
                continue

            key = f"mining/{path.name}"
            try:
                fhash = _file_hash(path)
            except OSError:
                continue

            if manifest and not manifest.is_changed(key, fhash):
                files_skipped += 1
                continue

            patterns = _extract_tool_patterns(path)
            session_id = path.stem
            entity_id = f"transcript:{session_id}"
            now = _now_iso()
            facts_added = 0

            # Infer first_ts from the file mtime as a fallback reference time
            ref_time = _mtime_iso(path)

            top_tool = max(patterns["tool_counts"], key=patterns["tool_counts"].get, default=None) if patterns["tool_counts"] else None
            distinct = len(patterns["tool_counts"])
            session_type = _classify_session_type(patterns["tool_counts"])
            files_n = len(patterns["files_touched"])

            if top_tool:
                _, ws = provider.add_fact(TemporalFact(
                    fact_id=f"f:{entity_id}:top_tool",
                    subject_id=entity_id,
                    predicate="top_tool_used",
                    object_id=top_tool,
                    fact_text=f"Session {session_id[:16]} most used tool was {top_tool} ({patterns['tool_counts'][top_tool]} calls).",
                    valid_from=ref_time,
                    observed_at=now,
                    ingested_at=now,
                    last_verified_at=now,
                    confidence=1.0,
                    authority="transcript_mining",
                    evidence_refs=[f"ep:transcript:{session_id}"],
                ))
                all_warnings.extend(ws)
                facts_added += 1

            if distinct > 0:
                _, ws = provider.add_fact(TemporalFact(
                    fact_id=f"f:{entity_id}:distinct_tools",
                    subject_id=entity_id,
                    predicate="distinct_tools",
                    object_id=str(distinct),
                    fact_text=f"Session {session_id[:16]} used {distinct} distinct tool types ({patterns['total_tool_calls']} total calls).",
                    valid_from=ref_time,
                    observed_at=now,
                    ingested_at=now,
                    last_verified_at=now,
                    confidence=1.0,
                    authority="transcript_mining",
                    evidence_refs=[f"ep:transcript:{session_id}"],
                ))
                all_warnings.extend(ws)
                facts_added += 1

            if session_type != "empty":
                _, ws = provider.add_fact(TemporalFact(
                    fact_id=f"f:{entity_id}:session_type",
                    subject_id=entity_id,
                    predicate="session_type",
                    object_id=session_type,
                    fact_text=f"Session {session_id[:16]} was {session_type} ({patterns['total_tool_calls']} tool calls).",
                    valid_from=ref_time,
                    observed_at=now,
                    ingested_at=now,
                    last_verified_at=now,
                    confidence=0.9,
                    authority="transcript_mining",
                    evidence_refs=[f"ep:transcript:{session_id}"],
                ))
                all_warnings.extend(ws)
                facts_added += 1

            if files_n > 0:
                _, ws = provider.add_fact(TemporalFact(
                    fact_id=f"f:{entity_id}:files_touched_count",
                    subject_id=entity_id,
                    predicate="files_touched_count",
                    object_id=str(files_n),
                    fact_text=f"Session {session_id[:16]} accessed {files_n} distinct file names.",
                    valid_from=ref_time,
                    observed_at=now,
                    ingested_at=now,
                    last_verified_at=now,
                    confidence=0.9,
                    authority="transcript_mining",
                    evidence_refs=[f"ep:transcript:{session_id}"],
                ))
                all_warnings.extend(ws)
                facts_added += 1

            if manifest:
                manifest.record(key, fhash, facts_added)

            total_facts += facts_added
            files_indexed += 1

        if manifest:
            manifest.save()

        return {
            "files_indexed": files_indexed,
            "files_skipped": files_skipped,
            "facts": total_facts,
            "warnings": all_warnings,
        }


# ---------------------------------------------------------------------------
# Built-in fixture: Atlas Phase 4 tunnel lifecycle
# ---------------------------------------------------------------------------

ATLAS_PHASE4_FIXTURE = FixtureBundle(
    episodes=[
        TemporalEpisode(
            episode_id="ep-phase4-domain-block",
            source_system="session_log",
            source_path="memory/sessions/2026-05-03.md",
            source_type="summary",
            body="Atlas Phase 4: tunnel blocked on domain. Operator needs to add example-tunnel.com to Cloudflare.",
            body_hash=_sha256_short("phase4-domain-block"),
            reference_time="2026-05-03T12:00:00+00:00",
            ingested_at=_now_iso(),
        ),
        TemporalEpisode(
            episode_id="ep-phase4-hardening-block",
            source_system="session_log",
            source_path="memory/sessions/2026-05-08.md",
            source_type="summary",
            body="Atlas tunnel should not go live until P0 security hardening is complete. Codex audit pending.",
            body_hash=_sha256_short("phase4-hardening-block"),
            reference_time="2026-05-08T12:00:00+00:00",
            ingested_at=_now_iso(),
        ),
        TemporalEpisode(
            episode_id="ep-phase4-live",
            source_system="session_log",
            source_path="memory/sessions/2026-05-16.md",
            source_type="summary",
            body="ATL-001 complete. Cloudflare named tunnel live at https://atlas-mcp.example-tunnel.com/mcp. All 9 pentest findings resolved before go-live.",
            body_hash=_sha256_short("phase4-live"),
            reference_time="2026-05-16T18:00:00+00:00",
            ingested_at=_now_iso(),
        ),
    ],
    entities=[
        TemporalEntity(
            entity_id="ent-atlas-tunnel",
            name="Atlas MCP Tunnel",
            labels=["Project"],
            aliases=["atlas-mcp", "atlas tunnel", "atl-001"],
            summary="Cloudflare named tunnel exposing Atlas MCP server at a public HTTPS endpoint.",
            first_seen_at="2026-05-03T12:00:00+00:00",
            last_seen_at="2026-05-16T18:00:00+00:00",
            source_refs=["ep-phase4-domain-block", "ep-phase4-hardening-block", "ep-phase4-live"],
        ),
        TemporalEntity(
            entity_id="ent-cloudflare-domain",
            name="Cloudflare Domain",
            labels=["Concept"],
            aliases=["example-tunnel.com", "domain"],
            summary="Domain added to Cloudflare for Atlas MCP tunnel routing.",
            first_seen_at="2026-05-03T12:00:00+00:00",
            last_seen_at="2026-05-16T18:00:00+00:00",
            source_refs=["ep-phase4-domain-block"],
        ),
        TemporalEntity(
            entity_id="ent-p0-hardening",
            name="Atlas P0 Hardening",
            labels=["Concept"],
            aliases=["p0 security", "pentest", "security hardening"],
            summary="Pre-tunnel security hardening: timing attacks, prompt injection, path traversal, YAML injection.",
            first_seen_at="2026-05-08T12:00:00+00:00",
            last_seen_at="2026-05-16T18:00:00+00:00",
            source_refs=["ep-phase4-hardening-block"],
        ),
        TemporalEntity(
            entity_id="ent-example-tunnel",
            name="example-tunnel.com",
            labels=["Reference"],
            aliases=["atlas-mcp.example-tunnel.com"],
            summary="Production hostname for the Atlas MCP tunnel.",
            first_seen_at="2026-05-16T18:00:00+00:00",
            last_seen_at="2026-05-16T18:00:00+00:00",
            source_refs=["ep-phase4-live"],
        ),
    ],
    facts=[
        TemporalFact(
            fact_id="f-phase4-domain-block",
            subject_id="ent-atlas-tunnel",
            predicate="blocked_by",
            object_id="ent-cloudflare-domain",
            fact_text="Atlas Phase 4 tunnel is blocked, waiting for example-tunnel.com to be added to Cloudflare.",
            valid_from="2026-05-03T12:00:00+00:00",
            invalid_at="2026-05-08T12:00:00+00:00",
            observed_at="2026-05-03T12:00:00+00:00",
            ingested_at=_now_iso(),
            last_verified_at="2026-05-03T12:00:00+00:00",
            confidence=0.95,
            authority="session_log",
            evidence_refs=["ep-phase4-domain-block"],
        ),
        TemporalFact(
            fact_id="f-phase4-hardening-block",
            subject_id="ent-atlas-tunnel",
            predicate="blocked_by",
            object_id="ent-p0-hardening",
            fact_text="Atlas tunnel should not go live until P0 security hardening and pentest findings are resolved.",
            valid_from="2026-05-08T12:00:00+00:00",
            invalid_at="2026-05-16T18:00:00+00:00",
            observed_at="2026-05-08T12:00:00+00:00",
            ingested_at=_now_iso(),
            last_verified_at="2026-05-08T12:00:00+00:00",
            confidence=0.95,
            authority="session_log",
            evidence_refs=["ep-phase4-hardening-block"],
            supersedes_fact_id="f-phase4-domain-block",
        ),
        TemporalFact(
            fact_id="f-phase4-live",
            subject_id="ent-atlas-tunnel",
            predicate="is_live_at",
            object_id="ent-example-tunnel",
            fact_text="Cloudflare named tunnel is live at https://atlas-mcp.example-tunnel.com/mcp. ATL-001 complete.",
            valid_from="2026-05-16T18:00:00+00:00",
            invalid_at=None,
            observed_at="2026-05-16T18:00:00+00:00",
            ingested_at=_now_iso(),
            last_verified_at="2026-05-16T18:00:00+00:00",
            confidence=1.0,
            authority="session_log",
            evidence_refs=["ep-phase4-live"],
            supersedes_fact_id="f-phase4-hardening-block",
        ),
    ],
)
