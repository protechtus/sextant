"""Why did the agent arm get a question wrong? Sorts each miss by how far the evidence got.

    python analyze_agent.py <mode-tag> <model> [--prompt N --suffix S --every 2 --offset 1]

For every answerable question it compares the sessions that hold the evidence with what the
agent's searches returned and which notes it opened:

    never_found   at least one evidence session never appeared in any search result
    found_unread  every evidence session appeared in a result, but one was never opened
    read_all      every evidence session was opened, and the answer was still wrong

Costs nothing: it reads the conversations and verdicts already on disk.
"""

from __future__ import annotations

import collections
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import arms  # noqa: E402
import common  # noqa: E402
import report  # noqa: E402

NOTE = re.compile(r"\bs(\d{3})\b")
FULL = re.compile(r"^===== s(\d{3}) in full =====$", re.M)


def trace(convo: dict) -> tuple[set[int], set[int]]:
    """(positions shown in search results, positions opened)."""
    shown: set[int] = set()
    opened: set[int] = set()
    calls: dict[str, dict] = {}
    for message in convo["messages"]:
        if not isinstance(message["content"], list):
            continue
        for block in message["content"]:
            if block.get("type") == "tool_use":
                calls[block["id"]] = block
            elif block.get("type") == "tool_result":
                call = calls.get(block["tool_use_id"], {})
                if call.get("name") == "memory_search":
                    listing, _, bodies = str(block["content"]).partition("\n=====")
                    shown |= {int(m) for m in NOTE.findall(listing)}
                    # variant 3 returns the top notes in full with the search: those count as read
                    opened |= {int(m) for m in FULL.findall("\n=====" + bodies)}
                elif call.get("name") == "memory_read":
                    m = NOTE.search(str(call["input"].get("note", "")))
                    if m and not str(block["content"]).startswith("There is no note"):
                        opened.add(int(m.group(1)))
    return shown, opened


def main(argv: list[str]) -> int:
    mode_tag, model = argv[0], argv[1]

    def opt(flag: str, default: str | None = None) -> str | None:
        return argv[argv.index(flag) + 1] if flag in argv else default

    every = opt("--every")
    keep = {r["question_id"] for r in arms.select(every=int(every) if every else None,
                                                  offset=int(opt("--offset", "0")))}
    prompt = opt("--prompt", "1") or "1"
    variant = "" if prompt == "1" else prompt          # same naming as agent.run
    if common.split() != "s":
        mode_tag = f"{mode_tag}-{common.split()}"
    base = f"agt{variant}.{mode_tag}.{model}.r1" + (opt("--suffix", "") or "")
    state = json.loads((common.work_dir() / "agent" / base / "state.json").read_text(encoding="utf-8"))
    scored = report.score_job(arms.answer_job_name(f"agent{variant}-{mode_tag}", model, 1, opt("--suffix", "") or ""))
    buckets: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    examples: dict[str, list[str]] = collections.defaultdict(list)
    for qid, convo in state["convos"].items():
        if qid not in keep or common.is_abstention(qid):
            continue
        entry = common.load_question(common.split(), qid)
        evidence = {i for i, sid in enumerate(entry["haystack_session_ids"]) if sid in set(entry["answer_session_ids"])}
        shown, opened = trace(convo)
        if not evidence <= shown | opened:
            stage = "never_found"
        elif not evidence <= opened:
            stage = "found_unread"
        else:
            stage = "read_all"
        ok = scored["rows"][qid]["correct"]
        buckets[entry["question_type"]][(stage, ok)] += 1
        buckets["all"][(stage, ok)] += 1
        if not ok and len(examples[stage]) < 4:
            examples[stage].append(f"{qid} [{entry['question_type']}] searches={convo['searches']} reads={convo['reads']} "
                                   f"evidence={len(evidence)} opened={len(evidence & opened)}: {entry['question'][:90]}")
    print(f"{'type':28s} {'stage':13s} {'right':>6s} {'wrong':>6s}")
    for qtype in ["all", *common.QUESTION_TYPES]:
        for stage in ("never_found", "found_unread", "read_all"):
            right, wrong = buckets[qtype][(stage, True)], buckets[qtype][(stage, False)]
            if right or wrong:
                print(f"{qtype:28s} {stage:13s} {right:6d} {wrong:6d}")
    for stage, lines in examples.items():
        print(f"\nwrong, {stage}:")
        for line in lines:
            print("  " + line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
