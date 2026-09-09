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
