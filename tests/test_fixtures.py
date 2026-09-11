"""
Golden-file regression tests: every tests/fixtures/<version>/ pair must
still parse to exactly the previously-pinned output. This is the
regression safety net optimizations are checked against -- if a change
to the parser/resolver alters output for any of these, this test fails
loudly instead of the difference being noticed later on a real page.

Regenerate expected.json files (only after verifying a behavior change
is intentional and correct) with:

    python tests/fixtures/generate_fixtures.py
"""

import json
import os

import pytest

from nextflight import extract

FIXTURES_DIR = os.path.join(os.path.dirname(__file__), "fixtures")


def _fixture_versions():
    if not os.path.isdir(FIXTURES_DIR):
        return []
    return sorted(
        name for name in os.listdir(FIXTURES_DIR)
        if os.path.isdir(os.path.join(FIXTURES_DIR, name))
    )


@pytest.mark.parametrize("version", _fixture_versions())
def test_fixture_matches_pinned_output(version):
    version_dir = os.path.join(FIXTURES_DIR, version)
    with open(os.path.join(version_dir, "input.html"), encoding="utf-8") as f:
        html = f.read()
    with open(os.path.join(version_dir, "expected.json"), encoding="utf-8") as f:
        expected = json.load(f)

    page = extract(html)
    actual = {
        "resolve_all": page.resolve_all(),
        "keys": page.keys(),
        "stats": {
            k: v for k, v in page.stats().items()
            if k not in ("html_size_bytes", "resolve_cache_hits", "resolve_cache_misses")
        },
    }
    # Compare through a JSON round-trip so key ordering/tuple-vs-list
    # differences that don't matter to JSON equality don't cause false
    # failures -- what matters is the pinned *value*, not the exact
    # Python container types the current implementation happens to use.
    assert json.loads(json.dumps(actual, default=str)) == expected


def test_fixture_corpus_is_non_empty():
    # A corpus with zero fixtures would make the parametrized test above
    # silently pass with nothing to check -- guard against that.
    assert len(_fixture_versions()) >= 4


def test_fixture_next_version_hint_is_plausible():
    # Loose sanity check: next_version_hint() should recognize markers
    # present in fixtures built specifically to contain them, without
    # pinning the exact range string (which would make this test as
    # brittle as the registry itself).
    version_dir = os.path.join(FIXTURES_DIR, "13.4")
    if not os.path.isdir(version_dir):
        pytest.skip("13.4 fixture not present")
    with open(os.path.join(version_dir, "input.html"), encoding="utf-8") as f:
        html = f.read()
    page = extract(html)
    hint = page.next_version_hint()
    assert hint["range"] is not None
