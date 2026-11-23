"""Retrieval recall for two or more index builds on the questions they all have.

    python compare_recall.py <split> <mode-tag> <mode-tag> [...]     e.g.  m hybrid-v1 hybrid-v2 fts-v1

Free: reads the retrieval files on disk. Reports, per question type, how often at least one
(any@k) and every (all@k) evidence session is in the top k, plus a paired count for all@10:
questions only the later build got, and questions only the first build got.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402
import report  # noqa: E402

KS = (1, 5, 10)


def load(split: str, mode_tag: str) -> dict[str, dict]:
    rows = {}
    for path in (common.work_dir() / "retrieval" / split / mode_tag).glob("*.json"):
        r = json.loads(path.read_text(encoding="utf-8"))
        if not common.is_abstention(r["question_id"]) and r["answer_positions"]:
            rows[r["question_id"]] = r
    return rows


def hit(r: dict, k: int, every: bool) -> bool:
    top = {h["position"] for h in r["hits"][:k]}
    need = set(r["answer_positions"])
    return need <= top if every else bool(need & top)


def main(argv: list[str]) -> int:
    split, tags = argv[0], argv[1:]
    builds = {t: load(split, t) for t in tags}
    shared = sorted(set.intersection(*(set(b) for b in builds.values())))
    print(f"{len(shared)} answerable questions present in all of: {', '.join(tags)}")
    for qtype in ["all", *common.QUESTION_TYPES]:
        qids = [q for q in shared if qtype == "all" or builds[tags[0]][q]["question_type"] == qtype]
        if not qids:
            continue
        print(f"\n{qtype} (n={len(qids)})")
        for t in tags:
            cells = []
            for k in KS:
                cells.append(f"any@{k} {sum(hit(builds[t][q], k, False) for q in qids) / len(qids):6.1%}")
                cells.append(f"all@{k} {sum(hit(builds[t][q], k, True) for q in qids) / len(qids):6.1%}")
            ms = sorted(builds[t][q]["ms"] for q in qids)[len(qids) // 2]
            print(f"  {t:14s} " + "  ".join(cells) + f"  median {ms:.0f} ms")
        base = tags[0]
        for t in tags[1:]:
            gained = sum(hit(builds[t][q], 10, True) and not hit(builds[base][q], 10, True) for q in qids)
            lost = sum(hit(builds[base][q], 10, True) and not hit(builds[t][q], 10, True) for q in qids)
            print(f"  all@10, {t} against {base}: gained {gained}, lost {lost}, "
                  f"exact McNemar p = {report.mcnemar_exact(gained, lost):.3g}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
