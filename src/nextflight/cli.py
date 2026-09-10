"""
Command-line entry point for quick exploration:

    nextflight page.html --keys sections,meta
    nextflight https://example.com/product/123 --keys price,title
    nextflight page.html --all > everything.json
    nextflight page.html --tree
    nextflight https://example.com/product/123 --watch 30
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time

from .extractor import FlightExtractor, detect_next_router, diff_pages, find_next_data

_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.\w+")
_PHONE_RE = re.compile(r"(?<!\d)(?:\+?\d[\d\-\s()]{7,}\d)(?!\d)")


def _redact(text: str) -> str:
    """Best-effort redaction of email- and phone-shaped strings from CLI
    output. Heuristic, not exhaustive -- for sharing debug output, not a
    compliance guarantee."""
    text = _EMAIL_RE.sub("[REDACTED_EMAIL]", text)
    text = _PHONE_RE.sub("[REDACTED_PHONE]", text)
    return text


def _load_html(source: str, use_rsc: bool = False) -> str:
    if source.startswith("http://") or source.startswith("https://"):
        extractor = (
            FlightExtractor.from_rsc_url(source) if use_rsc else FlightExtractor.from_url(source)
        )
        return extractor.html
    with open(source, "r", encoding="utf-8") as f:
        return f.read()


def _write_output(result, args) -> None:
    output = json.dumps(result, indent=args.indent, ensure_ascii=False, default=str)
    if args.redact:
        output = _redact(output)
    if args.save_path:
        with open(args.save_path, "w", encoding="utf-8") as f:
            f.write(output)
    else:
        sys.stdout.write(output)
        sys.stdout.write("\n")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="nextflight",
        description="Extract Next.js Flight (__next_f.push) data from a page.",
    )
    parser.add_argument("source", help="Path to an HTML file, or a URL to fetch.")
    parser.add_argument(
        "--keys", help="Comma-separated keys: find the first object containing all of them."
    )
    parser.add_argument(
        "--all-by-keys", dest="all_by_keys",
        help="Comma-separated keys: find EVERY object containing all of them (not just the first).",
    )
    parser.add_argument(
        "--any-keys", dest="any_keys",
        help="Comma-separated keys: find every object containing ANY of them.",
    )
    parser.add_argument(
        "--type", dest="type_value",
        help='Find every object whose "@type" (or --type-key) equals this value.',
    )
    parser.add_argument("--type-key", default="@type", help='Key to match --type against (default "@type").')
    parser.add_argument("--text", dest="text_pattern", help="Regex: list every distinct string value on the page that matches it.")
    parser.add_argument("--get", dest="get_path", help='Dotted path into the resolved page, e.g. "3f.props.price".')
    parser.add_argument("--all", action="store_true", help="Dump every resolved chunk.")
    parser.add_argument("--json-keys", dest="json_keys", action="store_true",
                         help="List chunk ids whose raw value is structured JSON (dict/list).")
    parser.add_argument("--html-keys", dest="html_keys", action="store_true",
                         help="List chunk ids from text rows that look like HTML fragments.")
    parser.add_argument("--tree", action="store_true",
                         help="Print a compact key/type shape summary of every chunk instead of full values.")
    parser.add_argument("--router", action="store_true",
                         help='Detect which Next.js router rendered the page: "app", "pages", "both", or "unknown".')
    parser.add_argument("--next-data", dest="next_data", action="store_true",
                         help="Dump the Pages Router __NEXT_DATA__ JSON blob instead of Flight data.")
    parser.add_argument("--stats", action="store_true", help="Print a quick diagnostic summary instead of data.")
    parser.add_argument("--watch", dest="watch_seconds", type=float, default=None,
                         help="Poll a URL every N seconds and print only what changed since the last poll (URL sources only).")
    parser.add_argument("--save", dest="save_path", help="Write output to this file instead of stdout.")
    parser.add_argument("--indent", type=int, default=2, help="JSON indent for output (default 2).")
    parser.add_argument("--redact", action="store_true",
                         help="Redact email- and phone-shaped strings from the output (best-effort).")
    parser.add_argument("--rsc", action="store_true",
                         help="Fetch the page's raw RSC payload (an RSC: 1 request) instead of the "
                              "full HTML page -- lighter weight, URL sources only. Auto-discovers a "
                              "build-specific _rsc id from the page when present.")
    return parser


def _watch(args, parser) -> int:
    if not (args.source.startswith("http://") or args.source.startswith("https://")):
        parser.error("--watch requires a URL source.")
        return 2
    prev = None
    try:
        while True:
            html = _load_html(args.source, use_rsc=args.rsc)
            current = FlightExtractor(html)
            timestamp = time.strftime("%Y-%m-%dT%H:%M:%S")
            if prev is None:
                sys.stdout.write(f"# [{timestamp}] baseline captured ({len(current)} chunks), watching...\n")
            else:
                d = diff_pages(prev, current)
                if d["added"] or d["removed"] or d["changed"]:
                    sys.stdout.write(f"# [{timestamp}] change detected\n")
                    output = json.dumps(d, indent=args.indent, ensure_ascii=False, default=str)
                    if args.redact:
                        output = _redact(output)
                    sys.stdout.write(output)
                    sys.stdout.write("\n")
                else:
                    sys.stdout.write(f"# [{timestamp}] no change\n")
            sys.stdout.flush()
            prev = current
            time.sleep(args.watch_seconds)
    except KeyboardInterrupt:
        return 0


def main(argv=None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.rsc and not (args.source.startswith("http://") or args.source.startswith("https://")):
        parser.error("--rsc requires a URL source.")
        return 2

    if args.watch_seconds is not None:
        return _watch(args, parser)

    html = _load_html(args.source, use_rsc=args.rsc)

    if args.router:
        _write_output(detect_next_router(html), args)
        return 0

    if args.next_data:
        result = find_next_data(html)
        if result is None:
            sys.stderr.write(
                "No __NEXT_DATA__ block found -- this may be an App Router "
                "page instead (try without --next-data, or check --router).\n"
            )
            return 1
        _write_output(result, args)
        return 0

    extractor = FlightExtractor(html)

    if args.keys:
        keys = {k.strip() for k in args.keys.split(",") if k.strip()}
        result = extractor.find_by_keys(keys)
    elif args.all_by_keys:
        keys = {k.strip() for k in args.all_by_keys.split(",") if k.strip()}
        result = extractor.find_all_by_keys(keys)
    elif args.any_keys:
        keys = {k.strip() for k in args.any_keys.split(",") if k.strip()}
        result = extractor.find_any_keys(keys)
    elif args.type_value:
        result = extractor.find_by_type(args.type_value, key=args.type_key)
    elif args.text_pattern:
        result = extractor.find_text(args.text_pattern)
    elif args.get_path:
        result = extractor.get(args.get_path)
    elif args.json_keys:
        result = extractor.json_keys()
    elif args.html_keys:
        result = extractor.html_keys()
    elif args.tree:
        result = extractor.shape()
    elif args.stats:
        result = extractor.stats()
    elif args.all:
        result = extractor.resolve_all()
    else:
        parser.error(
            "Provide one of --keys, --all-by-keys, --any-keys, --type, --text, --get, "
            "--json-keys, --html-keys, --tree, --router, --next-data, --stats, --watch, or --all."
        )
        return 2

    _write_output(result, args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
