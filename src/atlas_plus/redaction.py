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
_KV_NAMES = (r"(?:api[_-]?key|apikey|secret|token|password|passwd|client[_-]?secret|private[_-]?key"
             r"|access[_-]?key|auth[_-]?token|session[_-]?key|bearer|csrf[_-]?token|credentials?|passphrase"
             r"|パスワード|トークン|シークレット|秘密鍵|秘密|鍵|APIキー"
             r"|비밀번호|비밀|시크릿|토큰|API키|密码|密碼|口令|令牌|密钥|密鑰)")
_KV_SUFFIX = r"(?:[_-]?(?:key|token|secret|value|str|string|id|base|hash|v?\d{1,3})){0,2}"
# Exact key NAMES: a bounded prefix of up to eight `_`/`-` segments (`MY_APP_PROD_EU_WEST_DB_`), an
# optional camelCase head (`dbPassword`, `AccessToken`), the name, and up to two short suffixes
# (`_KEY_BASE`, `_HASH`, `_v2`). The open `[A-Za-z0-9_-]*name[A-Za-z0-9_-]*` form was quadratic and
# matched `tokenizer`, `max_tokens`, `secret_santa`, `password_policy` (MN31); the first bounded form
# missed camelCase, longer prefixes and `bearer`/`csrftoken` (AG44). Korean and Chinese key names
# join the Japanese ones (AG46). A quoted value is masked whole (spaces inside the quotes stay
# inside the mask); an unquoted value runs to the next whitespace, `&` included (AG43: a password
# with `&` leaked its tail; a query-string tail is over-masked instead). The lookbehind limits the
# scan to word starts, so a 1 MB blob costs one attempt per word, not per character.
_KV_RE = re.compile(
    r"(?i)(?<![A-Za-z0-9_])((?:[A-Za-z0-9]{1,32}[_-]){0,8}(?:[a-z0-9]{1,32})?" + _KV_NAMES + _KV_SUFFIX + r")"
    r"(?![A-Za-z0-9])"
    r"(['\"]?\s*[=:：＝]\s*r?)"
    r"(\"[^\"\n]{4,400}\"|'[^'\n]{4,400}'|[^\s'\"]{6,})"
)
_CLI_SECRET_RE = re.compile(r"(?i)(--(?:token|password|passwd|api[_-]?key|secret|passphrase|client[_-]?secret)[= ])(\S{6,})")
_BASIC_AUTH_RE = re.compile(r"(?i)(authorization:\s*basic\s+)([A-Za-z0-9+/=]{8,})")
# full-width ASCII (ＤＢ＿ＰＡＳＳＷＯＲＤ＝, a Japanese IME) folded one code point per code point, so a
# match in the folded text has the same span in the original and only the value is replaced
# (AG44: a whole-text NFKC pass rewrote x² and ½ in the summary)
_FULLWIDTH = {c: c - 0xFEE0 for c in range(0xFF01, 0xFF5F)}
_FULLWIDTH[0x3000] = 0x20


def _kv_quote(v: str) -> str:
    return v[0] if v and v[0] in "\"'" and len(v) > 1 and v[-1] == v[0] else ""


def _kv_mask(m: "re.Match[str]") -> str:
    v = m.group(3)
    if not any(ch.isalnum() for ch in v):
        return m.group(0)  # `password: ********` is a placeholder, not a secret
    q = _kv_quote(v)
    return f"{m.group(1)}{m.group(2)}{q}[REDACTED]{q}"


def _kv_redact(out: str) -> str:
    out = _KV_RE.sub(_kv_mask, out)
    folded = out.translate(_FULLWIDTH)
    if folded != out:
        edits = []
        for m in _KV_RE.finditer(folded):
            v = m.group(3)
            if any(ch.isalnum() for ch in v) and "[REDACTED]" not in v:
                q = _kv_quote(v)
                edits.append((m.start(3), m.end(3), f"{q}[REDACTED]{q}"))
        for s, e, rep in reversed(edits):
            out = out[:s] + rep + out[e:]
    return out
# Provider-prefixed key shapes (distinctive prefixes -> low false-positive risk).
_PROVIDER_KEY_RE = re.compile(
    r"\b(sk-[A-Za-z0-9_-]{16,}|sk_(?:live|test)_[A-Za-z0-9]{10,}|gh[opsru]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}"
    r"|glpat-[A-Za-z0-9_-]{20,}|hf_[A-Za-z0-9]{20,}"
    r"|AKIA[0-9A-Z]{16}|xox[abprs]-[A-Za-z0-9-]{10,}|AIza[0-9A-Za-z_-]{35})")
# a PEM or PGP private-key block, terminated or cut off at the end of the text (MN31)
_PEM_RE = re.compile(r"-----BEGIN (?:[A-Z ]*PRIVATE KEY|PGP PRIVATE KEY BLOCK)-----(?:.*?-----END [A-Z ]*(?:PRIVATE KEY|PRIVATE KEY BLOCK)-----|.*\Z)", re.S)
_URL_CRED_RE = re.compile(r"(://[^/\s:@]+:)([^\s/]+)(@)(?=[^\s/@]+)")  # user:password@host; the password may hold `@` (MN31/MN33, AG44)

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
    out = _kv_redact(out)
    out = _CLI_SECRET_RE.sub(r"\1[REDACTED]", out)
    out = _BASIC_AUTH_RE.sub(r"\1[REDACTED]", out)
    out = _URL_CRED_RE.sub(r"\1[REDACTED]\3", out)
    out = _PROVIDER_KEY_RE.sub("[REDACTED:provider-key]", out)
    out = _PEM_RE.sub("[REDACTED:private-key]", out)
    return out
