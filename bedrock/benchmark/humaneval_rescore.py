#!/usr/bin/env python3
"""
Re-score previously-saved HumanEval completions with the current extractor and
evaluator (which prepend the prompt preamble: imports + helper functions).

This recomputes pass@1 WITHOUT re-running the models -- it reads the raw
completion text files saved under <results_dir>/completions/.

Usage:
    python3 humaneval_rescore.py <results_dir>
"""

import csv
import json
import sys
from pathlib import Path

from datasets import load_dataset

# Reuse the exact extraction + evaluation logic from the runner.
from humaneval_runner import extract_code, evaluate


def main():
    results_dir = Path(sys.argv[1])
    completions_dir = results_dir / "completions"

    # Pin to the same revision the runner used so re-scoring is reproducible.
    ds = load_dataset(
        "openai_humaneval",
        split="test",
        revision="7dce6050a7d6d172f3cc5c32aa97f52fa1a2e544",
    )
    by_id = {t["task_id"]: t for t in ds}

    results = []
    for f in sorted(completions_dir.glob("*.txt")):
        # filename: <model>__<task_id_with_underscores>.txt
        stem = f.stem
        model, tid_us = stem.split("__", 1)
        tid = tid_us.replace("_", "/", 1)  # HumanEval_0 -> HumanEval/0
        task = by_id.get(tid)
        if not task:
            continue
        output = f.read_text()
        code = extract_code(output, task["entry_point"]) if output else None
        ok, detail = evaluate(task, code)
        results.append({"model": model, "task_id": tid, "passed": ok, "detail": detail})

    # write rescored csv + summary
    out_csv = results_dir / "results_rescored.csv"
    with open(out_csv, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["model", "task_id", "passed", "detail"])
        w.writeheader()
        w.writerows(results)

    models = sorted({r["model"] for r in results})
    print("=" * 52)
    print("HumanEval pass@1 (rescored)")
    print("=" * 52)
    print(f"{'Model':<20} {'Pass@1':>10} {'Passed':>12}")
    print("-" * 46)
    summary = []
    for m in models:
        mr = [r for r in results if r["model"] == m]
        p = sum(1 for r in mr if r["passed"])
        rate = 100.0 * p / len(mr) if mr else 0
        print(f"{m:<20} {rate:>9.1f}% {p:>6}/{len(mr):<5}")
        summary.append({"model": m, "pass_at_1": round(rate, 1), "passed": p, "total": len(mr)})

    with open(results_dir / "summary_rescored.json", "w") as fh:
        json.dump(summary, fh, indent=2)

    # show remaining failures for transparency
    fails = [r for r in results if not r["passed"]]
    if fails:
        print("\nRemaining failures:")
        for r in fails:
            print(f"  {r['model']:<18} {r['task_id']:<16} {r['detail']}")
    print(f"\nRescored: {out_csv}")


if __name__ == "__main__":
    main()
