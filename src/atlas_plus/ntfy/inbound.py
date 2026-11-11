"""Inbound ntfy command channel: phone -> Brain.

Subscribes to a SECRET command topic on ntfy, authenticates each message with a
shared-secret prefix (constant-time), routes the command to the Brain API, and
publishes the reply to the response topic. This is the reverse of the outbound
push helper in atlas_plus.telegram.notify -- together they make ntfy a two-way
control channel like Telegram, drivable from the ntfy phone app.

Security model (this is a command channel; treat it like one):
  - The command topic name is secret + high-entropy (ATLAS_NTFY_CMD_TOPIC). ntfy.sh
    topics are world-publishable, so the topic name is only a weak first factor.
  - Every inbound message MUST begin with the shared secret (ATLAS_NTFY_CMD_SECRET),
    checked in constant time. Messages that fail auth are logged and dropped with
    NO reply, so the channel is not an oracle for guessing the secret.
  - Commands route to Brain with the Brain API key. Brain's own ATLAS_AUTONOMOUS
    gate still applies, so a breached channel cannot trigger write tools unless
    autonomous mode is separately enabled. Defense in depth.
  - All inbound (including failed auth) is logged for the threat-intel pipeline.

Enable by setting ATLAS_NTFY_CMD_TOPIC and ATLAS_NTFY_CMD_SECRET. If either is
unset the listener stays disabled (no-op), so this is strictly opt-in.
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import time

import httpx

log = logging.getLogger("atlas.ntfy.inbound")

NTFY_BASE = os.environ.get("ATLAS_NTFY_BASE", "https://ntfy.sh")
CMD_TOPIC = os.environ.get("ATLAS_NTFY_CMD_TOPIC", "")
CMD_SECRET = os.environ.get("ATLAS_NTFY_CMD_SECRET", "")
RESP_TOPIC = os.environ.get("ATLAS_NTFY_TOPIC", "")  # reuse the outbound notification topic
BRAIN_URL = os.environ.get("ATLAS_BRAIN_URL", "http://127.0.0.1:8767").rstrip("/")
BRAIN_API_KEY = os.environ.get("ATLAS_BRAIN_API_KEY", "")

_SESSION_ID = "ntfy-control"
_REPLY_MAX = 380  # ntfy message bodies stay short; keep replies phone-friendly


def authenticate(raw_message: str, secret: str) -> tuple[bool, str | None]:
    """Validate the shared-secret prefix in constant time.

    Expected wire format: "<secret> <command...>". Returns (ok, command). When
    auth fails, command is None and the caller must drop the message silently.
    An empty configured secret denies everything (fail closed).
    """
    if not secret:
        return (False, None)
    text = (raw_message or "").strip()
    parts = text.split(None, 1)  # split on the first run of whitespace
    if not parts:
        return (False, None)
    presented = parts[0]
    # Compare as BYTES: hmac.compare_digest raises TypeError on non-ASCII *strings*, and
    # the presented token comes straight off a world-publishable topic -- a single emoji
    # message would otherwise throw, escape the per-line handler, and tear down the whole
    # stream (reconnect churn an outsider could induce at will). Bytes comparison is
    # constant-time for any content (fixed 2026-07-02, empirically confirmed).
    ok = hmac.compare_digest(presented.encode("utf-8"), secret.encode("utf-8"))
    if not ok:
        return (False, None)
    command = parts[1].strip() if len(parts) > 1 else ""
    return (True, command)


class BrainClient:
    """Thin sync client for the Brain REST API. Methods return phone-ready strings."""

    def __init__(self, base_url: str = BRAIN_URL, api_key: str = BRAIN_API_KEY, timeout: float = 20.0):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}

    def submit_task(self, instruction: str, session_id: str = _SESSION_ID) -> str:
        """POST /task. Returns a 'Queued ...' line or a readable error."""
        if not instruction:
            return "Empty instruction; nothing queued."
        try:
            r = httpx.post(
                f"{self.base_url}/task",
                json={"instruction": instruction[:10_000], "session_id": session_id},
                headers=self._headers(),
                timeout=self.timeout,
            )
            r.raise_for_status()
            data = r.json()
            return f"Queued task {data.get('task_id', '?')} ({data.get('status', '?')})"
        except Exception as exc:
            return f"Brain task submit failed: {exc}"

    def chat(self, message: str, session_id: str = _SESSION_ID) -> str:
        """POST /chat. Returns the model reply (truncated) or a readable error."""
        if not message:
            return "Empty message."
        try:
            r = httpx.post(
                f"{self.base_url}/chat",
                json={"session_id": session_id, "message": message[:100_000]},
                headers=self._headers(),
                timeout=max(self.timeout, 60.0),  # chat can take longer than task submit
            )
            r.raise_for_status()
            return (r.json().get("response") or "(no response)")[:_REPLY_MAX]
        except Exception as exc:
            return f"Brain chat failed: {exc}"

    def health_summary(self) -> str:
        """GET /health (open, no auth). Returns a compact one-line summary."""
        try:
            r = httpx.get(f"{self.base_url}/health", timeout=8.0)
            r.raise_for_status()
            d = r.json()
            bits = [f"{k}={d[k]}" for k in ("status", "model", "autonomous") if k in d]
            return "Brain: " + (", ".join(bits) if bits else json.dumps(d)[:_REPLY_MAX])
        except Exception as exc:
            return f"Brain health check failed: {exc}"


def route_command(command: str | None, brain: BrainClient, session_id: str = _SESSION_ID) -> str:
    """Map an authenticated command to a Brain action and return a reply string.

    Conventions:
      status            -> Brain health summary
      chat <message>    -> interactive Brain chat (synchronous)
      task <instruction>-> queue a Brain task (async; completion pushed separately)
      <anything else>   -> queued as a Brain task (the safe async default)
    """
    if not command:
        return "No command after the secret. Try: status | chat <msg> | task <instr>"
    low = command.lower()
    if low in ("status", "/status"):
        return brain.health_summary()
    if low.startswith(("chat ", "/chat ")):
        return brain.chat(command.split(None, 1)[1], session_id)
    if low.startswith(("task ", "/task ")):
        return brain.submit_task(command.split(None, 1)[1], session_id)
    return brain.submit_task(command, session_id)


def handle_message(msg: dict, *, secret: str, brain: BrainClient, publish, session_id: str = _SESSION_ID) -> str | None:
    """Process one ntfy stream object. Returns the reply text (also published) or None.

    Only event=="message" objects are acted on. Auth failures are logged and
    dropped with no reply (no guessing oracle).
    """
    if msg.get("event") != "message":
        return None
    raw = msg.get("message", "")
    ok, command = authenticate(raw, secret)
    if not ok:
        log.warning("ntfy inbound: AUTH FAILED (id=%s) -- dropped", msg.get("id"))
        return None
    reply = route_command(command, brain, session_id)
    try:
        publish(reply)
    except Exception as exc:
        log.warning("ntfy inbound: reply publish failed: %s", exc)
    return reply


class NtfyInboundListener:
    """Long-lived subscriber to the command topic that routes to Brain and replies."""

    def __init__(self, *, base=NTFY_BASE, cmd_topic=CMD_TOPIC, secret=CMD_SECRET,
                 resp_topic=RESP_TOPIC, brain: BrainClient | None = None, session_id=_SESSION_ID):
        self.base = base.rstrip("/")
        self.cmd_topic = cmd_topic
        self.secret = secret
        self.resp_topic = resp_topic
        self.brain = brain or BrainClient()
        self.session_id = session_id
        self._stop = False
        self._seen: set[str] = set()

    def publish(self, text: str) -> None:
        """Publish a reply to the response topic (the one the phone is subscribed to)."""
        if not self.resp_topic:
            log.debug("No ATLAS_NTFY_TOPIC set; reply not published: %s", text[:80])
            return
        headers = {"Title": "Atlas reply", "Tags": "robot"}
        _ntfy_token = os.environ.get("ATLAS_NTFY_TOKEN", "")  # ATL-007: auth for reserved topics
        if _ntfy_token:
            headers["Authorization"] = f"Bearer {_ntfy_token}"
        httpx.post(
            f"{self.base}/{self.resp_topic}",
            content=text.encode("utf-8")[:_REPLY_MAX],
            headers=headers,
            timeout=8.0,
        )

    def _consume_line(self, line: str) -> None:
        line = (line or "").strip()
        if not line:
            return
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            return
        mid = msg.get("id")
        if mid and mid in self._seen:
            return
        if msg.get("event") == "message" and mid:
            self._seen.add(mid)
            if len(self._seen) > 2000:  # bound the dedupe set
                self._seen = set(list(self._seen)[-1000:])
        # Isolate per-message failures: this runs inside run()'s stream loop, whose broad
        # except treats ANY escape as a connection error (teardown + backoff reconnect).
        # One poisoned message must never drop the whole channel.
        try:
            handle_message(msg, secret=self.secret, brain=self.brain,
                           publish=self.publish, session_id=self.session_id)
        except Exception:
            log.warning("ntfy inbound: message %s failed to process -- dropped", mid, exc_info=True)

    def run(self) -> None:
        """Stream the command topic forever, reconnecting with backoff on drop."""
        url = f"{self.base}/{self.cmd_topic}/json"
        backoff = 2
        log.info("ntfy inbound listener started on topic %s (responses -> %s)",
                 self.cmd_topic, self.resp_topic or "(none)")
        while not self._stop:
            try:
                with httpx.stream("GET", url, timeout=httpx.Timeout(None, connect=10.0)) as r:
                    r.raise_for_status()
                    backoff = 2  # reset after a clean connect
                    for line in r.iter_lines():
                        if self._stop:
                            break
                        self._consume_line(line)
            except Exception as exc:
                log.warning("ntfy stream error: %s -- reconnecting in %ss", exc, backoff)
                time.sleep(backoff)
                backoff = min(backoff * 2, 60)


def main() -> None:
    """Entry point: validate opt-in config and run the listener (atlas-ntfy-listener)."""
    # --help must print usage and exit 0 BEFORE any env validation
    # (Codex artifact audit 2026-09-21).
    import argparse

    argparse.ArgumentParser(
        prog="atlas-ntfy-listener",
        description="ntfy inbound command listener (needs ATLAS_NTFY_CMD_TOPIC "
                    "and ATLAS_NTFY_CMD_SECRET).",
    ).parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s %(message)s")
    if not CMD_TOPIC or not CMD_SECRET:
        log.error(
            "ntfy inbound disabled: set ATLAS_NTFY_CMD_TOPIC (a secret, high-entropy topic) "
            "and ATLAS_NTFY_CMD_SECRET (a shared secret prefix) to enable. Refusing to run "
            "an unauthenticated command channel."
        )
        raise SystemExit(1)
    if not RESP_TOPIC:
        log.warning("ATLAS_NTFY_TOPIC not set; replies will not be delivered to your phone.")
    NtfyInboundListener().run()


if __name__ == "__main__":
    main()
