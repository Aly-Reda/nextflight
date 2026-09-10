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


def test_json_keys_and_html_keys_and_text_keys():
    html = (
        '<script>self.__next_f.push([1, '
        '"0:{\\"sections\\":[1,2]}\\n'
        '1:T25,<div class=card><span>hi</span></div>\\n'
        '2:T5,hello\\n'
        '3:[1,2,3]"'
        '])</script>'
    )
    page = extract(html)
    assert page.json_keys() == ["0", "3"]
    assert page.text_keys() == ["1", "2"]
    assert page.html_keys() == ["1"]  # only the one that actually has a tag
    assert page.kind("0") == "json"
    assert page.kind("1") == "text"
    assert page.kind("missing") is None


def test_json_keys_html_keys_empty_on_page_with_no_matches():
    html = '<script>self.__next_f.push([1, "1:{\\"a\\":1}"])</script>'
    page = extract(html)
    assert page.json_keys() == ["1"]
    assert page.html_keys() == []
    assert page.text_keys() == []


def test_stats_includes_row_kind_and_json_html_counts():
    html = (
        '<script>self.__next_f.push([1, '
        '"0:{\\"a\\":1}\\n1:T9,<b>hi</b>"'
        '])</script>'
    )
    page = extract(html)
    stats = page.stats()
    assert stats["json_chunk_count"] == 1
    assert stats["html_chunk_count"] == 1
    assert stats["flight_row_kind_counts"] == {"json": 1, "text": 1}


def test_cli_json_keys_and_html_keys(tmp_path):
    html_file = tmp_path / "page.html"
    html_file.write_text(
        '<script>self.__next_f.push([1, '
        '"0:{\\"a\\":1}\\n1:T9,<b>hi</b>"'
        '])</script>'
    )
    result = subprocess.run(
        [sys.executable, "-m", "nextflight.cli", str(html_file), "--json-keys"],
        capture_output=True, text=True, check=True,
    )
    assert json.loads(result.stdout) == ["0"]

    result2 = subprocess.run(
        [sys.executable, "-m", "nextflight.cli", str(html_file), "--html-keys"],
        capture_output=True, text=True, check=True,
    )
    assert json.loads(result2.stdout) == ["1"]


# ------------------------------------------------------------------ #
# Performance regression guard: text-row parsing must be roughly linear
# in payload size, not quadratic. The naive `payload[body_start:].encode()`
# approach re-encodes the whole remainder of the payload on every text row,
# which blows up badly on pages with many text rows (translated copy,
# repeated card fragments, etc). This doesn't assert a hard time bound
# (too flaky across CI machines) -- it asserts that doubling the number of
# text rows doesn't roughly quadruple the time, which the O(n^2) bug did.
# ------------------------------------------------------------------ #
def test_many_text_rows_scales_roughly_linearly():
    import time

    def make_html(n_rows, body_len=300):
        body = "hello world, " * (body_len // 13 + 1)
        body = body[:body_len]
        hex_len = format(len(body.encode("utf-8")), "x")
        rows = [f"{i}:T{hex_len},{body}" for i in range(n_rows)]
        payload = "\n".join(rows)
        return f'<script>self.__next_f.push([1, {json.dumps(payload)}])</script>'

    small_html = make_html(300)
    large_html = make_html(2400)  # 8x the rows

    t0 = time.perf_counter()
    extract(small_html)
    small_time = time.perf_counter() - t0

    t0 = time.perf_counter()
    extract(large_html)
    large_time = time.perf_counter() - t0

    # Linear scaling would give ~8x; quadratic would give ~64x. Allow
    # generous headroom for noise/overhead but catch a regression back to
    # quadratic behaviour.
    assert large_time < small_time * 25, (
        f"text-row parsing looks quadratic again: {small_time=:.4f}s "
        f"for 300 rows vs {large_time=:.4f}s for 2400 rows"
    )


def test_well_formed_text_row_still_works():
    html = (
        '<script>self.__next_f.push([1, '
        '"0:{\\"sections\\":{\\"meta\\":1}}\\n1:T5,hello"])</script>'
    )
    page = extract(html)
    assert page.resolve_all() == {"0": {"sections": {"meta": 1}}, "1": "hello"}


# ------------------------------------------------------------------ #
# Pages Router support: __NEXT_DATA__ / router detection
# ------------------------------------------------------------------ #
def test_find_next_data():
    from nextflight import find_next_data

    html = (
        '<script id="__NEXT_DATA__" type="application/json">'
        '{"props":{"pageProps":{"price":100}},"page":"/x"}'
        '</script>'
    )
    data = find_next_data(html)
    assert data == {"props": {"pageProps": {"price": 100}}, "page": "/x"}


def test_find_next_data_returns_none_when_absent():
    from nextflight import find_next_data

    app_html = '<script>self.__next_f.push([1, "1:{\\"a\\":1}"])</script>'
    assert find_next_data(app_html) is None
    assert find_next_data("<html><body>plain</body></html>") is None


def test_find_next_data_returns_none_on_invalid_json():
    from nextflight import find_next_data

    html = '<script id="__NEXT_DATA__" type="application/json">not json</script>'
    assert find_next_data(html) is None


def test_detect_next_router():
    from nextflight import detect_next_router

    app_html = '<script>self.__next_f.push([1, "1:{\\"a\\":1}"])</script>'
    pages_html = (
        '<script id="__NEXT_DATA__" type="application/json">{"a":1}</script>'
    )
    assert detect_next_router(app_html) == "app"
    assert detect_next_router(pages_html) == "pages"
    assert detect_next_router(app_html + pages_html) == "both"
    assert detect_next_router("<html></html>") == "unknown"


# ------------------------------------------------------------------ #
# Query ergonomics: find_any_keys / find_by_key_pattern
# ------------------------------------------------------------------ #
def test_find_any_keys():
    html = (
        '<script>self.__next_f.push([1, '
        '"1:{\\"cards\\":[{\\"price_usd\\":10},{\\"price_aed\\":40},{\\"other\\":1}]}"'
        '])</script>'
    )
    page = extract(html)
    matches = page.find_any_keys({"price_usd", "price_aed"})
    assert len(matches) == 2
    assert {"price_usd": 10} in matches
    assert {"price_aed": 40} in matches
    assert {"other": 1} not in matches


def test_find_by_key_pattern():
    html = (
        '<script>self.__next_f.push([1, '
        '"1:{\\"cards\\":[{\\"price_usd\\":10},{\\"price_aed\\":40},{\\"other\\":1}]}"'
        '])</script>'
    )
    page = extract(html)
    matches = page.find_by_key_pattern(r"^price_")
    assert len(matches) == 2

    import re as _re
    matches2 = page.find_by_key_pattern(_re.compile(r"^price_"))
    assert matches2 == matches


# ------------------------------------------------------------------ #
# Provenance tracking: include_source
# ------------------------------------------------------------------ #
def test_find_all_include_source():
    html = (
        '<script>self.__next_f.push([1, '
        '"0:{\\"id\\":1}\\n1:{\\"id\\":2}"'
        '])</script>'
    )
    page = extract(html)
    results = page.find_all(
        lambda n: isinstance(n, dict) and "id" in n, include_source=True
    )
    assert results == [({"id": 1}, "0"), ({"id": 2}, "1")]

    # default (no include_source) is unchanged: bare nodes
    plain = page.find_all(lambda n: isinstance(n, dict) and "id" in n)
    assert plain == [{"id": 1}, {"id": 2}]


def test_find_by_keys_include_source():
    html = (
        '<script>self.__next_f.push([1, '
        '"0:{\\"sections\\":[1,2],\\"meta\\":{}}"'
        '])</script>'
    )
    page = extract(html)
    result = page.find_by_keys({"sections", "meta"}, include_source=True)
    assert result == ({"sections": [1, 2], "meta": {}}, "0")


def test_find_all_by_keys_include_source():
    html = (
        '<script>self.__next_f.push([1, '
        '"0:{\\"id\\":1,\\"price\\":5}\\n1:{\\"id\\":2,\\"price\\":6}"'
        '])</script>'
    )
    page = extract(html)
    results = page.find_all_by_keys({"id", "price"}, include_source=True)
    assert results == [
        ({"id": 1, "price": 5}, "0"),
        ({"id": 2, "price": 6}, "1"),
    ]


def test_include_source_with_custom_root_is_none():
    html = '<script>self.__next_f.push([1, "1:{\\"id\\":1}"])</script>'
    page = extract(html)
    root = page.resolve_chunk("1")
    result = page.find_all(lambda n: isinstance(n, dict) and "id" in n,
                            root=root, include_source=True)
    assert result == [({"id": 1}, None)]


# ------------------------------------------------------------------ #
# shape() / .stats() tree summary
# ------------------------------------------------------------------ #
def test_shape_summarizes_structure_not_values():
    html = (
        '<script>self.__next_f.push([1, '
        '"1:{\\"a\\":{\\"b\\":[1,2,3]},\\"c\\":\\"hi\\"}"'
        '])</script>'
    )
    page = extract(html)
    shape = page.shape()
    assert shape == {"1": {"a": {"b": ["int"]}, "c": "str"}}

    # single chunk
    assert page.shape("1") == {"a": {"b": ["int"]}, "c": "str"}


def test_shape_max_depth_collapses_deep_branches():
    html = (
        '<script>self.__next_f.push([1, '
        '"1:{\\"a\\":{\\"b\\":{\\"c\\":{\\"d\\":1}}}}"'
        '])</script>'
    )
    page = extract(html)
    shallow = page.shape("1", max_depth=1)
    assert shallow == {"a": "dict"}


def test_shape_handles_empty_list():
    html = '<script>self.__next_f.push([1, "1:{\\"items\\":[]}"])</script>'
    page = extract(html)
    assert page.shape("1") == {"items": []}


# ------------------------------------------------------------------ #
# diff_pages / page.diff
# ------------------------------------------------------------------ #
def test_diff_pages_detects_added_removed_changed():
    from nextflight import diff_pages

    old_html = '<script>self.__next_f.push([1, "1:{\\"price\\":100,\\"stock\\":5}"])</script>'
    new_html = (
        '<script>self.__next_f.push([1, '
        '"1:{\\"price\\":90,\\"promo\\":true}"'
        '])</script>'
    )
    old_page = extract(old_html)
    new_page = extract(new_html)

    d = diff_pages(old_page, new_page)
    assert d["changed"] == {"1.price": (100, 90)}
    assert d["added"] == {"1.promo": True}
    assert d["removed"] == {"1.stock": 5}


def test_diff_pages_no_changes_when_identical():
    from nextflight import diff_pages

    html = '<script>self.__next_f.push([1, "1:{\\"price\\":100}"])</script>'
    page_a = extract(html)
    page_b = extract(html)
    d = diff_pages(page_a, page_b)
    assert d == {"added": {}, "removed": {}, "changed": {}}


def test_page_diff_method_matches_diff_pages_function():
    from nextflight import diff_pages

    old_html = '<script>self.__next_f.push([1, "1:{\\"a\\":1}"])</script>'
    new_html = '<script>self.__next_f.push([1, "1:{\\"a\\":2}"])</script>'
    old_page = extract(old_html)
    new_page = extract(new_html)
    assert old_page.diff(new_page) == diff_pages(old_page, new_page)


# ------------------------------------------------------------------ #
# iter_resolved
# ------------------------------------------------------------------ #
def test_iter_resolved_matches_resolve_all():
    html = (
        '<script>self.__next_f.push([1, '
        '"0:{\\"a\\":1}\\n1:{\\"b\\":2}"'
        '])</script>'
    )
    page = extract(html)
    assert dict(page.iter_resolved()) == page.resolve_all()


def test_iter_resolved_is_lazy_generator():
    import types

    html = '<script>self.__next_f.push([1, "0:{\\"a\\":1}\\n1:{\\"b\\":2}"])</script>'
    page = extract(html)
    gen = page.iter_resolved()
    assert isinstance(gen, types.GeneratorType)
    first = next(gen)
    assert first == ("0", {"a": 1})


# ------------------------------------------------------------------ #
# to_dataframe / to_csv
# ------------------------------------------------------------------ #
def test_to_dataframe_requires_records_or_required_keys():
    html = '<script>self.__next_f.push([1, "1:{\\"a\\":1}"])</script>'
    page = extract(html)
    with pytest.raises(ValueError):
        page.to_dataframe()


def test_to_dataframe_from_required_keys():
    pd = pytest.importorskip("pandas")
    html = (
        '<script>self.__next_f.push([1, '
        '"1:{\\"cards\\":[{\\"id\\":1,\\"price\\":10},{\\"id\\":2,\\"price\\":20}]}"'
        '])</script>'
    )
    page = extract(html)
    df = page.to_dataframe(required_keys={"id", "price"})
    assert list(df["id"]) == [1, 2]
    assert list(df["price"]) == [10, 20]


def test_to_dataframe_from_explicit_records():
    pytest.importorskip("pandas")
    html = '<script>self.__next_f.push([1, "1:{\\"a\\":1}"])</script>'
    page = extract(html)
    df = page.to_dataframe(records=[{"x": 1}, {"x": 2}])
    assert list(df["x"]) == [1, 2]


def test_to_csv_falls_back_to_stdlib_without_pandas(tmp_path, monkeypatch):
    import builtins

    html = (
        '<script>self.__next_f.push([1, '
        '"1:{\\"cards\\":[{\\"id\\":1,\\"price\\":10}]}"'
        '])</script>'
    )
    page = extract(html)
    out_path = tmp_path / "out.csv"

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "pandas":
            raise ImportError("blocked for test")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    page.to_csv(str(out_path), required_keys={"id", "price"})
    content = out_path.read_text()
    assert "id" in content and "price" in content
    assert "1" in content and "10" in content


def test_to_csv_empty_records_writes_essentially_empty_file(tmp_path):
    html = '<script>self.__next_f.push([1, "1:{\\"a\\":1}"])</script>'
    page = extract(html)
    out_path = tmp_path / "empty.csv"
    page.to_csv(str(out_path), records=[])
    # With pandas installed, an empty DataFrame's to_csv writes just a
    # blank line; without pandas, the stdlib fallback writes nothing at
    # all. Either is fine -- no data rows, no crash.
    assert out_path.read_text().strip() == ""


# ------------------------------------------------------------------ #
# CLI: --router, --next-data, --tree, --any-keys, --redact
# ------------------------------------------------------------------ #
def test_cli_router(tmp_path):
    html_file = tmp_path / "page.html"
    html_file.write_text('<script>self.__next_f.push([1, "1:{\\"a\\":1}"])</script>')
    result = subprocess.run(
        [sys.executable, "-m", "nextflight.cli", str(html_file), "--router"],
        capture_output=True, text=True, check=True,
    )
    assert json.loads(result.stdout) == "app"


def test_cli_next_data(tmp_path):
    html_file = tmp_path / "page.html"
    html_file.write_text(
        '<script id="__NEXT_DATA__" type="application/json">{"props":{"a":1}}</script>'
    )
    result = subprocess.run(
        [sys.executable, "-m", "nextflight.cli", str(html_file), "--next-data"],
        capture_output=True, text=True, check=True,
    )
    assert json.loads(result.stdout) == {"props": {"a": 1}}


def test_cli_next_data_missing_errors(tmp_path):
    html_file = tmp_path / "page.html"
    html_file.write_text('<script>self.__next_f.push([1, "1:{\\"a\\":1}"])</script>')
    result = subprocess.run(
        [sys.executable, "-m", "nextflight.cli", str(html_file), "--next-data"],
        capture_output=True, text=True,
    )
    assert result.returncode == 1
    assert "No __NEXT_DATA__" in result.stderr


def test_cli_tree(tmp_path):
    html_file = tmp_path / "page.html"
    html_file.write_text(
        '<script>self.__next_f.push([1, "1:{\\"a\\":{\\"b\\":1}}"])</script>'
    )
    result = subprocess.run(
        [sys.executable, "-m", "nextflight.cli", str(html_file), "--tree"],
        capture_output=True, text=True, check=True,
    )
    assert json.loads(result.stdout) == {"1": {"a": {"b": "int"}}}


def test_cli_any_keys(tmp_path):
    html_file = tmp_path / "page.html"
    html_file.write_text(
        '<script>self.__next_f.push([1, "1:{\\"a\\":1,\\"b\\":2}"])</script>'
    )
    result = subprocess.run(
        [sys.executable, "-m", "nextflight.cli", str(html_file), "--any-keys", "a,z"],
        capture_output=True, text=True, check=True,
    )
    assert json.loads(result.stdout) == [{"a": 1, "b": 2}]


def test_cli_redact(tmp_path):
    html_file = tmp_path / "page.html"
    html_file.write_text(
        '<script id="__NEXT_DATA__" type="application/json">'
        '{"email":"sales@example.com"}</script>'
    )
    result = subprocess.run(
        [sys.executable, "-m", "nextflight.cli", str(html_file), "--next-data", "--redact"],
        capture_output=True, text=True, check=True,
    )
    assert "sales@example.com" not in result.stdout
    assert "[REDACTED_EMAIL]" in result.stdout


# ------------------------------------------------------------------ #
# resolve_json / resolve_html / resolve_text
# ------------------------------------------------------------------ #
def test_resolve_json_html_text_partition_the_page():
    html = (
        '<script>self.__next_f.push([1, '
        '"0:{\\"a\\":1}\\n1:T9,<b>hi</b>\\n2:T5,hello\\n3:[1,2,3]"'
        '])</script>'
    )
    page = extract(html)
    assert page.resolve_json() == {"0": {"a": 1}, "3": [1, 2, 3]}
    assert page.resolve_html() == {"1": "<b>hi</b>"}
    assert page.resolve_text() == {"1": "<b>hi</b>", "2": "hello"}
    # resolve_text is a superset of resolve_html; resolve_json + resolve_text
    # together account for every chunk on this page (no overlap here).
    combined = {**page.resolve_json(), **page.resolve_text()}
    assert combined == page.resolve_all()


def test_resolve_json_follows_refs_into_text_chunks():
    # A JSON chunk can reference a text chunk via a `$`-ref; resolve_json()
    # should still fully dereference that, even though the referenced
    # chunk itself is text-typed and wouldn't show up in resolve_json()'s
    # own top-level key list.
    html = (
        '<script>self.__next_f.push([1, '
        '"0:{\\"label\\":\\"$1\\"}\\n1:T5,hello"'
        '])</script>'
    )
    page = extract(html)
    assert page.json_keys() == ["0"]
    assert page.resolve_json() == {"0": {"label": "hello"}}


def test_resolve_html_and_text_empty_when_no_such_rows():
    html = '<script>self.__next_f.push([1, "1:{\\"a\\":1}"])</script>'
    page = extract(html)
    assert page.resolve_html() == {}
    assert page.resolve_text() == {}
    assert page.resolve_json() == {"1": {"a": 1}}


# ------------------------------------------------------------------ #
# find_one / find_by_keys stop RESOLVING early, not just searching early
# ------------------------------------------------------------------ #
def test_find_one_does_not_resolve_chunks_past_the_match():
    # Build a page where chunk "0" matches immediately and chunk "1" would
    # raise if it were ever resolved (it isn't valid $-ref syntax the
    # resolver would choke on, but we detect "was it touched at all" via
    # the resolve cache instead of relying on an exception).
    html = (
        '<script>self.__next_f.push([1, '
        '"0:{\\"id\\":1}\\n1:{\\"id\\":2}\\n2:{\\"id\\":3}"'
        '])</script>'
    )
    page = extract(html)
    result = page.find_one(lambda n: isinstance(n, dict) and n.get("id") == 1)
    assert result == {"id": 1}
    # Only chunk "0" should have been resolved (and cached) -- "1" and "2"
    # were never reached because find_one stopped at the first match.
    assert "0" in page._resolved_cache
    assert "1" not in page._resolved_cache
    assert "2" not in page._resolved_cache


def test_find_all_with_max_results_stops_resolving_early():
    html = (
        '<script>self.__next_f.push([1, '
        '"0:{\\"id\\":1}\\n1:{\\"id\\":2}\\n2:{\\"id\\":3}\\n3:{\\"id\\":4}"'
        '])</script>'
    )
    page = extract(html)
    results = page.find_all(lambda n: isinstance(n, dict) and "id" in n, max_results=2)
    assert results == [{"id": 1}, {"id": 2}]
    assert "0" in page._resolved_cache and "1" in page._resolved_cache
    assert "2" not in page._resolved_cache
    assert "3" not in page._resolved_cache


def test_find_all_without_max_results_still_resolves_everything():
    # Sanity check the early-exit optimization doesn't accidentally skip
    # chunks when there's no max_results to stop at.
    html = (
        '<script>self.__next_f.push([1, '
        '"0:{\\"id\\":1}\\n1:{\\"id\\":2}\\n2:{\\"id\\":3}"'
        '])</script>'
    )
    page = extract(html)
    results = page.find_all(lambda n: isinstance(n, dict) and "id" in n)
    assert results == [{"id": 1}, {"id": 2}, {"id": 3}]
    assert set(page._resolved_cache.keys()) == {"0", "1", "2"}


# ------------------------------------------------------------------ #
# $-ref path resolution: React element field names ("props") map to a
# fixed position in the ["$", type, key, props] wire tuple, not a literal
# list index -- and path-based refs into a chunk that's still mid-
# resolution (self-referential sibling nodes within the same chunk) must
# not be blocked by the circular-reference guard, since they aren't
# actually circular.
# ------------------------------------------------------------------ #
def test_ref_path_resolves_named_props_field_on_element_tuple():
    html = (
        '<script>self.__next_f.push([1, '
        '"0:[[\\"$\\",\\"$L1\\",null,{\\"filters\\":{\\"page\\":2}}],'
        '[\\"$\\",\\"$L2\\",null,{\\"filters\\":\\"$0:0:props:filters\\"}]]"'
        '])</script>'
    )
    page = extract(html)
    resolved = page.resolve_chunk("0")
    assert resolved[1] == ["$", "$L2", None, {"filters": {"page": 2}}]


def test_ref_path_self_reference_within_same_chunk_is_not_blocked():
    html = (
        '<script>self.__next_f.push([1, '
        '"0:[{\\"value\\":42},{\\"mirrored\\":\\"$0:0:value\\"}]"'
        '])</script>'
    )
    page = extract(html)
    resolved = page.resolve_chunk("0")
    assert resolved[1] == {"mirrored": 42}


def test_ref_path_genuine_whole_chunk_cycle_still_returns_none():
    html = '<script>self.__next_f.push([1, "0:\\"$0\\""])</script>'
    page = extract(html)
    assert page.resolve_chunk("0") is None


def test_ref_path_props_field_only_applies_to_marker_lists():
    # A plain 4-element list that does NOT start with the "$" marker
    # must not be treated as a React element tuple.
    html = (
        '<script>self.__next_f.push([1, '
        '"0:[1,2,3,4]\\n1:\\"$0:props\\""])</script>'
    )
    page = extract(html)
    assert page.resolve_chunk("1") is None


def test_ref_path_repeated_self_reference_does_not_loop_forever():
    # The same self-referential path resolved multiple times (e.g. via
    # find_all walking the tree) must not blow up or hang -- exercise the
    # (ref_id, path) guard more than once.
    html = (
        '<script>self.__next_f.push([1, '
        '"0:[{\\"value\\":42},{\\"a\\":\\"$0:0:value\\",\\"b\\":\\"$0:0:value\\"}]"'
        '])</script>'
    )
    page = extract(html)
    resolved = page.resolve_chunk("0")
    assert resolved[1] == {"a": 42, "b": 42}


# ------------------------------------------------------------------ #
# Raw RSC-fetch payloads: no HTML, no self.__next_f.push() wrapper --
# just the Flight row stream directly as the response body. This is what
# Next.js returns for a request carrying the `RSC: 1` header (client-side
# navigation fetches; see FlightExtractor.from_rsc_url).
# ------------------------------------------------------------------ #
def test_extract_handles_raw_rsc_payload_with_no_html_wrapper():
    raw = (
        '1:"$Sreact.fragment"\n'
        '0:{"a":1}\n'
        '2:[1,2,3]'
    )
    page = extract(raw)
    assert set(page.keys()) == {"0", "1", "2"}
    assert page.resolve_chunk("0") == {"a": 1}
    assert page.resolve_chunk("2") == [1, 2, 3]


def test_detect_next_router_recognizes_raw_rsc_payload():
    from nextflight import detect_next_router

    raw = '1:"$Sreact.fragment"\n0:{"a":1}'
    assert detect_next_router(raw) == "app"


def test_raw_rsc_detection_does_not_misfire_on_plain_text():
    from nextflight import detect_next_router

    assert detect_next_router("Hello, this is just a sentence.") == "unknown"
    assert detect_next_router("<html><body>hi</body></html>") == "unknown"


def test_raw_rsc_detection_does_not_misfire_on_key_value_or_timestamp_text():
    # These both match a naive "id:" prefix check on the first line, but
    # are NOT Flight payloads -- the value after the colon doesn't look
    # like a genuine Flight row value (a quoted string, array, object,
    # module/text row, bare ref, or standalone bare number).
    from nextflight import detect_next_router

    assert detect_next_router("name: John\nage: 30\ncity: NYC") == "unknown"
    assert detect_next_router("12:34:56 INFO started") == "unknown"
    assert detect_next_router("C: is the drive letter") == "unknown"


def test_raw_rsc_detection_recognizes_every_real_row_shape():
    # Each of Flight's row value shapes should be recognized as the start
    # of a genuine raw RSC payload, not just the quoted-string case.
    from nextflight import detect_next_router

    assert detect_next_router('0:{"a":1}') == "app"
    assert detect_next_router("0:[1,2,3]") == "app"
    assert detect_next_router('0:"$Sreact.fragment"') == "app"
    assert detect_next_router('3:I[897367,["a.js"],"Boundary"]') == "app"
    assert detect_next_router("17:T5,hello") == "app"
    assert detect_next_router("5:$3") == "app"
    assert detect_next_router("0:42") == "app"


def test_extract_still_prefers_wrapped_html_over_raw_detection():
    html = '<script>self.__next_f.push([1, "1:{\\"a\\":1}"])</script>'
    page = extract(html)
    assert page.keys() == ["1"]


def test_raw_rsc_payload_with_text_rows_and_named_refs():
    # A more realistic raw RSC payload: a text (T) row plus a $-ref by
    # name into a React element's props, mirroring a real production
    # response fetched with the RSC header.
    raw = (
        '1:"$Sreact.fragment"\n'
        '0:[["$","$L1",null,{"filters":{"page":2}}],'
        '["$","$L2",null,{"filters":"$0:0:props:filters"}]]\n'
        '5:T5,hello'
    )
    page = extract(raw)
    assert page.resolve_chunk("0")[1][3] == {"filters": {"page": 2}}
    assert page.resolve_chunk("5") == "hello"
