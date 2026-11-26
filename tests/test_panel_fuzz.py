"""Type-fuzz corpus for the dashboard panels that render outside data (lesson LES-071).

One shared corpus: every value below is placed at every key a renderer reads (walk a good sample
payload, replace one key at a time). A panel must render (never raise), escape the HTML value and
stay bounded in size. The values are what a hand-edited, truncated or hostile state file can hold.
"""
from __future__ import annotations

import copy
import json
from datetime import datetime, timezone

import pytest

HTML_PROBE = "<script>alert(1)</script>"
BIG_STRING = "A" * 1_000_000

FUZZ_VALUES = [
    "x", "", ["x"], [], None, float("nan"), float("inf"), -5, 10 ** 5000, True,
    {"nested": {"k": 1}}, 7, HTML_PROBE, BIG_STRING,
]
FUZZ_IDS = ["str", "empty-str", "list", "empty-list", "none", "nan", "inf", "negative", "huge-int",
            "bool", "mapping", "scalar", "html", "1mb-string"]
MAX_FRAGMENT = 64_000


def key_paths(obj, prefix=()):
    """Every path to a value in a sample payload (dict keys and list indices)."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield prefix + (k,)
            yield from key_paths(v, prefix + (k,))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield prefix + (i,)
            yield from key_paths(v, prefix + (i,))


def replaced(sample, path, value):
    out = copy.deepcopy(sample)
    cur = out
    for step in path[:-1]:
        cur = cur[step]
    cur[path[-1]] = value
    return out


def mutations(sample):
    for path in key_paths(sample):
        for value, vid in zip(FUZZ_VALUES, FUZZ_IDS):
            yield path, vid, replaced(sample, path, value)
    for value, vid in zip(FUZZ_VALUES, FUZZ_IDS):  # the whole payload replaced
        yield (), vid, value


def assert_safe_fragment(html, where):
    assert isinstance(html, str), where
    assert HTML_PROBE not in html, f"unescaped HTML at {where}"
    assert len(html) < MAX_FRAGMENT, f"{len(html):,} characters at {where}"


NOW = datetime(2026, 1, 2, 12, 0, tzinfo=timezone.utc)

SECURITY_SAMPLE = [{"verdict": "REVIEW", "ts": "2026-01-02T10:00:00Z", "high": 1, "medium": 2,
                    "report": "audit-2026-01-02.md"}]

LOOP_SAMPLE = {"generated": "2026-01-02T11:59:00Z", "running_count": 2, "backoff_count": 1,
               "opus_count": 1, "throttle_count": 3, "throttle_window_hours": 6, "stop_requested": False,
               "leases": [{"kind": "lib", "model": "sonnet", "skill": "polish", "state": "backoff 60s"}]}


def _security_fragment(index, tmp_path):
    from atlas_plus.web import security_status as sec
    p = tmp_path / "index.json"
    p.write_text(json.dumps(index), encoding="utf-8")  # through the file, as the route reads it
    summary = sec.summarize_security(sec.read_audit_index(p), now=NOW)
    return sec.render_panel(summary) + sec.rollup_span(summary)


def _loop_fragment(state, tmp_path):
    from atlas_plus.web import loop_status as loops
    p = tmp_path / "loop-state.json"
    p.write_text(json.dumps(state), encoding="utf-8")
    summary = loops.summarize_loops(loops.read_loop_state(p), now=NOW)
    return loops.render_panel(summary) + loops.rollup_span(summary)


def _json_safe(payload):
    try:
        json.dumps(payload)
    except (ValueError, TypeError):  # 10**5000 cannot be written as JSON text: not a file a writer makes
        return False
    return True


@pytest.mark.parametrize("render,sample", [(_security_fragment, SECURITY_SAMPLE), (_loop_fragment, LOOP_SAMPLE)],
                         ids=["security-audit", "build-loops"])
def test_shipped_panels_survive_the_fuzz_corpus(render, sample, tmp_path):
    failures = []
    for path, vid, payload in mutations(sample):
        if not _json_safe(payload):
            continue
        where = f"{'/'.join(map(str, path)) or '<root>'}={vid}"
        try:
            assert_safe_fragment(render(payload, tmp_path), where)
        except Exception as exc:  # noqa: BLE001 -- collect every failing key, not the first
            failures.append(f"{where}: {type(exc).__name__}: {str(exc)[:120]}")
    assert not failures, "\n".join(failures)
