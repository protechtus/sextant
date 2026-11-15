"""AG93 follow-ups: the Stop hook says when it falls back to a root lookup; the --- advice is one that works."""
import io
import json
import subprocess
import sys


def test_triple_dash_advice_is_drop_the_dashes():
    r = subprocess.run([sys.executable, "-m", "atlas_plus", "remember", "--- bad"], capture_output=True, text=True)
    assert r.returncode == 2
    assert "drop the leading dashes" in r.stderr and "Put the text after" not in r.stderr


def test_stop_hook_says_it_looks_the_session_up_under_the_root(tmp_path, monkeypatch, capsys):
    from atlas_plus.summarizer import hook_entry as he
    other = tmp_path / "other"
    other.mkdir()
    (other / "s-out.jsonl").write_text("{}\n", encoding="utf-8")
    monkeypatch.setenv("ATLAS_CLAUDE_CODE_ROOT", str(tmp_path / "ccroot"))
    monkeypatch.setenv("ATLAS_WORKSPACE_ROOT", str(tmp_path / "ws"))
    (tmp_path / "ws" / ".memory").mkdir(parents=True)
    (tmp_path / "ws" / ".atlas").mkdir()
    monkeypatch.setenv("ATLAS_DISABLE_AUTOSUMMARIZE", "1")
    monkeypatch.setenv("ATLAS_DISABLE_SUGGEST_FACTS", "1")
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"session_id": "s-out", "transcript_path": str(other / "s-out.jsonl")})))
    monkeypatch.setattr(he.subprocess, "Popen", lambda *a, **k: None)
    monkeypatch.setattr(he.sys, "argv", ["hook"])
    he._SESSION["id"] = ""
    he.main()
    assert "looking the session up by id under ATLAS_CLAUDE_CODE_ROOT" in capsys.readouterr().err
