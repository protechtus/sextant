"""Tests for the Sextant Clip inbox endpoint (POST /api/inbox/conversation)."""
from __future__ import annotations

import json
import os

import pytest

# Same pre-import guards as test_web_app: app.py freezes these at first
# import, and THIS file imports it first alphabetically -- without the
# non-routable BRAIN_URL, the proxy tests would hit a REAL local Brain.
os.environ.setdefault("ATLAS_WEB_API_KEY", "test-key-abc")
os.environ.setdefault("ATLAS_BRAIN_URL", "http://127.0.0.1:9999")
os.environ.setdefault("ATLAS_BRAIN_API_KEY", "brain-key")
os.environ.setdefault("ATLAS_MCP_TOKEN", "mcp-token")

from fastapi.testclient import TestClient  # noqa: E402

from atlas_plus.web.app import app  # noqa: E402

client = TestClient(app, raise_server_exceptions=False)
AUTH = {"Authorization": "Bearer test-key-abc"}


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setenv("ATLAS_TRANSCRIPT_ROOT", str(tmp_path / "transcripts"))
    monkeypatch.setenv("ATLAS_TRANSCRIPT_INDEX", str(tmp_path / "index.sqlite"))
    drop = tmp_path / "drop"
    monkeypatch.setenv("ATLAS_TRANSCRIPT_DROP", str(drop))
    return drop


def _payload(**over):
    base = {
        "title": "Great Ideas Thread",
        "source": "claude-web",
        "messages": [
            {"role": "user", "content": "what about a browser extension?"},
            {"role": "assistant", "content": "build it tonight"},
        ],
    }
    base.update(over)
    return base


class TestInbox:
    def test_happy_path_ingests_and_archives(self, world):
        r = client.post("/api/inbox/conversation", json=_payload(), headers=AUTH)
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["ok"] is True and data["turns"] == 2
        assert data["conversation_id"].startswith("claude-web-great-ideas-thread-")
        # Dropped file was ingested and archived, not left behind or deleted.
        assert not list(p for p in world.iterdir() if p.is_file())
        assert len(list((world / "_ingested").iterdir())) == 1

    def test_requires_auth(self, world):
        r = client.post("/api/inbox/conversation", json=_payload())
        assert r.status_code == 401

    def test_cookie_never_authenticates_post(self, world):
        client.cookies.set("sextant_ui", "test-key-abc")
        try:
            r = client.post("/api/inbox/conversation", json=_payload())
            assert r.status_code == 401  # header-only by design (CSRF)
        finally:
            client.cookies.delete("sextant_ui")

    def test_bad_json_400(self, world):
        r = client.post("/api/inbox/conversation", content=b"{nope",
                        headers={**AUTH, "Content-Type": "application/json"})
        assert r.status_code == 400

    def test_empty_messages_400(self, world):
        r = client.post("/api/inbox/conversation", json=_payload(messages=[]), headers=AUTH)
        assert r.status_code == 400

    def test_oversize_body_413(self, world):
        big = _payload(messages=[{"role": "user", "content": "x" * 2_100_000}])
        r = client.post("/api/inbox/conversation", json=big, headers=AUTH)
        assert r.status_code == 413

    def test_all_invalid_roles_422(self, world):
        r = client.post("/api/inbox/conversation",
                        json=_payload(messages=[{"role": "narrator", "content": "x"}]),
                        headers=AUTH)
        assert r.status_code == 422

    def test_hostile_title_slugged(self, world):
        r = client.post("/api/inbox/conversation",
                        json=_payload(title="../../etc <passwd>?"), headers=AUTH)
        assert r.status_code == 200
        cid = r.json()["conversation_id"]
        assert "/" not in cid and "\\" not in cid and ".." not in cid

    def test_capture_is_searchable(self, world):
        client.post("/api/inbox/conversation",
                    json=_payload(messages=[{"role": "user", "content": "the quokka gambit"}]),
                    headers=AUTH)
        from typer.testing import CliRunner
        from atlas_plus.transcripts.cli import app as tapp
        result = CliRunner().invoke(tapp, ["search", "quokka", "--json"])
        assert result.exit_code == 0 and "claude-web" in result.output


class TestExtensionArtifacts:
    """The shipped extension directory stays coherent."""

    def test_manifest_is_valid_mv3(self):
        from pathlib import Path
        root = Path(__file__).resolve().parents[1] / "extensions" / "sextant-clip"
        if not root.exists():
            pytest.skip("extension not in this tree")
        manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
        assert manifest["manifest_version"] == 3
        assert "scripting" in manifest["permissions"]
        # Loopback only: the extension must not request broad web host access.
        for pattern in manifest["host_permissions"]:
            assert "127.0.0.1" in pattern or "localhost" in pattern
        for f in ("background.js", "scraper.js", "options.html", "options.js", "README.md"):
            assert (root / f).exists(), f


class TestConcurrentCaptures:
    """Deep-pass probe (2026-09-27): four same-slug posts in the same second
    previously collided on one filename -- 3 of 4 returned 500."""

    def test_concurrent_same_slug_posts_all_land(self, world):
        import threading
        results = []

        def post(i):
            r = client.post("/api/inbox/conversation", json=_payload(
                messages=[{"role": "user", "content": f"concurrent-{i}"}]), headers=AUTH)
            results.append(r.status_code)

        threads = [threading.Thread(target=post, args=(i,)) for i in range(4)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        assert results == [200, 200, 200, 200], results
        from atlas_plus.transcripts.index import TranscriptIndex
        idx = TranscriptIndex()
        for i in range(4):
            assert idx.search(f"concurrent-{i}"), f"content {i} not indexed"
