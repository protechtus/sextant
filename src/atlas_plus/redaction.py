"""Secret redaction for logs, task results, and artifacts (ATL-017 / audit 2026-06-04 MEDIUM-3/4).

`redact(text)` masks secrets before they are persisted to cleartext-on-disk surfaces (the MCP
search log, Brain task results, executor artifacts). Conservative by design -- it targets realistic
leak vectors with low false-positive risk rather than a broad high-entropy sweep that would mangle
legitimate identifiers (git SHAs, UUIDs, base64 content):

  1. Exact known secret VALUES present in the environment (the strongest signal -- if an actual
     ATLAS_MCP_TOKEN / API key appears verbatim in output, it is masked as [REDACTED:NAME]).
  2. `Authorization: Bearer <token>` headers.
  3. Telegram bot-token shape (`<digits>:<base64url>`).
  4. `key=value` / `key: value` where the key name signals a secret (api_key/token/secret/password),
     in fourteen scripts and languages, plus the structured shapes listed in docs/SECURITY-TEST-MATRIX.md
     (YAML block scalars, Kubernetes name/value pairs, .netrc/.pgpass/.npmrc, XML attributes, `=>` rockets,
     escaped JSON, Markdown, Cookie headers, webhooks, Azure keys, CLI `-p` flags, provider tokens, and the
     prose form `my password is V` when the value looks like a secret: AG49, AG55)
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

_BEARER_RE = re.compile(r"(?i)(Bearer[ \t]+)([A-Za-z0-9\-_.=]{12,})")  # `[ \t]`: `bearer\ntoken.expiry.seconds=900` lost the next line (AG62)


def _bearer_ok(v: str) -> bool:
    """`Use Bearer authentication`, `Bearer YOUR_API_TOKEN_HERE`, `Bearer your_access_token` are prose or placeholders (AG62)."""
    return not re.fullmatch(r"[A-Za-z]+(?:[_-][A-Za-z]+)*", v) or not (v.islower() or v.isupper() or (v[0].isupper() and v[1:].islower()))
_TELEGRAM_RE = re.compile(r"\b\d{6,}:[A-Za-z0-9_-]{30,}\b")
# Optional quotes around the key name's close and the value's open ("api_key": "v", token='v')
# so JSON and quoted assignments redact too; the trailing quote is left in place, preserving
# structure: {"api_key": "[REDACTED]"}.
_AR_MARKS = "[\u064b-\u0655\u0670\u0640]*"  # harakat, hamza marks, dagger alif and tatweel between letters (AG60)
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
    _marked("סיסמא", _HE_MARKS),  # the other common spelling (AG60)
    _marked("טוקן", _HE_MARKS),
    _marked("סוד", _HE_MARKS),
])
_KV_NAMES = (r"(?:api[_-]?key|apikey|secret|token|password|passwd|pwd|pass|client[_-]?secret|private[_-]?key"
             r"|access[_-]?key|auth[_-]?token|session[_-]?key|bearer|csrf[_-]?token|credentials?|passphrase"
             r"|(?:master|encryption|decryption|signing|hmac|jwt|license)[_-]?key"  # AG62: RAILS_MASTER_KEY=V leaked
             r"|パスワード|トークン|シークレット|秘密鍵|秘密|鍵|APIキー"
             r"|비밀번호|비밀|비밀키|시크릿|토큰|API키|密码|密碼|口令|令牌|密钥|密鑰|暗証番号"
             r"|รหัสผ่าน|รหัสลับ|โทเคน|คีย์[ _]API|กุญแจ[ _]API|पासवर्ड|टोकन|गुप्त[ _]कुंजी|एपीआई[ _]कुंजी|कूटशब्द"
             r"|API-Schlüssel|Geheimnis|מפתח[ _]API|קוד[ _]גישה|توکن|کلید[ _]API|رمز[\u200c _]عبور|گذرواژه|رمز|سرّ?"  # MN52
             r"|пароль|токен|секрет|passwort|kennwort|mot[ _]de[ _]passe|contraseña|contrasena|senha|şifre"  # AG55: Russian, German, French, Spanish, Portuguese, Turkish
             r"|رمز[ _]عبور|گذرواژه|کلمه[ _]عبور" + _MARKED_NAMES + ")")  # Thai, Hindi, and the mark-tolerant Arabic/Hebrew names (AG52, MN46)
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
    r"(?i)(?<![A-Za-z0-9_-])(-{0,2}(?:[A-Za-z0-9]{1,32}[_-]){0,16}(?:[a-z0-9]{1,32})?" + _KV_NAMES + _KV_SUFFIX + r")"  # `--secret-key=`, `-Dpassword=`, `-ApiKey:` start at their dashes (MN43); up to sixteen segments (MN48: a nine-segment slug leaked)
    r"(?![A-Za-z0-9])"
    r"(['\"]?\]?\s*[=:：＝]\s*(?:r(?=['\"]))?)"  # AG62: the environ-subscript assignment `environ['DB_PASS...'] = 'V'` and `params[:password] = V` leaked
    r"(\"[^\"\n]{4,}\"|'[^'\n]{4,}'|[\"']?[^\s'\"]{6,})"
)
_CLI_SECRET_RE = re.compile(r"(?i)(--(?:[a-z]+-){0,3}(?:token|password|passwd|api[_-]?key|secret|passphrase|client[_-]?secret)[= ])(\S{6,})")  # --db-password, --api-token (MN37)
_BASIC_AUTH_RE = re.compile(r"(?i)(authorization:\s*(?:basic|token|apikey|api-key)\s+)(\S{8,})")
_CURL_USER_RE = re.compile(r"(?i)((?:^|\s)(?:-u|--user)\s+[^\s:]+:)(\S+)")  # the `-u user:password` form of curl (MN37)
_YAML_BLOCK_RE = re.compile(r"(?im)^([ \t]*(?:-[ \t]+)*(?:[A-Za-z0-9]{1,32}[_-]){0,4}(?:password|passwd|secret|token|api[_-]?key|private[_-]?key|passphrase|credentials?)[ \t]*:[ \t]*[>|][-+0-9]*[ \t]*\r?\n)((?:[ \t]*\r?\n|[ \t]+\S[^\n]*(?:\n|\Z))+)")  # password: > / | block scalars; `- password: |`, CRLF and blank lines before the body (MN37, AG55)
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
    key_indent = len(head) - len(head.lstrip(" \t-"))  # the key column: `- password: |` starts its mapping after the dash
    kept = []
    masked = False
    for line in m.group(2).splitlines(keepends=True):
        indent = len(line) - len(line.lstrip(" \t"))
        if indent > key_indent and line.strip():
            if not masked:
                kept.append(" " * (key_indent + 2) + "[REDACTED]" + ("\r\n" if line.endswith("\r\n") else "\n"))
                masked = True
            continue
        kept.append(line)
    return head + "".join(kept)


def _kv_quote(v: str) -> str:
    return v[0] if v and v[0] in "\"'" and len(v) > 1 and v[-1] == v[0] else ""


_ENVREF_RE = re.compile(r"^\$[A-Z_][A-Z0-9_]*$|^!!?[A-Za-z][A-Za-z/_.-]*$")
_STATUS_RE = re.compile(r"(?i)[\[(<]?(?:required|optional|expired|missing|invalid|hidden|redacted|undefined|string|str|secretstr|unset|empty|none|null|incorrect|wrong|rejected|refreshing|n/a)[\])>]?[;,.]*")
_WORDISH_RE = re.compile(r"[^\d\s!-/:-@\[-`{-~]+[,.;:!?]?")
_DOC_WORDS = frozenset({"password", "pass", "passwd", "pwd", "secret", "token", "user", "username", "yourpassword", "your_password", "changeme", "arg", "args", "file", "env", "fd", "stdin", "anonymous", "guest", "x-oauth-basic"})


def _doc_value(v: str) -> bool:
    """A documentation placeholder after a flag, a `user:` or a `Basic`: `<password>`, `${PW}`, `$PASS`, `TOKEN]`, `password` (AG62)."""
    v = v.strip("\"'")
    if not v:
        return True
    return bool(_PLACEHOLDER_RE.match(v) or re.fullmatch(r"\$[A-Za-z_]\w*|\$\{.*|[A-Z][A-Z_]*[\])>]*", v) or v.lower() in _DOC_WORDS)


_PLACEHOLDER_RE = re.compile(r"<[^<>\n]{0,60}(?:>|$)|\{\{|\$\{|%[A-Za-z_]+%|\[REDACTED|[xX]{4,}$|\.{3,}$")  # `XXXX` is a placeholder too (AG61)
_PROSE_WORD_RE = re.compile(r"(?:ing|ed|ion|ly|ness|able|ible|ment|ful|less|ous)$|^(?:invalid|missing|required|incorrect|expired|empty|unknown|wrong|rejected|changed|secret|hidden|default|optional|pending|redacted)$")
_PASS_WORDS = frozenset({
    "bypass", "compass", "encompass", "surpass", "trespass", "overpass", "underpass", "impasse", "passe", "multipass", "repass",
    # render-graph and compiler vocabulary (AG60: `renderPass: forward_pass` was masked)
    "renderpass", "firstpass", "secondpass", "thirdpass", "onepass", "twopass", "prepass", "postpass", "subpass", "depthpass",
    "shadowpass", "zpass", "forwardpass", "lightpass", "geometrypass", "finalpass", "singlepass", "fullpass", "blurpass", "colorpass",
    "lastpass", "nextpass", "currentpass", "shaderpass", "drawpass", "cullpass", "gbufferpass", "lightingpass", "transparentpass",
})


def _kv_skip(name: str, v: str, sep: str = "=", text: str | None = None, start: int = 0, end: int | None = None) -> bool:
    """Matches that are not secrets: placeholders, code expressions, a bare `pass:` in prose, `PWD=/path`,
    `bypass =`/`compass =` (a camel head before pass/pwd), and a lowercase word after a colon when the
    key sits mid-sentence (`The token: refreshing`), never when it starts its line (`password: hunter`
    in YAML, INI or compose: AG49, the earlier prose rule unmasked those)."""
    if not any(ch.isalnum() for ch in v):
        return True  # `password: ********` is a placeholder, not a secret
    if _PLACEHOLDER_RE.match(v):
        return True  # `password: <your-password>`, `{{ db_password }}`, `%PASSWORD%` (AG55)
    vq = v.strip("\"'")
    if vq != v and (_PLACEHOLDER_RE.match(vq) or _CODE_VALUE_RE.match(vq)):
        return True  # AG62: the quotes hid the placeholder: `password: "${DB_PASS...}"`, `token: "{{ t }}"`, `"os.environ['X']"`
    if "\n" in sep and text is not None and end is not None:
        vstart = end - len(v)
        nl = text.rfind("\n", 0, vstart)
        if vstart == nl + 1 or text[end:end + 1] == ":":
            return True  # AG62: `Enter password:` + newline + `Authentication successful`, `password:` + `  database: app`: the next line is not the value
    if _STATUS_RE.fullmatch(v):
        return True  # AG62: `token: expired`, `password: required`, `token: string;`, `password: (hidden)`: a status or a type, not a value
    if _ENVREF_RE.match(vq):
        return True  # AG62: `api_key: $API_KEY`, a YAML tag `password: !vault |`
    if sep.startswith("]") and not (text is not None and start > 0 and text[start - 1] == ":"):
        return True  # AG62: `cache[token] = value` is code; only a quoted key or a Ruby symbol (`params[:password]`) counts
    if text is not None and start >= 3 and text[start - 3:start] == "://":
        return True  # AG62: `https://token:x@host/` is URL userinfo, masked by the URL rule without eating the host
    if " " in sep and "=" in sep and re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+", v) and not re.search(r"\d", v):
        return True  # AG62: `secret = config.secret`, `api_key = settings.API_KEY` (attribute access in code)
    if v[0] not in "\"'" and len(v) >= 3 and not v.isascii() and not re.search(r"[A-Za-z]", v):
        return True  # AG62: `密码：必须至少包含八个字符` (CJK, Thai, Hindi label sentences); a secret holds ASCII
    if "=" not in sep and text is not None and end is not None and _WORDISH_RE.fullmatch(v) and re.match(r"[ \t]+[^\d\s!-/:-@\[-`{-~]{2,}", text[end:end + 40]):
        return True  # AG62: `password: expired yesterday`, `Passwort: falsch, bitte erneut versuchen`: more words follow, so it is prose
    low = name.lower()
    if low == "pass" and "=" not in sep:
        return True  # `pass:` alone is prose (a test result, a pass count); `PASS=hunter2` is an assignment (AG60)
    if low.lstrip("-") in _PASS_WORDS:  # `--compass=` too (AG60)
        return True  # `bypass`, `compass`: English words, not key names (the suffix rule unmasked `dbPass`/`PGPASS`: MN43)
    if low.endswith("pwd") and _PATH_VALUE_RE.match(v):
        return True  # the shell's PWD=/home/u
    if "=" not in sep and re.fullmatch(r"[a-z]+", v) and text is not None:
        lo = text.rfind("\n", max(0, start - 200), start)  # a bounded window: the unbounded rfind plus slice was quadratic on a 256 KB line (MN43)
        line_prefix = text[(lo + 1 if lo >= 0 else max(0, start - 200)):start]
        if re.search(r"[A-Za-z0-9,.;)]\s+$", line_prefix):
            # AG55: a lowercase word that ENDS the line, is 8+ chars and is not an English inflection or status
            # word reads as a secret (`my password: sunshinemeadow`); a word followed by more words
            # (`The token: refreshing the page`) stays prose
            if not (end is not None and len(v) >= 8 and not _PROSE_WORD_RE.search(v) and re.match(r"[ \t.,;!]*(?:\r?\n|\Z)", text[end:end + 6])):
                return True  # mid-sentence prose: `The token: refreshing the page` (MN37)
    return bool(_CODE_VALUE_RE.match(v))


def _kv_mask(m: "re.Match[str]") -> str:
    v = m.group(3)
    if _kv_skip(m.group(1), v, m.group(2), m.string, m.start(), m.end()):
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
            if _kv_skip(m.group(1), v, m.group(2), folded, m.start(), m.end()) or "[REDACTED]" in v:
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
_PEM_BEGIN_RE = re.compile(r"-----BEGIN (?:[A-Z ]*PRIVATE KEY|PGP PRIVATE KEY BLOCK)-----")
_PEM_END_RE = re.compile(r"-----END [A-Z ]*(?:PRIVATE KEY|PRIVATE KEY BLOCK)-----")
_PEM_BODY_RE = re.compile(r"\s*(?:[A-Za-z0-9+/=]{8,}|Proc-Type:|DEK-Info:|Version:)")


def _pem_redact(out: str) -> str:
    """A terminated block, or a cut-off one that STARTS with key material. `a PEM file begins with -----BEGIN PRIVATE KEY-----`
    in prose used to swallow every line after it (88 lines became one); one pass, one END search per text (AG62)."""
    if "-----BEGIN" not in out:
        return out
    pieces, pos, no_end = [], 0, False
    for m in _PEM_BEGIN_RE.finditer(out):
        if m.start() < pos:
            continue
        e = None if no_end else _PEM_END_RE.search(out, m.end())
        if e is None:
            no_end = True
            if not _PEM_BODY_RE.match(out, m.end()):
                continue
            end = len(out)
        else:
            end = e.end()
        pieces.append(out[pos:m.start()])
        pieces.append("[REDACTED:private-key]")
        pos = end
    pieces.append(out[pos:])
    return "".join(pieces)


_URL_CRED_RE = re.compile(r"(://[^/\s:@]*:)([^\s/]+)(@)(?=[^\s/@]+)")  # an empty user too: `redis://:V@host` (AG62)  # user:password@host; the password may hold `@` (MN31/MN33, AG44)


# ---- AG55 (sweep 18): shapes the key=value rule cannot see ------------------------------------------------------
_NAME_ASCII = (r"(?:api[_-]?key|apikey|secret|token|password|passwd|pwd|passphrase|client[_-]?secret|private[_-]?key"
               r"|access[_-]?key|auth[_-]?token|credentials?)")
_NAME_FULL = r"(?:(?:[A-Za-z0-9]{1,32}[_.-]){0,4}" + _NAME_ASCII + r"(?:[_-]?[A-Za-z0-9]{1,8})?)"  # dotted names too: `spring.datasource.password` (MN49)
_ANY_NAME = r"(?:" + _NAME_FULL + r"|" + _KV_NAMES[3:-1] + r")"
_VAL = r"(\"[^\"\n]{4,}\"|'[^'\n]{4,}'|[^\s'\"<>]{6,})"
# Kubernetes / compose / CI env lists: `- name: API_TOKEN` then `value: V` on the next line
_K8S_RE = re.compile(r"(?i)(^[ \t]*-?[ \t]*name[ \t]*:[ \t]*[\"']?(?:[A-Za-z0-9]{1,32}[_.-]){0,8}(?:[a-z0-9]{1,32})?" + _KV_NAMES + _KV_SUFFIX
                     + r"[\"']?[ \t]*\r?\n[ \t]*value[ \t]*:[ \t]*)" + _VAL, re.M)
# .netrc (`machine h login u password V`, also newline separated) and `default login u password V`
_NETRC_RE = re.compile(r"(?im)^([ \t]*(?:machine[ \t]+\S+[ \t]+|default[ \t]+)?login[ \t]+\S+(?:\s+account\s+\S+)?\s+password[ \t]+)(\S{4,})")  # a .netrc line, not prose about a login page (MN49); the indentation stays (AG61)
# .pgpass: host:port:database:user:password on a line of its own
_PGPASS_RE = re.compile(r"(?m)^((?=[^:\s]*[A-Za-z.*])[^:\s#]{1,253}:(?:\d{1,5}|\*):[^:\s]{1,63}:[^:\s]{1,63}:)([^\s]{8,})(?=[ \t]*\r?$)")
# npmrc / pip.conf style underscore keys: `//registry/:_authToken=V`, `_password=V`, `_auth=V`
_NPMRC_RE = re.compile(r"(?i)((?<![A-Za-z0-9])_(?:auth(?:token)?|password|token|secret)[ \t]*=[ \t]*)([\"']?)([^\s\"']{6,})")
# XML configuration: <add key="password" value="V"/>, <property name="password" value="V"/>, <entry key="password">V</entry>
_XML_ATTR_RE = re.compile(r"(?i)(<\w+\s[^<>\n]{0,80}?\b(?:key|name)=[\"']" + _NAME_FULL + r"[\"'][^<>\n]{0,60}?\bvalue=[\"'])([^\"'\n]{4,})")
_XML_ENTRY_RE = re.compile(r"(?i)(<\w+\s[^<>\n]{0,80}?\b(?:key|name)=[\"']" + _NAME_FULL + r"[\"'][^<>\n]{0,60}>\s*(?:<value>)?)([^<\n]{4,})(?=<)")
# PHP / Ruby hash rockets: 'password' => 'V'
_ROCKET_RE = re.compile(r"(?i)(?<![A-Za-z0-9_-])([\"']" + _NAME_FULL + r"[\"']\s*=>\s*)(\"[^\"\n]{4,}\"|'[^'\n]{4,}')")
# JSON that was escaped into a string: {\"password\": \"V\"}
_ESC_JSON_RE = re.compile(r"(?i)(?<![A-Za-z0-9_-])(" + _NAME_FULL + r"\\+[\"']\s*[:=]\s*\\+[\"'])((?:(?!\\+[\"'])[^\n]){4,}?)(?=\\+[\"'])")
# Markdown: **Password:** V, __Password__: V
_MD_BOLD_RE = re.compile(r"(?i)((?:\*\*|__)(?:" + _NAME_FULL + r"|" + _KV_NAMES[3:-1] + r")[ \t]*:?(?:\*\*|__)[ \t]*:?[ \t]*)([^\s*_<>|]{6,})")
_MD_ROW_RE = re.compile(r"(?im)^(\|[ \t]*`?(?:" + _NAME_FULL + r")`?[ \t]*\|[ \t]*`?)([^|\s`]{6,})(`?[ \t]*\|)")
# cookies: the pairs of a Cookie / Set-Cookie header (attributes such as Path and HttpOnly stay)
_COOKIE_RE = re.compile(r"(?im)^([ \t]*(?:Set-)?Cookie[ \t]*:[ \t]*)([^\r\n]+)")
_COOKIE_ATTRS = frozenset({"path", "domain", "expires", "max-age", "samesite", "secure", "httponly", "priority", "version", "comment"})
_COOKIE_PAIR_RE = re.compile(r"(?<![^\s;])([A-Za-z0-9_.%-]{1,64}=)([^\s;]{6,})")
_WEBHOOK_RE = re.compile(r"(hooks\.slack\.com/(?:services|workflows)/[A-Za-z0-9]{6,}/[A-Za-z0-9]{6,}/)([A-Za-z0-9]{12,})|(discord(?:app)?\.com/api/webhooks/\d{6,}/)([A-Za-z0-9_-]{20,})")
_AZURE_RE = re.compile(r"(?i)\b((?:AccountKey|SharedAccessKey|SharedAccessSignature|client_secret)=)([A-Za-z0-9+/%=_.-]{16,})|([?&;]sig=)([A-Za-z0-9%+/=_-]{16,})")
_MYSQL_P_RE = re.compile(r"((?<![\w./'\"\\-])(?:sudo[ \t]+)?(?:mysql|mysqldump|mysqladmin|mariadb|mysqlsh)\b[^\n|;&]{0,200}?[ \t]-p)([^\s\-][^\s]{2,})", re.M)  # the command word itself (not `*.mysql` in a find: MN49), wherever it stands: `RUN mysql`, `run: mysql`, `- mysql` (AG61)
_DOCKER_P_RE = re.compile(r"(\b(?:docker|podman|nerdctl)[ \t]+login\b[^\n|;&]{0,200}?[ \t](?:-p|--password)[ =]+)([^\s\-]\S*)")
_SSHPASS_RE = re.compile(r"(\bsshpass[ \t]+(?:-\w+[ \t]+)*-p[ \t]*)(?!-)(\S+)")
_TOKEN_SHAPES_RE = re.compile(r"\b(eyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}|ya29\.[A-Za-z0-9_-]{20,}|npm_[A-Za-z0-9]{36}|SG\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}|dop_v1_[a-f0-9]{64}|pypi-[A-Za-z0-9_-]{50,}|whsec_[A-Za-z0-9]{20,}|rk_live_[A-Za-z0-9]{10,}|shp(?:at|ss|ca)_[a-f0-9]{32})")
# prose: `my password is V` / `the token was V` where V looks like a secret (8+ chars, two or more character classes)
_PROSE_RE = re.compile(r"(?i)\b((?:(?:my|your|the|his|her|their|our|new|old|temp(?:orary)?|root|admin|wifi|login)[ \t]+){1,2}(?:password|passcode|passphrase|api[ \t]key|access[ \t]token|token|secret)[ \t]+(?:is|was|are|will[ \t]+be|=|:)[ \t]+)([!-~]{8,})")


# the same sentence in Russian, German, French, Spanish, Portuguese, Turkish, Persian, Japanese, Korean, Chinese (`mein Passwort ist V`, `パスワードは V`)
_PROSE_I18N2_RE = re.compile(
    r"(?i)("
    r"(?:รหัสผ่าน|โทเคน)(?:ของ(?:ฉัน|เรา|ผม))?คือ[ \t]*"                                        # Thai: my password is
    r"|(?:मेरा|हमारा|मेरी)?[ \t]*(?:पासवर्ड|टोकन|कूटशब्द)[ \t]+(?:है[ \t]+)?"                     # Hindi: mera password (hai) V (hai)
    r"|" + _marked("كلمة", _AR_MARKS) + r"[ \t]" + _marked("السر", _AR_MARKS) + r"(?:[ \t]" + _marked("الخاصة", _AR_MARKS) + r"[ \t]" + _marked("بي", _AR_MARKS) + r")?[ \t]" + _marked("هي", _AR_MARKS) + r"[ \t]*"
    r"|ה?" + _marked("סיסמ", _HE_MARKS) + r"[אה][\u0591-\u05c7]*(?:[ \t]שלי)?[ \t]היא[ \t]*"                 # Hebrew: ha-sisma sheli hi V
    r"|(?:رمز[\u200c \t]عبور|گذرواژه|توکن)(?:[ \t](?:من|شما|ما|او|تو))?[ \t]+"                                  # Persian: ramz-e obur(-e man) V ast
    r"|(?:мой[ \t]+|наш[ \t]+)?(?:пароль|токен)[ \t]+"                                        # Russian without a dash: moy parol V
    r"|(?:mein|das|unser)[ \t]+passwort[ \t]+(?:lautet|ist)[ \t]+"                            # German: mein Passwort lautet V
    r"|(?:meu|minha|o|a)[ \t]+(?:token|senha|chave)[ \t]+(?:é[ \t]+)?"                        # Portuguese: meu token V
    r")([!-~]{8,})"
)
_PROSE_I18N_RE = re.compile(r"(?i)((?:пароль|токен|passwort|kennwort|mot[ \t]de[ \t]passe|contraseña|contrasena|senha|şifre|رمز[ \t]عبور|パスワード|비밀번호|密码)(?:[ \t]+(?:мой|моя|mein|mon|mi|minha|من|my))?(?:[ \t]+(?:is|est|ist|es|é|-|—|است|:)|[はがは는은是:：])[ \t]*)([!-~]{8,})")


def _secretish(v: str) -> bool:
    """8+ chars, two or more of lower/upper/digit/symbol, and a digit, symbol or inner capital; not a path, URL or code."""
    v = v.strip("\"'`.,;:!?()[]{} ")  # `the token is "expired"`: the quotes made a prose word look mixed-class (MN49)
    if len(v) < 8 or _CODE_VALUE_RE.match(v) or _PATH_VALUE_RE.match(v) or "://" in v or (v.isalpha() and _PROSE_WORD_RE.search(v.lower())):  # the suffix veto only for an all-letter word: `Tr0ub4dor&Rising` is a passphrase (AG61)
        return False
    if re.match(r"\d{4}-\d{2}-\d{2}", v) or re.fullmatch(r"[A-Za-z]+(?:-[A-Za-z]+)+", v):
        return False  # a date or a hyphenated English compound (`out-of-date`, `case-sensitive`) is prose
    classes = sum(bool(re.search(p, v)) for p in (r"[a-z]", r"[A-Z]", r"[0-9]", r"[^A-Za-z0-9]"))
    return classes >= 2 and (re.search(r"[0-9]", v) is not None or re.search(r"[^A-Za-z0-9.-]", v) is not None or re.search(r"[a-z][A-Z]", v) is not None)


def _sub_value(rx, out: str, group: int, *, ok=None) -> str:
    """Replace group `group` of every match of rx; `ok(value)` may veto a match (placeholders, code)."""
    def f(m):
        v = m.group(group)
        if _PLACEHOLDER_RE.match(v) or not any(ch.isalnum() for ch in v) or "[REDACTED" in v or (ok is not None and not ok(v)):
            return m.group(0)
        q = _kv_quote(v)
        s, e = m.span(group)
        return m.group(0)[:s - m.start()] + f"{q}[REDACTED]{q}" + m.group(0)[e - m.start():]
    return rx.sub(f, out)


def _k8s_mask(m: "re.Match[str]") -> str:
    v = m.group(2)
    if _kv_skip("k", v, "=", None, 0) or "[REDACTED" in v:
        return m.group(0)
    q = _kv_quote(v)
    return m.group(1) + q + "[REDACTED]" + q


def _cookie_mask(m: "re.Match[str]") -> str:
    def pair(pm):
        name = pm.group(1)[:-1].lower()
        val = pm.group(2)
        plain = (re.fullmatch(r"[a-z]+(?:[_.-][a-z]+)*|[A-Za-z]+/[A-Za-z_]+", val) and not re.search(r"(?i)sess|(?:^|[_.-])sid(?:$|[_.-])|auth|token|jwt|key|login|secret|pass|remember|csrf|xsrf|user", name))
        return pm.group(0) if name in _COOKIE_ATTRS or "[REDACTED" in val or _PLACEHOLDER_RE.match(val) or plain else pm.group(1) + "[REDACTED]"  # `theme=dark`, `tz=Europe/Berlin`, `<session-id>` (AG62)
    return m.group(1) + _COOKIE_PAIR_RE.sub(pair, m.group(2))


def _md_columns(out: str) -> str:
    """`| user | password |` tables: mask the cells under a header whose name is a secret name."""
    lines = out.split("\n")
    i = 0
    while i < len(lines) - 1:
        hdr, sep = lines[i], lines[i + 1]
        if hdr.lstrip().startswith("|") and re.match(r"\s*\|[ \t:|-]*-[ \t:|-]*\r?$", sep):
            cells = [c.strip(" \t`*\r") for c in hdr.strip().strip("|").split("|")]
            idx = [k for k, c in enumerate(cells) if len(c) <= 60 and re.fullmatch(_ANY_NAME, c, re.I)]
            j = i + 2
            while j < len(lines) and lines[j].lstrip().startswith("|"):
                if idx:
                    parts = lines[j].split("|")
                    for k in idx:
                        if k + 1 < len(parts) - 1:
                            v = parts[k + 1].strip(" \t`*\r")
                            if len(v) >= 6 and " " not in v and "[REDACTED" not in v and not _PLACEHOLDER_RE.match(v) and (_secretish(v) or re.search(r"\d", v)):
                                parts[k + 1] = parts[k + 1].replace(v, "[REDACTED]")
                    lines[j] = "|".join(parts)
                j += 1
            i = max(j, i + 1)
        else:
            i += 1
    return "\n".join(lines)


def _extra_redact(out: str) -> str:
    """The AG55 rules, each gated by a cheap substring test so ordinary text pays almost nothing."""
    if "value" in out:
        out = _K8S_RE.sub(_k8s_mask, out)
    if "login" in out.lower():
        out = _sub_value(_NETRC_RE, out, 2, ok=lambda v: not _doc_value(v) and "@" not in v)  # `password <your-password>`, an anonymous FTP e-mail (AG62)
    if ":" in out:
        out = _sub_value(_PGPASS_RE, out, 2)
    if "_" in out:
        out = _sub_value(_NPMRC_RE, out, 3)
    if "<" in out:
        out = _sub_value(_XML_ATTR_RE, out, 2)
        out = _sub_value(_XML_ENTRY_RE, out, 2)
    if "=>" in out:
        out = _sub_value(_ROCKET_RE, out, 2)
    if "\\" in out:
        out = _sub_value(_ESC_JSON_RE, out, 2)
    if "**" in out or "__" in out:
        out = _sub_value(_MD_BOLD_RE, out, 2)
    if "|" in out:
        out = _sub_value(_MD_ROW_RE, out, 2, ok=lambda v: _secretish(v) or bool(re.search(r"\d", v)))
        out = _md_columns(out)
    if "ookie" in out:
        out = _COOKIE_RE.sub(_cookie_mask, out)
    if "hooks." in out or "webhooks" in out:
        out = _WEBHOOK_RE.sub(lambda m: m.group(0) if (_PLACEHOLDER_RE.search(m.group(2) or m.group(4) or "") or re.fullmatch(r"[xX]+", m.group(2) or m.group(4) or "")) else (m.group(1) or m.group(3)) + "[REDACTED]", out)
    if "=" in out:
        out = _AZURE_RE.sub(lambda m: m.group(0) if _PLACEHOLDER_RE.search(m.group(2) or m.group(4) or "") else (m.group(1) or m.group(3)) + "[REDACTED]", out)  # `AccountKey=xxxx` is a placeholder (MN49)
    if " -p" in out:
        out = _MYSQL_P_RE.sub(r"\1[REDACTED]", out)
        out = _DOCKER_P_RE.sub(r"\1[REDACTED]", out)
        out = _SSHPASS_RE.sub(r"\1[REDACTED]", out)
    out = _TOKEN_SHAPES_RE.sub("[REDACTED:provider-key]", out)
    out = _ag62(out)
    out = _sub_value(_PROSE_RE, out, 2, ok=_secretish)
    out = _sub_value(_PROSE_I18N_RE, out, 2, ok=_secretish)
    out = _sub_value(_PROSE_I18N2_RE, out, 2, ok=_secretish)  # the word orders of Thai, Hindi, Arabic, Hebrew, Persian, bare Russian, German `lautet`, Portuguese (MN52)
    return out


_ARG = r"(['\"]?)(?!-)([^\s'\"|;&]{3,})"
_AG62 = (  # (needle, regex with the value in group 2): command-specific shapes the key=value rule cannot see
    ("-pass", re.compile(r"(?<!\S)(-pass(?:in|out)[ \t=]+(?:pass:)?['\"]?|-pass[ \t=]+pass:['\"]?)(?!(?:env|file|fd):|stdin)([^\s'\"|;&]{3,})")),
    ("-pass ", re.compile(r"(?<!\S)(-pass[ \t=]+pass:['\"]?)([^\s'\"|;&]{3,})")),
    ("openssl", re.compile(r"(\bopenssl[ \t]+enc\b[^\n|;&]{0,120}?[ \t]-k[ \t]+['\"]?)(?!-)([^\s'\"|;&]{3,})")),
    ("pass", re.compile(r"(?<!\S)(-(?:(?:src|dest)?(?:store|key)pass)[ \t]+['\"]?)(?!-)([^\s'\"|;&]{3,})")),
    ("-u", re.compile(r"(?<!\S)(-U[ \t]*['\"]?[^\s%'\"]{1,64}%)([^\s'\"|;&]{3,})")),
    (("sqlcmd", "bcp", "zip", "mosquitto"), re.compile(r"(\b(?:sqlcmd|bcp|zip|unzip|zipcloak|mosquitto_pub|mosquitto_sub)\b[^\n|;&]{0,200}?[ \t]-P[ \t]*['\"]?)(?!-)([^\s'\"|;&]{3,})")),
    (("7z", "rar"), re.compile(r"(\b(?:7z|7za|7zr|rar|unrar)\b[^\n|;&]{0,200}?[ \t]-p['\"]?)(?!-)([^\s'\"|;&]{3,})")),
    (("mongo", "cqlsh", "psexec"), re.compile(r"(\b(?:mongo|mongosh|mongodump|mongorestore|mongoexport|mongoimport|cqlsh|psexec)\b[^\n|;&]{0,200}?[ \t](?:-p|--password)[ \t=]*['\"]?)(?!-)([^\s'\"|;&]{3,})")),
    ("redis-cli", re.compile(r"(\bredis-cli\b[^\n|;&]{0,200}?[ \t](?:-a|--pass)[ \t]+['\"]?)(?!-)([^\s'\"|;&]{3,})")),
    ("influx", re.compile(r"(\binflux\b[^\n|;&]{0,200}?[ \t]-password[ \t]+['\"]?)(?!-)([^\s'\"|;&]{3,})")),
    ("ldap", re.compile(r"(\bldap(?:search|add|modify|delete|passwd)\b[^\n|;&]{0,200}?[ \t]-w[ \t]*['\"]?)(?!-)([^\s'\"|;&]{3,})")),
    ("htpasswd", re.compile(r"(\bhtpasswd\b[ \t]+-[A-Za-z]*b[A-Za-z]*[ \t]+(?:-\w+[ \t]+)*(?:\S+[ \t]+){1,2})(?!-)(\S{3,})")),
    ("vault", re.compile(r"(\bvault[ \t]+login[ \t]+(?:-\w+(?:=\S+)?[ \t]+)*(?:token=)?)(?!-)(\S{6,})")),
    ("aws", re.compile(r"(\baws[ \t]+configure[ \t]+set[ \t]+(?:--profile[ \t]+\S+[ \t]+)?(?:\w+\.)?(?:aws_)?(?:secret_access_key|session_token)[ \t]+['\"]?)(\S{6,})")),
    ("net use", re.compile(r"(?i)(\bnet[ \t]+use\b[^\n|;&]{0,120}?\\\\\S+[ \t]+)(?![/-])([^\s/'\"]{3,})")),
    ("/pass:", re.compile(r"(?i)(/pass:['\"]?)([^\s'\"]{3,})")),
    ("schtasks", re.compile(r"(?i)(\bschtasks\b[^\n|;&]{0,200}?[ \t]/rp[ \t]+['\"]?)([^\s'\"]{3,})")),
    ("rabbitmqctl", re.compile(r"(\brabbitmqctl[ \t]+(?:add_user|change_password|authenticate_user)[ \t]+\S+[ \t]+['\"]?)([^\s'\"]{3,})")),
    ("chpasswd", re.compile(r"(\becho[ \t]+['\"]?[A-Za-z0-9_.-]+:)([^\s'\"|]+)(?=['\"]?[ \t]*\|[ \t]*(?:sudo[ \t]+)?chpasswd)")),
    ("config set", re.compile(r"(?i)(\b(?:npm|yarn|pnpm)[ \t]+config[ \t]+set[ \t]+\S*_(?:authToken|password|auth)[ \t]+['\"]?)([^\s'\"]{6,})")),
    ("setx", re.compile(r"(?i)(\bsetx[ \t]+\w*(?:password|token|secret|api_?key)\w*[ \t]+['\"]?)([^\s'\"]{6,})")),
    ("convertto-securestring", re.compile(r"(?i)(ConvertTo-SecureString[ \t]+['\"]?)([^\s'\"]{3,})(?=['\"]?[ \t]+-AsPlainText)")),
    ("jdbc:oracle", re.compile(r"(jdbc:oracle:thin:[^/\s:@]+/)([^@\s]+)(?=@)")),
    ("setpassword", re.compile(r"(?i)(\.set(?:Password|Passwd|Token|Secret|ApiKey|AccessToken|ClientSecret)\(\s*['\"])([^'\"\n]{4,})(?=['\"])")),
    ("usernamepasswordcredentials", re.compile(r"(UsernamePasswordCredentials\(\s*[^,()]{1,64},\s*['\"])([^'\"\n]{4,})(?=['\"])")),
)


def _ag62(out: str) -> str:
    low = out.lower()
    for needle, rx in _AG62:
        if any(n in low for n in ((needle,) if isinstance(needle, str) else needle)):
            out = _sub_value(rx, out, 2, ok=lambda v: not _doc_value(v))
    if "AUTH " in out:
        out = _sub_value(re.compile(r"(\bAUTH[ \t]+)([^\s'\"]{6,})"), out, 2, ok=_secretish)  # redis-cli AUTH V
    return out


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
    out = _sub_value(_BEARER_RE, out, 2, ok=_bearer_ok)
    out = _TELEGRAM_RE.sub("[REDACTED:telegram-token]", out)
    out = _YAML_BLOCK_RE.sub(_yaml_block_mask, out)
    out = _XML_RE.sub(r"\1[REDACTED]\4", out)
    out = _kv_redact(out)
    out = _sub_value(_CLI_SECRET_RE, out, 2, ok=lambda v: not _doc_value(v))  # `--token TOKEN`, `--token=<value>` in help text (AG62)
    out = _sub_value(_BASIC_AUTH_RE, out, 2, ok=lambda v: not _doc_value(v) and not _CODE_VALUE_RE.match(v))  # `Basic <credentials>`, `Basic base64(user:pass)`
    out = _sub_value(_CURL_USER_RE, out, 2, ok=lambda v: not _doc_value(v))  # `curl -u user:password`, `-u $USER:$PASS`
    out = _sub_value(_URL_CRED_RE, out, 2, ok=lambda v: not _doc_value(v) and v.lower() not in _DOC_WORDS)  # `postgres://user:password@host`, `user:${PW}@`
    out = _PROVIDER_KEY_RE.sub("[REDACTED:provider-key]", out)
    out = _pem_redact(out)
    out = _extra_redact(out)
    return out
