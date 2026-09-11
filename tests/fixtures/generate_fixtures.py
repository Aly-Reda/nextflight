"""
Generates tests/fixtures/<version>/input.html + expected.json pairs.

These are synthetic, anonymized Flight payloads constructed to exercise
the wire-format quirks documented in known_formats.yaml for each Next.js
version range -- not captures of any real site. `expected.json` is
produced by running the *current, trusted* FlightExtractor over
`input.html`, so this corpus is a regression pin (catches an
optimization accidentally changing behavior) rather than independent
ground truth -- see tests/test_fixtures.py, which is what actually
enforces it.

Run this only when deliberately adding a new fixture or updating one
after a verified, intentional behavior change:

    python tests/fixtures/generate_fixtures.py
"""

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from nextflight import extract  # noqa: E402

FIXTURES_DIR = os.path.dirname(__file__)


def _push(*rows: str) -> str:
    payload = "\n".join(rows)
    return f"<script>self.__next_f.push([1,{json.dumps(payload)}])</script>"


FIXTURES = {
    # 13.0 - 13.3: earliest App Router format. Plain JSON rows, a couple
    # of $-refs, no HL preload rows at all.
    "13.0": _push(
        '0:["$","div",null,{"children":["$","h1",null,{"children":"$1"}]}]',
        '1:"Hello World"',
        '2:{"title":"Product A","price":1999}',
    ),
    # 13.4 - 14.0: introduces HL preload rows, frequently sharing a
    # completely empty chunk id across many rows on the same page.
    "13.4": (
        _push(
            '0:["$","div",null,{"children":"$2"}]',
            '2:{"title":"Product B","price":2999}',
        )
        + '<script>self.__next_f.push([1,":HL[\\"/a.css\\",\\"style\\"]"])</script>'
        + '<script>self.__next_f.push([1,":HL[\\"/b.css\\",\\"style\\"]"])</script>'
    ),
    # 14.1 - 14.2: wider use of "$@<id>" Suspense placeholder markers for
    # streamed, below-the-fold content.
    "14.1": _push(
        '0:["$","div",null,{"children":["$","section",null,{"children":"$@5"}]}]',
        '5:{"title":"Product C","price":3999,"reviews":["$6"]}',
        '6:{"author":"A. Reviewer","rating":5}',
    ),
    # 15.0+: additional well-known "$S<name>" symbol references for
    # built-in React symbols (Fragment, Suspense), not just user
    # components.
    "15.0": _push(
        '0:["$","$Sreact.fragment",null,{"children":["$1","$2"]}]',
        '1:["$","$Sreact.suspense",null,{"children":"Loading..."}]',
        '2:{"title":"Product D","price":4999,"tags":["a","b"]}',
    ),
}


def main() -> None:
    for version, html in FIXTURES.items():
        version_dir = os.path.join(FIXTURES_DIR, version)
        os.makedirs(version_dir, exist_ok=True)
        with open(os.path.join(version_dir, "input.html"), "w", encoding="utf-8") as f:
            f.write(html)
        page = extract(html)
        expected = {
            "resolve_all": page.resolve_all(),
            "keys": page.keys(),
            "stats": {
                k: v for k, v in page.stats().items()
                if k not in ("html_size_bytes", "resolve_cache_hits", "resolve_cache_misses")
                # html_size_bytes varies trivially with formatting;
                # resolve_cache_hits/misses are diagnostic counters that
                # depend on call order/history, not on parsing behavior --
                # pinning them would make this golden file break on a
                # correct future optimization that merely changes how
                # many times something gets looked up internally.
            },
        }
        with open(os.path.join(version_dir, "expected.json"), "w", encoding="utf-8") as f:
            json.dump(expected, f, indent=2, sort_keys=True, default=str)
        print(f"wrote fixture: {version}")


if __name__ == "__main__":
    main()
