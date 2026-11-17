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

_BEARER_RE = re.compile(r"(?i)(Bearer[ \t]*(?:\r?\n[ \t]+)?)([A-Za-z0-9\-_.=]{12,})")  # `[ \t]`: `bearer\ntoken.expiry.seconds=900` lost the next line (AG62); a folded header continues on an indented line (MN55)


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
# AG91: Danish, Norwegian (bokmal and nynorsk), Swedish, Finnish, Icelandic and Faroese key names in their bare, definite
# and plural forms (`kodeord: V`, `nøglen: V`, `API-nøgle: V`, `hemmelighed: V`, `lykilorð: V` leaked); compounds such as
# `klienthemmelighed` and `db_kodeord` come through the camel head and the prefix segments
# AG91c: a decomposed letter (a macOS paste: `o` + U+0308) matches too
_NORDIC_NAMES = (r"|kodeord(?:et|ene)?|adgangskode(?:n|r|rne)?|passord(?:et|ene)?|l[øo]sen(?:et)?|l(?:ö|o\u0308?)senord(?:et|en)?"
                 r"|n[øo]gle(?:n|r|rne)?|n[øo]kkel(?:en)?|n[øo]kler(?:ne)?|n[øo]klene|nykel(?:en)?|nyklar(?:ne)?|nyckel(?:n)?|nycklar(?:na)?"
                 r"|hemmelighed(?:en|er|erne)?|hemmelighet(?:en|er|ene)?|hemmelegheit(?:a|er|ene)?|l[øo]yndom(?:en|ar|ane)?|hemlighet(?:en|er|erna)?"
                 r"|pin[_ -]?kode(?:n|r|rne)?|pin[_ -]?kod(?:en|er|erna)?|pin[_ -]?koodi|pin[_ -]?n(?:ú|u\u0301?)mer(?:i[ðd])?|pin[_ -]?kota(?:n)?"
                 r"|adgangsfrase(?:n)?|passfrase(?:n)?|l(?:ö|o\u0308?)senfras(?:en)?|tunnuslause|tunnussana|salasana(?:n|t)?|salaisuus|avain"
                 r"|lykilor[ðd](?:i[ðd]|in)?|a[ðd]gangsor[ðd](?:i[ðd])?|loynior[ðd](?:i[ðd])?|lykill(?:inn)?|lykil(?:in)?"
                 r"|leyndarm(?:á|a\u0301?)l(?:i[ðd])?|loyndarm(?:á|a\u0301?)l(?:i[ðd])?|adgangstoken(?:et)?|tilgangstoken(?:et)?|(?:å|a\u030a?)tkomsttoken(?:et)?|p(?:ä|a\u0308?)(?:ä|a\u0308?)sytunnus|tokenet|tokeni")
_KV_NAMES = (r"(?:api[_ -]?key|apikey|secret|token|password|passwd|pwd|pass|client[_ -]?secret|private[_ -]?key"
             r"|secret[ ]key|access[_ -]?key|auth[_-]?token|session[_-]?key|bearer|csrf[_-]?token|credentials?|passphrase"
             r"|(?:master|encryption|decryption|signing|hmac|jwt|license)[_-]?key"  # AG62: RAILS_MASTER_KEY=V leaked
             r"|パスワード|トークン|シークレット|秘密鍵|秘密|鍵|APIキー"
             r"|비밀번호|비밀|비밀키|시크릿|토큰|API키|密码|密碼|口令|令牌|密钥|密鑰|暗証番号"
             r"|รหัสผ่าน|รหัสลับ|โทเคน|คีย์[ _]API|กุญแจ[ _]API|पासवर्ड|टोकन|गुप्त[ _]कुंजी|एपीआई[ _]कुंजी|कूटशब्द"
             r"|API-Schlüssel|Geheimnis|מפתח[ _]API|קוד[ _]גישה|توکن|کلید[ _]API|رمز[\u200c _]عبور|گذرواژه|رمز|سرّ?"  # MN52
             r"|пароль|токен|секрет|passwort|kennwort|mot[ _]de[ _]passe|contraseña|contrasena|senha|şifre"  # AG55: Russian, German, French, Spanish, Portuguese, Turkish
             r"|hasło|haslo|jelszó|jelszo|nenosiri|mật[ _]khẩu|mat[ _]khau|kata[ _]sandi|κωδικός[ _]πρόσβασης|συνθηματικό"  # AG68: Polish, Hungarian, Swahili, Vietnamese, Indonesian, Greek
             r"|رمز[ _]عبور|گذرواژه|کلمه[ _]عبور"
             + _NORDIC_NAMES +
             r"|clave[ _]API|clave[ _]de[ _]API|API[ _-]?ключ|ключ[ _]API|API[ _]anahtar[ıi]|API[ ]키|API[ _]कुंजी|الرمز[ _]السري"  # MN74: two-word key names (`API key: V`, `secret key: V`, `clave API=V`, `API-ключ=V`) leaked
             + _MARKED_NAMES + ")")  # Thai, Hindi, and the mark-tolerant Arabic/Hebrew names (AG52, MN46)
_KV_SUFFIX = r"(?:[_-]?(?:key|token|secret|value|str|string|id|base|hash|v?\d{1,3})){0,2}"


# ---- AG95: the key-name alternation as a prefix trie with exact case classes ---------------------------------------
# Under (?i) the regex engine enters every one of the ~150 key-name alternatives at every word start and at every
# backtrack point of the camel head; a case-insensitive literal is never skipped by its first character. AG91 added
# ~65 Nordic names and the key=value pass grew 65% (SEX-027). The same alternation is rewritten as a trie: each branch
# opens with an exact case class (`[kKK]`), which the engine tests in C before entering the branch, and the rest of
# the branch keeps (?i). Alternatives are merged under a shared first character only when every group they move ahead
# of starts with a disjoint set of characters, so the leftmost alternative that matches is the same one as before.
_CASED_LIMIT = 0x20000  # every letter with case lies below U+20000; the planes above hold CJK ideographs and private use
_NAMES_SLOT = "\x00names\x00"  # where the key-name alternation goes in the two patterns built on first use


def _top_alts(body: str) -> list[str]:
    """Split a regex alternation body at its top-level bars (classes, groups and escapes respected)."""
    alts, cur, depth, i = [], [], 0, 0
    while i < len(body):
        ch = body[i]
        if ch == "\\":
            cur.append(body[i:i + 2])
            i += 2
            continue
        if ch == "[":
            j = i + 1 + (body[i + 1:i + 2] == "]")
            while body[j] != "]":
                j += 2 if body[j] == "\\" else 1
            cur.append(body[i:j + 1])
            i = j + 1
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif ch == "|" and depth == 0:
            alts.append("".join(cur))
            cur = []
            i += 1
            continue
        cur.append(ch)
        i += 1
    alts.append("".join(cur))
    return alts


def _group_end(alt: str, k: int) -> int:
    """The index just past the `)` that closes the group opened at alt[k - 1]."""
    depth = 1
    while depth:
        c = alt[k]
        if c == "\\":
            k += 2
            continue
        if c == "[":
            k += 1 + (alt[k + 1:k + 2] == "]")
            while alt[k] != "]":
                k += 2 if alt[k] == "\\" else 1
        elif c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
        k += 1
    return k


def _distribute(alt: str) -> list[str]:
    """`(?:a|b)X` -> `aX`, `bX` (the same order, so the same leftmost match); anything else stays whole."""
    if not alt.startswith("(?:"):
        return [alt]
    k = _group_end(alt, 3)
    tail = alt[k:]
    if tail[:1] in ("?", "*", "+", "{"):
        return [alt]
    return [a + tail for a in _top_alts(alt[3:k - 1])]


def _case_sets(chars) -> dict:
    """For each character, every character that (?i) matches it, asked of the regex engine itself (Greek symbol forms,
    U+1C80 Cyrillic, the Kelvin sign, dotless and dotted i all differ from lower()/upper())."""
    import array
    space = array.array("I", range(_CASED_LIMIT)).tobytes().decode("utf-32-le", "surrogatepass")
    chars = sorted(set(chars))
    found = "".join(sorted(set(re.findall("(?i)[" + "".join(re.escape(c) for c in chars) + "]", space)) | set(chars)))
    return {c: frozenset(re.findall("(?i)" + re.escape(c), found)) for c in chars}


def _case_trie(alts: list[str], sets: dict) -> str:
    """A case-sensitive regex equivalent to `(?i:alt1|alt2|...)`, branching on exact first-character classes."""
    groups: list = []  # [key, first char, rests]; key None = any first character (never merged, never jumped)
    for a in alts:
        c = a[:1]
        key = None
        if c and c not in "([\\.^$|)" and a[1:2] not in ("?", "*", "+", "{"):
            key = sets[c]
        target = None
        if key is not None:
            for g in reversed(groups):  # join the latest group with this key unless a group in between could match too
                if g[0] == key:
                    target = g
                    break
                if g[0] is None or g[0] & key:
                    break
        if target is not None:
            target[2].append(a[1:])
        elif key is not None:
            groups.append([key, c, [a[1:]]])
        else:
            groups.append([None, "", [a]])
    out = []
    for key, c, rests in groups:
        if key is None:
            out.append("(?i:" + rests[0] + ")")
            continue
        cls = re.escape(c) if len(key) == 1 else "[" + "".join(re.escape(x) for x in sorted(key)) + "]"
        if len(rests) == 1:
            out.append(cls + ("(?i:" + rests[0] + ")" if rests[0] else ""))
        else:
            out.append(cls + "(?:" + _case_trie(rests, sets) + ")")
    return "|".join(out)


def _fast_names(names: str) -> str:
    """`(?:a|b|...)` (matched under (?i)) -> the same set of matches, as a first-character trie."""
    alts = [d for a in _top_alts(names[3:-1]) for d in _distribute(a)]
    return "(?-i:" + _case_trie(alts, _case_sets(ch for a in alts for ch in a)) + ")"


_KV_PAIR = None


def _kv_res():
    """(_KV_RE, _K8S_RE), built on the first redact(): the trie and its case classes cost ~40 ms once."""
    global _KV_PAIR
    if _KV_PAIR is None:
        names = _fast_names(_KV_NAMES)
        _KV_PAIR = (re.compile(_KV_SRC.replace(_NAMES_SLOT, names)), re.compile(_K8S_SRC.replace(_NAMES_SLOT, names), re.M))
    return _KV_PAIR


def __getattr__(name: str):
    if name in ("_KV_RE", "_K8S_RE"):
        return _kv_res()[name == "_K8S_RE"]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
# Exact key NAMES: a bounded prefix of up to eight `_`/`-` segments (`MY_APP_PROD_EU_WEST_DB_`), an
# optional camelCase head (`dbPassword`, `AccessToken`), the name, and up to two short suffixes
# (`_KEY_BASE`, `_HASH`, `_v2`). The open `[A-Za-z0-9_-]*name[A-Za-z0-9_-]*` form was quadratic and
# matched `tokenizer`, `max_tokens`, `secret_santa`, `password_policy` (MN31); the first bounded form
# missed camelCase, longer prefixes and `bearer`/`csrftoken` (AG44). Korean and Chinese key names
# join the Japanese ones (AG46). A quoted value is masked whole (spaces inside the quotes stay
# inside the mask); an unquoted value runs to the next whitespace, `&` included (AG43: a password
# with `&` leaked its tail; a query-string tail is over-masked instead). The lookbehind limits the
# scan to word starts, so a 1 MB blob costs one attempt per word, not per character.
_KV_SRC = (
    r"(?i)(?<![A-Za-z0-9_\-\u0590-\u05ff\u0600-\u06ff])(-{0,2}(?:[A-Za-z0-9]{1,32}[_-]){0,16}(?:[a-z0-9]{1,32})?" + _NAMES_SLOT + _KV_SUFFIX + r")"  # `--secret-key=`, `-Dpassword=`, `-ApiKey:` start at their dashes (MN43); up to sixteen segments (MN48: a nine-segment slug leaked)
    r"(?![A-Za-z0-9\u00c0-\u024f])"
    r"(['\"]?\]?\s*[=:：＝]\s*(?:r(?=['\"]))?(?:(?:&[\w-]{1,64}|!![a-z]{1,16})[ \t]+)?)"  # AG62: the environ-subscript assignment `environ['DB_PASS...'] = 'V'` and `params[:password] = V` leaked; AG68: a YAML anchor or core tag before the value (`password: &pw V`, `password: !!str V`)
    r"(\"\"\"[^\n]{4,}?\"\"\"|'''[^\n]{4,}?'''"  # AG68: a TOML multi-line basic or literal string on one line leaked whole
    r"|\"(?:[^\"\\\n]|\\[^\n]){4,}\"|\"[^\"\n]{4,}\""  # AG68: an escaped quote inside a JSON string value (`\"`) ended the match early and leaked the value
    r"|\"[^\"\n]+(?:\r?\n[ \t]+[^\"\n]*){1,8}\""  # AG68: a YAML double-quoted scalar folded over indented lines leaked its tail
    r"|'[^'\n]{4,}'|[\"']?[^\s'\"]{6,})"
)
_CLI_SECRET_RE = re.compile(r"(?i)(--(?:[a-z]+-){0,3}(?:token|password|passwd|api[_-]?key|secret|passphrase|client[_-]?secret)[= ])(\S{6,})")  # --db-password, --api-token (MN37)
_BASIC_AUTH_RE = re.compile(r"(?i)(authorization:\s*(?:basic|token|apikey|api-key)\s+)(\S{8,})")
_CURL_USER_RE = re.compile(r"(?i)((?:^|\s)(?:-u|--user)\s+[^\s:]+:)(?!//)(\S+)")  # the `-u user:password` form of curl (MN37); `redis-cli -u redis://:V@host` is a URL, masked by the URL rule without eating the host (AG68)
_YAML_BLOCK_RE = re.compile(r"(?im)^([ \t]*(?:-[ \t]+)*(?:[A-Za-z0-9]{1,32}[_-]){0,4}(?:password|passwd|secret|token|api[_-]?key|private[_-]?key|passphrase|credentials?)[ \t]*:[ \t]*[>|][-+0-9]*[ \t]*\r?\n)((?:[ \t]*\r?\n|[ \t]+\S[^\n]*(?:\n|\Z))+)")  # password: > / | block scalars; `- password: |`, CRLF and blank lines before the body (MN37, AG55)
_XML_RE = re.compile(r"(?i)(<(password|passwd|secret|token|api[_-]?key)>)(<!\[CDATA\[.*?\]\]>|[^<\n]{4,})(</\2>)", re.S)  # a CDATA section too (AG68)
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
    for triple in ('"""', "'''"):
        if len(v) > 6 and v.startswith(triple) and v.endswith(triple):
            return triple  # AG68: TOML `"""V"""` keeps its triple quotes
    return v[0] if v and v[0] in "\"'" and len(v) > 1 and v[-1] == v[0] else ""


_ENVREF_RE = re.compile(r"^\$[A-Z_][A-Z0-9_]*$|^!!?[A-Za-z][A-Za-z/_.-]*$")
_STATUS_RE = re.compile(r"(?i)[\[(<]?(?:required|optional|expired|missing|invalid|hidden|redacted|undefined|string|str|secretstr|unset|empty|none|null|incorrect|wrong|rejected|refreshing|n/a"
                        r"|pending|revoked|rotated|changed|updated|renewed|unchanged|deprecated|disabled|regenerated|pendiente|revocada|revocado|geändert|widerrufen|révoqué|révoquée|отозван|изменён|изменен"  # AG85: the English siblings of MN58's Turkish `değiştirildi`/`yenilendi` (`API key: revoked`, `token: pending` were masked)
                        r"|abgelaufen|ungültig|fehlt|erforderlich|falsch|leer|expiré|expirée|invalide|manquant|requis|incorrect|vide|expirada|expirado|caducada|caducado|inválida|inválido|obrigatória|requerida|incorreta|incorrecta|vazia|vacía"
                        r"|истёк|истек|истекла|неверный|неверно|недействителен|отсутствует|обязателен|пустой|süresi[ \t]doldu|geçersiz|boş|değiştirildi|güncellendi|sıfırlandı|ayarlandı|yenilendi"
                        r"|påkrævet|påkrævede|udløbet|ugyldig|ugyldigt|mangler|forkert|tomt|ændret|nulstillet|tilbagekaldt|skjult|valgfri|valgfrit|deaktiveret|fornyet"  # AG91: Danish
                        r"|påkrevd|påkravd|utløpt|utgått|manglar|feil|endret|endra|tilbakestilt|tilbakekalt|valfri|deaktivert"  # Norwegian bokmal and nynorsk
                        r"|obligatorisk|obligatoriskt|krävs|utgånget|utgången|ogiltigt|ogiltig|saknas|felaktigt|felaktig|ändrat|ändrad|återställt|återkallad|återkallat|dolt|dold|valfritt"  # Swedish
                        r"|vaadittu|pakollinen|vanhentunut|virheellinen|puuttuu|väärä|tyhjä|muutettu|nollattu|peruttu|piilotettu|valinnainen"  # Finnish
                        r"|krafist|útrunnið|ógilt|ógildur|vantar|rangt|tómt|breytt|endurstillt|falið|valfrjálst|kravt|útgingið|ógyldugt|skeivt|broytt)[\])>]?[;,.]*")  # Icelandic, Faroese  # the status words of the other Latin and Cyrillic languages too (MN58)
_WORDISH_RE = re.compile(r"[^\d\s!-/:-@\[-`{-~]+[,.;:!?]?")
_DOC_WORDS = frozenset({"password", "pass", "passwd", "pwd", "secret", "token", "user", "username", "yourpassword", "your_password", "changeme", "arg", "args", "file", "env", "fd", "stdin", "anonymous", "guest", "x-oauth-basic"})


def _doc_value(v: str) -> bool:
    """A documentation placeholder after a flag, a `user:` or a `Basic`: `<password>`, `${PW}`, `$PASS`, `TOKEN]`, `password` (AG62)."""
    v = v.strip("\"'")
    if not v:
        return True
    _meta = re.fullmatch(r"([A-Z][A-Z_]*)[\])>]*", v)
    return bool(_PLACEHOLDER_RE.match(v) or re.fullmatch(r"\$[A-Za-z_]\w*|\$\{.*", v) or (_meta and ("_" in _meta.group(1) or len(_meta.group(1)) <= 5 or _meta.group(1).lower() in _DOC_WORDS)) or v.lower() in _DOC_WORDS)  # `TOKEN`, `API_KEY]`, `PASSWORD]` are metavariables; `HUNTERTWOXYZ` is a secret (MN55)


_PLACEHOLDER_RE = re.compile(r"<[^<>\n]{0,60}(?:>|$)|\{\{|\$\{|%[A-Za-z_]+%|\[REDACTED|^[xX]{4,}$|\.{3,}$")  # a value that is only x/X (AG61); a real key ending in XXXX is a key (MN55)
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
    if _STATUS_RE.fullmatch(v) or (vq != v and _STATUS_RE.fullmatch(vq)):  # `"api_key": "revoked"` (MN79)
        return True  # AG62: `token: expired`, `password: required`, `token: string;`, `password: (hidden)`: a status or a type, not a value
    if _ENVREF_RE.match(vq):
        return True  # AG62: `api_key: $API_KEY`, a YAML tag `password: !vault |`
    if sep.startswith("]") and not (text is not None and start > 0 and text[start - 1] == ":") and re.fullmatch(r"[A-Za-z_][A-Za-z_]*", v):  # only an identifier without digits is code (`cache[token] = value_from_somewhere`); `creds[password]=S3cr3t-Pa55w0rd` is a secret (MN55)
        return True  # AG62: `cache[token] = value` is code; only a quoted key or a Ruby symbol (`params[:password]`) counts
    if text is not None and start >= 3 and text[start - 3:start] == "://":
        return True  # AG62: `https://token:x@host/` is URL userinfo, masked by the URL rule without eating the host
    if " " in sep and "=" in sep and re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+", v) and not re.search(r"\d", v):
        return True  # AG62: `secret = config.secret`, `api_key = settings.API_KEY` (attribute access in code)
    if v[0] not in "\"'" and len(v) >= 3 and not v.isascii() and not re.search(r"[A-Za-z]", v) and not (re.search(r"\d", v) and re.search(r"[\u0370-\u03ff\u0400-\u04ff\u0530-\u058f]", v)):  # a label sentence in CJK/Thai/Hindi is prose (AG62); a Cyrillic/Greek/Armenian word with a digit is a secret: `Тайна2024секрет` (MN55)
        return True  # AG62: `密码：必须至少包含八个字符` (CJK, Thai, Hindi label sentences); a secret holds ASCII
    if "=" not in sep and text is not None and end is not None and v.islower() and _WORDISH_RE.fullmatch(v) and re.match(r"[ \t]+[^\d\s!-/:-@\[-`{-~]{2,}", text[end:end + 40]):  # a lowercase word only: `password: Dragonfly for both boxes` is a secret (MN55)
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
    kv_re = _kv_res()[0]
    out = kv_re.sub(_kv_mask, out)
    folded = out.translate(_FULLWIDTH)
    if folded != out:
        pieces = []
        pos = 0
        for m in kv_re.finditer(folded):
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
_PEM_BODY_RE = re.compile(r"\s*(?:[A-Za-z0-9+/=]{8,}|[A-Za-z-]+: )")  # any armor header line counts: `Comment:`, `Hash:` (MN55: a cut-off GPGTools key leaked)


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
_NAME_ASCII = (r"(?:api[_ -]?key|apikey|secret|token|password|passwd|pwd|passphrase|client[_ -]?secret|private[_ -]?key"
               r"|access[_ -]?key|auth[_-]?token|credentials?)")
_NAME_FULL = r"(?:(?:[A-Za-z0-9]{1,32}[_.-]){0,4}" + _NAME_ASCII + r"(?:[_-]?[A-Za-z0-9]{1,8})?)"  # dotted names too: `spring.datasource.password` (MN49)
_ANY_NAME = r"(?:" + _NAME_FULL + r"|" + _KV_NAMES[3:-1] + r")"
_VAL = r"(\"[^\"\n]{4,}\"|'[^'\n]{4,}'|[^\s'\"<>]{6,})"
# Kubernetes / compose / CI env lists: `- name: API_TOKEN` then `value: V` on the next line
_K8S_SRC = (r"(?i)(^[ \t]*-?[ \t]*name[ \t]*:[ \t]*[\"']?(?:[A-Za-z0-9]{1,32}[_.-]){0,8}(?:[a-z0-9]{1,32})?" + _NAMES_SLOT + _KV_SUFFIX
             + r"[\"']?[ \t]*\r?\n[ \t]*value[ \t]*:[ \t]*)" + _VAL)  # compiled with re.M by _kv_res()
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
_MYSQL_P_RE = re.compile(r"((?<![\w.-])(?:sudo[ \t]+)?(?:mysql|mysqldump|mysqladmin|mariadb|mysqlsh)\b[^\n|;&]{0,200}?[ \t]-p)([^\s\-][^\s]{2,})", re.M)  # the command word itself (not `*.mysql` in a find: MN49) after anything but a word, dot or dash: `RUN mysql`, `/usr/bin/mysql`, `"mysql ...`, `C:\\mysql\\bin\\mysql` (AG61, MN55)
_DOCKER_P_RE = re.compile(r"(\b(?:docker|podman|nerdctl)[ \t]+login\b[^\n|;&]{0,200}?[ \t](?:-p|--password)[ =]+)([^\s\-]\S*)")
_SSHPASS_RE = re.compile(r"(\bsshpass[ \t]+(?:-\w+[ \t]+)*-p[ \t]*)(?!-)(\S+)")
_TOKEN_SHAPES_RE = re.compile(r"\b(eyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}|ya29\.[A-Za-z0-9_-]{20,}|npm_[A-Za-z0-9]{36}|SG\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}|dop_v1_[a-f0-9]{64}|pypi-[A-Za-z0-9_-]{50,}|whsec_[A-Za-z0-9]{20,}|rk_live_[A-Za-z0-9]{10,}|shp(?:at|ss|ca)_[a-f0-9]{32})")
# prose: `my password is V` / `the token was V` where V looks like a secret (8+ chars, two or more character classes)
# AG68: the value may sit in curly or CJK quotes, hold a soft hyphen or zero-width space (a paste from a web page or chat),
# or be a double-quoted passphrase with spaces
_PQ = r"[“「『‘]?"
_PV = r"(\"[^\"\n]{4,64}\"|(?=(?-i:[!-~]))(?:(?-i:[!-~])|[­​-‍⁠﻿]){8,})"  # (?-i:): under (?i) Turkish ı and İ matched [!-~] (MN62)
_PROSE_RE = re.compile(r"(?i)\b((?:(?:my|your|the|his|her|their|our|new|old|temp(?:orary)?|root|admin|wifi|login)[ \t]+){1,2}(?:password|passcode|passphrase|api[ \t]key|access[ \t]token|token|(?:client[ \t]+)?secret)[ \t]+(?:is|was|are|will[ \t]+be|=|:)[ \t]+" + _PQ + ")" + _PV)
# AG68: `I set the password to V`, `password changed to V`, `the token has been rotated to V`
_PROSE_SET_RE = re.compile(r"(?i)\b((?:password|passcode|passphrase|api[ \t]key|access[ \t]token|token|secret)[ \t]+(?:(?:has|was|is)[ \t]+(?:been[ \t]+)?|got[ \t]+)?(?:set|changed|reset|updated|rotated)[ \t]+to[ \t]+" + _PQ + r"|\b(?:set|change|reset|update|rotate)[ \t]+(?:the|my|your|our|its)[ \t]+(?:\w+[ \t]+)?(?:password|passcode|passphrase|token|secret)[ \t]+to[ \t]+" + _PQ + ")" + _PV)
# AG68: Vietnamese, Indonesian/Malay, Turkish, Polish, Ukrainian, Greek, Hungarian, Swahili, Tagalog (the value must still look like a secret)
_PROSE_I18N3_RE = re.compile(
    r"(?i)("
    r"\bmật[ \t]khẩu(?:[ \t]của[ \t][^\n:=\"']{1,40}?)?[ \t]*(?:là|:)[ \t]*"                                        # Vietnamese: mat khau (cua may chu) la V
    r"|\b(?:kata[ \t]sandi|sandi)(?:[ \t](?:saya|aku|kamu|anda|kami|kita|untuk[ \t][^\n:=\"']{1,40}?))?[ \t]*(?:adalah|ialah|yaitu|:)[ \t]*"  # Indonesian/Malay
    r"|(?:\b\w+(?:nın|nin|nun|nün|ın|in|un|ün)[ \t]+)?\b(?:şifre|parola)(?:m|miz|mız|n|niz|nız|si|sı)?(?:[ \t]+(?:şu|şöyle))?[ \t]*(?:[:=][ \t]*|[ \t]+)"  # Turkish: sifrem V, sunucunun sifresi V
    r"|\b(?:(?:moje|twoje|nasze|wasze)[ \t]+)?has(?:ło|lo)(?:[ \t]+(?:do|dla|od)[ \t][^\n:=\"']{1,40}?)?[ \t]+(?:to|jest|brzmi)[ \t]*:?[ \t]*"  # Polish: moje haslo to V, haslo do serwera to V
    r"|\b(?:мій|моя|мой|наш|твій|твой|ваш)[ \t]+(?:пароль|токен)[ \t]*(?:[:–—-]|це|это|є)?[ \t]+"                        # Ukrainian/Russian: miy parol V
    r"|(?:\bο[ \t]+)?\bκωδικός(?:[ \t]+πρόσβασης)?(?:[ \t]+(?:μου|σου|μας))?(?:[ \t]+(?:για|του)[ \t][^\n:=\"']{1,40}?)?[ \t]*(?:είναι|:)[ \t]*"  # Greek
    r"|\bjelsz(?:ó|o|avam|avad|ava|avunk|avuk)[ \t]*(?:[:=]|[ \t]az|[ \t]pedig)?[ \t]+"                                  # Hungarian: a jelszavam V, a szerver jelszava: V
    r"|\bnenosiri(?:[ \t]+(?:langu|lako|letu|lake|lenu|lao))?[ \t]+(?:ni|:)[ \t]*"                                      # Swahili: nenosiri langu ni V
    r"|\b(?:ang[ \t]+)?(?:password|passcode|token)[ \t]+(?:ko|mo|namin|natin|niya|nila)[ \t]+(?:ay|:)[ \t]*"            # Tagalog: ang password ko ay V
    r")" + _PQ + _PV
)


# the same sentence in Russian, German, French, Spanish, Portuguese, Turkish, Persian, Japanese, Korean, Chinese (`mein Passwort ist V`, `パスワードは V`)
_QUALIFIED_RE = re.compile(
    r"(?i)("
    r"(?:\b(?:the|das|die|le|la|el|a|o|das|наш|мой|мій)[ \t]+)?"
    r"(?:password|passwort|kennwort|mot[ \t]de[ \t]passe|contraseña|contrasena|senha|пароль|şifre|" + _marked("كلمة", _AR_MARKS) + r"[ \t](?:" + _marked("السر", _AR_MARKS) + "|" + _marked("المرور", _AR_MARKS) + r")|رمز[\u200c \t]عبور|گذرواژه|ה?" + _marked("סיסמ", _HE_MARKS) + r"[אה]|รหัสผ่าน|पासवर्ड)"
    r"[ \t]*(?:for|of|to|für|du|de|del|della|do|da|от|для|к|до|від|сервер\w*|سرور|الجديدة|جدید|לשרת|החדשה|ใหม่|เซิร์ฟเวอร์|का|के)[^\n:=\"'“]{0,40}?"   # a qualifier: for the server, du serveur, الجديدة, ใหม่ ...
    r"[ \t]*(?:is|ist|est|es|é|هي|היא|คือ|है|это|це|є|[:=：])[ \t]*[\"'“]?"
    r")([!-~]{8,})"
)
_PROSE_I18N2_RE = re.compile(
    r"(?i)("
    r"(?:รหัสผ่าน|โทเคน)(?:ของ(?:ฉัน|เรา|ผม|คุณ|เขา|ท่าน|ดิฉัน))?คือ[ \t]*"                                        # Thai: my password is
    r"|(?:मेरा|हमारा|मेरी)?[ \t]*(?:पासवर्ड|टोकन|कूटशब्द)[ \t]+(?:है[ \t]+)?"                     # Hindi: mera password (hai) V (hai)
    r"|" + _marked("كلمة", _AR_MARKS) + r"[ \t](?:" + _marked("السر", _AR_MARKS) + "|" + _marked("المرور", _AR_MARKS) + "|" + _marked("سري", _AR_MARKS) + r")(?:[ \t]" + _marked("الخاصة", _AR_MARKS) + r"[ \t]" + _marked("بي", _AR_MARKS) + r")?[ \t]" + _marked("هي", _AR_MARKS) + r"[ \t]*[\"“]?"  # MN58: `كلمة المرور هي V`
    r"|ה?" + _marked("סיסמ", _HE_MARKS) + r"[אה][\u0591-\u05c7]*(?:[ \t]שלי)?[ \t]היא[ \t]*"                 # Hebrew: ha-sisma sheli hi V
    r"|(?:رمز[\u200c \t]عبور|گذرواژه|توکن)(?:م|ت|ش|مان|تان|شان)?(?:[ \t](?:من|شما|ما|او|تو))?[ \t]*[:：]?[ \t]+"   # Persian: ramz-e obur(-am | -e man | -e shoma)(:) V (ast) (MN58)
    r"|(?:(?:mein|das|unser|dein|ihr)[ \t]+)?(?:passwort|kennwort|token)[ \t]+(?:lautet|ist)[ \t]+[\"„]?"   # German: (mein) Passwort lautet V, das Token ist V (MN58)
    r"|(?:mon|le|ton|votre)[ \t]+(?:mot[ \t]de[ \t]passe|jeton)[ \t]+(?:c'est|est)[ \t]+"              # French: mon mot de passe c'est V, le jeton est V
    r"|(?:mi|el|tu|su)[ \t]+(?:contraseña|contrasena|token|clave[ \t]de[ \t]acceso)[ \t]+es[ \t]+"      # Spanish: mi token es V
    r"|ה?(?:טוקן|סיסמ[אה])(?:[ \t]של(?:י|ך|נו))?[ \t]הוא[ \t]*"                                        # Hebrew: ha-token sheli hu V
    r"|(?:我的|你的|您的)?(?:密码|令牌|口令)是[ \t]*[“\"]?"                                                  # Chinese: wo de lingpai shi V, with curly quotes
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
        out = _kv_res()[1].sub(_k8s_mask, out)
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
    out = _hcl_redact(out)
    # AG68: the prose rules run over the full-width-folded text too (`my password is Ｈｕｎｔ３ｒ...` from a Japanese IME leaked)
    folded_low = out.translate(_FULLWIDTH).lower()
    for rx, needles in _PROSE_RULES:
        if needles is None or any(n in folded_low for n in needles):  # a cheap substring gate: ordinary text pays one scan per rule
            out = _sub_value_folded(rx, out, 2, ok=_prose_ok)  # `the password for the server is V` (MN58)  # the word orders of Thai, Hindi, Arabic, Hebrew, Persian, bare Russian, German `lautet`, Portuguese (MN52)
    return out



_PROSE_RULES = (
    (_PROSE_RE, None),
    (_PROSE_SET_RE, (" to ",)),
    (_PROSE_I18N_RE, None),
    (_PROSE_I18N2_RE, None),
    (_PROSE_I18N3_RE, ("mật", "sandi", "şifre", "parola", "hasło", "haslo", "пароль", "токен", "κωδικός", "jelsz", "nenosiri", " ko ", " mo ", " namin ", " natin ", " niya ", " nila ")),
    (_QUALIFIED_RE, None),
)

def _prose_ok(v: str) -> bool:
    """A prose value: `_secretish`, and a double-quoted phrase with spaces needs a digit, an inner capital or a symbol
    (`"correct Horse 42"` is a passphrase; `"required for login"` is prose: AG68)."""
    if re.match(r"^[A-Za-z]{2,}[0-9]{1,4}(?:-\w+)?[.,;:!?]*$", v):
        return False  # `jelszava Windows11 alatt`, `az iPhone15-on`: a product name, not a value (MN62)
    if v.startswith('"') and " " in v:
        inner = v.strip('"')
        return bool(re.search(r"\d|[a-z][A-Z]|[^\w\s]", inner)) and _secretish(inner.replace(" ", "_"))
    return _secretish(v)


def _sub_value_folded(rx, out: str, group: int, *, ok=None) -> str:
    """`_sub_value`, then the same rule over the full-width-folded text with the spans mapped back (one code point per code point)."""
    out = _sub_value(rx, out, group, ok=ok)
    folded = out.translate(_FULLWIDTH)
    if folded == out:
        return out
    pieces, pos = [], 0
    for m in rx.finditer(folded):
        v = m.group(group)
        s, e = m.span(group)
        if s < pos or _PLACEHOLDER_RE.match(v) or "[REDACTED" in v or (ok is not None and not ok(v)):
            continue  # a match the plain pass masked already holds [REDACTED]
        q = _kv_quote(v)
        pieces.append(out[pos:s])
        pieces.append(f"{q}[REDACTED]{q}")
        pos = e
    pieces.append(out[pos:])
    return "".join(pieces)


_HCL_VAR_RE = re.compile(r"(?s)\bvariable[ \t]+\"([^\"\n]{1,128})\"[ \t]*\{([^{}]{0,2000})\}")
_HCL_DEFAULT_RE = re.compile(r"(?m)(^[ \t]*default[ \t]*=[ \t]*)(\"[^\"\n]{4,}\")")


def _hcl_redact(out: str) -> str:
    """AG68: a Terraform `variable "db_password" { default = "V" }` (or any variable marked `sensitive = true`) leaked its default."""
    if "variable" not in out or "default" not in out:
        return out

    def var(m):
        name, body = m.group(1), m.group(2)
        if not (re.search(_ANY_NAME, name, re.I) or re.search(r"(?m)^[ \t]*sensitive[ \t]*=[ \t]*true\b", body)):
            return m.group(0)
        nb = _sub_value(_HCL_DEFAULT_RE, body, 2, ok=lambda v: not _doc_value(v) and not _kv_skip("k", v, "=", None, 0))
        return m.group(0)[:m.start(2) - m.start()] + nb + m.group(0)[m.end(2) - m.start():]
    return _HCL_VAR_RE.sub(var, out)


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
    ("password-stdin", re.compile(r"(\b(?:echo|printf)[ \t]+(?:-n[ \t]+)?['\"]?)([^\s'\"|]{6,})(?=['\"]?[ \t]*\|[ \t]*(?:sudo[ \t]+)?(?:docker|podman|nerdctl|helm|oras|buildah|skopeo|crane)\b[^\n|]*--password-stdin)")),  # AG68: `echo V | docker login --password-stdin`
    ("curl", re.compile(r"(\bcurl\b[^\n|;&]{0,200}?[ \t]-u['\"]?[^\s:'\"-][^\s:'\"]*:)(?!//)([^\s'\"]{3,})")),  # AG68: the glued `curl -uuser:V`
    ("az ", re.compile(r"(\baz[ \t]+login\b[^\n|;&]{0,200}?[ \t]-p[ \t]+['\"]?)(?!-)([^\s'\"|;&]{3,})")),  # AG68: `az login -u me -p V`
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


# ---------------------------------------------------------------- keyword lines (AG72)
# Qualified prose ("the root password on the box is V", "das Passwort des Servers lautet V", "رمز عبور سرور V است") kept slipping
# past per-language regexes: one shape fixed, its neighbour leaked (MN58, AG68, AG72). This last pass is structural: on a line that
# holds a password word in any of these languages (vowel marks and joiners stripped, so pointed Hebrew and vocalised Arabic count),
# every token that LOOKS like a secret (8+ printable ASCII, a letter and a digit, mixed case or a symbol) is masked.
_PW_WORDS = (
    "password", "passwd", "passphrase", "passcode", "passwort", "kennwort", "mot de passe", "contraseña", "contrasena", "senha",
    "wachtwoord", "passord", "lösenord", "losenord", "adgangskode", "salasana", "hasło", "haslo", "heslo", "geslo", "lozinka",
    "parolă", "parola", "jelszó", "jelszo", "jelszava", "şifre", "sifre", "mật khẩu", "mat khau", "kata sandi", "kata laluan",
    "nenosiri", "şifrə", "sifre", "parol", "пароль", "парол", "лозинка", "κωδικός", "κωδικος", "συνθηματικό", "סיסמה", "סיסמא", "كلمة المرور", "كلمة السر",
    "رمز عبور", "گذرواژه", "پاس ورڈ", "پاسورڈ", "पासवर्ड", "পাসওয়ার্ড", "கடவுச்சொல்", "మనపదం", "పాస్‌వర్డ్", "පාස්වර්ඩ්", "මුරපදය",
    "പാസ്‌വേഡ്", "ಪಾಸ್‌ವರ್ಡ್", "รหัสผ่าน", "ລະຫັດຜ່ານ", "ពាក្យសម្ងាត់", "စကားဝှက်", "གསང་ཚིག", "პაროლი", "գաղտնաբառ",
    "密码", "密碼", "口令", "パスワード", "暗証番号", "비밀번호", "암호",
    # AG91: Danish, Norwegian, Swedish, Finnish, Icelandic, Faroese (a substring test: `kodeordet`, `nøglerne`, `API-nyckeln` count)
    "kodeord", "løsen", "nøgle", "nøkkel", "nøkle", "nykel", "nyckel", "nyckl", "hemmelighed", "hemmelighet", "hemmelegheit", "løyndom",
    "hemlighet", "pinkode", "pin-kode", "pinkod", "pin-kod", "pin-koodi", "pin-númer", "pin-kota", "adgangsfrase", "passfrase", "lösenfras",
    "tunnuslause", "tunnussana", "avain", "avaime", "salaisuus", "lykilorð", "aðgangsorð", "loyniorð", "lykil", "leyndarmál", "loyndarmál",
    "adgangstoken", "tilgangstoken", "åtkomsttoken", "pääsytunnus", "tokenet", "tokenen", "tokeni",
)
_PW_TOKEN_RE = re.compile(r"(?<![!-~])[!-~]{8,128}")
_PW_SYMBOLS = set("!@#$%^&*+~?")
_STRIP_CLASS: list = []
_MARKISH_RE = re.compile("[\u0300-\u036f\u0483-\u0489\u0591-\u05c7\u0610-\u061a\u064b-\u065f\u0670\u06d6-\u06ed\u0711\u0730-\u074a\u0900-\u0dff\u0e31-\u0e4e\u0eb1-\u0ecd\u0f18-\u0fbc\u1000-\u109f\u1712-\u1734\u17b4-\u17d3\u1ab0-\u1dff\u200b-\u200f\u202a-\u202e\u2060-\u206f\u20d0-\u20ff\u302a-\u302f\u3099\u309a\ufe00-\ufe2f\ufeff\U00011000-\U00011fff]")


def _strip_marks(s: str) -> str:
    if s.isascii():
        return s
    import unicodedata as _ud0
    s = _ud0.normalize("NFKC", s).casefold().replace("\u0640", "")  # NFKC first (half/full-width, NFD Hangul and kana), casefold before the strip (`İ` casefolds to i + a combining dot), tatweel out (MN62)
    if not _MARKISH_RE.search(s):
        return s  # nothing a mark strip could remove: the big class scan cost 0.5 s on a 660 KB line (CI 43662191398)
    if not _STRIP_CLASS:
        import unicodedata as _ud
        chars = [chr(c) for c in range(0x300, 0x30000) if _ud.category(chr(c)) in ("Mn", "Me", "Cf")]
        _STRIP_CLASS.append(re.compile("[" + "".join(re.escape(c) for c in chars) + "]"))
    return _STRIP_CLASS[0].sub("", s)


def _strip_marks_short(s: str) -> str:
    """_strip_marks for a short string, without building the class: the same folds, then the same
    characters (Mn, Me, Cf from U+0300 to U+2FFFF) removed one by one."""
    if s.isascii():
        return s
    import unicodedata as _ud0
    s = _ud0.normalize("NFKC", s).casefold().replace("\u0640", "")
    if not _MARKISH_RE.search(s):
        return s
    return "".join(c for c in s if not (0x300 <= ord(c) < 0x30000 and _ud0.category(c) in ("Mn", "Me", "Cf")))


_PW_WORDS_STRIPPED = tuple(sorted({_strip_marks_short(w).casefold() for w in _PW_WORDS}, key=len, reverse=True))  # no class build at import (AG92)
_PW_WORDS_ASCII = tuple(w for w in _PW_WORDS_STRIPPED if w.isascii())  # the ASCII pre-check is derived: it missed adgangskode and losenord (MN62)


def _literal_trie(words) -> str:
    """A regex that finds any of `words` (plain strings, matched as written): one scan instead of one `in` per word."""
    if "" in words:
        return ""  # a word ended here: the shorter word already matches, the longer ones add nothing to a search
    heads: dict = {}
    for w in words:
        heads.setdefault(w[0], []).append(w[1:])
    parts = [re.escape(h) + ("(?:" + _literal_trie(r) + ")" if len(r) > 1 or r[0] else "") for h, r in heads.items()]
    return parts[0] if len(parts) == 1 else "(?:" + "|".join(parts) + ")"


_PW_WORDS_RE = re.compile(_literal_trie(_PW_WORDS_STRIPPED))  # AG95: `any(w in probe for w in _PW_WORDS_STRIPPED)` per line cost 0.24 s per MB of Danish
_PRODUCT_RE = re.compile(r"^[A-Za-z]{2,}[0-9]{1,4}(?:-\w+)?$")  # Windows11, iPhone15, Office365, Ubuntu24: names, not values (MN62: 35 of 69 ordinary sentences)


_VALUE_CUE_RE = re.compile(
    r"(?:\b(?:is|was|are|ist|lautet|est|es|era|é|è|är|er|je|jest|to|on|là|adalah|ialah|είναι|ειναι)\b"
    r"|(?:היא|הוא|هي|هو|คือ|是|为|為)"
    r"|[:=：＝—–])[ \t]*['\"`(\[「『“]?$", re.IGNORECASE)
_NORDIC_ER = ("adgangskode", "passord", "kodeord", "løsen", "nøgle", "nøkkel", "nøkle", "nykel", "hemmelighed", "hemmelighet",
              "hemmelegheit", "løyndom", "pinkode", "pin-kode", "adgangsfrase", "passfrase", "lykilorð", "aðgangsorð", "loyniorð", "lykil",
              "leyndarmál", "loyndarmál", "adgangstoken", "tilgangstoken", "tokenet", "tokenen", "pin-númer", "pin-kota")  # AG91: `er` is the copula of every one
_WEAK_CUES = {"on": ("salasana", "avain", "avaime", "salaisuus", "pin-koodi", "tunnuslause", "tunnussana", "tokeni", "pääsytunnus"),
              "to": ("hasło", "haslo", "hasla"), "je": ("heslo",), "er": _NORDIC_ER,
              "es": ("contraseña", "contrasena", "clave"), "est": ("mot de passe",), "ist": ("passwort", "kennwort")}  # cue -> the password words of its language
_PARTICLE_CUE_RE = re.compile(r"(?:は|が|는|은|이|가)[ \t]*['\"`(\[「『“]?$")  # topic particles: a cue only with the verb-final ending too
# verb-final languages put the cue AFTER the value: `पासवर्ड V है`, `رمز عبور V است`, `パスワードはVです`, `V 입니다`, `V -dir` (MN68)
_VALUE_CUE_AFTER_RE = re.compile(r"^['\"`)\]」』”]?[ \t]*(?:है|हैं|است|です|だ|입니다|이다|이에요|예요|-?dir\b|-?dır\b|-?dur\b|olarak\b|idi\b|oldu\b)")
_ROLE_PREFIX_RE = re.compile(r"^\s*(?:\[(?:USER|ASSISTANT|SYSTEM|TOOL)\]|(?:USER|ASSISTANT|SYSTEM|TOOL|Human|AI):)\s*", re.IGNORECASE)
_TRAILING_COMMENT_RE = re.compile(r"\s+(?:#|//|--)\s.*$")
_LINE_ENDS_WITH_CUE_RE = re.compile(r"(?:[:=：]|\b(?:is|below|unten|abajo|ci-dessous|qui sotto|poniżej|níže|alla|ниже|aşağıda|bên dưới|di bawah)\b)\s*$", re.IGNORECASE)
_LIST_MARK_RE = re.compile(r"^(?:[-*+•>]|\d{1,3}[.)])[ \t]+")  # `- V`, `> V`, `1. V` under `password is:` (AG79)
# a qualifier longer than 20 characters still binds the cue when it is a prepositional phrase or a comma clause:
# `The password for the staging database is V`, `the wifi password, which I set yesterday, is V` (AG79)
_QUALIFIER_RE = re.compile(r"^(?:[,(]|(?:for|of|on|at|in|to|from|used|that|which|für|des|der|del|de|du|pour|para|per|dla|do|na|van|voor|til|till|for|för|fyrir|af|av|på|hos|að|á|mitt|mit|min|mín|dit|ditt|vores|vårt|okkar)\b)", re.IGNORECASE)


def _secret_shaped(tok: str, value_slot: bool = False) -> bool:
    if "://" in tok or "/" in tok or "\\" in tok or "=" in tok or tok.startswith("[REDACTED"):  # `token_count=12` is a key=value pair: the kv rule judges it
        return False
    low = tok.casefold()
    if any(w in low for w in ("password", "passwd", "secret", "passphrase", "passcode", "redacted")):
        return False  # an identifier (`getPassword2`, `SecretAccessKey`), not a value
    if not any(c.isdigit() for c in tok) or not any(c.isalpha() for c in tok):
        return False
    if value_slot:
        return True  # right after `is`/`:`/`è`/`は`/`是` (or before `है`/`です`): letters and digits are the value, any case (MN68: `blue7horse42`, `Natsu2024`)
    if any(c in _PW_SYMBOLS for c in tok):
        return True
    # outside a value slot only a long mixed-class run is a value: `Python3.11`, `OAuth2.0`, `Argon2id`, `GPT-4o-mini`, `Ubuntu24.04`
    # on a line that merely mentions a password word are names (MN61, MN68: 64 of 110 ordinary sentences were mangled)
    return (len(tok) >= 12 and any(c.isupper() for c in tok) and any(c.islower() for c in tok)
            and "." not in tok and "-" not in tok and ":" not in tok and not _PRODUCT_RE.match(tok))  # `PostgreSQL16:lle` is a name with a case ending


def _keyword_line_redact(text: str) -> str:
    if text.isascii():
        low_all = text.casefold()
        if not any(w in low_all for w in _PW_WORDS_ASCII):
            return text

    def _mask(m, carried=False):
        tok = m.group(0)
        core = tok.rstrip(".,;:!?)]}\"'」』”")
        lead = len(core) - len(core.lstrip("(\"'[{「『“"))
        core2 = core[lead:]
        if len(core2) < 8 or not _secret_shaped(core2, value_slot=True):
            return tok  # cheap first: only a letters-and-digits token can be a value (the slot test per token was super-linear)
        if not carried and _secret_shaped(core2, value_slot=False):
            return tok[:lead] + "[REDACTED]" + tok[lead + len(core2):]
        before = m.string[max(0, m.start() - 80):m.start()].rstrip(" \t")
        after = m.string[m.end():m.end() + 16]
        if not before.isascii():
            import unicodedata as _ud1
            before = _ud1.normalize("NFC", before)  # AG91: the cues are matched on composed letters
        slot = carried
        if not slot:
            bprobe = _strip_marks(before).casefold()
            hits = [(bprobe.rfind(w), w) for w in _PW_WORDS_STRIPPED if w in bprobe] if _PW_WORDS_RE.search(bprobe) else []
            if hits:  # a cue counts only beside a password word: `Password hashing moved to Argon2id` is no value (MN68)
                pos, w = max(hits)
                gap = bprobe[pos + len(w):].strip(" \t'\"`(「『“")
                rest = m.string[m.end():].split("\n", 1)[0].strip(" \t.,;:!?。、」』”)")
                gap_q = re.sub(r"^\w{1,4}[ \t]+", "", gap)  # AG91b: past a definite or plural ending (`Adgangskoden til den nye database på serveren er V`)
                close = len(gap) <= 20 or (len(gap) <= 60 and bool(_QUALIFIER_RE.match(gap) or _QUALIFIER_RE.match(gap_q)) and not re.search(r"[.;!?。]", gap))  # the word, a qualifier (`de root`, `for the staging database`), the cue
                _cue = _VALUE_CUE_RE.search(before)
                if _cue and close:
                    _cw = _cue.group(0).strip(" \t'\"`([「『“:=：＝—–").casefold()
                    _own = _WEAK_CUES.get(_cw)
                    if _own is not None and not any(w.startswith(k) for k in _own):
                        close = False  # `reset the password on Windows11`: English `on`/`to` are prepositions; only Finnish `on`, Polish `to`, Czech `je` are copulas
                slot = ((close and bool(_VALUE_CUE_RE.search(before)))
                        or (close and bool(_VALUE_CUE_AFTER_RE.match(after)))
                        or (not gap and not _PRODUCT_RE.match(core2))  # `Şifrə Yay2024xxx`; `forgot my password iPhone15` is a name
                        or (bprobe.strip().startswith(w) and len(bprobe.strip()) <= len(w) + 3)  # a line that opens with the word: `Şifrem V`
                        or (close and bool(_PARTICLE_CUE_RE.search(before)) and (bool(_VALUE_CUE_AFTER_RE.match(after)) or not rest)))
        if _secret_shaped(core2, value_slot=slot):
            return tok[:lead] + "[REDACTED]" + tok[lead + len(core2):]
        return tok

    def _mask_carried(m):
        return _mask(m, carried=True)

    out_lines = []
    carry = False      # the previous line held a password word and no value: `password is below` / `Пароль сервера:` then V (MN62)
    carry_slot = False  # ...and it ended with a cue, so the next value keeps no name exemption (MN68: `password is\nSummer2024`)
    for line in text.split("\n"):
        probe = _strip_marks(line).casefold()
        if _PW_WORDS_RE.search(probe):
            new = _PW_TOKEN_RE.sub(_mask, line)
            carry = new == line and "[REDACTED" not in line  # an already-masked value does not carry to the next line (AG73)
            carry_slot = carry and bool(_LINE_ENDS_WITH_CUE_RE.search(_TRAILING_COMMENT_RE.sub("", _ROLE_PREFIX_RE.sub("", line))))  # `password is  # below` (AG79: the comment rule was on the value line only)
            out_lines.append(new)
            continue
        body = _TRAILING_COMMENT_RE.sub("", _ROLE_PREFIX_RE.sub("", line)).strip()  # `[USER] V` / `USER: V` / `V  # staging` (MN68: across turns)
        if carry:
            body = _LIST_MARK_RE.sub("", body)  # `- V` / `> V` / `1. V` (AG79)
        if carry and (not body or body.startswith(("```", "~~~"))):
            out_lines.append(line)  # `password is below:`, a blank line or a fence, then V: the value leaked (AG73)
            continue
        if carry and body and len(body.split()) == 1:
            out_lines.append(_PW_TOKEN_RE.sub(_mask_carried if carry_slot else _mask, line))  # without a cue the name exemption stays: `Change the password` then a heading `Installation2`
        else:
            out_lines.append(line)
        carry = carry_slot = False
    return "\n".join(out_lines)


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
    out = _keyword_line_redact(out)  # the structural last pass (AG72)
    return out
