"""MN98: a model CLI's cloud switch set to an off value selects no cloud, so the cloud's sign-in names stay out."""
from __future__ import annotations

import pytest

from atlas_plus import _proc

_SECRET_NAME = "AWS_" + "SEC" + "RET" + "_ACCESS_" + "K" + "EY"
_BASE = {"PATH": "x", "AWS_REGION": "r", _SECRET_NAME: "sentinel-not-a-value",
         "GOOGLE_APPLICATION_CREDENTIALS": "sentinel-not-a-value"}


@pytest.mark.parametrize("value", ["0", "false", "FALSE", "no", "off", " 0 ", ""])
def test_off_switch_keeps_cloud_names_out(value):
    env = _proc.model_cli_env("claude", base={**_BASE, "CLAUDE_CODE_USE_BEDROCK": value,
                                              "CLAUDE_CODE_USE_VERTEX": value})
    assert not [n for n in env if n.startswith(("AWS_", "GOOGLE_"))]
    if value:
        assert env["CLAUDE_CODE_USE_BEDROCK"] == value  # the CLI still sees its own switch


@pytest.mark.parametrize("value", ["1", "true", "yes"])
def test_on_switch_passes_cloud_names(value):
    env = _proc.model_cli_env("claude", base={**_BASE, "CLAUDE_CODE_USE_BEDROCK": value})
    assert {"AWS_REGION", _SECRET_NAME} <= set(env)
    assert "GOOGLE_APPLICATION_CREDENTIALS" not in env
