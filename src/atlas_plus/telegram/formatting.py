"""Message formatting helpers for the Atlas Telegram bot."""

from __future__ import annotations

from atlas_plus.tickets.store import Ticket

STATUS_EMOJI = {
    "todo": "o",
    "in-progress": "yellow_circle",
    "done": "white_check_mark",
    "blocked": "red_circle",
    "archived": "file_folder",
}

EFFORT_LABEL = {"S": "S", "M": "M", "L": "L", "XL": "XL"}


def _esc(text: str) -> str:
    """Escape special chars in dynamic fields for legacy Markdown."""
    return text.replace("_", "\\_").replace("*", "\\*").replace("`", "\\`").replace("[", "\\[")


def fmt_ticket(t: Ticket) -> str:
    """Format a single ticket as Markdown: *ATL-001* Title - _[M] todo @brain_."""
    effort = f"[{t.effort}]" if t.effort else ""
    assignee = f"@{t.assignee}" if t.assignee != "human" else ""
    parts = [f"*{t.id}*", _esc(t.title)]
    meta = " ".join(x for x in [effort, t.status, assignee] if x)
    if meta:
        parts.append(f"_{_esc(meta)}_")
    return " - ".join(parts)


def fmt_ticket_list(tickets: list[Ticket], header: str = "") -> str:
    """Format a list of tickets as a Markdown bullet list with optional header."""
    if not tickets:
        return header + "\n_No tickets found._" if header else "_No tickets found._"
    lines = [header] if header else []
    for t in tickets:
        lines.append(fmt_ticket(t))
    return "\n".join(lines)


def fmt_git_state(git: dict, project: str) -> str:
    """Format git state dict as a compact Markdown string (branch, dirty/clean, ahead/behind)."""
    if not git.get("is_git_repo"):
        return f"*{project}*: not a git repo"
    branch = git.get("branch", "?")
    dirty = "dirty" if git.get("dirty") else "clean"
    ahead = git.get("ahead")
    behind = git.get("behind")
    ab = ""
    if ahead is not None and behind is not None:
        ab = f" | +{ahead}/-{behind}"
    recent = git.get("recent_commits", [])
    last = f"\nLast: {_esc(recent[0]['message'][:60])}" if recent else ""
    return f"*{_esc(project)}*\nbranch: `{branch}` | {dirty}{ab}{last}"


def escape_md(text: str) -> str:
    """Escape special characters for Telegram MarkdownV2."""
    for ch in r"_*[]()~`>#+-=|{}.!":
        text = text.replace(ch, f"\\{ch}")
    return text
