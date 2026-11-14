"""P2-T1: atlas-summarize-session CLI.

Orchestrates: load turns -> chunk -> map summarize -> reduce -> render -> review -> write.

Usage:
  atlas-summarize-session
  atlas-summarize-session --since today --source claude-code
  atlas-summarize-session --since 4h --dry-run
  atlas-summarize-session --auto-commit
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

import typer
from atlas_plus._typer import Typer as _SextantTyper  # no Windows $VAR expansion
from rich.console import Console

from .chunker import chunk_turns, needs_reduce
from .loader import load_turns
from .prompts import map_prompt, reduce_prompt, render_session_log, render_updates_entry
from .providers import ProviderError, complete
from .renderer import write_outputs
from .review import review

app = _SextantTyper(help="Summarize a Claude Code session into the Atlas session log")
console = Console()
log = logging.getLogger("atlas.summarizer")


def _redact(text: str) -> str:
    try:
        from atlas_plus.redaction import redact
        return redact(text) or ""
    except Exception:  # noqa: BLE001
        return text


def _event(kind: str, **fields) -> None:
    """One activity-log line; the Stop hook runs this detached with its output discarded, so the
    log is the only place a failure can be seen (MN28: empty, 500 and 404 replies were silent)."""
    try:
        from atlas_plus import activity
        activity.record("hook", kind, **fields)
    except Exception:  # noqa: BLE001
        pass


def _chunk_key(redacted_text: str) -> str:
    import hashlib as _hl
    return _hl.sha256(redacted_text.encode("utf-8", "replace")).hexdigest()[:24]


def _chunk_cache_path(safe_id: str):
    from atlas_plus import paths as _paths
    return _paths.state_dir() / "summarizer" / f"chunks-{safe_id or 'anon'}.json"


def _load_chunk_cache(safe_id: str) -> dict:
    try:
        import json as _json
        p = _chunk_cache_path(safe_id)
        if p.exists() and p.stat().st_size < 8 * 1024 * 1024:
            data = _json.loads(p.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001
        pass
    return {}


def _save_chunk_cache(safe_id: str, cache: dict) -> None:
    """Only the chunks of the current run are kept (the last partial chunk is replaced as it grows)."""
    try:
        import json as _json
        p = _chunk_cache_path(safe_id)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(_json.dumps(cache, ensure_ascii=False), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def _min_new_turns() -> int:
    import os as _os
    try:
        return max(1, int(_os.environ.get("ATLAS_SUMMARIZER_MIN_NEW_TURNS", "6")))
    except ValueError:
        return 6


def _failure_file(safe_id: str):
    from atlas_plus import paths as _ap
    return _ap.state_dir() / "summarizer" / f"failed-{safe_id}.json" if safe_id else None


def _note_failure(safe_id: str, nturns: int) -> None:
    """Count a failed run at this turn count (AG64: a single-chunk or reduce failure paid a model call per Stop for ever)."""
    import json as _json
    f = _failure_file(safe_id)
    if not f:
        return
    try:
        f.parent.mkdir(parents=True, exist_ok=True)
        prev = _json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}
        count = int(prev.get("count", 0)) + 1 if prev.get("turns") == nturns else 1
        f.write_text(_json.dumps({"turns": nturns, "count": count}), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def _gave_up(safe_id: str, nturns: int) -> bool:
    """True after three failed runs at the same turn count: the next Stops skip the model until the session grows."""
    import json as _json
    f = _failure_file(safe_id)
    try:
        if not f or not f.exists():
            return False
        prev = _json.loads(f.read_text(encoding="utf-8"))
        return prev.get("turns") == nturns and int(prev.get("count", 0)) >= 3
    except Exception:  # noqa: BLE001
        return False


def _acquire_session_lock(safe_id: str, stale_s: float = 900.0) -> bool:
    """One summarizer per session at a time: `<state>/summarizer/<id>.lock`, released at exit."""
    import atexit
    import os as _os
    import time as _time
    try:
        from atlas_plus import paths as _paths
        lock = _paths.state_dir() / "summarizer" / f"s-{safe_id or 'anon'}.lock"  # `CON`/`nul` never locked on Windows (AG44)
        lock.parent.mkdir(parents=True, exist_ok=True)
        try:
            from atlas_plus.summarizer.renderer import _lock_owner_dead as _dead
            if _time.time() - lock.stat().st_mtime > stale_s or _dead(lock):  # a killed child's lock blocked its session for 15 minutes (MN57)
                lock.unlink()
        except OSError:
            pass
        fd = _os.open(str(lock), _os.O_CREAT | _os.O_EXCL | _os.O_WRONLY)
        _os.write(fd, str(_os.getpid()).encode())
        _os.close(fd)
    except FileExistsError:
        return False
    except OSError:
        return True  # no state dir to lock in: proceed

    def _release():
        try:
            lock.unlink()
        except OSError:
            pass
    atexit.register(_release)
    return True


def _alnum(s: str) -> str:
    import re as _re
    return _re.sub(r"[^0-9a-z]+", "", str(s).lower())


_TEMPLATE_CACHE: dict = {}


def _template_parts():
    """(long template lines, schema placeholder strings), both alphanumeric-folded, read from the two prompts
    themselves so an edit to a prompt can never leave the echo check behind (AG58: the five hard-coded phrases
    missed the reduce prompt entirely and fired on a session about editing the prompt)."""
    if not _TEMPLATE_CACHE:
        import re as _re
        lines: set = set()
        holders: set = set()
        for p in (map_prompt("", 0, 1), reduce_prompt([], "")):
            for ln in p.splitlines():
                n = _alnum(ln)
                if len(n) >= 24:
                    lines.add(n)
                mm = _re.match(r'^\s*"[a-z_]+":\s*(.*)$', ln)
                if mm:
                    for q in _re.findall(r'"([^"]+)"', mm.group(1)):
                        if len(_alnum(q)) >= 12:
                            holders.add(_alnum(q))
        _TEMPLATE_CACHE["v"] = (lines, holders)
    return _TEMPLATE_CACHE["v"]


def _echo_hits(parsed) -> int:
    """How many of the prompt template's own long lines appear, whole, in the reply."""
    import json as _json
    try:
        flat = _json.dumps(parsed, ensure_ascii=False)
    except Exception:  # noqa: BLE001
        flat = str(parsed)
    flat = _alnum(flat)
    return sum(1 for ln in _template_parts()[0] if ln in flat)


def _is_prompt_echo(parsed) -> bool:
    """An echoed prompt parses as a summary because the prompt holds the schema (AG50). A RATIO of the
    template, not a phrase count: whole template lines, six or more of the 26 (an echo of either prompt carries 12; a session
    about editing the prompt quotes one or two phrases, which are fragments of lines, not lines: AG58)."""
    return _echo_hits(parsed) >= 6


_LIST_KEYS = ("worked_on", "completed", "pending", "decisions", "files_touched", "lessons")


def _drop_placeholders(parsed):
    """(copy of ``parsed`` without schema placeholder items, how many were dropped). A reduce reply that is
    the schema itself ("deduplicated list of things worked on") parsed as a summary and became the entry (AG58)."""
    if not isinstance(parsed, dict):
        return parsed, 0
    holders = _template_parts()[1]
    out = dict(parsed)
    dropped = 0
    for k in _LIST_KEYS:
        if isinstance(out.get(k), list):
            keep = [x for x in out[k] if _alnum(x) not in holders]
            dropped += len(out[k]) - len(keep)
            out[k] = keep
    if isinstance(out.get("title"), str) and _alnum(out["title"]) in holders:
        out["title"] = ""
        dropped += 1
    return out, dropped


def _reject_reason(parsed) -> str:
    """One phrase saying WHY a reply is not a summary (AG58: "model reply was not a summary" told the user nothing)."""
    if not isinstance(parsed, dict):
        return "the reply was not a JSON object"
    if parsed.get("_fallback"):
        return "the reply was not JSON (plain text)"
    if _is_prompt_echo(parsed):
        return f"the reply repeated the prompt ({_echo_hits(parsed)} of its template lines)"
    clean, dropped = _drop_placeholders(parsed)
    if dropped and not any(isinstance(clean.get(k), list) and clean.get(k) for k in ("completed", "pending", "worked_on", "decisions")):
        return f"the reply was the prompt's schema with its placeholders ({dropped} of them) and no real content"
    t = parsed.get("title")
    if t is not None and (not isinstance(t, str) or len(t) > 160 or "\n" in t or "JSON" in t):
        return "the title was not a short one-line title"
    return "the reply had no completed, pending or worked-on items"


def _looks_like_chunk_summary(parsed) -> bool:
    """A map reply worth caching: a JSON object with at least one of the schema's list fields, and not an
    echo of the prompt."""
    if not isinstance(parsed, dict) or parsed.get("_fallback") or _is_prompt_echo(parsed):
        return False
    clean, dropped = _drop_placeholders(parsed)
    if dropped and not any(isinstance(clean.get(k), list) and clean.get(k) for k in _LIST_KEYS):
        return False  # only the schema's placeholders (AG58)
    return any(isinstance(parsed.get(k), list) for k in ("worked_on", "completed", "pending", "decisions", "files_touched"))


def _looks_like_summary(summary) -> bool:
    if not isinstance(summary, dict) or summary.get("_fallback") or _is_prompt_echo(summary):
        return False
    _c, _d = _drop_placeholders(summary)
    if _d and not any(isinstance(_c.get(k), list) and _c.get(k) for k in ("completed", "pending", "worked_on", "decisions")):
        return False  # the schema's own placeholders and nothing else (AG58)
    title = summary.get("title")
    # the MAP prompt's schema has no title, so a single-chunk session (no reduce) arrives without one
    # (MN39: every short session was refused as "not a summary" for a whole day); a title that IS present
    # must look like a title
    if title is not None and (not isinstance(title, str) or len(title) > 160 or "\n" in title or "JSON" in title):
        return False
    return any(isinstance(summary.get(k), list) and summary.get(k) for k in ("completed", "pending", "worked_on", "decisions"))


def _parse_json_response(text: str) -> dict:
    """Extract JSON object from LLM response, tolerating markdown fences."""
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        inner = [l for l in lines[1:] if not l.startswith("```")]
        text = "\n".join(inner).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # best-effort: find first { ... }
        start = text.find("{")
        end = text.rfind("}") + 1
        if start >= 0 and end > start:
            try:
                return json.loads(text[start:end])
            except json.JSONDecodeError:
                pass
        return {
            "_fallback": True,  # plain text is not a summary (AG43: a junk reply became the session's entry)
            "title": "session summary",
            "worked_on": [],
            "completed": [text[:500]],
            "pending": [],
            "files_touched": [],
            "lessons": [],
        }


@app.command()
def main(
    source: str = typer.Option("claude-code", help="Transcript source: claude-code | atlas | auto"),
    conversation_id: str | None = typer.Option(None, "--conversation-id", "-c"),
    since: str | None = typer.Option("today", "--since", "-s", help="Start time: today | Nh | ISO timestamp"),
    until: str | None = typer.Option(None, "--until"),
    model: str | None = typer.Option(None, "--model", help="Override LLM model (sets ATLAS_SUMMARIZER_MODEL)"),
    dry_run: bool = typer.Option(False, "--dry-run", "-n"),
    auto_commit: bool = typer.Option(False, "--auto-commit", help="Skip review and write immediately"),
    token_budget: int = typer.Option(6000, "--token-budget", help="Tokens per map chunk"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
):
    """Summarize a session and write to .memory/sessions/ and updates.txt."""
    if verbose:
        logging.basicConfig(level=logging.INFO, format="%(name)s %(levelname)s %(message)s")

    if model:
        import os
        os.environ["ATLAS_SUMMARIZER_MODEL"] = model

    date = datetime.now().astimezone().strftime("%Y-%m-%d")  # the user's day (MN28)
    if not dry_run:
        from atlas_plus.autonomous import is_session_autonomous as _gate
        if not _gate():
            # the gate used to run AFTER the model call: the transcript went out, then a traceback (AG38)
            console.print("[yellow]Session summaries are off: ATLAS_SESSION_AUTONOMOUS=on (or ATLAS_AUTONOMOUS=on) must reach this process (the Stop hook's --env flag).[/yellow]")
            _event("summarizer_skipped", reason="ATLAS_SESSION_AUTONOMOUS is off", conversation_id=conversation_id or "")
            raise typer.Exit(2)
    marker = ""
    replace_prefix = ""
    prior_turns = None
    safe_id = ""
    if conversation_id:
        from atlas_plus.summarizer.renderer import MEMORY_SESSIONS_DIR as _sdir, find_session_entry
        safe_id = ''.join(ch for ch in conversation_id if ch.isalnum() or ch in '-_.')[:80]
        found = find_session_entry(_sdir, safe_id)
        if found is not None:
            prior_turns = found[1]
        if since == "today":
            since = None  # the session is the unit: a session across midnight lost its first hour (AG40)
        if not dry_run and not _acquire_session_lock(safe_id):
            console.print(f"[yellow]Another summarizer run for session {conversation_id} is in progress; nothing to do.[/yellow]")
            _event("summarizer_skipped", reason="another run for this session is in progress", conversation_id=conversation_id or "")  # it left no trace (MN57)
            raise typer.Exit(0)  # four Stops in one second each paid for the model (MN33)

    # --- 1. Load ---
    console.print(f"Loading turns (source={source}, since={since})...")
    try:
        turns = load_turns(source=source, conversation_id=conversation_id, since=since, until=until)
    except ValueError as exc:  # a bad --since/--source is one line, not a traceback (AG34)
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(2)
    if not turns:
        console.print("[yellow]No turns found for the given filters. Check --since or --source.[/yellow]")
        _event("summarizer_failed", reason="no turns found", conversation_id=conversation_id or "")
        raise typer.Exit(1)
    console.print(f"Loaded [bold]{len(turns)}[/bold] turns.")
    if conversation_id:
        # Claude Code fires Stop after every reply: the entry is refreshed when the session grew by
        # ATLAS_SUMMARIZER_MIN_NEW_TURNS turns, and left alone otherwise (MN33: the first summary froze)
        if _gave_up(safe_id, len(turns)):
            console.print(f"[yellow]Session {conversation_id} failed to summarise three times at {len(turns)} turns; waiting for new turns.[/yellow]")
            _event("summarizer_skipped", reason="gave up until the session grows", conversation_id=conversation_id or "")
            raise typer.Exit(0)
        if prior_turns is not None and prior_turns > 0 and len(turns) - prior_turns < _min_new_turns():  # 0 = a partial entry: always retried (AG64: a 3-turn partial session never was)
            console.print(f"[yellow]Session {conversation_id} is summarised at {prior_turns} turns and has {len(turns)} now: nothing new.[/yellow]")
            raise typer.Exit(0)
        marker = f"<!-- session:{safe_id} turns:{len(turns)} -->"
        replace_prefix = f"<!-- session:{safe_id}"  # always: a second workspace on the same state dir had duplicated the updates entry (AG64)

    # --- 2. Chunk ---
    chunks = chunk_turns(turns, token_budget=token_budget)
    console.print(f"Split into [bold]{len(chunks)}[/bold] chunk(s).")

    # --- 3. Map ---
    # A refresh re-summarises only the chunks whose text changed: the cache is keyed by the chunk's
    # redacted text (AG48: a 5,000-turn session paid 78 model calls per six new turns)
    chunk_cache = _load_chunk_cache(safe_id) if safe_id else {}
    used_cache: dict[str, str] = {}
    chunk_summaries: list[str] = []
    reused = 0
    rejected: list[str] = []
    for chunk in chunks:
        red = _redact(chunk.text)
        key = _chunk_key(red)
        cached = chunk_cache.get(key)
        if cached:
            chunk_summaries.append(cached)
            used_cache[key] = cached
            reused += 1
            continue
        console.print(f"Summarizing chunk {chunk.index + 1}/{len(chunks)}...")
        prompt = map_prompt(red, chunk.index, len(chunks))  # keys never reach the model (MN28)
        try:
            text, provider = complete(prompt)
            console.print(f"  [dim]via {provider}[/dim]")
        except ProviderError as e:
            console.print(f"[red]LLM call failed:[/red] {e}")
            _event("summarizer_failed", reason=str(e)[:200], conversation_id=conversation_id or "")
            if safe_id and not dry_run and used_cache:
                _save_chunk_cache(safe_id, used_cache)  # the chunks already summarised are kept for the retry (AG54: 34 calls paid twice)
            raise typer.Exit(1)
        _parsed = _parse_json_response(text)
        if _looks_like_chunk_summary(_parsed):
            _clean, _ = _drop_placeholders(_parsed)
            text = json.dumps(_clean, ensure_ascii=False)  # one placeholder next to real items is dropped, not kept
            chunk_summaries.append(text)
            used_cache[key] = text  # only a reply shaped like a chunk summary is cached (MN39, AG49: a JSON list crashed here and a JSON object of junk was cached)
        else:
            # AG58: a rejected reply (an echoed 21 KB prompt, plain text) used to go into the reduce prompt as
            # a chunk summary: the reduce grew to 34 KB, the entry lost the chunk and kept the prompt's own
            # placeholders, and the only event was summary_written. Now: an event naming the chunk, a placeholder
            # in the reduce input instead of the reply, and a `partial` marker so the next Stop retries.
            _first_turn = sum(len(c.turns) for c in chunks[:chunk.index]) + 1
            _label = f"part {chunk.index + 1} of {len(chunks)} (turns {_first_turn}-{_first_turn + len(chunk.turns) - 1})"
            rejected.append(_label)
            _event("summarizer_chunk_rejected", chunk=chunk.index + 1, of=len(chunks), reply_chars=len(text),
                   reason=_reject_reason(_parsed)[:160], conversation_id=conversation_id or "")
            console.print(f"[yellow]Chunk {chunk.index + 1}/{len(chunks)} rejected: {_reject_reason(_parsed)}.[/yellow]")
            chunk_summaries.append(json.dumps({"worked_on": [f"{_label} could not be summarised"], "completed": [], "pending": [], "files_touched": [], "lessons": []}))
    if rejected and len(rejected) == len(chunks):
        console.print("[red]Every chunk reply was unusable; nothing written.[/red]")
        _event("summarizer_failed", reason=f"all {len(chunks)} chunk replies were unusable (not summaries); see summarizer_chunk_rejected", conversation_id=conversation_id or "")
        if not dry_run:
            _note_failure(safe_id, len(turns))
        raise typer.Exit(1)
    if reused:
        console.print(f"[dim]{reused} of {len(chunks)} chunk summaries reused from the last run.[/dim]")
    if safe_id and not dry_run:
        _save_chunk_cache(safe_id, used_cache)

    # --- 4. Reduce ---
    if needs_reduce(chunks):
        console.print("Reducing chunk summaries...")
        prompt = reduce_prompt(chunk_summaries, date)
        try:
            final_text, provider = complete(prompt)
            console.print(f"  [dim]via {provider}[/dim]")
        except ProviderError as e:
            console.print(f"[red]Reduce call failed:[/red] {e}")
            _event("summarizer_failed", reason=str(e)[:200], conversation_id=conversation_id or "")
            raise typer.Exit(1)
        summary = _parse_json_response(final_text)
    else:
        summary = _parse_json_response(chunk_summaries[0])
    if isinstance(summary, dict):
        _s, _n = _drop_placeholders(summary)
        if _looks_like_summary(_s) or not _n:
            summary = _s
    if isinstance(summary, dict) and not str(summary.get("title") or "").strip():
        _first = next((v[0] for k in ("worked_on", "completed", "pending") for v in [summary.get(k)] if isinstance(v, list) and v and isinstance(v[0], str)), "")
        summary["title"] = (_first.strip()[:80] or "Session summary")  # the map schema carries no title (MN39)
    if not _looks_like_summary(summary):
        # a non-JSON reply used to be written as the session's summary and then protected by its
        # marker (MN33: "things that were finished and confirmed working Pending: ...")
        _why = _reject_reason(summary)
        console.print(f"[red]The model's reply was not a usable summary: {_why}; nothing written. It is retried at the next Stop; if it keeps happening, try a larger model (ATLAS_SUMMARIZER_MODEL).[/red]")
        _event("summarizer_failed", reason=f"model reply was not a summary: {_why}", conversation_id=conversation_id or "")
        if not dry_run:
            _note_failure(safe_id, len(turns))  # a dry run changes no state (MN57)
        raise typer.Exit(1)

    if rejected:
        # the entry says what is missing, by name, and the marker makes the next Stop retry it
        summary.setdefault("pending", [])
        if not isinstance(summary["pending"], list):
            summary["pending"] = []
        from atlas_plus.summarizer.renderer import MEMORY_SESSIONS_DIR as _sdir2, _PARTIAL_RETRIES as _PR, partial_count as _pc
        _n = (_pc(_sdir2, safe_id) + 1) if marker else 1
        _again = "retried at the next Stop" if _n < _PR else "retried when the session grows (the model gave up on it twice)"  # AG61: the text promised a retry the cap no longer makes
        summary["pending"].append("Incomplete: " + "; ".join(rejected) + " could not be summarised (unusable model reply); " + _again)
        console.print(f"[yellow]{len(rejected)} of {len(chunks)} chunks could not be summarised; the entry says so and is {_again}.[/yellow]")
        if marker:
            marker = marker[:-4] + f" partial:{_n} -->"  # the count caps the retries (MN49)
    # --- 5. Render ---
    for _k in ("worked_on", "completed", "pending", "decisions", "files_touched", "lessons"):  # lessons too (AG61: a newline inside a lesson left orphaned headings and end markers)
        if isinstance(summary.get(_k), list):
            summary[_k] = [__import__("re").sub(r"^#+\s+", "", " ".join(str(x).split())) for x in summary[_k] if str(x).strip()]  # one line per item (AG49); no item starts a heading (AG64); `#1 priority` keeps its hash (MN55)
    if isinstance(summary.get("title"), str):
        summary["title"] = " ".join(summary["title"].split())
    session_log_text = _redact(render_session_log(summary, date))
    updates_text = _redact(render_updates_entry(summary, date))
    if marker:
        # the entry names its session and its turn count: a re-fired Stop writes nothing twice, a
        # grown session replaces its entry (MN28, MN33)
        head, _, rest = session_log_text.partition("\n")
        session_log_text = head + "\n" + marker + "\n" + rest
        uhead, _, urest = updates_text.partition("\n")
        updates_text = uhead + "\n" + marker + "\n" + urest  # the updates entry is found and replaced by its marker too (AG43)
        updates_text = updates_text.rstrip("\n") + f"\n<!-- end session:{safe_id} -->\n"  # AG58: the entry's last line, so removal ends exactly here
        session_log_text = session_log_text.rstrip("\n") + f"\n<!-- end session:{safe_id} -->\n"  # the day log too (MN49, MN51: a hand-written line under the entry was deleted)

    # --- 6. Review ---
    if auto_commit or dry_run:
        accepted, session_log_text, updates_text = True, session_log_text, updates_text
    else:
        accepted, session_log_text, updates_text = review(session_log_text, updates_text)

    if not accepted:
        raise typer.Exit(0)

    # --- 7. Write ---
    try:
        result = write_outputs(session_log_text, updates_text, date, dry_run=dry_run, session_marker=marker, replace_session=replace_prefix)
    except Exception as exc:  # noqa: BLE001
        # AG58: a write that died (Windows PermissionError under concurrent Stops) was a traceback in a discarded stream
        _event("summarizer_failed", reason=f"write failed: {type(exc).__name__}: {exc}"[:200], conversation_id=conversation_id or "")
        raise
    if result.get("skipped"):
        console.print(f"[yellow]{result['skipped']}[/yellow]")
        raise typer.Exit(0)
    if not dry_run:
        _event("summary_written", path=result.get("session_log", ""), conversation_id=conversation_id or "", **({"partial_chunks": len(rejected)} if rejected else {}))
    if dry_run:
        console.print(session_log_text)  # the entry the run would have written: the dry-run log had held only progress lines (AG64)
    dry_tag = " (dry-run)" if dry_run else ""
    for label, path in result.items():
        console.print(f"[green]Written{dry_tag}:[/green] {label} -> {path}")

    console.print("\n[bold green]Done.[/bold green]")


if __name__ == "__main__":  # the Stop hook spawns `python -m atlas_plus.summarizer.cli` (AG34: it was a no-op)
    app()
