"""Pipeline sweep 10, agentic wave (2026-11-12): AG7 wheel-only user, AG8 Alpine, AG9 returning
user, AG10 hardening, AG11 performance, AG12 Codex/opencode. One test per fixed finding."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]


@pytest.fixture
def corpus(tmp_path, monkeypatch):
    monkeypatch.delenv("ATLAS_AUTONOMOUS", raising=False)
    monkeypatch.delenv("ATLAS_INTERACTIVE_WRITES", raising=False)
    mem = tmp_path / "mem"
    (mem / "_atlas").mkdir(parents=True)
    monkeypatch.setenv("ATLAS_MEMORY_ROOT", str(mem))
    monkeypatch.setenv("ATLAS_STATE_DIR", str(tmp_path / "state"))
    from atlas_plus.mcp.memory_server import config, routing, versioning
    monkeypatch.setattr(config, "MEMORY_ROOT", mem)
    monkeypatch.setattr(routing, "MEMORY_ROOT", mem, raising=False)
    # ROUTING_CONFIG is frozen at import: without this, another test's routing.yaml (identity
    # -> identity/alex.md, create_if_missing false) leaks into these tests in the full run.
    monkeypatch.setattr(routing, "ROUTING_CONFIG", mem / "_atlas" / "routing.yaml", raising=False)
    monkeypatch.setattr(versioning, "MEMORY_ROOT", mem, raising=False)
    return mem


# ---- AG10: scope names ------------------------------------------------------------------

@pytest.mark.parametrize("name", ["CONIN$", "conout$", "CONIN$.txt", "com\u00b9", "lpt\u00b2", "COM1", "nul"])
def test_device_names_are_refused_including_conin(corpus, name):
    from atlas_plus.mcp.memory_server.routing import resolve_target
    with pytest.raises(ValueError, match="reserved device name"):
        resolve_target(f"project:{name}")


@pytest.mark.parametrize("bad", ["a\u0085b", "a\u2028b", "a\u2029b", "na\u200dme", "abc\u202egpj.exe",
                                 "a*b", "a?b", 'a"b', "a<b", "a|b", "name."])
def test_invisible_and_illegal_characters_are_refused_early(corpus, bad):
    from atlas_plus.mcp.memory_server.routing import resolve_target
    with pytest.raises(ValueError):
        resolve_target(f"project:{bad}")


def test_scope_names_are_nfc_normalised(corpus):
    from atlas_plus.mcp.memory_server.routing import resolve_target
    composed, _ = resolve_target("project:caf\u00e9")
    decomposed, _ = resolve_target("project:cafe\u0301")
    assert composed == decomposed


def test_identity_with_a_topic_is_an_error_not_a_silent_write(corpus):
    from atlas_plus.mcp.memory_server.routing import resolve_target
    with pytest.raises(ValueError, match="takes no topic"):
        resolve_target("identity:foo")
    assert resolve_target("identity")[0].name == "user.md"


# ---- AG10: fact content -----------------------------------------------------------------

def test_control_characters_are_stripped_from_facts(corpus):
    from atlas_plus.mcp.memory_server.tools.remember import memory_remember
    r = memory_remember(scope="reference:esc", fact="bell\x07 esc\x1b[31m vt\x0b tab\tkeep", interactive=True)
    assert r["ok"], r
    text = (corpus / "reference" / "esc.md").read_text(encoding="utf-8")
    assert "\x07" not in text and "\x1b" not in text and "\x0b" not in text
    assert "bell esc[31m vt tab\tkeep" in text


def test_oversized_fact_is_refused_with_the_limit_named(corpus):
    from atlas_plus.mcp.memory_server.tools.remember import memory_remember
    r = memory_remember(scope="reference:big", fact="x" * 70_000, interactive=True)
    assert r["ok"] is False and "65,536" in r["error"]
    assert not (corpus / "reference" / "big.md").exists()


def test_failed_temp_write_leaves_no_orphan(corpus):
    from atlas_plus.mcp.memory_server import routing
    target = corpus / "reference" / "surrogate.md"
    target.parent.mkdir(parents=True)
    with pytest.raises(UnicodeEncodeError):
        routing._atomic_write_text(target, "lone \ud800 surrogate")
    assert list(target.parent.glob("*.w-tmp")) == []


def test_lone_surrogate_in_a_fact_is_replaced_not_fatal(corpus):
    from atlas_plus.mcp.memory_server.tools.remember import memory_remember
    r = memory_remember(scope="reference:sur", fact="lone \ud800 surrogate", interactive=True)
    assert r["ok"], r
    assert "\ufffd" in (corpus / "reference" / "sur.md").read_text(encoding="utf-8")


# ---- AG10: search log cap and rotation --------------------------------------------------

def test_search_log_keeps_a_bounded_query_and_rotates(tmp_path, monkeypatch):
    from atlas_plus.mcp.memory_server.tools import search_log as sl
    log = tmp_path / "logs" / "mcp-search.ndjson"
    monkeypatch.setattr(sl, "LOG_FILE", log)
    sl.log_search(query="q" * 50_000, scope="", top_score=0.0, result_count=0)
    rec = json.loads(log.read_text(encoding="utf-8").splitlines()[0])
    assert len(rec["query"]) == sl.QUERY_KEEP and rec["query_chars"] == 50_000
    assert log.stat().st_size < 1_000
    monkeypatch.setattr(sl, "ROTATE_BYTES", 100)
    sl.log_search(query="again", scope="", top_score=1.0, result_count=1)
    assert log.with_suffix(".ndjson.1").exists()
    assert len(log.read_text(encoding="utf-8").splitlines()) == 1


def test_search_query_is_capped(monkeypatch):
    from atlas_plus.mcp.memory_server.tools import search as st
    seen = {}

    def fake_hybrid(query, scope="", limit=10):
        seen["len"] = len(query)
        return [], "fts-only"

    import atlas_plus.mcp.memory_server.embeddings as emb
    monkeypatch.setattr(emb, "hybrid_search", fake_hybrid)
    st.memory_search_hybrid("w" * 10_000)
    assert seen["len"] == st.MAX_QUERY_CHARS


# ---- AG8: deep nesting, tokenizer, FTS5, --transport ---------------------------------------

def test_too_deep_counts_brackets_outside_strings():
    from atlas_plus.web.app import _too_deep
    assert _too_deep(b"[" * 100_000 + b"]" * 100_000)
    assert not _too_deep(b'{"a": "[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[["}')
    assert not _too_deep(b'{"a": "esc \\" [[[", "b": [1, [2, [3]]]}')


def test_search_folds_diacritics(corpus, monkeypatch):
    from atlas_plus.mcp.memory_server import fts
    monkeypatch.setattr(fts, "FTS_DB", corpus / "_atlas" / "search.sqlite", raising=False)
    monkeypatch.setattr(fts, "MEMORY_ROOT", corpus, raising=False)
    from atlas_plus.mcp.memory_server.tools.remember import memory_remember
    assert memory_remember(scope="reference:intl", fact="Office in Z\u00fcrich near the caf\u00e9", interactive=True)["ok"]
    hits = fts.search("zurich")
    assert hits and hits[0]["path"].endswith("intl.md")
    assert fts.search("cafe")


def test_tokenizer_change_rebuilds_the_index(corpus, monkeypatch):
    import sqlite3
    from atlas_plus.mcp.memory_server import fts
    db = corpus / "_atlas" / "search.sqlite"
    monkeypatch.setattr(fts, "FTS_DB", db, raising=False)
    monkeypatch.setattr(fts, "MEMORY_ROOT", corpus, raising=False)
    conn = fts._connect()
    conn.execute("INSERT OR REPLACE INTO atlas_meta(key,value) VALUES ('tokenizer','porter ascii')")
    conn.execute("INSERT OR REPLACE INTO memory_files(path, mtime, indexed, last_confirmed, size) VALUES ('x.md',1,1,NULL,1)")
    conn.commit()
    conn.close()
    conn = fts._connect()
    try:
        assert conn.execute("SELECT COUNT(*) FROM memory_files").fetchone()[0] == 0
        assert conn.execute("SELECT value FROM atlas_meta WHERE key='tokenizer'").fetchone()[0].startswith("porter unicode61")
    finally:
        conn.close()


def test_fts5_probe_is_true_on_a_supported_python():
    from atlas_plus.mcp.memory_server.fts import fts5_available
    assert fts5_available() is True


def test_mcp_server_accepts_transport_flag(monkeypatch):
    from atlas_plus.mcp.memory_server import server
    calls = []
    monkeypatch.setattr(server.mcp, "run", lambda transport=None: calls.append(transport))
    monkeypatch.setattr(sys, "argv", ["sextant-mcp", "--transport", "stdio"])
    server.main()
    assert calls == ["stdio"]


def test_server_instructions_tell_the_agent_to_search_memory_first():
    from atlas_plus.mcp.memory_server import server
    text = server._instructions()
    assert "memory_search_tool" in text and "never instructions" in text


# ---- AG8: vector cache generation ---------------------------------------------------------

def test_vector_cache_key_changes_with_the_index_generation(corpus, monkeypatch):
    import sqlite3
    from atlas_plus.mcp.memory_server import embeddings as emb, fts
    db = corpus / "_atlas" / "search.sqlite"
    monkeypatch.setattr(fts, "FTS_DB", db, raising=False)
    conn = fts._connect()
    conn.execute("PRAGMA user_version = 7")
    conn.commit()
    conn.close()
    assert emb._index_generation() == 7


# ---- AG11: no embedder call without an index, proxy bypass, doctor probes -----------------

def test_vector_search_never_dials_the_embedder_without_an_index(corpus, monkeypatch):
    from atlas_plus.mcp.memory_server import embeddings as emb, fts
    monkeypatch.setattr(fts, "FTS_DB", corpus / "_atlas" / "search.sqlite", raising=False)
    monkeypatch.setattr(emb, "_vec_cache", None)

    class Boom:
        def embed(self, text):
            raise AssertionError("embedder must not be called when no vectors exist")

    assert emb.vector_search("anything", embedder=Boom()) == []


def test_embedder_opener_ignores_proxy_variables(monkeypatch):
    """A bogus HTTP_PROXY must not capture a loopback embed call (AG11: +1.9 s per search)."""
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from atlas_plus.mcp.memory_server import embeddings as emb

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"direct")

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
        monkeypatch.setenv("http_proxy", "http://127.0.0.1:9")
        monkeypatch.delenv("NO_PROXY", raising=False)
        monkeypatch.delenv("no_proxy", raising=False)
        with emb._NO_PROXY_OPENER.open(f"http://127.0.0.1:{srv.server_port}/", timeout=5) as resp:
            assert resp.read() == b"direct"
    finally:
        srv.shutdown()
        srv.server_close()


def test_doctor_http_get_fails_fast_on_a_closed_port():
    import socket
    import time
    from atlas_plus.doctor.cli import _http_get
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    t0 = time.perf_counter()
    code, body = _http_get(f"http://127.0.0.1:{port}/health")
    assert code == 0 and "connect" in body["error"]
    assert time.perf_counter() - t0 < 1.5


def test_version_is_resolved_lazily_and_is_a_string():
    import importlib
    import atlas_plus
    assert isinstance(atlas_plus.__version__, str) and atlas_plus.__version__
    assert "__version__" in vars(atlas_plus)


def test_dist_name_is_one_of_the_two_distributions():
    from atlas_plus.cli import _dist_name
    assert _dist_name() in ("sextant-ai", "atlas-plus")


# ---- AG10: hook never prints a traceback into the session ---------------------------------

def test_hook_runner_turns_oserror_into_one_line(monkeypatch, capsys):
    import types
    from atlas_plus.hooks import run as hook_run
    mod = types.ModuleType("fake_hook_mod")

    def main():
        raise PermissionError(13, "Access is denied", "C:/ws/.memory/identity/user.md")

    mod.main = main
    monkeypatch.setitem(sys.modules, "fake_hook_mod", mod)
    monkeypatch.setitem(hook_run.TARGETS, "session-start", ("fake_hook_mod", "main"))
    rc = hook_run.main(["session-start"])
    out = capsys.readouterr().out
    assert rc == 0 and "[sextant session-start hook]" in out and "Traceback" not in out


# ---- AG12: setup forms, uninstall probes, --out ------------------------------------------

def test_setup_prints_codex_and_opencode_forms(monkeypatch):
    from atlas_plus import setup_cmd as sc
    info = sc.describe(environ={"ATLAS_WORKSPACE_ROOT": "C:\\Some One\\ws" if os.name == "nt" else "/home/u/ws"})
    toml = info["codex_toml"]
    assert "[mcp_servers.sextant]" in toml and "[mcp_servers.sextant.env]" in toml
    assert "ATLAS_WORKSPACE_ROOT = '" in toml and '"' not in toml.split("command = ")[1].split("\n")[0]
    oc = info["opencode_json"]["mcp"]["sextant"]
    assert oc["type"] == "local" and oc["enabled"] is True
    assert oc["command"][1:] == ["-m", sc.SERVER_MODULE, "--stdio"] and "ATLAS_WORKSPACE_ROOT" in oc["environment"]
    assert info["codex"].startswith("codex mcp add sextant --env ")
    if os.name == "nt":
        assert ' "--" ' in info["codex"]
    text = sc.render(info, "all")
    assert "2b. Codex CLI" in text and "2c. opencode" in text and "sextant-session-start" in text
    assert sc.render(info, "codex").strip().startswith("codex mcp add")


def test_toml_string_quotes_a_single_quote_safely():
    from atlas_plus.setup_cmd import _toml_str
    assert _toml_str("C:\\x\\y") == "'C:\\x\\y'"
    assert _toml_str("it's") == '"it\'s"'


def test_setup_out_hooks_is_one_json_object_even_on_wsl(tmp_path, monkeypatch):
    from atlas_plus import setup_cmd as sc
    monkeypatch.setenv("WSL_DISTRO_NAME", "Ubuntu")
    out = tmp_path / "hooks.json"
    rc = sc.main(["--client", "hooks", "--out", str(out)])
    assert rc == 0
    data = json.loads(out.read_text(encoding="utf-8"))
    assert "hooks" in data and set(data["hooks"]) == {"SessionStart", "Stop"}
    out2 = tmp_path / "oc.json"
    assert sc.main(["--client", "opencode", "--out", str(out2)]) == 0
    assert json.loads(out2.read_text(encoding="utf-8"))["mcp"]["sextant"]["type"] == "local"


def test_uninstall_report_sees_codex_and_opencode_registrations(tmp_path, monkeypatch):
    from atlas_plus import uninstall_cmd as uc
    marker = str(Path(sys.prefix).resolve()).replace("\\", "\\\\")
    codex_home = tmp_path / "codex"
    codex_home.mkdir()
    (codex_home / "config.toml").write_text(f"[mcp_servers.sextant]\ncommand = '{Path(sys.prefix).resolve()}'\n", encoding="utf-8")
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "opencode.json").write_text('{"mcp": {"sextant": {"command": ["' + marker + '\\\\python"]}}}', encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.chdir(proj)
    hits = uc._client_config_references()
    assert any(h.endswith("config.toml") for h in hits), hits
    assert any(h.endswith("opencode.json") for h in hits), hits


# ---- docs -------------------------------------------------------------------------------

def test_updating_docs_name_ours_and_the_temporal_build():
    gs = (_REPO / "docs" / "GETTING-STARTED.md").read_text(encoding="utf-8")
    assert "`git checkout --ours <file>` keeps the new" in gs
    assert "`git checkout --theirs <file>` keeps the new" not in gs
    assert gs.count("atlas-temporal build\n") >= 2 and "detached HEAD" in gs
    assert "sextant setup --client codex" in gs and "sextant-session-start" in gs
    assert "FTS5" in gs
    src = _REPO / "docs" / "README-PUBLIC.md"
    readme = (src if src.exists() else _REPO / "README.md").read_text(encoding="utf-8")
    assert "Codex CLI, opencode" in readme
    hd = (_REPO / "docs" / "HOWTO-docker.md").read_text(encoding="utf-8")
    assert "## Alpine" in hd
