"""AG62 (sweep 19): the false-positive hunt over ordinary text, and the leak shapes of the eighth sitting."""
import time

import pytest

from atlas_plus.redaction import redact

V = "Zq9wX7vB3nK1"
# assembled at runtime: the gitleaks pre-commit hook reads a literal JWT or PEM header as a real secret
JWT = "eyJ" + "hbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9" + ".eyJ" + "zdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4ifQ" + ".SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
PEM_HEAD = "-----BEGIN " + "PRIVATE KEY-----"
PEM_RSA = "-----BEGIN " + "RSA PRIVATE KEY-----"
PEM_RSA_END = "-----END " + "RSA PRIVATE KEY-----"

UNCHANGED = [
    # quoted and unquoted placeholders
    'password: "${DB_PASSWORD}"', "password: '${DB_PASSWORD}'", 'password: "<your-password>"', 'token: "{{ vault_token }}"',
    'api_key: "$API_KEY"', "api_key: $API_KEY", 'password = "os.environ[\'DB_PASSWORD\']"', 'export TOKEN="${TOKEN:-}"', "password: !vault |",
    # attribute access in code
    "secret = config.secret", "api_key = settings.API_KEY",
    # status and type words at the start of a line
    "token: expired", "password: required", "Password: required", "token: string;", "password: (hidden)", "password: [hidden]", "token: redacted",
    # more words follow: prose
    "password: expired yesterday", "Token: refreshing the page", "Passwort: falsch, bitte erneut versuchen", "mot de passe: jamais stocké en clair",
    "şifre: süresi doldu", "पासवर्ड: समाप्त हो गया", "密码：必须至少包含八个字符", "パスワード：8文字以上で入力してください", "รหัสผ่าน: ต้องมีอย่างน้อยแปดตัวอักษร",
    "비밀번호: 만료되었습니다",
    # a prompt followed by output
    "Enter password:\nAuthentication successful", "password:\n  database: app", "Password: \npassword_hash = bcrypt.hashpw(pw, salt)",
    # Bearer
    "Use Bearer authentication for the API.", "Bearer authentication is described in RFC 6750.", "Authorization: Bearer YOUR_API_TOKEN_HERE",
    "Authorization: Bearer your_access_token", "token_type: bearer\ntoken.expiry.seconds=900",
    # CLI help and URLs
    "usage: tool login [--token TOKEN] [--password PASSWORD]", "  --token=<value>       The API token", "  --password <password> Your account password",
    "  --token ${TOKEN}", "curl -u user:password https://example.com", "curl -u $USER:$PASS https://example.com", "curl --user alice:${PASSWORD} https://e.com",
    "postgres://user:password@localhost:5432/db", "mysql://USER:PASS@HOST/DB", "https://user:${PASSWORD}@example.com/repo.git",
    "https://token:x-oauth-basic@github.com/o/r.git", "ftp://anonymous:guest@example.com",
    "-passin arg   The input file password source.", "Authorization: Basic <credentials>", "Authorization: Basic base64(user:pass)",
    "machine example.com login bob password <your-password>", "default login anonymous password user@example.com",
    # PEM mention
    "A PEM file begins with the line " + PEM_HEAD + " and ends with the matching END line.\nnext line stays",
    # cookies and webhooks
    "Set-Cookie: tz=Europe/Berlin; Path=/", "Cookie: ab_test=variantb; layout=compact", "Set-Cookie: sidebar=collapsed; Path=/", "Cookie: session_id=<session-id>; theme=dark",
    "https://hooks." + "slack.com/services/" + "T00000000/B00000000/XXXXXXXXXXXXXXXXXXXXXXXX",
]

MASKED = [
    "os.environ['DB_PASSWORD'] = '{V}'", 'os.environ["API_TOKEN"] = "{V}"', "config['password'] = '{V}'", "params[:password] = '{V}'", 'd["api_key"]="{V}"',
    "RAILS_MASTER_KEY={V}", "ENCRYPTION_KEY={V}", "MASTER_KEY={V}", "signing_key: {V}",
    "openssl enc -aes-256-cbc -pass pass:{V}", "openssl rsa -in k -passin pass:{V}", "openssl genrsa -aes256 -passout pass:{V} 2048", "keytool -genkey -storepass {V} -keypass {V}",
    "smbclient //h/s -U bob%{V}", "redis://:{V}@redis:6379/0", "redis-cli -a {V}", "redis-cli --pass {V} ping", "redis-cli AUTH {V}", "vault login {V}",
    "aws configure set aws_secret_access_key {V}", "htpasswd -b -c .htpasswd bob {V}", "htpasswd -nb bob {V}", "jdbc:oracle:thin:u/{V}@h:1521:xe",
    "conn.setPassword(\"{V}\");", "new UsernamePasswordCredentials(\"u\", \"{V}\")", "zip -P {V} a.zip f", "unzip -P {V} a.zip", "rar a -p{V} a.rar f", "7z a -p{V} a.7z f",
    "sqlcmd -S h -U sa -P {V}", "bcp t out f -S h -U sa -P {V}", "mongo -u bob -p {V} --authenticationDatabase admin", "cqlsh -u cassandra -p {V}",
    "psexec \\\\h -u bob -p {V} cmd", "ldapsearch -x -D cn=admin -w {V}", "influx -username bob -password {V}", "net use Z: \\\\h\\s {V} /user:bob",
    "cmdkey /add:h /user:bob /pass:{V}", "schtasks /create /ru bob /rp {V} /tn t", "rabbitmqctl add_user bob {V}", "echo 'bob:{V}' | chpasswd",
    "npm config set //registry.npmjs.org/:_authToken {V}", "setx API_KEY {V}", "ConvertTo-SecureString \"{V}\" -AsPlainText -Force",
    "jwt = {JWT}", "gcloud auth print-access-token  # ya29.{V}{V}{V}", "رمز عبور شما {V} است", "Authorization: Bearer {JWT}",
    "password: |\n  {V}\nx: 1", "password:\n  {V}", PEM_RSA + "\nMIIEpAIBAAKCAQEA{V}\n" + PEM_RSA_END,
    PEM_RSA + "\nMIIEpAIBAAKCAQEA{V}{V}\ncut off",
]


@pytest.mark.parametrize("text", UNCHANGED)
def test_ordinary_text_is_left_alone(text):
    assert redact(text, env={}) == text


@pytest.mark.parametrize("text", MASKED)
def test_secret_shapes_are_masked(text):
    src = text.replace("{V}", V).replace("{JWT}", JWT)
    out = redact(src, env={})
    assert V not in out and JWT not in out and "MIIEpAIBAAKCAQEA" not in out and "[REDACTED" in out


def test_pem_mention_does_not_swallow_the_rest():
    text = "head\nA PEM file begins with " + PEM_HEAD + "\n" + "\n".join(f"line {i}" for i in range(50))
    assert redact(text, env={}) == text


def test_pem_header_spam_stays_linear():
    blob = (PEM_HEAD + " ") * 40000
    t = time.perf_counter()
    redact(blob, env={})
    assert time.perf_counter() - t < 5
