"""AG87 (sweep 23): the Stop hook's cost per reply, and the hooks' scope.

1. The facts stage loaded and redacted the whole transcript on every Stop only to print "nothing
   new" (72 s and 290 MB on a 97 MB session). It now counts the turns first: from the bytes
   written since the last Stop when the hook names the transcript, and before the per-turn
   redaction otherwise. When something is new it runs exactly as before.
2. The mining stage hashed and re-parsed every transcript of the project on every Stop. An
   unchanged file is skipped unread; a file that only grew is read from where the last run
   stopped and gives the same facts as a whole read.
3. SessionStart decided ATLAS_HOOK_SCOPE on the process cwd while Stop used the payload's cwd,
   so one session could be primed by one install and summarised by another. Both use the
   payload's cwd now. A scope value carrying its own quotes matched nothing, silently; both
   hooks now say so.
"""
from __future__ import annotations

import io
import json
from pathlib import Path

import pytest


def _clean_env(monkeypatch, ws):
    monkeypatch.setenv("ATLAS_WORKSPACE_ROOT", str(ws))
    for var in ("ATLAS_MEMORY_ROOT", "ATLAS_STATE_DIR", "ATLAS_CLAUDE_CODE_ROOT", "ATLAS_PROJECTS_ROOT",
                "ATLAS_UPDATES_FILE", "ATLAS_HOOK_SCOPE", "ATLAS_IDENTITY_PATH", "ATLAS_USER_NAME",
                "ATLAS_HOME_TZ", "ATLAS_TASK_HINT", "ATLAS_TRANSCRIPT_FILE", "ATLAS_SUMMARIZER_DRY_RUN",
                "ATLAS_DISABLE_AUTOSUMMARIZE", "ATLAS_DISABLE_SUGGEST_FACTS", "ATLAS_AUTONOMOUS",
                "ATLAS_SUMMARIZER_MIN_NEW_TURNS"):
        monkeypatch.delenv(var, raising=False)


def _workspace(tmp_path, name="ws"):
    ws = tmp_path / name
    (ws / ".memory").mkdir(parents=True)
    (ws / ".atlas").mkdir()
    return ws


def _turn(i: int, sid: str) -> str:
    role = "user" if i % 2 == 0 else "assistant"
    content = ([{"type": "text", "text": f"turn {i} " + "x" * 200},
                {"type": "tool_use", "name": ["Read", "Edit", "Bash"][i % 3], "input": {"file_path": f"C:\\w\\f{i % 7}.py"}}]
               if role == "assistant" else f"turn {i} please " + "y" * 200)
    return json.dumps({"type": role, "uuid": f"u{i}", "sessionId": sid, "timestamp": "2026-11-14T10:00:00Z",
                       "message": {"role": role, "content": content}}) + "\n"


def _append(path: Path, start: int, n: int, sid: str = "sess") -> None:
    with open(path, "a", encoding="utf-8", newline="\n") as f:
        f.write("".join(_turn(i, sid) for i in range(start, start + n)))


# --------------------------------------------------------------------------- (1) facts stage

@pytest.fixture
def _facts_env(tmp_path, monkeypatch):
    ws = _workspace(tmp_path)
    _clean_env(monkeypatch, ws)
    cc = tmp_path / "cc" / "projects"
    (cc / "p").mkdir(parents=True)
    transcript = cc / "p" / "sess.jsonl"
    transcript.write_text("".join(_turn(i, "sess") for i in range(40)), encoding="utf-8")
    from atlas_plus.summarizer import loader
    from atlas_plus.curation import suggest_facts as sf
    monkeypatch.setattr(loader, "CLAUDE_CODE_ROOT", cc)
    monkeypatch.setenv("ATLAS_CLAUDE_CODE_ROOT", str(cc))
    monkeypatch.setenv("ATLAS_TRANSCRIPT_FILE", str(transcript))
    model = []
    monkeypatch.setattr(sf, "_extract_candidates", lambda text, **kw: (model.append(text), ([], 0))[1])
    loads = []
    real = loader.load_turns
    monkeypatch.setattr(loader, "load_turns", lambda **kw: (loads.append(kw), real(**kw))[1])
    return ws, transcript, sf, model, loads


def test_facts_nothing_new_does_not_load_the_transcript(_facts_env):
    ws, transcript, sf, model, loads = _facts_env
    sf.suggest_facts(conversation_id="sess", auto=True, memory_root=ws / ".memory")
    assert len(model) == 1 and len(loads) == 1
    assert sf._session_done_marker("sess").read_text(encoding="utf-8").split()[:2] == ["40", "turns"]
    _append(transcript, 40, 4)  # four new turns: below the six-turn gate
    res = sf.suggest_facts(conversation_id="sess", auto=True, memory_root=ws / ".memory")
    assert res.get("already_done")
    assert len(model) == 1
    assert len(loads) == 1, "the nothing-new Stop loaded the whole transcript again"


def test_facts_with_something_new_runs_exactly_as_before(_facts_env):
    ws, transcript, sf, model, loads = _facts_env
    sf.suggest_facts(conversation_id="sess", auto=True, memory_root=ws / ".memory")
    _append(transcript, 40, 4)
    sf.suggest_facts(conversation_id="sess", auto=True, memory_root=ws / ".memory")
    _append(transcript, 44, 4)  # 48 now: eight past the marker
    sf.suggest_facts(conversation_id="sess", auto=True, memory_root=ws / ".memory")
    assert len(model) == 2
    assert sf._session_done_marker("sess").read_text(encoding="utf-8").split()[0] == "48"
    # the text the model saw is the whole session, as a full load gives it
    getattr(sf, "_STOP_IF", {})["fn"] = None  # a guard: passes before and after
    assert model[-1] == sf._load_transcript_text(conversation_id="sess")
    assert model[-1].count("\n") + 1 == 48


def test_facts_gave_up_is_decided_before_the_load(_facts_env):
    ws, transcript, sf, model, loads = _facts_env
    f = sf._facts_failure_file("sess")
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps({"lines": 40, "count": 3}), encoding="utf-8")
    assert sf.suggest_facts(conversation_id="sess", auto=True, memory_root=ws / ".memory") == 0
    assert model == [] and loads == []


def test_facts_without_a_named_transcript_skips_the_redaction_when_nothing_is_new(_facts_env, monkeypatch):
    ws, transcript, sf, model, loads = _facts_env
    monkeypatch.delenv("ATLAS_TRANSCRIPT_FILE")
    sf.suggest_facts(conversation_id="sess", auto=True, memory_root=ws / ".memory")
    assert len(model) == 1
    from atlas_plus import redaction
    redacted = []
    real = redaction.redact
    monkeypatch.setattr(redaction, "redact", lambda t, *a, **k: (redacted.append(1), real(t, *a, **k))[1])
    _append(transcript, 40, 2)
    res = sf.suggest_facts(conversation_id="sess", auto=True, memory_root=ws / ".memory")
    assert res.get("already_done") and len(model) == 1
    assert redacted == [], f"{len(redacted)} turns redacted for a nothing-new run"


# --------------------------------------------------------------------------- (2) mining stage

def _mine(root: Path, graph: Path):
    from atlas_plus.temporal_graph.file_provider import FileTemporalProvider
    from atlas_plus.temporal_graph.sources import Manifest, TranscriptMiningSource
    provider = FileTemporalProvider(graph_root=graph)
    manifest = Manifest.load(provider._manifest_file)
    res = TranscriptMiningSource(claude_root=root, target_dir="p").ingest(provider, manifest)
    return res, provider, None


def _fact_texts(provider) -> dict:
    return {fid: f.fact_text for fid, f in provider._facts.items() if not f.invalid_at}


@pytest.fixture
def _mining_env(tmp_path, monkeypatch):
    root = tmp_path / "cc" / "projects"
    (root / "p").mkdir(parents=True)
    t = root / "p" / "sess.jsonl"
    t.write_text("".join(_turn(i, "sess") for i in range(30)), encoding="utf-8")
    (root / "p" / "old.jsonl").write_text("".join(_turn(i, "old") for i in range(12)), encoding="utf-8")
    from atlas_plus.temporal_graph import sources
    hashed = []
    real = sources._file_hash
    monkeypatch.setattr(sources, "_file_hash", lambda p: (hashed.append(Path(p).name), real(p))[1])
    return root, t, hashed, tmp_path


def test_mining_skips_an_unchanged_transcript_unread(_mining_env):
    root, t, hashed, tmp = _mining_env
    res1, _, _ = _mine(root, tmp / "g")
    assert res1["files_indexed"] == 2
    hashed.clear()
    res2, _, _ = _mine(root, tmp / "g")
    assert res2["files_indexed"] == 0 and res2["files_skipped"] == 2
    assert hashed == [], f"unchanged transcripts were read whole: {hashed}"


def test_mining_reads_only_what_was_appended_and_gives_the_whole_read_facts(_mining_env):
    root, t, hashed, tmp = _mining_env
    _mine(root, tmp / "g")
    _append(t, 30, 9)
    with open(t, "a", encoding="utf-8", newline="\n") as f:
        f.write(_turn(39, "sess").rstrip("\n"))  # a last line without its newline yet
    hashed.clear()
    res, prov, _ = _mine(root, tmp / "g")
    assert res["files_indexed"] == 1 and res["files_skipped"] == 1
    assert hashed == [], f"a grown transcript was hashed whole: {hashed}"
    _, fresh, _ = _mine(root, tmp / "fresh")  # a whole read of the same files
    assert _fact_texts(prov) == _fact_texts(fresh)
    # the unfinished line completes; the next run picks it up exactly once
    with open(t, "a", encoding="utf-8", newline="\n") as f:
        f.write("\n")
    _append(t, 40, 3)
    _, prov2, _ = _mine(root, tmp / "g")
    _, fresh2, _ = _mine(root, tmp / "fresh2")
    assert _fact_texts(prov2) == _fact_texts(fresh2)


def test_mining_rereads_a_rewritten_transcript_whole(_mining_env):
    root, t, hashed, tmp = _mining_env
    _mine(root, tmp / "g")
    t.write_text("".join(_turn(i, "sess") for i in range(101, 125)), encoding="utf-8")  # other bytes, smaller
    hashed.clear()
    _, prov, _ = _mine(root, tmp / "g")
    assert "sess.jsonl" in hashed
    _, fresh, _ = _mine(root, tmp / "fresh")
    assert _fact_texts(prov) == _fact_texts(fresh)


# --------------------------------------------------------------------------- (3) scope on the payload cwd

def _session_start(monkeypatch, payload, capsys):
    from atlas_plus.hooks import session_start
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload) if payload is not None else ""))
    monkeypatch.setattr(session_start.sys, "argv", ["session-start"])
    session_start.main()
    return capsys.readouterr()


def _scoped(tmp_path, monkeypatch):
    ws = _workspace(tmp_path)
    _clean_env(monkeypatch, ws)
    inside = tmp_path / "code" / "beta"
    outside = tmp_path / "code" / "alpha"
    inside.mkdir(parents=True)
    outside.mkdir(parents=True)
    monkeypatch.setenv("ATLAS_HOOK_SCOPE", str(inside))
    return inside, outside


def test_session_start_primes_a_session_whose_payload_cwd_is_in_scope(tmp_path, monkeypatch, capsys):
    inside, outside = _scoped(tmp_path, monkeypatch)
    monkeypatch.chdir(outside)  # the hook process runs elsewhere; the payload names the session's directory
    out = _session_start(monkeypatch, {"session_id": "s", "hook_event_name": "SessionStart", "cwd": str(inside)}, capsys)
    assert "SEXTANT SESSION CONTEXT" in out.out


def test_session_start_leaves_alone_a_session_whose_payload_cwd_is_outside(tmp_path, monkeypatch, capsys):
    inside, outside = _scoped(tmp_path, monkeypatch)
    monkeypatch.chdir(inside)
    out = _session_start(monkeypatch, {"session_id": "s", "hook_event_name": "SessionStart", "cwd": str(outside)}, capsys)
    assert "SEXTANT SESSION CONTEXT" not in out.out


def test_session_start_without_a_payload_still_uses_the_process_cwd(tmp_path, monkeypatch, capsys):
    inside, outside = _scoped(tmp_path, monkeypatch)
    monkeypatch.chdir(inside)
    assert "SEXTANT SESSION CONTEXT" in _session_start(monkeypatch, None, capsys).out
    monkeypatch.chdir(outside)
    assert "SEXTANT SESSION CONTEXT" not in _session_start(monkeypatch, None, capsys).out


def test_a_quoted_scope_value_is_named_by_both_hooks(tmp_path, monkeypatch, capsys):
    inside, outside = _scoped(tmp_path, monkeypatch)
    monkeypatch.setenv("ATLAS_HOOK_SCOPE", f'"{inside}"')
    monkeypatch.chdir(inside)
    out = _session_start(monkeypatch, {"session_id": "s", "cwd": str(inside)}, capsys)
    assert "SEXTANT SESSION CONTEXT" not in out.out
    assert "quote" in out.err and "ATLAS_HOOK_SCOPE" in out.err
    from atlas_plus.summarizer import hook_entry as he
    monkeypatch.setenv("ATLAS_SESSION_AUTONOMOUS", "on")
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"session_id": "s", "cwd": str(inside)})))
    monkeypatch.setattr(he, "_summarizer_unavailable_reason", lambda: "")
    monkeypatch.setattr(he, "_capture_fsdiffs", lambda: None)
    spawned = []
    monkeypatch.setattr(he.subprocess, "Popen", lambda args, **kw: spawned.append(args))
    monkeypatch.setattr(he.sys, "argv", ["hook"])
    assert he.main() == 0
    err = capsys.readouterr().err
    assert spawned == []
    assert "ran outside ATLAS_HOOK_SCOPE" in err and "quote" in err
