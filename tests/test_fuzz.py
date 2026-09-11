"""
Fuzz tests targeting the hand-rolled, crash-prone parsing internals:
`_split_rows`, `_read_balanced`, `_attempt_repair`, `_close_unbalanced`.

These don't assert specific output (there's no "correct" answer for
arbitrary garbage input) -- they assert the much more basic, load-bearing
property that malformed/adversarial input never raises an unhandled
exception, on any of `strict=False` (default), `repair=True`, or through
the public `extract()`/`FlightExtractor` constructor. A crash on
attacker- or CDN-mangled input in production is the failure mode this
guards against.

Requires the optional `hypothesis` package -- skipped entirely if it
isn't installed, same as any other purely-developer-facing test
dependency (this is not part of the zero-dependency runtime install).
"""

import json

import pytest

hypothesis = pytest.importorskip("hypothesis")
from hypothesis import given, settings, strategies as st  # noqa: E402

from nextflight.extractor import FlightExtractor  # noqa: E402

# Characters that actually matter to the row grammar, plus a little
# printable noise -- an unconstrained fully-random Unicode strategy
# would spend almost all its budget on inputs that trivially bail out of
# every branch immediately, rather than exercising bracket/quote
# balancing logic.
_GRAMMAR_CHARS = st.sampled_from(list('{}[]":,0123456789abcdefTIHL$@\\n \t'))
_ROW_BODIES = st.text(alphabet=_GRAMMAR_CHARS, min_size=0, max_size=200)


def _push_html(row_id: str, body: str) -> str:
    payload = f"{row_id}:{body}"
    return f"<script>self.__next_f.push([1,{json.dumps(payload)}])</script>"


@given(body=_ROW_BODIES)
@settings(max_examples=300, deadline=None)
def test_split_rows_never_crashes_non_strict(body):
    html = _push_html("9", body)
    page = FlightExtractor(html, strict=False)
    page.resolve_all()  # force full materialization, not just parsing


@given(body=_ROW_BODIES)
@settings(max_examples=300, deadline=None)
def test_split_rows_never_crashes_with_repair(body):
    html = _push_html("9", body)
    page = FlightExtractor(html, repair=True)
    page.resolve_all()
    page.parse_confidence()


@given(body=st.text(alphabet=_GRAMMAR_CHARS, min_size=0, max_size=200))
@settings(max_examples=200, deadline=None)
def test_attempt_repair_never_crashes_or_hangs(body):
    # Directly fuzz the repair internals with JSON-shaped-ish garbage --
    # the constructor path above already covers this indirectly, but a
    # direct call pins the contract of `_attempt_repair` itself (returns
    # a value or None, never raises) independent of how it's invoked.
    result = FlightExtractor._attempt_repair(body)
    assert result is None or isinstance(result, (dict, list, str, int, float, bool))


@given(body=st.text(alphabet=_GRAMMAR_CHARS, min_size=0, max_size=300))
@settings(max_examples=200, deadline=None)
def test_close_unbalanced_always_returns_parseable_bracket_structure(body):
    closed = FlightExtractor._close_unbalanced(body)
    # Whatever this produces, brackets/quotes must be balanced -- that's
    # the entire contract of this function. Re-run the same tracking
    # logic used inside it (rather than re-implementing bracket counting
    # here) by checking the stack empties out and no string is left open.
    stack = []
    in_string = False
    escape = False
    for ch in closed:
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch in "[{":
            stack.append("]" if ch == "[" else "}")
        elif ch in "]}":
            if stack and stack[-1] == ch:
                stack.pop()
    assert not in_string
    assert not stack


@given(
    row_id=st.text(alphabet=st.sampled_from(list("abcXYZ019_-")), min_size=0, max_size=5),
    body=_ROW_BODIES,
)
@settings(max_examples=200, deadline=None)
def test_arbitrary_row_id_never_crashes(row_id, body):
    html = _push_html(row_id, body)
    page = FlightExtractor(html, repair=True)
    page.resolve_all()


@given(pieces=st.lists(_ROW_BODIES, min_size=1, max_size=6))
@settings(max_examples=100, deadline=None)
def test_multi_row_payload_never_crashes(pieces):
    payload = "\n".join(f"{i}:{p}" for i, p in enumerate(pieces))
    html = f"<script>self.__next_f.push([1,{json.dumps(payload)}])</script>"
    page = FlightExtractor(html, repair=True)
    page.resolve_all()
    page.stats()
    page.parse_confidence()
