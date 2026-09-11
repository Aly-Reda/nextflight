"""
Benchmark runner for nextflight. Plain stdlib `timeit` rather than
`pytest-benchmark`, so running benchmarks doesn't require an extra
dependency beyond what's already needed to develop the library at all.

    python benchmarks/run_benchmarks.py
    python benchmarks/run_benchmarks.py --json out.json   # for CI comparison

Covers, per page size (small ~50KB, medium ~500KB, large ~3MB+):
  - construction + full resolve_all() (the worst case: touch everything)
  - find_by_keys() for an early-appearing match (the lazy-resolution case)
  - repeated resolve_chunk() on the same id (memoization)
"""

import argparse
import json
import os
import sys
import timeit

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from benchmarks.generate_pages import generate_page, SIZE_PRESETS  # noqa: E402
from nextflight import extract  # noqa: E402


def bench_full_resolve(html: str, number: int) -> float:
    def run():
        extract(html).resolve_all()
    return timeit.timeit(run, number=number) / number


def bench_find_by_keys_early_match(html: str, number: int) -> float:
    def run():
        page = extract(html)
        page.find_by_keys({"__needle_price__", "__needle_title__"})
    return timeit.timeit(run, number=number) / number


def bench_repeated_resolve_chunk(html: str, number: int) -> float:
    page = extract(html)
    first_id = page.keys()[0]
    page.resolve_chunk(first_id)  # warm the cache once, outside the timed loop

    def run():
        page.resolve_chunk(first_id)
    return timeit.timeit(run, number=number) / number


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", help="Write results as JSON to this path")
    parser.add_argument("--repeats", type=int, default=5, help="timeit `number` for the fast benchmarks")
    args = parser.parse_args()

    results: dict = {}
    for size_name, num_chunks in SIZE_PRESETS.items():
        html = generate_page(num_chunks, seed=1)
        page_kb = len(html) / 1024

        full_resolve_number = max(1, args.repeats // max(1, num_chunks // 350))
        full_resolve_s = bench_full_resolve(html, number=full_resolve_number)
        early_match_s = bench_find_by_keys_early_match(html, number=args.repeats)
        repeated_resolve_s = bench_repeated_resolve_chunk(html, number=1000)

        page = extract(html)
        page.find_by_keys({"__needle_price__", "__needle_title__"})
        resolved_for_early_match = len(page._resolved_cache)

        results[size_name] = {
            "num_chunks": num_chunks,
            "page_kb": round(page_kb, 1),
            "full_resolve_seconds": full_resolve_s,
            "find_by_keys_early_match_seconds": early_match_s,
            "repeated_resolve_chunk_seconds": repeated_resolve_s,
            "chunks_resolved_for_early_match": resolved_for_early_match,
            "pages_per_second_full_resolve": round(1 / full_resolve_s, 1) if full_resolve_s else None,
        }

        print(f"--- {size_name} ({num_chunks} chunks, {page_kb:.1f} KB) ---")
        print(f"  full resolve_all():                 {full_resolve_s * 1000:.2f} ms/page "
              f"({results[size_name]['pages_per_second_full_resolve']} pages/sec)")
        print(f"  find_by_keys (early match):          {early_match_s * 1000:.3f} ms/call "
              f"(resolved {resolved_for_early_match}/{num_chunks} chunks)")
        print(f"  repeated resolve_chunk (warm cache): {repeated_resolve_s * 1e6:.2f} us/call")
        print()

    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(results, f, indent=2)
        print(f"wrote {args.json}")


if __name__ == "__main__":
    main()
