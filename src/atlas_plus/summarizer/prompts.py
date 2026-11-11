"""P2-T4: Summarization prompts and Jinja2 template rendering."""

from __future__ import annotations

from pathlib import Path

from jinja2 import Environment, FileSystemLoader

TEMPLATES_DIR = Path(__file__).parent / "templates"
_env: Environment | None = None  # lazy-initialized on first render call


def _get_env() -> Environment:
    global _env
    if _env is None:
        # bandit B701 justification: these templates render PLAIN-TEXT prompts for
        # an LLM, never HTML served to a browser; autoescaping would corrupt
        # the prompt text (bandit triage, quality loop run 7).
        _env = Environment(loader=FileSystemLoader(str(TEMPLATES_DIR)), autoescape=False)  # nosec B701
    return _env


def map_prompt(chunk_text: str, chunk_index: int, chunk_total: int) -> str:
    """Build the per-chunk map prompt asking the LLM for JSON {worked_on, completed, pending, ...}."""
    return f"""\
You are summarizing a chunk of a software development session transcript.
This is chunk {chunk_index + 1} of {chunk_total}.

Extract the following from the transcript below and return ONLY valid JSON
(no markdown, no preamble):

{{
  "worked_on": ["short description of each thing worked on"],
  "completed": ["things that were finished and confirmed working"],
  "pending": ["things started but not finished, or next steps mentioned"],
  "files_touched": ["file paths that were created or edited"],
  "lessons": ["gotchas, decisions, or non-obvious facts worth remembering"]
}}

Think step by step about what was actually accomplished versus what was
just discussed. Only include things that actually happened in this chunk.

TRANSCRIPT:
{chunk_text}

JSON:"""


def reduce_prompt(chunk_summaries: list[str], date: str) -> str:
    """Build the reduce prompt that merges N chunk summaries into one final structured JSON."""
    joined = "\n\n---\n\n".join(
        f"Chunk {i + 1}:\n{s}" for i, s in enumerate(chunk_summaries)
    )
    return f"""\
You are merging {len(chunk_summaries)} partial summaries of a software development
session on {date} into one cohesive summary.

Deduplicate entries, resolve any contradictions (prefer later chunks), and
produce a final structured JSON object:

{{
  "title": "8-word-max description of the session (no punctuation)",
  "worked_on": ["deduplicated list of things worked on"],
  "completed": ["things finished and confirmed working, with key details"],
  "pending": ["next steps or unfinished items"],
  "files_touched": ["deduplicated file paths"],
  "lessons": ["deduplicated gotchas, decisions, non-obvious facts"]
}}

Rules:
- title must be short enough to fit on one line
- completed entries should include enough detail to be useful in 6 months
- No em-dashes, no emojis, no XML tags
- Return ONLY valid JSON, no markdown

PARTIAL SUMMARIES:
{joined}

JSON:"""


def render_session_log(summary: dict, date: str) -> str:
    """Render session_log.md.j2 Jinja2 template with the given summary + date."""
    tmpl = _get_env().get_template("session_log.md.j2")
    return tmpl.render(summary=summary, date=date)


def render_updates_entry(summary: dict, date: str) -> str:
    """Render updates_entry.txt.j2 Jinja2 template with the given summary + date."""
    tmpl = _get_env().get_template("updates_entry.txt.j2")
    return tmpl.render(summary=summary, date=date)
