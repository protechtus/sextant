"""AG68 (sweep 20): redaction red team, wave 3 -- config formats, command lines, ten more prose languages,
`_secretish` edges, and the truncate-before-redact order in the facts prompt. Every case fails on c73c2b6."""
import time

import pytest

from atlas_plus.redaction import redact

# assembled at runtime so the gitleaks pre-commit hook does not read a literal as a real secret
P = "Hunt3r" + "Tw0Go"
V = "Zq9w" + "X7vB3nK1"

LEAKS = [
    # config formats
    f'db_password = """{P}"""',                                            # TOML multi-line basic string
    f"api_token = '''{V}'''",                                              # TOML multi-line literal string
    f"defaults: &d\n  password: &pw {P}\n",                                # YAML anchor before the value
    f"password: !!str {P}",                                                # YAML core tag before the value
    f'password: "Hunt3r\n  {P}"',                                          # YAML double-quoted scalar folded over lines
    '{"password": "ab\\"' + P + '"}',                                      # an escaped quote inside a JSON string
    f"<password><![CDATA[{P}]]></password>",                               # XML CDATA
    f'variable "db_password" {{\n  default   = "{P}"\n  sensitive = true\n}}',  # Terraform variable default
    f'variable "db_creds" {{\n  sensitive = true\n  default   = "{P}"\n}}',      # any variable marked sensitive
    # command lines
    f"echo {P} | docker login --password-stdin",
    f"echo '{P}' | sudo helm registry login r.io --password-stdin",
    "curl -u" + f"me:{P} https://x",  # assembled: gitleaks reads the literal as a credential
    f"az login -u me@x.com -p {P}",
    # prose: English
    f"my password is “{P}”",
    f"my password is 「{P}」",
    "my password is Hunt3r­Tw0Go",
    "my password is Hunt3r​Tw0Go",
    "my password is Ｈｕｎｔ３ｒＴｗ０Ｇｏ",
    f"ｍｙ ｐａｓｓｗｏｒｄ ｉｓ {P}",
    'my password is "correct Horse 42"',
    f"I set the password to {P}",
    f"password changed to {P}",
    f"the client secret is {P}",
    # prose: ten more languages, with a qualifier between the key word and the value
    f"mật khẩu là {P}", f"mật khẩu của máy chủ là {P}",
    f"kata sandi saya adalah {P}", f"kata sandi untuk server adalah {P}",
    f"şifrem {P}", f"sunucunun şifresi {P}",
    f"moje hasło to {P}", f"hasło do serwera to {P}",
    f"мій пароль {P}", f"пароль до сервера: {P}",
    f"ο κωδικός μου είναι {P}", f"κωδικός: {P}",
    f"a jelszavam {P}", f"a szerver jelszava: {P}",
    f"nenosiri langu ni {P}",
    f"ang password ko ay {P}",
]


@pytest.mark.parametrize("text", LEAKS)
def test_leak_is_masked(text):
    out = redact(text, env={})
    for piece in (P, V, "Tw0Go", "Ｔｗ０Ｇｏ", "Horse 42"):
        assert piece not in out, out
    assert "[REDACTED" in out


@pytest.mark.parametrize("text, want", [
    (f'db_password = """{P}"""', 'db_password = """[REDACTED]"""'),
    (f"defaults: &d\n  password: &pw {P}\n", "defaults: &d\n  password: &pw [REDACTED]\n"),
    (f"<password><![CDATA[{P}]]></password>", "<password>[REDACTED]</password>"),
    (f"echo {P} | docker login --password-stdin", "echo [REDACTED] | docker login --password-stdin"),
    ("curl -u" + f"me:{P} https://x", "curl -u" + "me:[REDACTED] https://x"),
    (f"my password is “{P}”", "my password is “[REDACTED]”"),
])
def test_structure_is_kept(text, want):
    assert redact(text, env={}) == want


UNCHANGED = [
    # the brief's false-positive sentences
    "the password policy requires twelve characters", "reset your password at the portal", "token bucket rate limiter",
    "the secret to good bread is time",
    # the project's own install command (on c73c2b6 `-U git+https:` was read as curl's `-u user:password`)
    "pip install -U git+https://github.com/example-org/sextant",
    # YAML: an alias, a CloudFormation tag, a vault tag
    "password: *pw", "secret: !Ref DbSecret", "password: !vault |",
    # Terraform: a non-secret variable, a placeholder default
    'variable "region" {\n  default = "us-east-1abc"\n}', 'variable "db_password" {\n  default = "${var.x}"\n}',
    # prose in the new languages that carries no value
    "kata sandi harus minimal delapan karakter", "hasło musi mieć co najmniej 8 znaków", "mật khẩu là bắt buộc",
    "a jelszavam elfelejtettem", "nenosiri langu ni siri", "ang password ko ay nawala", "şifrem yanlış",
    'my password is "required for login"', "I set the password to expire", "the client secret is required",
    "openssl rsa -passin env:KEYPASS", "docker login --password-stdin", "echo hello | docker login --password-stdin",
]


@pytest.mark.parametrize("text", UNCHANGED)
def test_ordinary_text_is_left_alone(text):
    assert redact(text, env={}) == text


def test_redis_cli_url_keeps_its_host():
    out = redact(f"redis-cli -u redis://:{P}@cache.internal:6379", env={})
    assert P not in out and "cache.internal:6379" in out, out


def test_new_rules_stay_linear():
    shapes = [
        "kata sandi " * 40000, "şifre " * 50000, "hasło do " * 40000, "mật khẩu của " * 30000, "κωδικός για " * 30000,
        "set the password to " * 20000, "password: \"" + "\\a" * 100000, ("password: \"abc\n  " + "x\n  " * 10) * 3000,
        "variable \"db_password\" {" * 20000, "curl " + "-ua:b " * 40000, "my password is Ｈｕｎｔ３ｒ " * 15000,
    ]
    for s in shapes:
        t = time.perf_counter()
        redact(s, env={})
        assert time.perf_counter() - t < 3.0, s[:30]


def test_facts_prompt_is_redacted_before_the_cut(monkeypatch):
    """The facts prompt kept the last 6,000 characters and THEN redacted them: a key before the cut and its value
    after it sent the value to the model bare."""
    import atlas_plus.summarizer.providers as providers
    from atlas_plus.curation import suggest_facts

    seen = {}

    def fake_complete(prompt):
        seen["prompt"] = prompt
        return "[]", "fake"

    monkeypatch.setattr(providers, "complete", fake_complete)
    tail = ("\n" + "we talked about the deploy plan. " * 200)[: 6000 - len(P)]
    text = "intro\nuser: the staging db password: " + P + tail
    assert text[-6000:].startswith(P)  # the cut falls exactly between the key and the value
    suggest_facts._extract_candidates(text)
    assert "prompt" in seen
    assert P not in seen["prompt"]


def test_facts_prompt_fails_closed_when_redaction_breaks(monkeypatch):
    import atlas_plus.redaction as redaction
    import atlas_plus.summarizer.providers as providers
    from atlas_plus.curation import suggest_facts

    called = []
    monkeypatch.setattr(providers, "complete", lambda prompt: called.append(prompt) or ("[]", "fake"))

    def boom(text, **kw):
        raise RuntimeError("redaction broke")

    monkeypatch.setattr(redaction, "redact", boom)
    assert suggest_facts._extract_candidates(f"user: my password is {P}\n") == ([], 0)
    assert called == []
