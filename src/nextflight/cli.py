"""
Command-line entry point for quick exploration:

    nextflight page.html --keys sections,meta
    nextflight https://example.com/product/123 --keys price,title
    nextflight page.html --all > everything.json
"""

from __future__ import annotations

import argparse
import json
import sys

from .extractor import FlightExtractor


def _load_html(source: str) -> str:
    if source.startswith("http://") or source.startswith("https://"):
        extractor = FlightExtractor.from_url(source)
        return extractor.html
    with open(source, "r", encoding="utf-8") as f:
        return f.read()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="nextflight",
        description="Extract Next.js Flight (__next_f.push) data from a page.",
    )
    parser.add_argument("source", help="Path to an HTML file, or a URL to fetch.")
    parser.add_argument(
        "--keys", help="Comma-separated keys: find the first object containing all of them."
    )
    parser.add_argument(
        "--type", dest="type_value",
        help='Find every object whose "@type" (or --type-key) equals this value.',
    )
    parser.add_argument("--type-key", default="@type", help='Key to match --type against (default "@type").')
    parser.add_argument("--all", action="store_true", help="Dump every resolved chunk.")
    parser.add_argument("--indent", type=int, default=2, help="JSON indent for output (default 2).")
    args = parser.parse_args(argv)

    html = _load_html(args.source)
    extractor = FlightExtractor(html)

    if args.keys:
        keys = {k.strip() for k in args.keys.split(",") if k.strip()}
        result = extractor.find_by_keys(keys)
    elif args.type_value:
        result = extractor.find_by_type(args.type_value, key=args.type_key)
    elif args.all:
        result = extractor.resolve_all()
    else:
        parser.error("Provide one of --keys, --type, or --all.")
        return 2

    json.dump(result, sys.stdout, indent=args.indent, ensure_ascii=False, default=str)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
