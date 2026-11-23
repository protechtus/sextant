"""Build every per-question corpus and record what Sextant retrieves for the question as asked.

This step costs nothing: no hosted model is called. It produces the retrieval files the answer
arms read, plus recall figures (did the sessions that hold the answer come back?).

    python retrieve.py prepare            # split the downloaded dataset into per-question files
    python retrieve.py run s fts          # all questions, keyword index only
    python retrieve.py run s hybrid       # keyword + local Ollama embeddings
    python retrieve.py recall s fts
"""

from __future__ import annotations

import concurrent.futures as futures
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402
import corpus  # noqa: E402

LIMIT = 10
KS = (1, 3, 5, 10)


def tag() -> str:
    """Which Sextant build produced the retrieval (kept apart so builds can be compared)."""
    return os.environ.get("SEXTANT_PROOF_TAG", "").strip() or "v0"


def retrieval_dir(split: str, mode: str) -> Path:
    path = common.work_dir() / "retrieval" / split / f"{mode}-{tag()}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def worker_env(split: str, mode: str, qid: str) -> dict:
    root = corpus.corpus_root(split, qid)
    env = dict(os.environ)
    # SEXTANT_PROOF_STATE names a separate index folder, so a changed index recipe is built
    # beside the one earlier figures came from instead of over it
    state = os.environ.get("SEXTANT_PROOF_STATE", "").strip()
    env.update({
        "ATLAS_WORKSPACE_ROOT": str(root),
        "ATLAS_MEMORY_ROOT": str(root / "memory"),
        "ATLAS_STATE_DIR": str(root / (f"state-{mode}-{state}" if state else f"state-{mode}")),
        "ATLAS_SEMANTIC_SEARCH": "on" if mode == "hybrid" else "off",
        "PYTHONIOENCODING": "utf-8",
    })
    env.pop("ATLAS_TEMPORAL_ROOT", None)
    return env


def run_worker(split: str, mode: str, qid: str, extra: list[str] | None = None) -> dict:
    # the worker needs a Python with Sextant installed; the caller may be running under another
    python = os.environ.get("SEXTANT_PROOF_SEXTANT_PYTHON", "").strip() or sys.executable
    cmd = [python, str(common.HERE / "corpus.py"), split, mode, qid, "--limit", str(LIMIT)]
    proc = subprocess.run(cmd + (extra or []), env=worker_env(split, mode, qid),
                          capture_output=True, text=True, encoding="utf-8", timeout=900)
    if proc.returncode != 0:
        raise RuntimeError(f"{qid}: worker failed\n{proc.stderr[-2000:]}")
    return json.loads(proc.stdout.strip().splitlines()[-1])


def run(split: str, mode: str, only: list[str] | None = None, jobs: int = 6) -> None:
    out_dir = retrieval_dir(split, mode)
    todo = [row["question_id"] for row in common.load_index(split)
            if (not only or row["question_id"] in only)
            and not (out_dir / f"{row['question_id']}.json").exists()]
    print(f"{split}/{mode}: {len(todo)} questions to retrieve")
    failed = 0
    with futures.ThreadPoolExecutor(max_workers=jobs) as pool:
        pending = {pool.submit(run_worker, split, mode, qid): qid for qid in todo}
        for done, future in enumerate(futures.as_completed(pending), 1):
            qid = pending[future]
            try:
                result = future.result()
            except Exception as exc:  # noqa: BLE001 - report and continue; rerun picks up the rest
                failed += 1
                print(f"  FAILED {qid}: {str(exc)[:300]}")
                continue
            (out_dir / f"{qid}.json").write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
            if done % 50 == 0:
                print(f"  {done}/{len(todo)}")
    print(f"done, {failed} failed")


def recall(split: str, mode: str) -> dict:
    """Session-level recall on answerable questions: recall_any@k (at least one answer session
    in the top k) and recall_all@k (every answer session in the top k)."""
    rows = []
    for path in sorted(retrieval_dir(split, mode).glob("*.json")):
        r = json.loads(path.read_text(encoding="utf-8"))
        if common.is_abstention(r["question_id"]) or not r["answer_positions"]:
            continue
        rows.append(r)
    table: dict = {"mode": mode, "tag": tag(), "questions": len(rows), "by_type": {}}
    groups = {"all": rows}
    for qtype in common.QUESTION_TYPES:
        groups[qtype] = [r for r in rows if r["question_type"] == qtype]
    for name, group in groups.items():
        if not group:
            continue
        stats = {"n": len(group)}
        for k in KS:
            top = [set(h["position"] for h in r["hits"][:k]) for r in group]
            stats[f"any@{k}"] = round(sum(bool(t & set(r["answer_positions"])) for t, r in zip(top, group)) / len(group), 4)
            stats[f"all@{k}"] = round(sum(set(r["answer_positions"]) <= t for t, r in zip(top, group)) / len(group), 4)
        stats["empty"] = sum(1 for r in group if not r["hits"])
        stats["search_ms_median"] = sorted(r["ms"] for r in group)[len(group) // 2]
        table["by_type"][name] = stats
    return table


def main(argv: list[str]) -> int:
    if argv[0] == "prepare":
        data = common.work_dir() / "data"
        for split, name in common.DATASET_FILES.items():
            if len(argv) > 1 and split != argv[1]:
                continue
            if (data / name).exists():
                print(split, common.split_dataset(split, data / name), "questions")
        return 0
    if argv[0] == "run":
        only = argv[argv.index("--only") + 1].split(",") if "--only" in argv else None
        jobs = int(argv[argv.index("--jobs") + 1]) if "--jobs" in argv else 6
        run(argv[1], argv[2], only, jobs)
        return 0
    if argv[0] == "recall":
        table = recall(argv[1], argv[2])
        out = common.work_dir() / "retrieval" / argv[1] / f"recall-{argv[2]}-{tag()}.json"
        out.write_text(json.dumps(table, indent=1), encoding="utf-8")
        for name, s in table["by_type"].items():
            print(f"{name:28s} n={s['n']:3d}  any@1 {s['any@1']:.3f}  any@5 {s['any@5']:.3f}  "
                  f"any@10 {s['any@10']:.3f}  all@5 {s['all@5']:.3f}  all@10 {s['all@10']:.3f}  empty {s['empty']}")
        return 0
    raise SystemExit(__doc__)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
