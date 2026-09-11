"""
Compares a freshly-run benchmark against the checked-in baseline
(benchmarks/baseline.json) and fails (non-zero exit) if any page size's
`full_resolve_seconds` regresses by more than `--threshold` (default 10%).

Used by the `benchmark` CI job (see .github/workflows/ci.yml). Run
locally with:

    python benchmarks/run_benchmarks.py --json /tmp/current.json
    python benchmarks/check_regression.py /tmp/current.json

To intentionally update the baseline after a verified, real improvement
(or an accepted regression with a documented reason), overwrite
benchmarks/baseline.json with a fresh run's output and commit it.
"""

import argparse
import json
import os
import sys

BASELINE_PATH = os.path.join(os.path.dirname(__file__), "baseline.json")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("current_json", help="Path to a JSON file produced by run_benchmarks.py --json")
    parser.add_argument("--threshold", type=float, default=0.10, help="Fractional regression threshold (default 0.10 = 10%%)")
    parser.add_argument("--baseline", default=BASELINE_PATH, help="Path to the baseline JSON to compare against")
    args = parser.parse_args()

    if not os.path.exists(args.baseline):
        print(f"No baseline found at {args.baseline} -- nothing to compare against, treating as pass.")
        return 0

    with open(args.baseline, encoding="utf-8") as f:
        baseline = json.load(f)
    with open(args.current_json, encoding="utf-8") as f:
        current = json.load(f)

    failed = False
    for size_name, baseline_entry in baseline.items():
        current_entry = current.get(size_name)
        if current_entry is None:
            print(f"WARN: {size_name} missing from current run, skipping.")
            continue
        for metric in ("full_resolve_seconds", "find_by_keys_early_match_seconds"):
            old = baseline_entry.get(metric)
            new = current_entry.get(metric)
            if old is None or new is None:
                continue
            regression = (new - old) / old if old else 0.0
            status = "REGRESSION" if regression > args.threshold else "ok"
            print(f"{size_name:8s} {metric:35s} baseline={old*1000:8.3f}ms current={new*1000:8.3f}ms "
                  f"delta={regression * 100:+6.1f}% [{status}]")
            if regression > args.threshold:
                failed = True

    if failed:
        print(f"\nOne or more benchmarks regressed by more than {args.threshold * 100:.0f}%.")
        return 1
    print("\nNo regressions beyond threshold.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
