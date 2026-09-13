"""
Tests for the v0.4.5 feature set:

  4. FlightExtractor.streamed_chunks()
  5. FlightExtractor.is_ppr_page() / .static_vs_dynamic_chunks()
"""
import json

from nextflight import extract, FlightExtractor


def _multi_push_html(*payloads):
    return "".join(
        f'<script>self.__next_f.push([1, {json.dumps(p)}])</script>'
        for p in payloads
    )


def test_streamed_chunks_empty_for_single_push_call():
    html = _multi_push_html('0:{"title":"hi"}')
    page = extract(html)
    assert page.streamed_chunks() == []


def test_streamed_chunks_identifies_later_push_calls():
    html = _multi_push_html(
        '0:["$","div",null,{"children":"$@2"}]\n1:{"title":"hi"}',
        '2:{"streamed":true}',
    )
    page = extract(html)
    assert page.streamed_chunks() == ["2"]


def test_streamed_chunks_multiple_later_calls():
    html = _multi_push_html(
        '0:{"a":1}',
        '1:{"b":2}',
        '2:{"c":3}',
    )
    page = extract(html)
    assert page.streamed_chunks() == ["1", "2"]


def test_streamed_chunks_result_cached():
    html = _multi_push_html('0:{"a":1}', '1:{"b":2}')
    page = extract(html)
    first = page.streamed_chunks()
    # cache populated now -- calling again should return the same result
    # without recomputing (behavioral check, not a mock-based one, since
    # the cache is an implementation detail)
    second = page.streamed_chunks()
    assert first == second == ["1"]
    assert page._push_call_index_cache is not None


def test_static_vs_dynamic_chunks_split():
    html = _multi_push_html(
        '0:{"a":1}',
        '1:{"b":2}',
    )
    page = extract(html)
    split = page.static_vs_dynamic_chunks()
    assert split == {"static": ["0"], "dynamic": ["1"]}


def test_static_vs_dynamic_chunks_all_static_when_one_push_call():
    page = extract(_multi_push_html('0:{"a":1}'))
    split = page.static_vs_dynamic_chunks()
    assert split == {"static": ["0"], "dynamic": []}


def test_is_ppr_page_true_with_streamed_async_placeholder():
    html = _multi_push_html(
        '0:["$","div",null,{"children":"$@2"}]',
        '2:{"streamed":true}',
    )
    page = extract(html)
    assert page.is_ppr_page() is True


def test_is_ppr_page_false_when_nothing_streamed():
    html = _multi_push_html('0:{"a":1}')
    page = extract(html)
    assert page.is_ppr_page() is False


def test_is_ppr_page_false_when_streamed_but_no_async_marker():
    # something streamed in, but no "$@<id>" placeholder anywhere --
    # doesn't meet the (streamed AND async-placeholder) bar
    html = _multi_push_html('0:{"a":1}', '1:{"b":2}')
    page = extract(html)
    assert page.is_ppr_page() is False


def test_from_stream_still_works_unaffected():
    # Regression check: from_stream()'s manual object construction needs
    # the new _push_call_index_cache attribute initialized too, since a
    # future internal use of streamed_chunks()/is_ppr_page() against a
    # from_stream()-built extractor should not raise AttributeError.
    chunks = ['<script>self.__next_f.push([1, "0:{\\"a\\":1}"])</script>']
    results = list(FlightExtractor.from_stream(chunks))
    assert results == [("0", {"a": 1})]
