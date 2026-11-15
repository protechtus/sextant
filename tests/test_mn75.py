"""MN75 (sweep 22 manual wave, Stop hook day ten).

(1) The Stop hook's transcript-mining stage split a transcript with str.splitlines(), so a turn holding a
raw U+2028/U+2029 inside a JSON string broke into halves that do not parse and its tool calls vanished
from the mined statistics; AG84 fixed the summarizer's loader only.
(2) With ATLAS_CLAUDE_CODE_ROOT set, a Stop payload naming a transcript outside that root was passed on;
the loader ignores such a file, walked the root instead and summarised another copy of the session (or
recorded "no turns found"), with nothing on stderr.
"""
from __future__ import annotations

import io
import json
from pathlib import Path


def _assistant_with_tool(sid: str, name: str, fp: str, text: str) -> str:
    rec = {"type": "assistant", "sessionId": sid, "uuid": f"u-{name}", "timestamp": "2026-11-15T07:00:00Z",
           "message": {"role": "assistant", "content": [{"type": "text", "text": text},
                                                        {"type": "tool_use", "id": f"t-{name}", "name": name,
                                                         "input": {"file_path": fp}}]}}
    return json.dumps(rec, ensure_ascii=False) + "\n"


def _transcript_with_separator_turn(tmp_path: Path) -> Path:
    f = tmp_path / "s.jsonl"
    f.write_bytes((_assistant_with_tool("s", "Read", "a.py", "plain")
                   + _assistant_with_tool("s", "Edit", "b.py", "one\u2028two\u2029three")).encode("utf-8"))
    return f


def test_mining_keeps_the_tool_call_of_a_turn_holding_a_raw_line_separator(tmp_path):
    from atlas_plus.temporal_graph.sources import _extract_tool_patterns
    p = _extract_tool_patterns(_transcript_with_separator_turn(tmp_path))
    assert p["tool_counts"] == {"Read": 1, "Edit": 1}
    assert sorted(p["files_touched"]) == ["a.py", "b.py"]


def test_metadata_counts_a_turn_holding_a_raw_line_separator(tmp_path):
    from atlas_plus.temporal_graph.sources import _extract_transcript_metadata
    meta = _extract_transcript_metadata(_transcript_with_separator_turn(tmp_path))
    assert meta["turn_count"] == 2


def _clean_env(monkeypatch, ws):
    monkeypatch.setenv("ATLAS_WORKSPACE_ROOT", str(ws))
    for var in ("ATLAS_MEMORY_ROOT", "ATLAS_STATE_DIR", "ATLAS_CLAUDE_CODE_ROOT", "ATLAS_PROJECTS_ROOT",
                "ATLAS_UPDATES_FILE", "ATLAS_HOOK_SCOPE", "ATLAS_TRANSCRIPT_FILE", "ATLAS_SUMMARIZER_DRY_RUN",
                "ATLAS_DISABLE_AUTOSUMMARIZE", "ATLAS_DISABLE_SUGGEST_FACTS", "ATLAS_AUTONOMOUS"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("ATLAS_SESSION_AUTONOMOUS", "on")
    monkeypatch.setenv("ATLAS_DISABLE_TRANSCRIPT_MINING", "1")


def _run_stop(monkeypatch, payload):
    from atlas_plus.summarizer import hook_entry as he
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    monkeypatch.setattr(he, "_summarizer_unavailable_reason", lambda: "")
    monkeypatch.setattr(he, "_capture_fsdiffs", lambda: None)
    spawned = []

    class _P:
        def __init__(self, args, **kw):
            spawned.append((list(args), dict(kw.get("env") or {})))

    monkeypatch.setattr(he.subprocess, "Popen", _P)
    monkeypatch.setattr(he.sys, "argv", ["hook"])
    he._SESSION["id"] = ""
    assert he.main() == 0
    return spawned


def _setup(tmp_path):
    ws = tmp_path / "ws"
    (ws / ".memory").mkdir(parents=True)
    (ws / ".atlas").mkdir()
    root = tmp_path / "cc" / "projects"
    inside = root / "slug"
    inside.mkdir(parents=True)
    outside = tmp_path / "old" / "projects" / "slug"
    outside.mkdir(parents=True)
    for d, n in ((inside, 3), (outside, 9)):
        (d / "sess.jsonl").write_text("".join(json.dumps({"type": "user", "sessionId": "sess", "uuid": f"{d.parent.parent.name}{i}",
                                                          "timestamp": "2026-11-15T07:00:00Z", "message": {"role": "user", "content": f"t{i}"}}) + "\n"
                                              for i in range(n)), encoding="utf-8")
    return ws, root, inside / "sess.jsonl", outside / "sess.jsonl"


def test_stop_summarises_the_payload_transcript_outside_the_configured_root(tmp_path, monkeypatch, capsys):
    ws, root, _inside, outside = _setup(tmp_path)
    _clean_env(monkeypatch, ws)
    monkeypatch.setenv("ATLAS_CLAUDE_CODE_ROOT", str(root))
    spawned = _run_stop(monkeypatch, {"session_id": "sess", "transcript_path": str(outside)})
    summ = [env for a, env in spawned if "atlas_plus.summarizer.cli" in a]
    assert summ, "the payload's own transcript is summarised, not refused (test_sweep14 pins the same intent)"
    env = summ[0]
    assert Path(env["ATLAS_TRANSCRIPT_FILE"]) == outside.resolve()
    assert Path(env["ATLAS_CLAUDE_CODE_ROOT"]) == outside.resolve().parents[1]
    assert "not under ATLAS_CLAUDE_CODE_ROOT" in capsys.readouterr().err
    # and the loader, given that child environment, reads the payload's 9 turns, not the stale 3-turn copy under the root
    from atlas_plus.summarizer import loader
    for k in ("ATLAS_TRANSCRIPT_FILE", "ATLAS_CLAUDE_CODE_ROOT"):
        monkeypatch.setenv(k, env[k])
    import importlib
    importlib.reload(loader)
    try:
        assert loader.single_transcript() == outside.resolve()
        assert len(loader.load_turns(conversation_id="sess")) == 9
    finally:
        monkeypatch.delenv("ATLAS_TRANSCRIPT_FILE")
        monkeypatch.delenv("ATLAS_CLAUDE_CODE_ROOT")
        importlib.reload(loader)


def test_stop_still_summarises_a_payload_transcript_under_the_configured_root(tmp_path, monkeypatch):
    ws, root, inside, _outside = _setup(tmp_path)
    _clean_env(monkeypatch, ws)
    monkeypatch.setenv("ATLAS_CLAUDE_CODE_ROOT", str(root))
    spawned = _run_stop(monkeypatch, {"session_id": "sess", "transcript_path": str(inside)})
    summ = [(a, env) for a, env in spawned if "atlas_plus.summarizer.cli" in a]
    assert summ and Path(summ[0][1]["ATLAS_TRANSCRIPT_FILE"]) == inside.resolve()


def test_stop_without_a_configured_root_uses_the_payload_root(tmp_path, monkeypatch):
    ws, _root, _inside, outside = _setup(tmp_path)
    _clean_env(monkeypatch, ws)
    spawned = _run_stop(monkeypatch, {"session_id": "sess", "transcript_path": str(outside)})
    summ = [env for a, env in spawned if "atlas_plus.summarizer.cli" in a]
    assert summ and Path(summ[0]["ATLAS_CLAUDE_CODE_ROOT"]) == outside.resolve().parents[1]
