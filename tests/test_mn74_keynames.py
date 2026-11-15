"""MN74 F3: two-word key names leaked through redact() (`API key: V`, `secret key: V`, `clave API=V`)."""
import pytest

from atlas_plus.redaction import redact

V = "Xq7mK2pLw9" + "zQ"  # assembled so secret scanners do not flag the fixture


@pytest.mark.parametrize("name", [
    "API key", "API Key", "secret key", "access key", "private key", "client secret",
    "clave API", "API-ключ", "API anahtarı", "API 키", "API कुंजी", "الرمز السري",
])
@pytest.mark.parametrize("sep", [": ", " = ", "="])
def test_two_word_key_names_are_masked(name, sep):
    out = redact(f"{name}{sep}{V}")
    assert V not in out and "[REDACTED]" in out


@pytest.mark.parametrize("prose", [
    "the API key rotation is weekly",
    "press the secret key combo",
    "Set the API key in settings: done",
    "API key: see the vault",
    "the private key file lives in ~/.ssh",
])
def test_prose_about_keys_is_left_alone(prose):
    assert redact(prose) == prose
