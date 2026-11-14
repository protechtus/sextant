"""AG55 (sweep 18): the sixth sitting's leak list, each with its traps. Drop into tests/ of a clone patched with ag55-redaction.diff."""
import time

import pytest

from atlas_plus.redaction import redact

V = "Zq9wX7vB3nK1mP4t"

LEAKS = [
    ("yaml block CRLF", f"password: |\r\n  {V}\r\n"),
    ("yaml block in list item", f"- password: |\n    {V}\n"),
    ("yaml block after blank line", f"password: |\n\n  {V}\n"),
    ("markdown bold", f"**Password:** {V}"),
    ("escaped json", '{\\"password\\": \\"%s\\"}' % V),
    ("k8s name/value", f"- name: API_TOKEN\n  value: {V}\n"),
    ("netrc", f"machine example.com login bob password {V}"),
    ("pgpass", f"db.internal:5432:appdb:appuser:{V}"),
    ("npmrc", f"//registry.npmjs.org/:_authToken={V}"),
    ("xml add key", f'<add key="password" value="{V}"/>'),
    ("xml entry", f'<entry key="password">{V}</entry>'),
    ("php rocket", f"'password' => '{V}',"),
    ("tab", f"password:\t{V}"),
    ("russian", f"пароль: {V}"),
    ("russian token", f"токен = {V}"),
    ("german", f"Passwort: {V}"),
    ("german kennwort", f"Kennwort: {V}"),
    ("french", f"mot de passe : {V}"),
    ("spanish", f"contraseña: {V}"),
    ("persian", f"رمز عبور: {V}"),
    ("prose lowercase", "my password: sunshinemeadow"),
    ("log prefix", "2026-11-14 12:00:01 INFO login password: sunshinemeadow"),
    ("semicolon", "user=bob; password: sunshinemeadow"),
    ("numbered", "1. password: sunshinemeadow"),
    ("cookie", f"Cookie: sessionid={V}; theme=dark"),
    ("set-cookie", f"Set-Cookie: session={V}; Path=/; HttpOnly"),
    ("slack webhook", "https://hooks." + "slack.com/services/" + "T01234567/B01234567/abcdEFGH1234ijklMNOP5678"),
    ("azure account key", "DefaultEndpointsProtocol=https;AccountName=a;AccountKey=" + "Zq9wX7vB3nK1mP4t" * 4 + "==;EndpointSuffix=x"),
    ("azure sas sig", "https://a.blob.core.windows.net/c/b?sv=2020&sig=" + "Zq9wX7vB3nK1mP4t%2Bq9wX7vB3nK1mP4t%3D"),
    ("mysql -p", f"mysql -u root -p{V} mydb"),
    ("docker login -p", f"docker login -u bob -p {V} registry.io"),
    ("sshpass -p", f"sshpass -p {V} ssh host"),
    ("markdown table column", f"| user | password |\n|---|---|\n| bob | {V} |"),
    ("markdown table row", f"| password | {V} |"),
    ("npm token", "npm_" + "a" * 36),
    ("sendgrid", "SG." + "a" * 22 + "." + "b" * 43),
    ("digitalocean", "dop_v1_" + "a" * 64),
    ("pypi", "pypi-AgEIcHlwaS5vcmc" + "A" * 50),
    ("prose is", f"my password is {V}"),
    ("prose german", f"mein Passwort ist {V}"),
    ("prose japanese", f"パスワードは {V} です"),
]

TRAPS = [
    "note: |\r\n  hello world\r\n", "- note: |\n    hello world\n", "password: |\n\nhost: h\n", "text: |\n\n  plain words\n",
    "**Password:** ********", "**Password policy:** rotate often", '{\\"name\\": \\"bob\\"}', '{\\"password\\": \\"****\\"}',
    "- name: API_TOKEN\n  valueFrom:\n    secretKeyRef:\n      name: s\n", "- name: LOG_LEVEL\n  value: debugging\n", "- name: API_TOKEN\n",
    "value: 42", "- name: apps\n  value: 12345678", "machine example.com login bob", "the password of the user was reset",
    "db.internal:5432:appdb:appuser", "12:30:45:10:20", "//registry.npmjs.org/:always-auth=true", '<add key="timeout" value="30000"/>',
    '<add key="password" value="****"/>', "'name' => 'something',", "'password' => $_ENV['X'],", "Введите пароль для входа в систему.",
    "Das Passwort ist zu kurz, bitte ändern.", "Votre mot de passe a expiré.", "La contraseña debe tener ocho caracteres.", "رمز عبور خود را فراموش کردم",
    "The token: refreshing the page", "pass: yes we did", "Note the password: changeme is a placeholder we rotate", "password: <your-password>",
    "password: {{ db_password }}", "password: %PASSWORD%", "Cookie: ", "Set-Cookie: theme=dark; Path=/", "mysql -u root -p mydb", "ls -p /tmp",
    "docker login -u bob registry.io", "docker run -p 8080:80 nginx", "ssh -p 2222 host", "sshpass -f file ssh host",
    "| name | token_count |\n|---|---|\n| bob | 12 |", "| Field | Description |\n|---|---|\n| password | The user password |", "| password | string |",
    "npm_config_cache", "pypi-mirror is down", "the token is out-of-date", "the secret is well-known", "the token is 2026-11-14T12:00:00Z",
    "my password is self-explanatory", "the password is required for login", "パスワードは8文字以上である必要があります。", "ログイン画面でパスワードを入力してください。",
    "كلمة المرور مطلوبة لتسجيل الدخول.",
]


@pytest.mark.parametrize("label,text", LEAKS, ids=[x[0] for x in LEAKS])
def test_leak_is_masked(label, text):
    out = redact(text, env={})
    for secret in (V, "sunshinemeadow", "a" * 22, "a" * 36, "a" * 64, "abcdEFGH1234ijklMNOP5678", "Zq9wX7vB3nK1mP4t%2B"):
        if secret in text:
            assert secret not in out, (label, out)


@pytest.mark.parametrize("text", TRAPS)
def test_trap_is_untouched(text):
    assert redact(text, env={}) == text


def test_yaml_block_keeps_siblings_and_crlf():
    out = redact("db:\r\n  password: |\r\n    secretline\r\n  host: dbhost.internal\r\n")
    assert "secretline" not in out and "host: dbhost.internal" in out and "\r\n" in out
    out = redact("- password: |\n    secretline\n  host: h\n")
    assert "secretline" not in out and "  host: h" in out


def test_new_rules_are_linear():
    shapes = [
        "- name: API_TOKEN\n" * 20000, "<add key=\"password\" " * 15000, "'password' =>" * 40000, 'password\\": \\"' * 30000,
        "**Password:** " * 40000, "| a | b | c | d |\n" * 30000, "Cookie: a=b; c=d\n" * 30000, "login u " * 100000, "mysql " + "-u r -x " * 60000,
        "my password is " * 40000, "host.example:5432:db:user:" * 20000,
    ]
    for s in shapes:
        t = time.perf_counter()
        redact(s)
        assert time.perf_counter() - t < 3.0, s[:30]
