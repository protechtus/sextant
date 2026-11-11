"""ATL-026 step 3 tests -- capture-payload parsing (Telegram drop -> POST /inbox body)."""

from __future__ import annotations

from atlas_plus.inbox.capture import build_capture_payload, is_bare_url


class TestIsBareUrl:
    def test_true(self):
        assert is_bare_url("https://example.com/a")
        assert is_bare_url("  http://x.io  ")

    def test_false(self):
        assert not is_bare_url("read this https://x.io")
        assert not is_bare_url("just a note")
        assert not is_bare_url("")


class TestBuildPayload:
    def test_bare_url(self):
        p = build_capture_payload("https://example.com/a")
        assert p == {"source": "telegram", "research": False, "url": "https://example.com/a"}

    def test_text_only(self):
        p = build_capture_payload("look into SIP ALG issues")
        assert p["text"] == "look into SIP ALG issues"
        assert "url" not in p
        assert p["research"] is False

    def test_text_with_url(self):
        p = build_capture_payload("great write-up https://example.com/x on RTP")
        assert p["text"].startswith("great write-up")
        assert p["url"] == "https://example.com/x"

    def test_research_prefix_bare_url(self):
        p = build_capture_payload("research https://example.com/deep")
        assert p["research"] is True
        assert p["url"] == "https://example.com/deep"

    def test_research_prefix_text(self):
        p = build_capture_payload("deep compare FAISS vs Chroma")
        assert p["research"] is True
        assert p["text"] == "compare FAISS vs Chroma"

    def test_dig_prefix(self):
        assert build_capture_payload("dig https://x.io")["research"] is True

    def test_empty(self):
        assert build_capture_payload("") == {}
        assert build_capture_payload("   ") == {}

    def test_research_keyword_only(self):
        # 'research' with nothing after it has nothing to capture
        assert build_capture_payload("research") == {}

    def test_source_override(self):
        p = build_capture_payload("https://x.io", source="web")
        assert p["source"] == "web"
