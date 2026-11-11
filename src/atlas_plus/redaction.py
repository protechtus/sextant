"""Secret redaction for logs, task results, and artifacts (ATL-017 / audit 2026-06-04 MEDIUM-3/4).

`redact(text)` masks secrets before they are persisted to cleartext-on-disk surfaces (the MCP
search log, Brain task results, executor artifacts). Conservative by design -- it targets realistic
leak vectors with low false-positive risk rather than a broad high-entropy sweep that would mangle
legitimate identifiers (git SHAs, UUIDs, base64 content):

  1. Exact known secret VALUES present in the environment (the strongest signal -- if an actual
     ATLAS_MCP_TOKEN / API key appears verbatim in output, it is masked as [REDACTED:NAME]).
  2. `Authorization: Bearer <token>` headers.
  3. Telegram bot-token shape (`<digits>:<base64url>`).
  4. `key=value` / `key: value` where the key name signals a secret (api_key/token/secret/password)
     -- including quoted values (`token='...'`, JSON `"api_key": "..."`), the dominant shape in
     Brain task results (gap found + fixed 2026-07-02: the value class excluded quotes, so every
     JSON-shaped secret passed through unredacted).
  5. Distinctive provider-prefixed keys pasted verbatim (`sk-...` Anthropic/OpenAI, `ghp_` /
     `github_pat_` GitHub) -- catches keys that are NOT this machine's own env values (e.g. a
     key pasted mid-rotation), which rule 1 cannot see. Prefix-gated, so still low-FP.
"""
from __future__ import annotations

import os
import re

# Env vars whose VALUES must never appear in cleartext logs/artifacts.
_SECRET_ENV_VARS = (
    "ATLAS_MCP_TOKEN",
    "ATLAS_BRAIN_API_KEY",
    "ATLAS_WEB_API_KEY",
    "ATLAS_NTFY_TOKEN",
    "ATLAS_NTFY_CMD_SECRET",
    "ATLAS_CF_ACCESS_CLIENT_ID",
    "ATLAS_CF_ACCESS_CLIENT_SECRET",
    "ATLAS_TELEGRAM_BOT_TOKEN",
    "GEMINI_API_KEY",
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "RESEARCH_API_KEY",
    # Services inherit this from atlas-env loading, and rclone output flows into task
    # results; a leaked crypt passphrase = unrecoverable-backup exposure (ADR-016).
    "RCLONE_CONFIG_PASS",
)

_BEARER_RE = re.compile(r"(Bearer\s+)[A-Za-z0-9\-_.=]{12,}", re.IGNORECASE)
_TELEGRAM_RE = re.compile(r"\b\d{6,}:[A-Za-z0-9_-]{30,}\b")
# Optional quotes around the key name's close and the value's open ("api_key": "v", token='v')
# so JSON and quoted assignments redact too; the trailing quote is left in place, preserving
# structure: {"api_key": "[REDACTED]"}.
_KV_RE = re.compile(
    r"(?i)\b(api[_-]?key|secret|token|password|passwd|client[_-]?secret|bearer)\b"
    r"(['\"]?\s*[=:]\s*['\"]?)"
    r"([^\s'\"]{6,})"
)
# Provider-prefixed key shapes (distinctive prefixes -> low false-positive risk).
_PROVIDER_KEY_RE = re.compile(r"\b(sk-[A-Za-z0-9_-]{16,}|ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})")

_MIN_VALUE_LEN = 6  # do not mask very short env values (avoid masking "on"/"off"/ports)


def redact(text: str | None, *, env: dict | None = None) -> str | None:
    """Return ``text`` with secrets masked. None/empty pass through unchanged."""
    if not text:
        return text
    environ = os.environ if env is None else env
    out = text

    # 1) Exact known secret values from the environment.
    for name in _SECRET_ENV_VARS:
        val = environ.get(name)
        if val and len(val) >= _MIN_VALUE_LEN and val in out:
            out = out.replace(val, f"[REDACTED:{name}]")

    # 2) Structural patterns.
    out = _BEARER_RE.sub(r"\1[REDACTED]", out)
    out = _TELEGRAM_RE.sub("[REDACTED:telegram-token]", out)
    out = _KV_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}[REDACTED]", out)
    out = _PROVIDER_KEY_RE.sub("[REDACTED:provider-key]", out)
    return out
