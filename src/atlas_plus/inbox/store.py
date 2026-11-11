"""ATL-026: inbox store -- read/write .memory/inbox/<id>.md.

Mirrors tickets/store.py conventions: YAML-frontmatter markdown, an injectable root for
tests, autonomous-write gating on create, and version-before-rewrite on update. The inbox is
a single flat queue (status lives in frontmatter), not project-scoped like tickets.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional

import yaml

from atlas_plus.autonomous import require_autonomous_writes
from atlas_plus import paths as atlas_paths

# Read env directly to avoid a circular import with mcp.memory_server.config (same as tickets).
_MEMORY_ROOT = atlas_paths.memory_root()
INBOX_ROOT = _MEMORY_ROOT / "inbox"

VALID_INBOX_STATUSES = {"new", "summarized", "researched", "promoted", "archived"}
VALID_SOURCES = {"telegram", "web", "cli", "ntfy", "api"}

_MAX_URL = 2000
_MAX_TITLE = 500
_MAX_TEXT = 20000
_MAX_SUMMARY = 8000


def _now_iso() -> str:
    """UTC timestamp, second precision, ISO 8601 (sortable, locale-free)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class InboxItem:
    """Parsed representation of an inbox capture (markdown + YAML frontmatter)."""

    id: str
    source: str = "telegram"
    status: str = "new"
    url: Optional[str] = None
    title: Optional[str] = None
    tags: list[str] = field(default_factory=list)
    flagged_research: bool = False
    summary: str = ""
    promoted_ticket: Optional[str] = None
    created: str = field(default_factory=_now_iso)
    updated: str = field(default_factory=_now_iso)
    body: str = ""
    path: Optional[Path] = None

    def as_dict(self) -> dict:
        """JSON-serialisable view (excludes path)."""
        return {
            "id": self.id,
            "source": self.source,
            "status": self.status,
            "url": self.url,
            "title": self.title,
            "tags": list(self.tags),
            "flagged_research": self.flagged_research,
            "summary": self.summary,
            "promoted_ticket": self.promoted_ticket,
            "created": self.created,
            "updated": self.updated,
        }

    def to_markdown(self) -> str:
        """Render as YAML-frontmatter markdown for disk."""
        fm: dict = {
            "id": self.id,
            "type": "inbox",
            "source": self.source,
            "status": self.status,
            "created": self.created,
            "updated": self.updated,
        }
        if self.url:
            fm["url"] = self.url
        if self.title:
            fm["title"] = self.title
        if self.tags:
            fm["tags"] = list(self.tags)
        if self.flagged_research:
            fm["flagged_research"] = True
        if self.summary:
            fm["summary"] = self.summary
        if self.promoted_ticket:
            fm["promoted_ticket"] = self.promoted_ticket
        fm_text = yaml.dump(fm, default_flow_style=False, allow_unicode=True, sort_keys=False).strip()
        body = self.body.strip() or "## Captured\n\n(empty)"
        return f"---\n{fm_text}\n---\n\n{body}\n"


def _parse_item(path: Path) -> Optional[InboxItem]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
        if not text.startswith("---"):
            return None
        end = text.find("---", 3)
        if end == -1:
            return None
        fm = yaml.safe_load(text[3:end]) or {}
        body = text[end + 3:].strip()
        tags = fm.get("tags") or []
        if not isinstance(tags, list):
            tags = [str(tags)]
        return InboxItem(
            id=fm.get("id", path.stem),
            source=fm.get("source", "telegram"),
            status=fm.get("status", "new"),
            url=fm.get("url"),
            title=fm.get("title"),
            tags=[str(t) for t in tags],
            flagged_research=bool(fm.get("flagged_research", False)),
            summary=str(fm.get("summary", "")),
            promoted_ticket=fm.get("promoted_ticket"),
            created=str(fm.get("created", "")),
            updated=str(fm.get("updated", "")),
            body=body,
            path=path,
        )
    except Exception:
        return None


def _next_id(inbox_root: Path) -> str:
    existing = []
    if inbox_root.exists():
        for f in inbox_root.glob("*.md"):
            if f.name.startswith("_"):
                continue
            m = re.search(r"-(\d+)", f.stem)
            if m:
                existing.append(int(m.group(1)))
    return f"INB-{max(existing, default=0) + 1:04d}"


def list_items(
    status: Optional[str] = None,
    inbox_root: Optional[Path] = None,
) -> list[InboxItem]:
    """Return inbox items, newest first (by created), optionally filtered by status."""
    root = inbox_root or INBOX_ROOT
    if not root.exists():
        return []
    results = []
    for f in root.glob("*.md"):
        if f.name.startswith("_"):
            continue
        it = _parse_item(f)
        if it is None:
            continue
        if status and it.status != status:
            continue
        results.append(it)
    results.sort(key=lambda i: i.created, reverse=True)
    return results


def get_item(item_id: str, inbox_root: Optional[Path] = None) -> Optional[InboxItem]:
    """Return an item by ID (e.g. 'INB-0001'), or None. Matches on frontmatter id, not filename."""
    root = inbox_root or INBOX_ROOT
    if not root.exists():
        return None
    for f in root.glob("*.md"):
        if f.name.startswith("_"):
            continue
        it = _parse_item(f)
        if it and it.id == item_id:
            return it
    return None


def create_item(
    source: str = "telegram",
    url: Optional[str] = None,
    title: Optional[str] = None,
    text: str = "",
    tags: Optional[list[str]] = None,
    flagged_research: bool = False,
    inbox_root: Optional[Path] = None,
) -> InboxItem:
    """Create a new inbox capture under INBOX_ROOT/<id>.md.

    Gated by the autonomous kill switch (capture is an autonomous .memory write). Validates
    inputs; raises ValueError on bad input. An item must have at least a url or some text.
    """
    require_autonomous_writes("inbox_create")

    if source not in VALID_SOURCES:
        raise ValueError(f"Invalid source {source!r}. Valid: {sorted(VALID_SOURCES)}")
    url = (url or "").strip() or None
    title = (title or "").strip() or None
    text = text or ""
    if not url and not text.strip():
        raise ValueError("Inbox item must have a url or some text.")
    if url and len(url) > _MAX_URL:
        raise ValueError(f"url must be {_MAX_URL} characters or fewer.")
    if title and len(title) > _MAX_TITLE:
        raise ValueError(f"title must be {_MAX_TITLE} characters or fewer.")
    if len(text) > _MAX_TEXT:
        raise ValueError(f"text must be {_MAX_TEXT} characters or fewer.")
    clean_tags = [str(t).strip() for t in (tags or []) if str(t).strip()]

    root = inbox_root or INBOX_ROOT
    root.mkdir(parents=True, exist_ok=True)
    item_id = _next_id(root)

    captured = []
    if url:
        captured.append(url)
    if text.strip():
        captured.append(text.strip())
    body = "## Captured\n\n" + ("\n\n".join(captured) if captured else "(empty)")

    item = InboxItem(
        id=item_id,
        source=source,
        status="new",
        url=url,
        title=title,
        tags=clean_tags,
        flagged_research=flagged_research,
        body=body,
    )
    dest = root / f"{item_id}.md"
    dest.write_text(item.to_markdown(), encoding="utf-8")
    item.path = dest
    return item


def update_item(
    item_id: str,
    status: Optional[str] = None,
    summary: Optional[str] = None,
    tags: Optional[list[str]] = None,
    flagged_research: Optional[bool] = None,
    promoted_ticket: Optional[str] = None,
    append_body: Optional[str] = None,
    inbox_root: Optional[Path] = None,
) -> InboxItem:
    """Update an inbox item and write it back (version-before-rewrite).

    Any subset of fields may be set. `append_body` adds a timestamped note to the markdown body
    (the "discussion thread" use). Raises ValueError on bad status/summary; FileNotFoundError if
    the item is missing.
    """
    if status is not None and status not in VALID_INBOX_STATUSES:
        raise ValueError(f"Invalid status {status!r}. Valid: {sorted(VALID_INBOX_STATUSES)}")
    if summary is not None and len(summary) > _MAX_SUMMARY:
        raise ValueError(f"summary must be {_MAX_SUMMARY} characters or fewer.")

    it = get_item(item_id, inbox_root)
    if it is None:
        raise FileNotFoundError(f"Inbox item {item_id!r} not found")

    if status is not None:
        it.status = status
    if summary is not None:
        it.summary = summary
    if tags is not None:
        it.tags = [str(t).strip() for t in tags if str(t).strip()]
    if flagged_research is not None:
        it.flagged_research = flagged_research
    if promoted_ticket is not None:
        it.promoted_ticket = promoted_ticket
    if append_body:
        it.body = f"{it.body.rstrip()}\n\n## Note ({_now_iso()})\n\n{append_body.strip()}"
    it.updated = _now_iso()

    # Version before rewrite -- house rule (best effort, same as tickets).
    if it.path and it.path.exists():
        try:
            from atlas_plus.mcp.memory_server.versioning import version_before_write
            version_before_write(it.path)
        except Exception:
            pass

    assert it.path is not None
    it.path.write_text(it.to_markdown(), encoding="utf-8")
    return it


def promote_to_ticket(
    item_id: str,
    project: str = "atlas-memory+",
    priority: str = "normal",
    inbox_root: Optional[Path] = None,
    tickets_root: Optional[Path] = None,
) -> tuple[InboxItem, str]:
    """Promote an inbox item to a ticket. Returns (updated_item, new_ticket_id).

    Creates a ticket from the item's title/url/summary, then marks the item promoted with a
    back-reference. Both writes are gated by the autonomous kill switch (via create_ticket and
    update_item paths). Raises FileNotFoundError if the item is missing.
    """
    from atlas_plus.tickets.store import create_ticket

    it = get_item(item_id, inbox_root)
    if it is None:
        raise FileNotFoundError(f"Inbox item {item_id!r} not found")

    title = (it.title or it.url or (it.summary[:80] if it.summary else item_id)).strip()[:200]
    body_parts = ["## What\n"]
    if it.url:
        body_parts.append(f"Source: {it.url}")
    if it.summary:
        body_parts.append(f"\n{it.summary}")
    body_parts.append(f"\n\nPromoted from inbox item {it.id}.")
    body_parts.append("\n\n## Acceptance criteria\n\n- [ ] TBD")
    ticket = create_ticket(
        title=title,
        project=project,
        assignee="any",
        priority=priority,
        body="\n".join(body_parts),
        tickets_root=tickets_root,
    )
    updated = update_item(item_id, status="promoted", promoted_ticket=ticket.id, inbox_root=inbox_root)
    return updated, ticket.id
