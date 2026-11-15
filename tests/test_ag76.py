"""AG76 (sweep 21): `sextant setup` per-client configs on odd workspace paths and `--out` merges.

Each test runs the real `main()` and checks the file or the stdout a person gets.
"""
from __future__ import annotations

import json
import os
import tomllib

import pytest

from atlas_plus import setup_cmd as sc

QUOTE_WS = "Ann" + "'" + "s Wörk é"


def _run(monkeypatch, tmp_path, ws_name, argv, capsys):
    for k in list(os.environ):
        if k.startswith("ATLAS_"):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("ATLAS_WORKSPACE_ROOT", str(tmp_path / ws_name))
    rc = sc.main(argv)
    out = capsys.readouterr()
    return rc, out.out, out.err


def test_codex_out_keeps_the_existing_config(monkeypatch, tmp_path, capsys):
    cfg = tmp_path / "config.toml"
    cfg.write_text('# mine\nmodel = "o3"\n\n[mcp_servers.other]\ncommand = "node"\nargs = ["x.js"]\n',
                   encoding="utf-8")
    rc, _, err = _run(monkeypatch, tmp_path, "My Wörk", ["--client", "codex", "--out", str(cfg)], capsys)
    assert rc == 0, err
    text = cfg.read_text(encoding="utf-8")
    assert text.startswith("# mine\n")
    doc = tomllib.loads(text)
    assert doc["model"] == "o3" and set(doc["mcp_servers"]) == {"other", "sextant"}
    assert doc["mcp_servers"]["sextant"]["env"]["ATLAS_WORKSPACE_ROOT"].endswith("My Wörk")
    assert list(tmp_path.glob("config.toml.bak-*"))
    # a second run is a no-op, a different registration under the same name is refused
    rc, _, err = _run(monkeypatch, tmp_path, "My Wörk", ["--client", "codex", "--out", str(cfg)], capsys)
    assert rc == 0 and "nothing written" in err
    rc, _, err = _run(monkeypatch, tmp_path, "other ws", ["--client", "codex", "--out", str(cfg)], capsys)
    assert rc == 1 and "differently" in err
    assert tomllib.loads(cfg.read_text(encoding="utf-8"))["mcp_servers"]["sextant"]["env"][
        "ATLAS_WORKSPACE_ROOT"].endswith("My Wörk")


@pytest.mark.parametrize("client,key,other", [
    ("hooks", "permissions", {"allow": ["Bash(ls:*)"]}),
    ("opencode", "theme", "dark"),
    ("json", "globalShortcut", "Ctrl+Space"),
])
def test_json_flag_out_merges_like_the_plain_form(monkeypatch, tmp_path, capsys, client, key, other):
    f = tmp_path / "c.json"
    f.write_text(json.dumps({key: other}), encoding="utf-8")
    rc, _, err = _run(monkeypatch, tmp_path, "ws", ["--client", client, "--json", "--out", str(f)], capsys)
    assert rc == 0, err
    assert json.loads(f.read_text(encoding="utf-8"))[key] == other
    assert list(tmp_path.glob("c.json.bak-*"))


def test_text_forms_never_replace_an_existing_file(monkeypatch, tmp_path, capsys):
    f = tmp_path / "settings.json"
    f.write_text('{"permissions": {}}', encoding="utf-8")
    rc, _, err = _run(monkeypatch, tmp_path, "ws", ["--client", "claude-code", "--out", str(f)], capsys)
    assert rc == 1 and "nothing was written" in err
    assert f.read_text(encoding="utf-8") == '{"permissions": {}}'


def test_dotted_name_is_refused(monkeypatch, tmp_path, capsys):
    rc, out, err = _run(monkeypatch, tmp_path, "ws", ["--client", "codex", "--name", "sextant.work"], capsys)
    assert rc == 2 and "must be letters" in err and out == ""


@pytest.mark.skipif(os.name != "nt", reason="the quote refusal is the Windows command-line rule")
def test_hooks_with_a_quote_in_the_path_stay_json_and_exit_zero(monkeypatch, tmp_path, capsys):
    rc, out, err = _run(monkeypatch, tmp_path, QUOTE_WS, ["--client", "hooks"], capsys)
    assert rc == 0
    cmd = json.loads(out)["hooks"]["SessionStart"][0]["hooks"][0]["command"]
    assert "Ann'\"'\"'s" in cmd and "CANNOT PRINT" not in out
    rc, out, _ = _run(monkeypatch, tmp_path, QUOTE_WS, ["--client", "codex"], capsys)
    assert "merge the TOML table below" in out and "[mcp_servers.sextant]" in out


def test_wsl_windows_side_refusal_does_not_fail_the_linux_forms(monkeypatch):
    env = {"ATLAS_WORKSPACE_ROOT": "/home/u/" + QUOTE_WS, "WSL_DISTRO_NAME": "Ubuntu"}
    orig_q = sc._q   # the Linux side quotes for POSIX; the Windows-side line keeps posix=False
    monkeypatch.setattr(sc, "_q", lambda v, posix=None: orig_q(v, True if posix is None else posix))
    info = sc.describe(environ=env)
    assert info["claude_code"] and info["claude_code_from_windows"] is None
    assert sc.piece_errors(info, "claude-code") and not sc.piece_errors(info, "hooks")
    assert not sc.piece_errors(info, "codex")
    assert not sc.render(info, "hooks").startswith("CANNOT PRINT")


def test_utf16_target_gets_a_sentence(monkeypatch, tmp_path, capsys):
    f = tmp_path / "claude_desktop_config.json"
    f.write_bytes('{"mcpServers": {}}'.encode("utf-16"))
    rc, _, err = _run(monkeypatch, tmp_path, "ws", ["--client", "json", "--out", str(f)], capsys)
    assert rc == 1 and "UTF-16" in err and "codec" not in err
