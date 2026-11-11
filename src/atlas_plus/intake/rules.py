"""Routing rules: decide where an intake file belongs.

A rule is a match block plus a destination template. Rules are tried in
order; the first match wins. The built-in set is deliberately small and
frontmatter-first, because frontmatter is the only signal the author
controls completely:

- ``destination`` in frontmatter: an explicit workspace-relative path,
  confined to the landing root, the projects root or the corpus (see
  ``_check_destination``).
- ``project`` in frontmatter: ``projects/<slug>/intake/<name>``, plus a
  ticket so the brief shows up in the work queue.
- Otherwise extension families under the landing root.

Operators extend or replace the defaults with a JSON file
(``ATLAS_INTAKE_RULES``, default ``<state>/intake/rules.json``)::

    {"replace_defaults": false,
     "rules": [{"name": "receipts", "match": {"glob": "receipt*"},
                "dest": "{landing}/finance/receipts/{date}/{name}"}]}

Destination templates may use ``{landing}``, ``{projects}``, ``{memory}``,
``{workspace}``, ``{project_slug}``, ``{name}``, ``{stem}``, ``{ext}``,
``{date}`` and ``{fm.<key>}``. Every resolved destination must land inside
the workspace root or it is quarantined instead of written.

Replacement policy: only a destination under the projects root may replace
an existing file (after a version copy). Anywhere else (landing root,
corpus, operator-rule targets) an occupied path gets a ``_v1``-style
sibling instead, so two drops never overwrite each other.
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from atlas_plus import paths as atlas_paths
from atlas_plus.sync import policy

from .frontmatter import scalar

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def project_slug(value) -> str:
    """``"MyApp"`` -> ``"myapp"``; anything unusable -> ``"unsorted"``."""
    text = scalar(value, 120).lower()
    slug = _SLUG_RE.sub("-", text).strip("-")
    return slug or "unsorted"


@dataclass(frozen=True)
class Rule:
    name: str
    dest: str
    match: dict = field(default_factory=dict)
    on_import: tuple[str, ...] = ()
    describe: str = ""

    def matches(self, name: str, fm: dict) -> bool:
        m = self.match
        if not m:
            return True
        ext = Path(name).suffix.lower()
        if "ext" in m and ext not in {e.lower() for e in m["ext"]}:
            return False
        if "glob" in m and not fnmatch.fnmatch(name.lower(), str(m["glob"]).lower()):
            return False
        if "frontmatter_has" in m:
            keys = m["frontmatter_has"]
            if isinstance(keys, str):
                keys = [keys]
            if not all(k in fm and fm[k] not in (None, "") for k in keys):
                return False
        if "frontmatter_equals" in m:
            for k, want in dict(m["frontmatter_equals"]).items():
                if scalar(fm.get(k)).lower() != str(want).lower():
                    return False
        return True


DEFAULT_RULES: tuple[Rule, ...] = (
    Rule(
        name="frontmatter-destination",
        match={"frontmatter_has": ["destination"]},
        dest="{workspace}/{fm.destination}",
        on_import=("notify",),
        describe="Frontmatter names an explicit workspace-relative destination.",
    ),
    Rule(
        name="frontmatter-project",
        match={"frontmatter_has": ["project"]},
        dest="{projects}/{project_slug}/intake/{name}",
        on_import=("ticket", "notify"),
        describe="Frontmatter names a project; lands in that project's intake/ and opens a ticket.",
    ),
    Rule(
        name="markdown",
        match={"ext": [".md", ".markdown", ".mdx", ".txt"]},
        dest="{landing}/notes/{date}/{name}",
        on_import=("notify",),
    ),
    Rule(
        name="documents",
        match={"ext": [".pdf", ".docx", ".doc", ".pptx", ".ppt", ".xlsx", ".xls", ".epub", ".rtf", ".odt"]},
        dest="{landing}/docs/{date}/{name}",
        on_import=("notify",),
    ),
    Rule(
        name="data",
        match={"ext": [".csv", ".tsv", ".json", ".jsonl", ".xml", ".yaml", ".yml", ".toml", ".sqlite", ".db", ".zip", ".7z", ".tar", ".gz"]},
        dest="{landing}/data/{date}/{name}",
        on_import=("notify",),
    ),
    Rule(
        name="images",
        match={"ext": [".png", ".jpg", ".jpeg", ".gif", ".webp", ".heic", ".svg", ".bmp", ".tiff"]},
        dest="{landing}/media/images/{date}/{name}",
        on_import=("notify",),
    ),
    Rule(
        name="audio-video",
        match={"ext": [".mp3", ".m4a", ".wav", ".flac", ".ogg", ".mp4", ".mov", ".mkv", ".webm", ".avi"]},
        dest="{landing}/media/av/{date}/{name}",
        on_import=("notify",),
    ),
    Rule(
        name="unsorted",
        match={},
        dest="{landing}/unsorted/{date}/{name}",
        on_import=("notify",),
        describe="Fallback: nothing else matched.",
    ),
)


# -- Config ------------------------------------------------------------------

def landing_root() -> Path:
    """Where non-project files land: ``ATLAS_INTAKE_LANDING`` or ``<workspace>/intake``."""
    env = os.environ.get("ATLAS_INTAKE_LANDING", "").strip()
    return Path(env) if env else atlas_paths.workspace_root() / "intake"


def rules_path() -> Path:
    env = os.environ.get("ATLAS_INTAKE_RULES", "").strip()
    return Path(env) if env else atlas_paths.state_dir() / "intake" / "rules.json"


def _rule_from_dict(d: dict) -> Rule:
    on_import = d.get("on_import", ("notify",))
    if isinstance(on_import, str):
        on_import = (on_import,)
    return Rule(
        name=str(d["name"]),
        dest=str(d["dest"]),
        match=dict(d.get("match") or {}),
        on_import=tuple(str(a) for a in on_import),
        describe=str(d.get("describe", "")),
    )


def load_rules(path: Path | None = None) -> list[Rule]:
    """Effective rule list: operator rules first, then defaults (unless replaced).

    A missing file means defaults only. A malformed file is reported on
    stderr-free terms: it is ignored and defaults apply, because a broken
    rules file must not stop intake from landing files somewhere safe.
    """
    path = path or rules_path()
    user: list[Rule] = []
    replace = False
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, list):
                data = {"rules": data}
            replace = bool(data.get("replace_defaults", False))
            user = [_rule_from_dict(r) for r in data.get("rules", [])]
        except (json.JSONDecodeError, OSError, KeyError, TypeError, ValueError):
            user, replace = [], False
    if replace and user:
        return user
    return user + list(DEFAULT_RULES)


# -- Resolution ---------------------------------------------------------------

@dataclass(frozen=True)
class Decision:
    rule: Rule
    dest: Path | None          # absolute destination, or None when unsafe
    version_root: Path | None  # the root version_before_write files under
    reason: str = ""           # why dest is None
    replace: bool = False      # may an existing file at dest be replaced (versioned)?


class _Fm(dict):
    """``{fm.key}`` template access with a safe empty default."""

    def __getattr__(self, key):  # pragma: no cover - trivial
        return scalar(self.get(key))


def _within(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except (ValueError, OSError):
        return False


def _safe_component(text: str) -> str:
    return "".join(c for c in text if c not in '<>:"|?*\x00').strip()


# Suffixes a frontmatter destination may never land as (audit 2026-11-09
# row 3): a dropped note whose three frontmatter lines cmd.exe or a shell
# ignores must not become a script in a Startup folder, a profile or a hook.
EXECUTABLE_SUFFIXES: frozenset[str] = frozenset({
    ".cmd", ".bat", ".ps1", ".psm1", ".psd1", ".exe", ".sh", ".bash", ".zsh",
    ".py", ".pyw", ".js", ".jse", ".mjs", ".vbs", ".vbe", ".wsf", ".wsh",
    ".hta", ".lnk", ".url", ".scr", ".pif", ".com", ".msi", ".msc", ".cpl",
    ".reg", ".dll", ".jar", ".command", ".desktop", ".app",
})

# Corpus subtrees a dropped file may never be routed into: history and
# sync-internal dirs, plus dirs whose files are acted on automatically
# (a ticket file can be dispatched to an agent; skills and personas are
# loaded as instructions).
_CORPUS_DENIED_DIRS: frozenset[str] = frozenset(
    {n.casefold() for n in policy.EXCLUDED_DIR_NAMES}
    | {"_atlas", "_sync", "tickets", "skills", "personas", "anti"}
)


# Files a project reads as instructions or build input. A drop may never create or
# replace one of these: that is agent-instruction or supply-chain injection into every
# session in that project (round-B re-audit 2026-11-09, N4).
_PROJECT_CONTROL_FILES: frozenset[str] = frozenset({
    "claude.md", "agents.md", "gemini.md", "copilot-instructions.md", "cursorrules",
    "pyproject.toml", "setup.py", "setup.cfg", "package.json", "package-lock.json",
    "pnpm-lock.yaml", "yarn.lock", "requirements.txt", "dockerfile", "makefile",
    "justfile", "cargo.toml", "go.mod", "gemfile", "composer.json", "env",
})
_PROJECT_CONTROL_RE = re.compile(r"^(requirements[-_.].*\.txt|docker-compose.*\.ya?ml|compose\.ya?ml|.*\.(toml|ini|cfg|lock))$", re.I)


def _is_project_control_file(name: str) -> bool:
    low = name.casefold().lstrip(".")
    return low in _PROJECT_CONTROL_FILES or bool(_PROJECT_CONTROL_RE.match(low))


def _unsafe_component(part: str) -> str | None:
    """Why a single destination path component is refused, or None."""
    if part.startswith("."):
        return f"dot-prefixed component {part!r}"
    if not policy.is_portable_component(part):
        return f"non-portable component {part!r} (trailing dot/space, device name or 8.3 alias)"
    return None


def _crosses_reparse_point(dest: Path, root: Path) -> bool:
    """True when any existing component from root down to dest is a symlink or junction."""
    try:
        rel_parts = dest.relative_to(root).parts
    except ValueError:
        return True
    probe = root
    for part in rel_parts:
        probe = probe / part
        if probe.exists() and policy._is_reparse_point(probe):
            return True
    return False


def _check_destination(rel: str, dest: Path, landing: Path, projects: Path,
                       memory: Path) -> tuple[str | None, Path | None, bool]:
    """Confine a frontmatter ``destination`` (audit 2026-11-09 row 3).

    Returns ``(reason, version_root, replace)``; ``reason`` is None when the
    destination is allowed. Rules: the workspace-relative path must be plain
    (no absolute, drive, UNC, ``..``, ``:``), no component may be
    dot-prefixed or non-portable, the final name may not carry an
    executable or script suffix, and the resolved destination must lie under
    the landing root, the projects root or the corpus (excluding history,
    sync-internal and auto-acted subtrees). Only projects-root destinations
    may replace an existing file.
    """
    if not policy.is_safe_relpath(rel):
        return f"unsafe destination in frontmatter: {rel!r}", None, False
    for part in rel.split("/") + [dest.name]:
        why = _unsafe_component(part)
        if why:
            return f"unsafe destination in frontmatter: {why}", None, False
    if dest.suffix.lower() in EXECUTABLE_SUFFIXES:
        return f"unsafe destination in frontmatter: executable suffix {dest.suffix!r}", None, False
    for root in (memory, projects, landing):
        if _within(dest, root) and _crosses_reparse_point(dest, root):
            return f"unsafe destination in frontmatter: crosses a symlink or junction {rel!r}", None, False
    if _within(dest, memory):
        try:
            inner = dest.resolve().relative_to(memory.resolve()).parts
        except (ValueError, OSError):
            inner = ()
        if any(policy.normalize_component(p) in _CORPUS_DENIED_DIRS for p in inner[:-1]):
            return f"unsafe destination in frontmatter: protected corpus subtree {rel!r}", None, False
        return None, memory, False
    if _within(dest, projects):
        if _is_project_control_file(dest.name):
            return (f"unsafe destination in frontmatter: project control file {dest.name!r} "
                    "(instructions and build inputs are never written by intake)", None, False)
        return None, None, True
    if _within(dest, landing):
        return None, None, False
    return (f"destination outside the allowed roots (landing, projects, corpus): {rel!r}",
            None, False)


def resolve(name: str, fm: dict, rules: list[Rule] | None = None, *, today: str | None = None) -> Decision:
    """Pick the first matching rule and render its destination for ``name``."""
    rules = rules if rules is not None else load_rules()
    today = today or datetime.now(timezone.utc).date().isoformat()
    rule = next((r for r in rules if r.matches(name, fm)), DEFAULT_RULES[-1])
    workspace = atlas_paths.workspace_root()
    projects = atlas_paths.projects_root()
    slug = project_slug(fm.get("project"))
    tokens = {
        "landing": str(landing_root()),
        "projects": str(projects),
        "memory": str(atlas_paths.memory_root()),
        "workspace": str(workspace),
        "project_slug": slug,
        "name": _safe_component(name),
        "stem": _safe_component(Path(name).stem),
        "ext": Path(name).suffix,
        "date": today,
        "fm": _Fm(fm),
    }
    rel = ""
    if rule.name == "frontmatter-destination":
        rel = scalar(fm.get("destination"), 512).replace("\\", "/")
        if not policy.is_safe_relpath(rel):
            return Decision(rule, None, None, f"unsafe destination in frontmatter: {rel!r}")
    try:
        rendered = rule.dest.format(**tokens)
    except (KeyError, IndexError, AttributeError, ValueError) as exc:
        return Decision(rule, None, None, f"bad dest template {rule.dest!r}: {exc}")
    dest = Path(rendered)
    if dest.suffix == "" and dest.name != tokens["name"]:
        # A directory-shaped destination gets the file name appended.
        dest = dest / tokens["name"]
    if not _within(dest, workspace):
        return Decision(rule, None, None, f"destination escapes workspace: {dest}")
    if rule.name == "frontmatter-destination":
        memory = atlas_paths.memory_root()
        reason, version_root, replace = _check_destination(
            rel, dest, landing_root(), projects, memory)
        if reason is not None:
            return Decision(rule, None, None, reason)
        # Corpus writes version into the corpus versions/; everything else
        # keeps the workspace mirror it always used.
        return Decision(rule, dest, version_root or workspace, replace=replace)
    version_root = projects / slug if rule.name == "frontmatter-project" else workspace
    # Only the projects root may replace an existing file; landing-root and
    # other targets get a collision-free sibling (audit 2026-11-09 rows 3, 35).
    return Decision(rule, dest, version_root, replace=_within(dest, projects))
