"""AG98 (sweep 25, fix wave): hardening follow-ups from the paranoid lane (MN86).

1. Dependabot config for the SHA-pinned actions and the pip dependencies.
2. Corpus, state and sync-state roots are created 0700 on POSIX; doctor warns about older 0755 roots.
3. The sync hub's unauthenticated health routes apply the Host check the MCP server uses.
4. Tailwind is served by the dashboard from the package with SRI; the CSP no longer names its CDN.
5. A link (symlink, junction) out of the corpus is left out of every listing, not only reading/search.
6. The research link checker applies the public-address rule to every hop.
"""

from __future__ import annotations

import base64
import hashlib
import http.server
import os
import stat
import subprocess
import threading
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
posix_only = pytest.mark.skipif(os.name == "nt", reason="mode bits are a POSIX concept; Windows uses ACLs")


# --------------------------------------------------------------------------- 1. dependabot

def test_dependabot_covers_actions_and_pip_weekly_grouped():
    import yaml
    cfg = yaml.safe_load((REPO / ".github" / "dependabot.yml").read_text(encoding="utf-8"))
    assert cfg["version"] == 2
    by_eco = {u["package-ecosystem"]: u for u in cfg["updates"]}
    assert set(by_eco) >= {"github-actions", "pip"}
    for eco in ("github-actions", "pip"):
        u = by_eco[eco]
        assert u["directory"] == "/"
        assert u["schedule"]["interval"] == "weekly"
        assert u.get("groups"), f"{eco} updates are not grouped"
    # every workflow action is SHA-pinned, the style Dependabot keeps up to date
    for wf in (REPO / ".github" / "workflows").glob("*.yml"):
        for line in wf.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith(("- uses:", "uses:")):
                ref = line.split("@", 1)[1].split()[0]
                assert len(ref) == 40 and all(c in "0123456789abcdef" for c in ref), (wf.name, line)


# --------------------------------------------------------------------------- 2. modes

def _fresh_ws(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    for var in list(os.environ):
        if var.startswith("ATLAS_"):
            monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("ATLAS_WORKSPACE_ROOT", str(ws))
    return ws


def _mode(p: Path) -> int:
    return stat.S_IMODE(os.stat(p).st_mode)


def modes_ignored(path, mounts_text=None, windows=os.name == "nt") -> bool:
    """True when `path` sits on a mount where chmod changes nothing (a Windows drive under WSL
    without the metadata option, FAT, SMB): the mode tests below then cannot pass and skip with
    the reason instead of failing (SEX-048, AG107). `_fsutil.mode_fix` is the one judge."""
    if windows:
        return False
    from atlas_plus import _fsutil
    return not _fsutil.mode_fix(str(path), mounts_text).startswith("chmod ")


@pytest.fixture
def honours_modes(tmp_path):
    if modes_ignored(tmp_path):
        pytest.skip(f"the temp folder {tmp_path} is on a mount that ignores chmod; mode bits cannot be tested there")


@posix_only
def test_init_creates_memory_and_state_roots_0700(tmp_path, monkeypatch, honours_modes):
    ws = _fresh_ws(tmp_path, monkeypatch)
    old = os.umask(0o022)
    try:
        from atlas_plus.init_cmd import run_init
        run_init()
    finally:
        os.umask(old)
    assert _mode(ws / ".memory") == 0o700
    assert _mode(ws / ".atlas") == 0o700
    assert (ws / ".memory" / "identity" / "user.md").is_file()


@posix_only
def test_init_leaves_an_existing_root_alone_and_doctor_names_the_fix(tmp_path, monkeypatch, honours_modes):
    ws = _fresh_ws(tmp_path, monkeypatch)
    (ws / ".memory").mkdir(mode=0o755)
    (ws / ".atlas").mkdir(mode=0o755)
    os.chmod(ws / ".memory", 0o755)
    os.chmod(ws / ".atlas", 0o755)
    from atlas_plus.init_cmd import run_init
    run_init()
    assert _mode(ws / ".memory") == 0o755  # the owner's existing choice is not changed silently
    from atlas_plus.doctor import cli as doctor
    monkeypatch.setattr(doctor, "MEMORY_ROOT", ws / ".memory")
    rows = doctor.check_permissions()["loose"]
    assert {r["path"] for r in rows} == {str(ws / ".memory"), str(ws / ".atlas")}
    assert all(r["mode"] == "755" and r["fix"].startswith("chmod 700 ") for r in rows)
    text = doctor.render_text({"permissions": {"loose": rows}})
    assert "Permissions" in text and f"chmod 700 {ws / '.memory'}" in text
    os.chmod(ws / ".memory", 0o700)
    os.chmod(ws / ".atlas", 0o700)
    assert doctor.check_permissions()["loose"] == []


@posix_only
def test_sync_registry_dir_is_private_even_before_init(tmp_path, monkeypatch, honours_modes):
    ws = _fresh_ws(tmp_path, monkeypatch)
    old = os.umask(0o022)
    try:
        from atlas_plus.sync import devices, store
        devices._save_raw([])
        store.audit("test_event")
    finally:
        os.umask(old)
    assert _mode(ws / ".atlas") == 0o700
    assert _mode(ws / ".atlas" / "sync") == 0o700


def test_doctor_permissions_check_is_empty_on_windows_or_private_roots(tmp_path, monkeypatch, honours_modes):
    from atlas_plus import _fsutil
    d = tmp_path / "private"
    _fsutil.make_private_dir(d)
    assert d.is_dir()
    assert _fsutil.loose_dir_modes([d, tmp_path / "missing"]) == []


# --------------------------------------------------------------------------- 3. hub Host check

def _hub(monkeypatch, tmp_path, bind_host=None, allowed=""):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    _fresh_ws(tmp_path, monkeypatch)
    if allowed:
        monkeypatch.setenv("ATLAS_SYNC_ALLOWED_HOSTS", allowed)
    from atlas_plus.sync.api import create_app
    return TestClient(create_app(bind_host=bind_host), base_url="http://127.0.0.1:9474")


@pytest.mark.parametrize("path", ["/health", "/sync/v1/health"])
def test_hub_health_refuses_a_foreign_host(monkeypatch, tmp_path, path):
    c = _hub(monkeypatch, tmp_path)
    r = c.get(path, headers={"Host": "rebind.example-org.test"})
    assert r.status_code == 421
    assert "ATLAS_SYNC_ALLOWED_HOSTS" in r.json()["detail"]
    for host in ("127.0.0.1:9474", "localhost", "[::1]:9474"):
        assert c.get(path, headers={"Host": host}).json()["role"] == "hub", host


def test_hub_health_allows_the_configured_public_name(monkeypatch, tmp_path):
    c = _hub(monkeypatch, tmp_path, allowed="Sync.Example-Org.test, lan.example-org.test:8768")
    assert c.get("/sync/v1/health", headers={"Host": "sync.example-org.test"}).status_code == 200
    assert c.get("/sync/v1/health", headers={"Host": "sync.example-org.test:443"}).status_code == 200
    assert c.get("/sync/v1/health", headers={"Host": "lan.example-org.test:8768"}).status_code == 200
    assert c.get("/sync/v1/health", headers={"Host": "lan.example-org.test:9999"}).status_code == 421
    assert c.get("/sync/v1/health", headers={"Host": "other.example-org.test"}).status_code == 421


def test_hub_wide_bind_without_a_list_keeps_the_check_off_and_device_routes_unchanged(monkeypatch, tmp_path):
    c = _hub(monkeypatch, tmp_path, bind_host="0.0.0.0")
    assert c.get("/health", headers={"Host": "100.101.102.103:8768"}).status_code == 200
    c2 = _hub(monkeypatch, tmp_path)
    # the token guards the device routes; a proxy forwarding its public name still reaches them
    assert c2.get("/sync/v1/manifest", headers={"Host": "sync.example-org.test"}).status_code == 401


# --------------------------------------------------------------------------- 4. Tailwind

def _web(monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from atlas_plus.web import app as webapp
    monkeypatch.setattr(webapp, "_ALLOW_NO_AUTH", True, raising=False)
    monkeypatch.setattr(webapp, "API_KEY", "", raising=False)
    return webapp, TestClient(webapp.app)


def test_tailwind_is_served_locally_with_sri_and_the_csp_drops_its_cdn(monkeypatch):
    webapp, c = _web(monkeypatch)
    page = c.get("/")
    assert page.status_code == 200
    csp = page.headers["content-security-policy"]
    assert "cdn.tailwindcss.com" not in csp and "script-src 'self'" in csp
    assert "cdn.tailwindcss.com" not in page.text.split("</head>")[0]
    asset = c.get("/static/tailwind-3.4.17.js")
    assert asset.status_code == 200
    assert asset.headers["content-type"].startswith("text/javascript")
    sri = "sha384-" + base64.b64encode(hashlib.sha384(asset.content).digest()).decode()
    assert sri == "sha384-igm5BeiBt36UU4gqwWS7imYmelpTsZlQ45FZf+XBn9MuJbn4nQr7yx1yFydocC/K"
    assert f'<script src="/static/tailwind-3.4.17.js" integrity="{sri}"></script>' in page.text


def test_tailwind_asset_needs_no_session(monkeypatch):
    webapp, _ = _web(monkeypatch)
    from fastapi.testclient import TestClient
    monkeypatch.setattr(webapp, "_ALLOW_NO_AUTH", False, raising=False)
    monkeypatch.setattr(webapp, "API_KEY", "k" * 32, raising=False)
    c = TestClient(webapp.app)
    assert c.get("/api/memory/files").status_code == 401
    assert c.get("/static/tailwind-3.4.17.js").status_code == 200
    assert c.head("/static/tailwind-3.4.17.js").status_code == 200


def test_vendored_asset_is_never_line_ending_converted():
    """With `* text=auto` a Windows checkout (core.autocrlf=true) rewrote the file to CRLF, the bytes
    no longer matched the integrity hash, and the browser refused the script (found while vendoring)."""
    import shutil
    if not shutil.which("git") or not (REPO / ".git").exists():
        pytest.skip("not a git checkout")
    out = subprocess.run(["git", "-C", str(REPO), "check-attr", "text", "--", "src/atlas_plus/web/static/tailwind-3.4.17.js"],
                         capture_output=True, text=True, check=True).stdout
    assert out.strip().endswith("text: unset"), out


def test_tailwind_ships_in_the_package_data():
    import tomllib
    data = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    assert "static/*.js" in data["tool"]["setuptools"]["package-data"]["atlas_plus.web"]


# --------------------------------------------------------------------------- 5. links out of the corpus

def _link_dir(link: Path, target: Path) -> None:
    if os.name == "nt":
        subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], check=True, capture_output=True)
    else:
        os.symlink(target, link, target_is_directory=True)


@pytest.fixture
def linked_corpus(tmp_path):
    mem = tmp_path / "mem"
    (mem / "projects").mkdir(parents=True)
    (mem / "projects" / "ok.md").write_text("---\ntitle: inside\ntags: [kept]\n---\nhello\n", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.md").write_text("---\ntitle: OUTSIDE-TITLE\ntags: [leaked]\n---\nsecret\n", encoding="utf-8")
    _link_dir(mem / "projects" / "jdir", outside)
    file_link = False
    try:
        os.symlink(outside / "secret.md", mem / "projects" / "flink.md")
        file_link = True
    except OSError:
        pass  # Windows without Developer Mode: file symlinks need a privilege; the junction still runs
    return mem, file_link


def test_web_tree_leaves_out_links_out_of_the_corpus(linked_corpus, monkeypatch):
    mem, file_link = linked_corpus
    webapp, c = _web(monkeypatch)
    monkeypatch.setattr(webapp, "MEMORY_ROOT", mem)
    r = c.get("/api/memory/files")
    assert r.status_code == 200 and "ok.md" in r.text
    assert "jdir" not in r.text and "secret.md" not in r.text
    if file_link:
        assert "flink.md" not in r.text


def test_mcp_list_never_reads_or_names_a_file_out_of_the_corpus(linked_corpus, monkeypatch):
    mem, file_link = linked_corpus
    from atlas_plus.mcp.memory_server.tools import list as list_tool
    monkeypatch.setattr(list_tool, "MEMORY_ROOT", mem)
    list_tool._cache.clear()
    items = list_tool.memory_list(limit=50)
    assert [i["path"] for i in items] == ["projects/ok.md"]
    assert all(i["title"] != "OUTSIDE-TITLE" for i in items)
    list_tool._cache.clear()
    assert list_tool.memory_list(prefix="projects", limit=50) == items


def test_topic_index_skips_files_out_of_the_corpus(linked_corpus):
    mem, _ = linked_corpus
    from atlas_plus.web.app import _build_topic_index
    idx = _build_topic_index(mem)
    assert "kept" in idx and "leaked" not in idx


# --------------------------------------------------------------------------- 6. link checker

class _Recorder(http.server.BaseHTTPRequestHandler):
    hits: list = []

    def do_HEAD(self):
        type(self).hits.append(self.path)
        self.send_response(200)
        self.end_headers()

    do_GET = do_HEAD

    def log_message(self, *a):
        pass


def _serve(handler, port):
    srv = http.server.HTTPServer(("127.0.0.1", port), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def test_link_check_never_probes_a_loopback_target():
    from atlas_plus.curation.link_check import check_url
    _Recorder.hits = []
    srv = _serve(_Recorder, 9477)
    try:
        res = check_url("http://127.0.0.1:9477/admin")
    finally:
        srv.shutdown()
    assert _Recorder.hits == []
    assert res["skipped"] and "non-public" in res["error"]


def test_link_check_checks_every_redirect_hop(monkeypatch):
    """A public page that redirects to a metadata address: the second hop is never requested."""
    httpx = pytest.importorskip("httpx")
    import socket

    real_gai = socket.getaddrinfo

    def fake_gai(host, port, *a, **k):
        table = {"public.example-org.test": "93.184.216.34", "meta.example-org.test": "169.254.169.254"}
        if host in table:
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (table[host], port))]
        return real_gai(host, port, *a, **k)

    monkeypatch.setattr(socket, "getaddrinfo", fake_gai)
    seen = []

    def handler(request):
        seen.append(str(request.url))
        if request.url.host == "public.example-org.test":
            return httpx.Response(302, headers={"Location": "http://meta.example-org.test/latest/meta-data/"})
        return httpx.Response(200)

    real_client = httpx.Client

    def client(*a, **k):
        k["transport"] = httpx.MockTransport(handler)
        return real_client(*a, **k)

    monkeypatch.setattr(httpx, "Client", client)
    from atlas_plus.curation.link_check import check_url
    res = check_url("https://public.example-org.test/paper")
    assert seen == ["https://public.example-org.test/paper"]
    assert res["skipped"] and "meta.example-org.test" in res["error"]
    assert res["ok"]  # never counted broken, so --remove never edits the report over it


def test_link_check_still_follows_public_redirects(monkeypatch):
    httpx = pytest.importorskip("httpx")
    import socket

    monkeypatch.setattr(socket, "getaddrinfo",
                        lambda host, port, *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))])

    def handler(request):
        if request.url.path == "/old":
            return httpx.Response(301, headers={"Location": "/new"})
        return httpx.Response(404 if request.url.path == "/gone" else 200)

    real_client = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda *a, **k: real_client(*a, **{**k, "transport": httpx.MockTransport(handler)}))
    from atlas_plus.curation.link_check import check_url
    assert check_url("https://docs.example-org.test/old")["status"] == 200
    assert check_url("https://docs.example-org.test/gone")["ok"] is False


def test_inbox_fetch_message_is_unchanged():
    from atlas_plus.inbox.summarize import _refuse_private_target
    with pytest.raises(ValueError, match=r"inbox fetch refused: .*ATLAS_INBOX_FETCH_PRIVATE=on"):
        _refuse_private_target("http://127.0.0.1:9478/")
