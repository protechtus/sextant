"""MN78 F1/F2: verify announces the one-time index rebuild; setup before init warns."""
import sys


def test_setup_before_init_warns_on_stderr(tmp_path, monkeypatch, capsys):
    for k in list(__import__("os").environ):
        if k.startswith("ATLAS_"):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("ATLAS_WORKSPACE_ROOT", str(tmp_path / "ws-never-inited"))
    from atlas_plus import setup_cmd
    rc = setup_cmd.main(["--client", "claude-code"])
    out = capsys.readouterr()
    assert rc == 0
    assert "does not exist yet" in out.err and "sextant init" in out.err
    assert "claude mcp add" in out.out


def test_setup_after_init_stays_quiet(tmp_path, monkeypatch, capsys):
    for k in list(__import__("os").environ):
        if k.startswith("ATLAS_"):
            monkeypatch.delenv(k, raising=False)
    (tmp_path / "ws" / ".memory").mkdir(parents=True)
    monkeypatch.setenv("ATLAS_WORKSPACE_ROOT", str(tmp_path / "ws"))
    from atlas_plus import setup_cmd
    setup_cmd.main(["--client", "claude-code"])
    assert "does not exist yet" not in capsys.readouterr().err


def test_verify_announces_the_one_time_index_rebuild(tmp_path, monkeypatch, capsys):
    """The documented post-update path runs `sextant verify` before any search; the rebuild it triggers must be said."""
    import os
    import subprocess
    for k in list(os.environ):
        if k.startswith("ATLAS_"):
            monkeypatch.delenv(k, raising=False)
    ws = tmp_path / "ws"
    monkeypatch.setenv("ATLAS_WORKSPACE_ROOT", str(ws))
    monkeypatch.setenv("ATLAS_SEMANTIC_SEARCH", "off")
    for k in ("ATLAS_BRAIN_URL", "ATLAS_OLLAMA_URL", "ATLAS_EMBED_URL", "OLLAMA_HOST", "OLLAMA_URL"):
        monkeypatch.setenv(k, "http://127.0.0.1:9")
    subprocess.run([sys.executable, "-m", "atlas_plus.init_cmd"], check=True, capture_output=True, env=os.environ.copy())
    notes = ws / ".memory" / "reference" / "bulk"
    notes.mkdir(parents=True)
    for i in range(230):
        (notes / f"n{i:03d}.md").write_text(f"---\ntitle: n{i}\n---\nbulk note {i} about rebuilding\n", encoding="utf-8")
    from atlas_plus import roundtrip
    rc = roundtrip.main([])
    out = capsys.readouterr()
    assert rc == 0, out.out[-800:]
    assert "Sextant: building the search index" in out.err and "search index built" in out.err

