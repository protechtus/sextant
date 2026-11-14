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
_AR_MARKS = "[\u064b-\u0652\u0670]*"  # harakat and dagger alif between letters
_HE_MARKS = "[\u0591-\u05c7]*"  # niqqud and cantillation between letters


def _marked(word: str, marks: str) -> str:
    """A key name that still matches when vowel marks sit between its letters (MN46: vocalised Arabic keys leaked)."""
    return "".join(ch + marks for ch in word)


_MARKED_NAMES = "|" + "|".join([
    _marked("كلمة", _AR_MARKS) + "[_ ]" + _marked("السر", _AR_MARKS),
    _marked("كلمة", _AR_MARKS) + "[_ ]" + _marked("المرور", _AR_MARKS),
    _marked("السر", _AR_MARKS),
    _marked("مفتاح", _AR_MARKS) + "[_ ]API",
    _marked("رمز", _AR_MARKS) + "[_ ]" + _marked("الوصول", _AR_MARKS),
    _marked("סיסמה", _HE_MARKS),
    _marked("טוקן", _HE_MARKS),
    _marked("סוד", _HE_MARKS),
])
_KV_NAMES = (r"(?:api[_-]?key|apikey|secret|token|password|passwd|pwd|pass|client[_-]?secret|private[_-]?key"
             r"|access[_-]?key|auth[_-]?token|session[_-]?key|bearer|csrf[_-]?token|credentials?|passphrase"
             r"|パスワード|トークン|シークレット|秘密鍵|秘密|鍵|APIキー"
             r"|비밀번호|비밀|비밀키|시크릿|토큰|API키|密码|密碼|口令|令牌|密钥|密鑰|暗証番号"
             r"|รหัสผ่าน|รหัสลับ|โทเคน|पासवर्ड|टोकन|गुप्त[ _]कुंजी" + _MARKED_NAMES + ")")  # Thai, Hindi, and the mark-tolerant Arabic/Hebrew names (AG52, MN46)
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
    r"(?i)(?<![A-Za-z0-9_-])(-{0,2}(?:[A-Za-z0-9]{1,32}[_-]){0,8}(?:[a-z0-9]{1,32})?" + _KV_NAMES + _KV_SUFFIX + r")"  # `--secret-key=`, `-Dpassword=`, `-ApiKey:` start at their dashes (MN43)
    r"(?![A-Za-z0-9])"
    r"(['\"]?\s*[=:：＝]\s*(?:r(?=['\"]))?)"
    r"(\"[^\"\n]{4,}\"|'[^'\n]{4,}'|[\"']?[^\s'\"]{6,})"
)
_CLI_SECRET_RE = re.compile(r"(?i)(--(?:[a-z]+-){0,3}(?:token|password|passwd|api[_-]?key|secret|passphrase|client[_-]?secret)[= ])(\S{6,})")  # --db-password, --api-token (MN37)
_BASIC_AUTH_RE = re.compile(r"(?i)(authorization:\s*(?:basic|token|apikey|api-key)\s+)(\S{8,})")
_CURL_USER_RE = re.compile(r"(?i)((?:^|\s)(?:-u|--user)\s+[^\s:]+:)(\S+)")  # the `-u user:password` form of curl (MN37)
_YAML_BLOCK_RE = re.compile(r"(?im)^([ \t]*(?:[A-Za-z0-9]{1,32}[_-]){0,4}(?:password|passwd|secret|token|api[_-]?key)\s*:\s*[>|][-+0-9]*[ \t]*\n)((?:[ \t]+\S[^\n]*\n?)+)")  # password: > / | block scalars (MN37)
_XML_RE = re.compile(r"(?i)(<(password|passwd|secret|token|api[_-]?key)>)([^<\n]{4,})(</\2>)")
_CODE_VALUE_RE = re.compile(r"^(?:os\.|env\.|process\.env|\$\{|\$\(|[A-Za-z_][A-Za-z0-9_.]*(?:\[|\())")  # `token = tokens[0]`, `os.environ[...]` are code, not secrets (MN37)
_PATH_VALUE_RE = re.compile(r"^(?:/|[A-Za-z]:[\\/]|\.{1,2}/|~)")
# full-width ASCII (ＤＢ＿ＰＡＳＳＷＯＲＤ＝, a Japanese IME) folded one code point per code point, so a
# match in the folded text has the same span in the original and only the value is replaced
# (AG44: a whole-text NFKC pass rewrote x² and ½ in the summary)
_FULLWIDTH = {c: c - 0xFEE0 for c in range(0xFF01, 0xFF5F)}
_FULLWIDTH[0x3000] = 0x20


def _yaml_block_mask(m: "re.Match[str]") -> str:
    """Mask only the lines indented deeper than the key: `host:`/`port:` siblings stay (AG49)."""
    head = m.group(1)
    key_indent = len(head) - len(head.lstrip(" \t"))
    kept = []
    masked = False
    for line in m.group(2).splitlines(keepends=True):
        indent = len(line) - len(line.lstrip(" \t"))
        if indent > key_indent and line.strip():
            if not masked:
                kept.append(" " * (key_indent + 2) + "[REDACTED]\n")
                masked = True
            continue
        kept.append(line)
    return head + "".join(kept)


def _kv_quote(v: str) -> str:
    return v[0] if v and v[0] in "\"'" and len(v) > 1 and v[-1] == v[0] else ""


_PASS_WORDS = frozenset({"bypass", "compass", "encompass", "surpass", "trespass", "overpass", "underpass", "impasse", "passe"})


def _kv_skip(name: str, v: str, sep: str = "=", text: str | None = None, start: int = 0) -> bool:
    """Matches that are not secrets: placeholders, code expressions, a bare `pass:` in prose, `PWD=/path`,
    `bypass =`/`compass =` (a camel head before pass/pwd), and a lowercase word after a colon when the
    key sits mid-sentence (`The token: refreshing`), never when it starts its line (`password: hunter`
    in YAML, INI or compose: AG49, the earlier prose rule unmasked those)."""
    if not any(ch.isalnum() for ch in v):
        return True  # `password: ********` is a placeholder, not a secret
    low = name.lower()
    if low == "pass":
        return True  # only a prefixed `DB_PASS`/`MYSQL_PASS` is a key name; `pass:` alone is prose
    if low in _PASS_WORDS:
        return True  # `bypass`, `compass`: English words, not key names (the suffix rule unmasked `dbPass`/`PGPASS`: MN43)
    if low.endswith("pwd") and _PATH_VALUE_RE.match(v):
        return True  # the shell's PWD=/home/u
    if "=" not in sep and re.fullmatch(r"[a-z]+", v) and text is not None:
        lo = text.rfind("\n", max(0, start - 200), start)  # a bounded window: the unbounded rfind plus slice was quadratic on a 256 KB line (MN43)
        line_prefix = text[(lo + 1 if lo >= 0 else max(0, start - 200)):start]
        if re.search(r"[A-Za-z0-9,.;)]\s+$", line_prefix):
            return True  # mid-sentence prose: `The token: refreshing the page` (MN37)
    return bool(_CODE_VALUE_RE.match(v))


def _kv_mask(m: "re.Match[str]") -> str:
    v = m.group(3)
    if _kv_skip(m.group(1), v, m.group(2), m.string, m.start()):
        return m.group(0)
    q = _kv_quote(v)
    if not q and v[0] in "\"'":
        return f"{m.group(1)}{m.group(2)}{v[0]}[REDACTED]"  # an unterminated quote (a cut log line) still masks
    return f"{m.group(1)}{m.group(2)}{q}[REDACTED]{q}"


def _kv_redact(out: str) -> str:
    out = _KV_RE.sub(_kv_mask, out)
    folded = out.translate(_FULLWIDTH)
    if folded != out:
        pieces = []
        pos = 0
        for m in _KV_RE.finditer(folded):
            v = m.group(3)
            if _kv_skip(m.group(1), v, m.group(2), folded, m.start()) or "[REDACTED]" in v:
                continue
            q = _kv_quote(v)
            pieces.append(out[pos:m.start(3)])
            pieces.append(f"{q}[REDACTED]{q}")
            pos = m.end(3)
        pieces.append(out[pos:])
        out = "".join(pieces)  # one pass, not a string rebuild per match (MN37: 97 s per MB of full-width text)
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
    out = _YAML_BLOCK_RE.sub(_yaml_block_mask, out)
    out = _XML_RE.sub(r"\1[REDACTED]\4", out)
    out = _kv_redact(out)
    out = _CLI_SECRET_RE.sub(r"\1[REDACTED]", out)
    out = _BASIC_AUTH_RE.sub(r"\1[REDACTED]", out)
    out = _CURL_USER_RE.sub(r"\1[REDACTED]", out)
    out = _URL_CRED_RE.sub(r"\1[REDACTED]\3", out)
    out = _PROVIDER_KEY_RE.sub("[REDACTED:provider-key]", out)
    out = _PEM_RE.sub("[REDACTED:private-key]", out)
    return out
