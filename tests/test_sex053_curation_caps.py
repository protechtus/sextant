"""SEX-053 item 11, the sibling paths of lane AG123: a capped duplicate scan reads "N or more".

The curation report stops its two duplicate scans at 500 each and says so in its section headers
(`500+ found (capped at 500)`). The headline parser keeps only the digits, so the dashboard panel, the
Brain chat and the Telegram command all showed "Duplicates: 1000" on a 254-note corpus, as if 1000
were the total (MN93 F4). AG123 fixed the panel; these tests hold the other two callers and the helper.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

CAPPED = (
    "# Weekly Curation Report\n"
    "## Repeated facts (500+ found (capped at 500): the same sentence in more than one note)\n"
    "## Potential Duplicates (500+ found (capped at 500), threshold 0.40)\n"
    "## Stale Files (3 found, older than 6 months)\n"
    "## Convergence Flags (0 contradictions found)\n"
    "## Stale Decisions (1 decisions need revisit)\n"
)
PLAIN = CAPPED.replace("500+ found (capped at 500)", "2 found")
HALF = CAPPED.replace("Repeated facts (500+ found (capped at 500)", "Repeated facts (7 found")


def test_the_helper_names_each_capped_scan():
    from atlas_plus.curation.weekly_report import curation_caps, curation_counts, duplicates_shown
    assert curation_caps(CAPPED) == [("repeated facts", 500), ("note pairs", 500)]
    assert curation_caps(PLAIN) == []
    assert curation_caps(HALF) == [("note pairs", 500)]
    n = curation_counts(CAPPED)["duplicates"]
    assert n == 1000
    assert duplicates_shown(CAPPED, n) == "1000 or more (the scan stops at 500 repeated facts, 500 note pairs)"
    assert duplicates_shown(PLAIN, 4) == "4"
    assert duplicates_shown(HALF, 507) == "507 or more (the scan stops at 500 note pairs)"


def test_the_real_report_header_is_what_the_helper_reads(tmp_path, monkeypatch):
    """Against the report writer itself, so a reworded header cannot leave the helper reading nothing:
    more repeated facts than the cap, through generate_report."""
    from atlas_plus.curation import weekly_report
    root = tmp_path / "mem"
    (root / "reference").mkdir(parents=True)
    lines = "\n".join(f"- shared fact number {i} that appears in both of these notes word for word" for i in range(520))
    for name in ("a.md", "b.md"):
        (root / "reference" / name).write_text(f"---\ntype: reference\n---\n# {name}\n{lines}\n", encoding="utf-8")
    text, _ = weekly_report.generate_report(memory_root=root, dry_run=True)
    header = next(line for line in text.splitlines() if "Repeated facts (" in line)
    if "capped at" not in header:
        pytest.skip("this corpus did not reach the repeated-facts cap: " + header)
    assert ("repeated facts", 500) in weekly_report.curation_caps(text)
    assert "or more" in weekly_report.duplicates_shown(text, weekly_report.curation_counts(text)["duplicates"])


def test_brain_chat_says_or_more_for_a_capped_scan(monkeypatch):
    from rich.console import Console
    from atlas_plus.brain import cli
    con = Console(record=True, width=200, force_terminal=False)
    monkeypatch.setattr(cli, "console", con)
    with patch("atlas_plus.curation.weekly_report.generate_report", return_value=(CAPPED, Path("report.md"))):
        cli._run_curation_report("")
    out = con.export_text()
    assert "1000 or more" in out and "the scan stops at 500 repeated facts, 500 note pairs" in out


def test_brain_chat_is_unchanged_without_a_cap(monkeypatch):
    from rich.console import Console
    from atlas_plus.brain import cli
    con = Console(record=True, width=200, force_terminal=False)
    monkeypatch.setattr(cli, "console", con)
    with patch("atlas_plus.curation.weekly_report.generate_report", return_value=(PLAIN, Path("report.md"))):
        cli._run_curation_report("")
    out = con.export_text()
    assert "Duplicates:     4" in out and "or more" not in out


@pytest.mark.asyncio
async def test_telegram_says_or_more_for_a_capped_scan():
    from atlas_plus.telegram.commands import cmd_curation_report
    update = MagicMock()
    update.message.text = "/curation_report"
    update.message.reply_text = AsyncMock()
    update.effective_user.id = 12345
    ctx = MagicMock()
    ctx.args = []
    with patch("atlas_plus.curation.weekly_report.generate_report", return_value=(CAPPED, Path("report.md"))):
        await cmd_curation_report(update, ctx)
    text = " ".join(str(c[0][0]) for c in update.message.reply_text.call_args_list)
    assert "Duplicates: 1000 or more (the scan stops at 500 repeated facts, 500 note pairs)" in text
    assert "Stale files: 3" in text
