"""Tests for atlas_plus.ntfy.inbound -- the inbound ntfy command channel.

Focus is the security-critical path: constant-time auth (no bypass, fail closed)
and command routing. The streaming loop itself is I/O and is not unit-tested.
"""

from __future__ import annotations

from atlas_plus.ntfy import inbound


class FakeBrain:
    """Records calls and returns canned strings, like the real BrainClient."""

    def __init__(self):
        self.calls = []

    def submit_task(self, instruction, session_id="ntfy-control"):
        self.calls.append(("task", instruction, session_id))
        return "Queued task t-1 (pending)"

    def chat(self, message, session_id="ntfy-control"):
        self.calls.append(("chat", message, session_id))
        return f"reply: {message}"

    def health_summary(self):
        self.calls.append(("health",))
        return "Brain: status=ok"


# --- authenticate ----------------------------------------------------------

def test_authenticate_correct_secret_extracts_command():
    ok, cmd = inbound.authenticate("s3cret status", "s3cret")
    assert ok is True
    assert cmd == "status"


def test_authenticate_keeps_full_command_after_secret():
    ok, cmd = inbound.authenticate("s3cret task do the thing", "s3cret")
    assert ok is True
    assert cmd == "task do the thing"


def test_authenticate_wrong_secret_fails_closed():
    ok, cmd = inbound.authenticate("wrong status", "s3cret")
    assert ok is False
    assert cmd is None


def test_authenticate_empty_configured_secret_denies_everything():
    assert inbound.authenticate("anything at all", "") == (False, None)


def test_authenticate_secret_only_gives_empty_command():
    ok, cmd = inbound.authenticate("s3cret", "s3cret")
    assert ok is True
    assert cmd == ""


def test_authenticate_is_not_prefix_matchable():
    # A token that merely starts with the secret must not pass.
    ok, _ = inbound.authenticate("s3cretX status", "s3cret")
    assert ok is False


# --- route_command ---------------------------------------------------------

def test_route_status_calls_health():
    b = FakeBrain()
    out = inbound.route_command("status", b)
    assert ("health",) in b.calls
    assert "ok" in out


def test_route_task_prefix_submits_task():
    b = FakeBrain()
    inbound.route_command("task build the widget", b)
    assert b.calls[-1] == ("task", "build the widget", "ntfy-control")


def test_route_chat_prefix_chats():
    b = FakeBrain()
    inbound.route_command("chat hello there", b)
    assert b.calls[-1] == ("chat", "hello there", "ntfy-control")


def test_route_default_is_task_submission():
    b = FakeBrain()
    inbound.route_command("just do this thing", b)
    assert b.calls[-1] == ("task", "just do this thing", "ntfy-control")


def test_route_empty_command_returns_help_without_calling_brain():
    b = FakeBrain()
    out = inbound.route_command("", b)
    assert "status" in out.lower()
    assert b.calls == []


# --- handle_message --------------------------------------------------------

def test_handle_ignores_non_message_events():
    b = FakeBrain()
    published = []
    out = inbound.handle_message({"event": "open"}, secret="s", brain=b, publish=published.append)
    assert out is None
    assert published == []
    assert b.calls == []


def test_handle_auth_failure_drops_silently_no_oracle():
    b = FakeBrain()
    published = []
    out = inbound.handle_message(
        {"event": "message", "message": "wrong status", "id": "abc"},
        secret="s3cret", brain=b, publish=published.append,
    )
    assert out is None
    assert published == []   # no reply -> not a guessing oracle
    assert b.calls == []     # Brain never touched


def test_handle_authed_routes_and_publishes():
    b = FakeBrain()
    published = []
    out = inbound.handle_message(
        {"event": "message", "message": "s3cret status", "id": "abc"},
        secret="s3cret", brain=b, publish=published.append,
    )
    assert ("health",) in b.calls
    assert published and published[0] == out


class TestNonAsciiRobustness:
    """Fixed 2026-07-02: hmac.compare_digest(str, str) raises TypeError on non-ASCII, and
    the presented token comes off a world-publishable topic -- one emoji message escaped
    the per-line handler and tore down the stream (reconnect churn an outsider could
    induce). authenticate now compares bytes; _consume_line isolates per-message errors."""

    def test_emoji_first_token_fails_auth_without_raising(self):
        from atlas_plus.ntfy.inbound import authenticate
        ok, cmd = authenticate("\U0001F44D hello", "topsecret123")
        assert ok is False and cmd is None

    def test_non_ascii_secret_still_matches(self):
        from atlas_plus.ntfy.inbound import authenticate
        ok, cmd = authenticate("pässwörd status", "pässwörd")
        assert ok is True and cmd == "status"

    def test_non_ascii_command_after_valid_secret(self):
        from atlas_plus.ntfy.inbound import authenticate
        ok, cmd = authenticate("topsecret123 chat café plans \U0001F44D", "topsecret123")
        assert ok is True and "café" in cmd

    def test_consume_line_isolates_handler_exception(self):
        from unittest.mock import patch
        from atlas_plus.ntfy.inbound import NtfyInboundListener
        listener = NtfyInboundListener(cmd_topic="t", secret="s", resp_topic="")
        line = '{"event": "message", "id": "m1", "message": "s boom"}'
        with patch("atlas_plus.ntfy.inbound.handle_message", side_effect=RuntimeError("boom")):
            listener._consume_line(line)  # must NOT raise (stream loop would tear down)
