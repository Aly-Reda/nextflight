import json
import subprocess
import sys
import warnings

import pytest

from nextflight import (
    FlightExtractor,
    FlightParseError,
    extract,
    find_json_ld,
)
from nextflight.extractor import FlightExtractor as _FE  # same object, sanity check


def test_extract_shorthand_returns_extractor():
    html = '<script>self.__next_f.push([1, "1:{\\"a\\":1}"])</script>'
    page = extract(html)
    assert isinstance(page, FlightExtractor)
    assert page is not None


def test_basic_json_row():
    html = '''
    <script>self.__next_f.push([1, "1:[\\"$\\",\\"div\\",null,{\\"title\\":\\"hi\\"}]"])</script>
    '''
    page = FlightExtractor(html)
    resolved = page.resolve_all()
    assert "1" in resolved
    assert len(page) == 1
    assert "chunks=1" in repr(page)


def test_list_keys_then_fetch_json_for_one_key():
    # the two-step workflow: (1) see what keys exist on the page,
    # (2) fetch the resolved JSON for one of them
    html = r'''
    <script>self.__next_f.push([1, "1:{\"sections\":[1,2],\"meta\":{}}"])</script>
    <script>self.__next_f.push([1, "3f:{\"trims\":[{\"name\":\"A\"}]}"])</script>
    '''
    page = FlightExtractor(html)

    keys = page.keys()
    assert keys == ["1", "3f"]

    assert page["1"] == {"sections": [1, 2], "meta": {}}
    assert page["3f"] == {"trims": [{"name": "A"}]}
    assert page.resolve_chunk("3f") == page["3f"]

    assert "3f" in page
    assert "missing" not in page
    assert list(page) == keys

    with pytest.raises(KeyError):
        page["missing"]


def test_find_all_by_keys_returns_every_match():
    html = r'''
    <script>self.__next_f.push([1, "1:{\"cards\":[{\"id\":1,\"price\":10},{\"id\":2,\"price\":20},{\"id\":3,\"name\":\"no price here\"}]}"])</script>
    '''
    page = FlightExtractor(html)
    one = page.find_by_keys({"id", "price"})
    every = page.find_all_by_keys({"id", "price"})
    assert one == every[0]
    assert len(every) == 2
    assert {c["id"] for c in every} == {1, 2}


def test_find_text_regex_search():
    html = r'''
    <script>self.__next_f.push([1, "1:{\"email\":\"sales@example.com\",\"other\":\"nothing here\",\"phone\":\"555-1234\"}"])</script>
    '''
    page = FlightExtractor(html)
    # find_text matches whole string *values* containing the pattern, not
    # extracted substrings -- so it returns the full value(s) that matched.
    emails = page.find_text(r"[\w.+-]+@[\w-]+\.\w+")
    assert emails == ["sales@example.com"]

    phones = page.find_text(r"^\d{3}-\d{4}$")
    assert phones == ["555-1234"]

    import re as _re
    compiled = _re.compile(r"^nothing")
    notes = page.find_text(compiled)
    assert notes == ["nothing here"]

    # a value containing the pattern as a substring is still a match, even
    # if it isn't an exact match -- this is a substring search, not ==
    html2 = r'''
    <script>self.__next_f.push([1, "1:{\"a\":\"contact sales@example.com now\",\"b\":\"sales@example.com\"}"])</script>
    '''
    page2 = FlightExtractor(html2)
    matches = page2.find_text(r"[\w.+-]+@[\w-]+\.\w+")
    assert set(matches) == {"contact sales@example.com now", "sales@example.com"}


def test_get_dotted_path():
    html = r'''
    <script>self.__next_f.push([1, "3f:{\"props\":{\"items\":[{\"name\":\"widget\",\"price\":9}]}}"])</script>
    '''
    page = FlightExtractor(html)
    assert page.get("3f.props.items.0.name") == "widget"
    assert page.get("3f.props.items.0.price") == 9
    assert page.get("3f.props.items.99.name") is None
    assert page.get("3f.props.items.99.name", "fallback") == "fallback"
    assert page.get("nope.at.all", "missing") == "missing"


def test_stats():
    html = r'''
    <script>self.__next_f.push([1, "1:{\"a\":1}"])</script>
    <script>self.__next_f.push([1, "3f:[1,2,3]"])</script>
    '''
    page = FlightExtractor(html)
    stats = page.stats()
    assert stats["chunk_count"] == 2
    assert set(stats["chunk_ids"]) == {"1", "3f"}
    assert stats["html_size_bytes"] > 0
    assert "value_type_counts" in stats


def test_to_json_string_and_file(tmp_path):
    html = '<script>self.__next_f.push([1, "1:{\\"a\\":1}"])</script>'
    page = FlightExtractor(html)

    as_string = page.to_json()
    assert json.loads(as_string) == {"1": {"a": 1}}

    out_file = tmp_path / "out.json"
    result = page.to_json(str(out_file))
    assert result is None
    assert json.loads(out_file.read_text()) == {"1": {"a": 1}}


def test_extract_accepts_response_like_object():
    class FakeResponse:
        text = '<script>self.__next_f.push([1, "1:{\\"a\\":1}"])</script>'

    page = extract(FakeResponse())
    assert page["1"] == {"a": 1}

    class FakeBytesResponse:
        body = b'<script>self.__next_f.push([1, "1:{\\"a\\":1}"])</script>'

    page2 = extract(FakeBytesResponse())
    assert page2["1"] == {"a": 1}

    page3 = extract(b'<script>self.__next_f.push([1, "1:{\\"a\\":1}"])</script>')
    assert page3["1"] == {"a": 1}

    with pytest.raises(TypeError):
        extract(12345)


def test_find_by_keys():
    html = '''
    <script>self.__next_f.push([1, "1:{\\"sections\\":[1,2],\\"meta\\":{}}"])</script>
    '''
    page = FlightExtractor(html)
    found = page.find_by_keys({"sections", "meta"})
    assert found is not None
    assert found["sections"] == [1, 2]

    # Accept any iterable of keys, not just a set literal
    found2 = page.find_by_keys(["sections", "meta"])
    assert found2 == found


def test_find_one_matches_find_all_first_result():
    html = '''
    <script>self.__next_f.push([1, "1:{\\"items\\":[{\\"id\\":1},{\\"id\\":2}]}"])</script>
    '''
    page = FlightExtractor(html)
    all_items = page.find_all(lambda n: isinstance(n, dict) and "id" in n)
    one_item = page.find_one(lambda n: isinstance(n, dict) and "id" in n)
    assert one_item == all_items[0]


def test_find_by_type():
    html = '''
    <script>self.__next_f.push([1, "1:{\\"a\\":{\\"@type\\":\\"Product\\",\\"name\\":\\"Widget\\"},\\"b\\":{\\"@type\\":\\"Offer\\"}}"])</script>
    '''
    page = FlightExtractor(html)
    products = page.find_by_type("Product")
    assert len(products) == 1
    assert products[0]["name"] == "Widget"

    # custom key
    html2 = '<script>self.__next_f.push([1, "1:{\\"a\\":{\\"kind\\":\\"car\\"}}"])</script>'
    page2 = FlightExtractor(html2)
    cars = page2.find_by_type("car", key="kind")
    assert len(cars) == 1


def test_truncated_payload_does_not_crash():
    # A payload that ends right after an id: with nothing following it
    # (e.g. a proxy that cut the response off mid-chunk) must not raise.
    html = '''
    <script>self.__next_f.push([1, "1:[\\"a\\",\\"b\\"]\\n3f:"])</script>
    '''
    page = FlightExtractor(html)  # should not raise
    resolved = page.resolve_all()
    assert "1" in resolved


def test_strict_mode_raises_on_invalid_row():
    # An unresolvable, non-JSON, non-$-marker row should raise in strict mode
    html = '<script>self.__next_f.push([1, "1:not valid json and not a $ marker"])</script>'
    with pytest.raises(FlightParseError):
        FlightExtractor(html, strict=True)

    # ...but is tolerated (kept as a raw string) in default (non-strict) mode
    page = FlightExtractor(html, strict=False)
    assert page.raw_chunks["1"] == "not valid json and not a $ marker"


def test_find_json_ld():
    html = '''
    <script type="application/ld+json">{"@type": "Product", "name": "Widget"}</script>
    '''
    results = find_json_ld(html, type_="Product")
    assert len(results) == 1
    assert results[0]["name"] == "Widget"

    results_all = find_json_ld(html)
    assert results_all == results


def test_backward_compatible_aliases_still_work_with_deprecation_warning():
    from nextflight import NextFlightExtractor, extract_json_ld

    html = '<script>self.__next_f.push([1, "1:{\\"a\\":1}"])</script>'
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        page = NextFlightExtractor(html)
        assert any(issubclass(x.category, DeprecationWarning) for x in w)
    assert page.resolve_all()["1"] == {"a": 1}

    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        page.find_first(lambda n: isinstance(n, dict))
        assert any(issubclass(x.category, DeprecationWarning) for x in w)

    ld_html = '<script type="application/ld+json">{"@type": "Product"}</script>'
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        results = extract_json_ld(ld_html, schema_type="Product")
        assert any(issubclass(x.category, DeprecationWarning) for x in w)
    assert len(results) == 1


def test_cli_keys_lookup(tmp_path):
    html_file = tmp_path / "page.html"
    html_file.write_text(
        '<script>self.__next_f.push([1, "1:{\\"sections\\":[1],\\"meta\\":{}}"])</script>'
    )
    result = subprocess.run(
        [sys.executable, "-m", "nextflight.cli", str(html_file), "--keys", "sections,meta"],
        capture_output=True, text=True, check=True,
    )
    parsed = json.loads(result.stdout)
    assert parsed == {"sections": [1], "meta": {}}


def test_cli_requires_a_mode(tmp_path):
    html_file = tmp_path / "page.html"
    html_file.write_text('<script>self.__next_f.push([1, "1:{}"])</script>')
    result = subprocess.run(
        [sys.executable, "-m", "nextflight.cli", str(html_file)],
        capture_output=True, text=True,
    )
    assert result.returncode != 0


def test_cli_stats_and_save(tmp_path):
    html_file = tmp_path / "page.html"
    html_file.write_text('<script>self.__next_f.push([1, "1:{\\"a\\":1}"])</script>')

    result = subprocess.run(
        [sys.executable, "-m", "nextflight.cli", str(html_file), "--stats"],
        capture_output=True, text=True, check=True,
    )
    parsed = json.loads(result.stdout)
    assert parsed["chunk_count"] == 1

    out_file = tmp_path / "out.json"
    subprocess.run(
        [sys.executable, "-m", "nextflight.cli", str(html_file), "--all", "--save", str(out_file)],
        capture_output=True, text=True, check=True,
    )
    assert json.loads(out_file.read_text()) == {"1": {"a": 1}}


def test_cli_get_and_text(tmp_path):
    html_file = tmp_path / "page.html"
    html_file.write_text(
        '<script>self.__next_f.push([1, "1:{\\"a\\":{\\"b\\":42},\\"email\\":\\"x@y.com\\"}"])</script>'
    )

    result = subprocess.run(
        [sys.executable, "-m", "nextflight.cli", str(html_file), "--get", "1.a.b"],
        capture_output=True, text=True, check=True,
    )
    assert json.loads(result.stdout) == 42

    result2 = subprocess.run(
        [sys.executable, "-m", "nextflight.cli", str(html_file), "--text", r"[\w.]+@[\w.]+"],
        capture_output=True, text=True, check=True,
    )
    assert json.loads(result2.stdout) == ["x@y.com"]


# ------------------------------------------------------------------ #
# Regression tests: truncated text-row ("T<hexLen>,<body>") payloads
# must never raise an uncaught ValueError, matching the module's stated
# guarantee that it "holds up on both well-formed and truncated payloads".
# ------------------------------------------------------------------ #
def test_truncated_text_row_missing_comma_nonstrict():
    # "1:T1a" -- a text row header cut off before its comma ever arrives.
    html = (
        '<script>self.__next_f.push([1, '
        '"0:{\\"a\\":1}\\n1:T1a"])</script>'
    )
    page = extract(html)  # must not raise
    assert page.keys() == ["0"]


def test_truncated_text_row_missing_comma_strict():
    html = (
        '<script>self.__next_f.push([1, '
        '"0:{\\"a\\":1}\\n1:T1a"])</script>'
    )
    with pytest.raises(FlightParseError):
        extract(html, strict=True)


def test_truncated_text_row_short_body_nonstrict():
    # Header promises 0xff bytes but only "short" (5 bytes) actually follow.
    html = (
        '<script>self.__next_f.push([1, '
        '"0:{\\"a\\":1}\\n1:Tff,short"])</script>'
    )
    page = extract(html)  # must not raise
    assert page.resolve_chunk("1") == "short"


def test_truncated_text_row_short_body_strict():
    html = (
        '<script>self.__next_f.push([1, '
        '"0:{\\"a\\":1}\\n1:Tff,short"])</script>'
    )
    with pytest.raises(FlightParseError):
        extract(html, strict=True)


def test_well_formed_text_row_still_works():
    html = (
        '<script>self.__next_f.push([1, '
        '"0:{\\"sections\\":{\\"meta\\":1}}\\n1:T5,hello"])</script>'
    )
    page = extract(html)
    assert page.resolve_all() == {"0": {"sections": {"meta": 1}}, "1": "hello"}
