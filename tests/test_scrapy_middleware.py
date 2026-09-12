"""
Tests for nextflight.scrapy_middleware -- skipped entirely if Scrapy
isn't installed, since it's an optional extra, not a runtime dependency
of the core library.
"""

import gc
import json
import threading

import pytest

scrapy = pytest.importorskip("scrapy")

from scrapy.http import HtmlResponse, Request, Response  # noqa: E402
from scrapy.crawler import Crawler  # noqa: E402
from scrapy.settings import Settings  # noqa: E402
from scrapy.spiders import Spider  # noqa: E402

from nextflight.extractor import FlightExtractor  # noqa: E402
from nextflight.scrapy_middleware import (  # noqa: E402
    FlightMiddleware,
    FlightDedupeMiddleware,
    FlightItemPipeline,
    FlightRetryMiddleware,
    FlightRSCMiddleware,
    FlightStreamingMiddleware,
    NextflightSpiderMixin,
    NextflightStatsExtension,
    StopDownload,
    recommended_settings,
    _fallback_cache,
    _flight_property,
)


def _push_html(*rows: str) -> bytes:
    payload = "\n".join(rows)
    return f"<script>self.__next_f.push([1,{json.dumps(payload)}])</script>".encode("utf-8")


def _make_crawler(settings: dict) -> Crawler:
    # A minimal stand-in exposing just `.settings` (and a no-op
    # `.signals.connect`, needed by `FlightStreamingMiddleware`) -- avoids
    # spinning up a full Crawler (reactor, engine, ...) just to test
    # settings plumbing.
    class _FakeSignals:
        def connect(self, *args, **kwargs):
            pass

    class _FakeCrawler:
        pass

    fc = _FakeCrawler()
    fc.settings = Settings(settings)
    fc.signals = _FakeSignals()
    return fc


def test_flight_property_returns_extractor():
    FlightMiddleware()
    resp = HtmlResponse(url="https://example.com", body=_push_html('0:{"a":1}'))
    assert isinstance(resp.flight, FlightExtractor)
    assert resp.flight.resolve_chunk("0") == {"a": 1}


def test_flight_property_caches_per_instance():
    FlightMiddleware()
    resp = HtmlResponse(url="https://example.com", body=_push_html('0:{"a":1}'))
    first = resp.flight
    second = resp.flight
    assert first is second

    other = HtmlResponse(url="https://example.com/2", body=_push_html('0:{"a":1}'))
    assert other.flight is not first


def test_flight_property_never_raises_on_non_nextjs_page():
    FlightMiddleware()
    resp = HtmlResponse(url="https://example.com", body=b"<html><body>plain page</body></html>")
    page = resp.flight
    assert page.keys() == []
    assert page.stats()["chunk_count"] == 0


def test_flight_property_never_raises_on_non_html_response():
    FlightMiddleware()
    resp = Response(url="https://example.com/file.bin", body=b"\x00\x01\x02\xff\xfe not html at all")
    page = resp.flight
    assert page.keys() == []


def test_flight_property_never_raises_on_empty_body():
    FlightMiddleware()
    resp = HtmlResponse(url="https://example.com", body=b"")
    assert resp.flight.keys() == []


def test_settings_strict_is_forwarded():
    crawler = _make_crawler({"NEXTFLIGHT_STRICT": True})
    FlightMiddleware.from_crawler(crawler)
    # A malformed (non-JSON, non-$-ref) row should now raise via .flight.
    resp = HtmlResponse(url="https://example.com", body=_push_html('0:{not valid json'))
    from nextflight.extractor import FlightParseError
    with pytest.raises(FlightParseError):
        resp.flight


def test_settings_repair_is_forwarded():
    crawler = _make_crawler({"NEXTFLIGHT_REPAIR": True, "NEXTFLIGHT_STRICT": False})
    FlightMiddleware.from_crawler(crawler)
    resp = HtmlResponse(url="https://example.com", body=_push_html('0:{"title":"Truncated","price":1'))
    assert resp.flight.resolve_chunk("0") == {"title": "Truncated", "price": 1}


def test_most_recently_constructed_middleware_settings_win():
    # Re-installing the property (rather than guarding on hasattr) means
    # the last-constructed middleware's settings apply -- documented
    # behavior, exercised here so a future change can't silently make
    # settings "sticky" to whichever instance came first.
    FlightMiddleware(strict=False, repair=False)
    resp1 = HtmlResponse(url="https://example.com/1", body=_push_html('0:{"a":1'))
    assert isinstance(resp1.flight.resolve_chunk("0"), str)  # raw fallback, not repaired

    FlightMiddleware(strict=False, repair=True)
    resp2 = HtmlResponse(url="https://example.com/2", body=_push_html('0:{"a":1'))
    assert resp2.flight.resolve_chunk("0") == {"a": 1}  # now repaired


def test_fallback_cache_entries_are_garbage_collected():
    # Regression: the fallback cache used to be a plain dict keyed by
    # id(response), which never shrank and risked an id-reuse collision.
    #
    # NOTE on how this forces the fallback path: `_flight_property` tries
    # `object.__setattr__(self, ...)` directly -- which calls straight
    # into the base `object` implementation and does NOT go through a
    # subclass's own overridden `__setattr__` at all. So a
    # `class LockedResponse(HtmlResponse): def __setattr__(...): raise
    # ...` trick (an earlier version of this test) never actually forced
    # anything; whether the primary path succeeds instead depends
    # entirely on whether the installed Scrapy version's own `Response`
    # class happens to lack a `__dict__` slot for arbitrary attributes --
    # which varies across Scrapy versions/Python versions and made this
    # test flaky in CI's version matrix. A minimal object we define from
    # scratch, with an explicit `__slots__` and no inherited `__dict__`,
    # makes `object.__setattr__` genuinely and portably fail for any
    # attribute name not in `__slots__`, on every Python/Scrapy version.
    class LockedFakeResponse:
        __slots__ = ("body", "url", "__weakref__")

        def __init__(self, url, body):
            self.url = url
            self.body = body

    FlightMiddleware()
    resp = LockedFakeResponse(url="https://example.com", body=_push_html('0:{"a":1}'))
    flight = _flight_property(resp)  # forces the fallback path
    assert flight.resolve_chunk("0") == {"a": 1}
    assert len(_fallback_cache) >= 1

    resp_id = id(resp)
    del resp, flight
    gc.collect()
    # The entry keyed on the now-collected response must be gone -- not
    # just eventually, but immediately after collection, since that's
    # exactly what distinguishes a WeakKeyDictionary from the old
    # id()-keyed plain dict this replaced.
    assert not any(id(k) == resp_id for k in list(_fallback_cache.keys()))


def test_middleware_caches_version_hint_per_domain():
    mw = FlightMiddleware()
    resp1 = HtmlResponse(url="https://example.com/a", body=_push_html('0:{"a":1}'))
    resp2 = HtmlResponse(url="https://example.com/b", body=_push_html('0:{"a":1}'))
    resp_other = HtmlResponse(url="https://other.com/a", body=b"<html>not next.js</html>")

    hint1 = mw.version_hint_for_response(resp1)
    hint2 = mw.version_hint_for_response(resp2)
    assert hint1 == hint2
    assert "example.com" in mw._version_hint_cache
    assert len(mw._version_hint_cache) == 1

    hint_other = mw.version_hint_for_response(resp_other)
    assert hint_other == {"range": None, "notes": None, "matches": []}
    assert len(mw._version_hint_cache) == 2
    pipeline = FlightItemPipeline(required_keys=["title", "price"])
    resp = HtmlResponse(
        url="https://example.com",
        body=_push_html('0:{"title":"A","price":1}', '1:{"title":"B","price":2}'),
    )
    matches = pipeline.matches_for_response(resp)
    assert len(matches) == 2
    assert {"title": "A", "price": 1} in matches


def test_item_pipeline_from_crawler_reads_settings():
    crawler = _make_crawler({"NEXTFLIGHT_PIPELINE_KEYS": ["title", "price"]})
    pipeline = FlightItemPipeline.from_crawler(crawler)
    assert pipeline.required_keys == ["title", "price"]


def test_item_pipeline_process_item_is_passthrough():
    pipeline = FlightItemPipeline()
    item = {"foo": "bar"}
    assert pipeline.process_item(item, spider=None) is item


def test_item_pipeline_no_required_keys_returns_empty():
    pipeline = FlightItemPipeline()
    resp = HtmlResponse(url="https://example.com", body=_push_html('0:{"a":1}'))
    assert pipeline.matches_for_response(resp) == []


def test_docs_spider_pattern_constructing_pipeline_in_init():
    # Pins the exact pattern shown in docs/scrapy.md ("Using
    # FlightItemPipeline") -- a regression here means the docs are wrong.
    class ProductSpider(Spider):
        name = "products"

        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            self._flight_pipeline = FlightItemPipeline(required_keys=["price", "title"])

        def parse(self, response):
            yield from self._flight_pipeline.matches_for_response(response)

    spider = ProductSpider()
    resp = HtmlResponse(url="https://example.com", body=_push_html('0:{"price":10,"title":"A"}'))
    assert list(spider.parse(resp)) == [{"price": 10, "title": "A"}]


def test_docs_spider_pattern_from_crawler():
    # Pins the `from_crawler`-based variant shown in docs/scrapy.md.
    from scrapy.signalmanager import SignalManager

    class ProductSpider(Spider):
        name = "products"

        @classmethod
        def from_crawler(cls, crawler, *args, **kwargs):
            spider = super().from_crawler(crawler, *args, **kwargs)
            spider._flight_pipeline = FlightItemPipeline.from_crawler(crawler)
            return spider

        def parse(self, response):
            yield from self._flight_pipeline.matches_for_response(response)

    class _FakeCrawler:
        pass

    fc = _FakeCrawler()
    fc.settings = Settings({"NEXTFLIGHT_PIPELINE_KEYS": ["price", "title"]})
    fc.signals = SignalManager(fc)
    spider = ProductSpider.from_crawler(fc)
    resp = HtmlResponse(url="https://example.com", body=_push_html('0:{"price":10,"title":"A"}'))
    assert list(spider.parse(resp)) == [{"price": 10, "title": "A"}]


class _DummySpider(NextflightSpiderMixin, Spider):
    name = "dummy"


def test_spider_mixin_flight_shorthand_without_middleware():
    spider = _DummySpider()
    resp = HtmlResponse(url="https://example.com", body=_push_html('0:{"a":1}'))
    page = spider.flight(resp)
    assert page.resolve_chunk("0") == {"a": 1}


def test_spider_mixin_uses_cached_extractor_when_middleware_present():
    FlightMiddleware()
    spider = _DummySpider()
    resp = HtmlResponse(url="https://example.com", body=_push_html('0:{"a":1}'))
    via_property = resp.flight
    via_mixin = spider.flight(resp)
    assert via_mixin is via_property


def test_spider_mixin_logs_version_hint_once_per_domain(caplog):
    import logging

    spider = _DummySpider()
    spider.crawler = None  # Spider.logger falls back to a name-based logger without a crawler
    caplog.set_level(logging.INFO)

    resp1 = HtmlResponse(url="https://example.com/a", body=_push_html('0:{"a":1}'))
    resp2 = HtmlResponse(url="https://example.com/b", body=_push_html('0:{"a":1}'))
    resp3 = HtmlResponse(url="https://other.com/a", body=_push_html('0:{"a":1}'))

    spider.log_version_hint(resp1)
    spider.log_version_hint(resp2)  # same domain -- must not log again
    spider.log_version_hint(resp3)  # different domain -- logs once more

    nextflight_records = [r for r in caplog.records if "nextflight" in r.message]
    assert len(nextflight_records) == 2


def test_spider_mixin_follow_flight_urls_yields_resolved_requests():
    spider = _DummySpider()
    html = _push_html(
        '0:{"title":"A","href":"/product/1"}',
        '1:{"title":"B","href":"/product/2"}',
    )
    resp = HtmlResponse(url="https://example.com/category", body=html)

    def cb(r):
        pass

    requests = list(spider.follow_flight_urls(resp, keys={"href"}, callback=cb))
    urls = {r.url for r in requests}
    assert urls == {"https://example.com/product/1", "https://example.com/product/2"}
    assert all(r.callback is cb for r in requests)


def test_spider_mixin_follow_flight_urls_respects_pattern_filter():
    spider = _DummySpider()
    html = _push_html('0:{"a":"/product/1","b":"/category/2"}')
    resp = HtmlResponse(url="https://example.com", body=html)

    requests = list(spider.follow_flight_urls(resp, pattern=r"/product/"))
    assert [r.url for r in requests] == ["https://example.com/product/1"]


def test_spider_mixin_follow_flight_urls_no_matches_yields_nothing():
    spider = _DummySpider()
    resp = HtmlResponse(url="https://example.com", body=_push_html('0:{"a":1}'))
    assert list(spider.follow_flight_urls(resp, keys={"href"})) == []


def test_docs_flagship_example_spider_runs_correctly():
    # Pins the "Complete runnable example spider" from docs/scrapy.md end
    # to end -- this is the first thing a reader copy-pastes, so it must
    # actually work.
    import dataclasses

    from nextflight import normalize_price, clean_text

    FlightMiddleware()

    class ProductSpider(NextflightSpiderMixin, Spider):
        name = "products"
        start_urls = ["https://example.com/category/electronics"]

        def parse(self, response):
            self.log_version_hint(response)
            for listing in response.flight.find_all_by_keys(
                {"price", "title", "url"}, dedupe=True
            ):
                yield {
                    "title": clean_text(listing.get("title")),
                    "price": normalize_price(listing.get("price")),
                    "url": response.urljoin(listing["url"]),
                }

            @dataclasses.dataclass
            class ProductDetail:
                title: str
                price: int
                description: str

            detail = response.flight.extract_as(ProductDetail)
            if detail:
                yield {"detail": detail}

            next_page = response.flight.get("pagination.next_url")
            if next_page:
                yield response.follow(next_page, self.parse)

    spider = ProductSpider()
    html = _push_html(
        '0:{"price":1999,"title":"Widget","url":"/p/1"}',
        '1:{"title":"Widget","price":1999,"description":"A nice widget"}',
    )
    resp = HtmlResponse(url="https://example.com/category/electronics", body=html)
    results = list(spider.parse(resp))

    assert results[0] == {
        "title": "Widget",
        "price": {"amount": 1999.0, "currency": None},
        "url": "https://example.com/p/1",
    }
    assert results[1]["detail"].title == "Widget"
    assert results[1]["detail"].price == 1999


# -- FlightStreamingMiddleware --------------------------------------------


def test_streaming_disabled_by_default_raises_not_configured():
    from scrapy.exceptions import NotConfigured

    crawler = _make_crawler({})
    with pytest.raises(NotConfigured):
        FlightStreamingMiddleware.from_crawler(crawler)


def test_streaming_from_crawler_reads_settings():
    crawler = _make_crawler({
        "NEXTFLIGHT_STREAMING": True,
        "NEXTFLIGHT_STREAM_KEYS": ["price", "title"],
        "NEXTFLIGHT_STREAM_MAX_BYTES": 12345,
        "NEXTFLIGHT_STREAM_CHECK_INTERVAL": 99,
    })
    mw = FlightStreamingMiddleware.from_crawler(crawler)
    assert mw.stream_keys == {"price", "title"}
    assert mw.max_bytes == 12345
    assert mw.min_check_interval == 99


def _streaming_middleware(**settings_overrides):
    settings = {"NEXTFLIGHT_STREAMING": True, "NEXTFLIGHT_STREAM_CHECK_INTERVAL": 1}
    settings.update(settings_overrides)
    return FlightStreamingMiddleware.from_crawler(_make_crawler(settings))


def test_streaming_stops_download_early_on_match():
    mw = _streaming_middleware(NEXTFLIGHT_STREAM_KEYS=["price", "title"])
    req = Request("https://example.com")
    body = _push_html('0:{"title":"Big Item","price":999}') + b"X" * 5000

    stopped = False
    consumed = 0
    for i in range(0, len(body), 100):
        piece = body[i:i + 100]
        try:
            mw.bytes_received(piece, req, None)
            consumed += len(piece)
        except StopDownload:
            stopped = True
            break

    assert stopped
    # Must have stopped well before consuming the whole (padded) body.
    assert consumed < len(body) // 2
    assert mw._matches[req] == {"title": "Big Item", "price": 999}


def test_streaming_never_stops_when_no_stream_keys_configured():
    mw = _streaming_middleware()  # NEXTFLIGHT_STREAM_KEYS not set
    req = Request("https://example.com")
    body = _push_html('0:{"title":"Big Item","price":999}') + b"X" * 2000
    for i in range(0, len(body), 100):
        mw.bytes_received(body[i:i + 100], req, None)  # must not raise
    assert req not in mw._matches


def test_streaming_never_stops_when_keys_never_match():
    mw = _streaming_middleware(NEXTFLIGHT_STREAM_KEYS=["totally_absent_key"])
    req = Request("https://example.com")
    body = _push_html('0:{"title":"Big Item","price":999}')
    for i in range(0, len(body), 50):
        mw.bytes_received(body[i:i + 50], req, None)  # must not raise
    assert req not in mw._matches


def test_streaming_process_response_prewarms_flight_on_early_stop():
    FlightMiddleware()
    mw = _streaming_middleware(NEXTFLIGHT_STREAM_KEYS=["price", "title"])
    req = Request("https://example.com")
    body = _push_html('0:{"title":"Big Item","price":999}') + b"X" * 2000

    try:
        for i in range(0, len(body), 100):
            mw.bytes_received(body[i:i + 100], req, None)
    except StopDownload:
        pass

    # Scrapy would hand us a response truncated to whatever was actually
    # downloaded before the cancellation took effect -- simulate that
    # with a body shorter than the full padded one.
    resp = HtmlResponse(url=req.url, body=body[:150], request=req)
    resp = mw.process_response(req, resp, None)

    assert resp.meta["nextflight_stopped_early"] is True
    assert resp.meta["nextflight_match"] == {"title": "Big Item", "price": 999}
    assert resp.flight.resolve_chunk("0") == {"title": "Big Item", "price": 999}


def test_streaming_process_response_prewarms_flight_on_full_download():
    FlightMiddleware()
    mw = _streaming_middleware()  # no stream keys -- benefit 2 only
    req = Request("https://example.com")
    body = _push_html('0:{"other":1}')
    for i in range(0, len(body), 30):
        mw.bytes_received(body[i:i + 30], req, None)

    resp = HtmlResponse(url=req.url, body=body, request=req)
    resp = mw.process_response(req, resp, None)

    assert "nextflight_match" not in resp.meta
    assert resp.flight.resolve_chunk("0") == {"other": 1}


def test_streaming_capped_buffer_falls_back_to_full_response_text():
    # If the buffer hits NEXTFLIGHT_STREAM_MAX_BYTES before a match is
    # found, it stops growing (bounded memory) -- but the actual
    # download continues normally, so `.flight` must still see the
    # FULL response via its normal lazy path, not the truncated buffer.
    FlightMiddleware()
    mw = _streaming_middleware(
        NEXTFLIGHT_STREAM_KEYS=["title", "price"],
        NEXTFLIGHT_STREAM_MAX_BYTES=40,
    )
    req = Request("https://example.com")
    body = _push_html('0:{"title":"Late Match","price":1}')
    for i in range(0, len(body), 15):
        mw.bytes_received(body[i:i + 15], req, None)  # must not raise -- never matched before cap

    assert req not in mw._matches
    buf = mw._buffers.get(req)
    # The buffer itself is capped and incomplete...
    assert buf is None or buf.capped

    resp = HtmlResponse(url=req.url, body=body, request=req)  # Scrapy delivers the FULL body
    resp = mw.process_response(req, resp, None)

    assert "nextflight_match" not in resp.meta
    # ... but .flight must still find the match by falling through to
    # the complete response.text, not the capped buffer.
    assert resp.flight.find_by_keys(["title", "price"]) == {"title": "Late Match", "price": 1}


def test_streaming_multiple_requests_have_independent_buffers():
    mw = _streaming_middleware(NEXTFLIGHT_STREAM_KEYS=["title", "price"])
    req_a = Request("https://example.com/a")
    req_b = Request("https://example.com/b")
    body_a = _push_html('0:{"title":"A","price":1}')
    body_b = _push_html('0:{"unrelated":1}')

    stopped_a = False
    try:
        for i in range(0, len(body_a), 20):
            mw.bytes_received(body_a[i:i + 20], req_a, None)
    except StopDownload:
        stopped_a = True

    for i in range(0, len(body_b), 20):
        mw.bytes_received(body_b[i:i + 20], req_b, None)  # must not raise

    assert stopped_a
    assert mw._matches[req_a] == {"title": "A", "price": 1}
    assert req_b not in mw._matches


def test_streaming_handles_multibyte_utf8_split_across_chunks():
    # A UTF-8 character can be 1-4 bytes; splitting the byte stream at an
    # arbitrary offset can land mid-character. The incremental decoder
    # must hold the dangling bytes rather than raising or corrupting text.
    html = _push_html('0:{"title":"caf\u00e9 \u2014 \U0001F600","price":1}')
    mw = _streaming_middleware(NEXTFLIGHT_STREAM_KEYS=["title", "price"])
    req = Request("https://example.com")

    match = None
    try:
        # Feed one byte at a time -- guarantees every possible multi-byte
        # split point gets exercised somewhere in this run.
        for i in range(len(html)):
            mw.bytes_received(html[i:i + 1], req, None)
    except StopDownload:
        match = mw._matches[req]

    assert match == {"title": "caf\u00e9 \u2014 \U0001F600", "price": 1}


def test_streaming_requires_scrapy_2_6_features(monkeypatch):
    import nextflight.scrapy_middleware as sm
    from scrapy.exceptions import NotConfigured

    monkeypatch.setattr(sm, "_STREAMING_AVAILABLE", False)
    crawler = _make_crawler({"NEXTFLIGHT_STREAMING": True})
    with pytest.raises(NotConfigured):
        FlightStreamingMiddleware.from_crawler(crawler)


# -- per-request stream-key override --------------------------------------


def test_stream_keys_per_request_override():
    mw = _streaming_middleware(NEXTFLIGHT_STREAM_KEYS=["price"])
    req = Request("https://example.com", meta={"nextflight_stream_keys": ["title"]})
    body = _push_html('0:{"title":"Only title, no price"}') + b"X" * 2000
    match = None
    try:
        for i in range(0, len(body), 50):
            mw.bytes_received(body[i:i + 50], req, None)
    except StopDownload:
        match = mw._matches[req]
    assert match == {"title": "Only title, no price"}


def test_stream_keys_per_request_opt_out():
    mw = _streaming_middleware(NEXTFLIGHT_STREAM_KEYS=["price", "title"])
    req = Request("https://example.com", meta={"nextflight_stream_keys": False})
    body = _push_html('0:{"title":"Would normally match","price":1}') + b"X" * 2000
    for i in range(0, len(body), 50):
        mw.bytes_received(body[i:i + 50], req, None)  # must not raise -- opted out
    assert req not in mw._matches


def test_stream_keys_per_request_default_falls_back_to_global():
    mw = _streaming_middleware(NEXTFLIGHT_STREAM_KEYS=["price", "title"])
    req = Request("https://example.com")  # no override -- uses global setting
    body = _push_html('0:{"title":"A","price":1}') + b"X" * 2000
    match = None
    try:
        for i in range(0, len(body), 50):
            mw.bytes_received(body[i:i + 50], req, None)
    except StopDownload:
        match = mw._matches[req]
    assert match == {"title": "A", "price": 1}


# -- FlightRSCMiddleware ----------------------------------------------------


def test_rsc_middleware_matches_url_pattern():
    crawler = _make_crawler({"NEXTFLIGHT_RSC_URL_PATTERN": r"/product/"})
    mw = FlightRSCMiddleware.from_crawler(crawler)

    req = Request("https://example.com/product/123")
    mw.process_request(req, None)
    headers = req.headers.to_unicode_dict()
    assert headers["Rsc"] == "1"
    assert headers["Next-Url"] == "/product/123"
    assert req.meta["nextflight_rsc_request"] is True


def test_rsc_middleware_no_pattern_match_leaves_request_unchanged():
    crawler = _make_crawler({"NEXTFLIGHT_RSC_URL_PATTERN": r"/product/"})
    mw = FlightRSCMiddleware.from_crawler(crawler)

    req = Request("https://example.com/category/list")
    mw.process_request(req, None)
    assert "Rsc" not in req.headers.to_unicode_dict()
    assert "nextflight_rsc_request" not in req.meta


def test_rsc_middleware_explicit_opt_in_overrides_missing_pattern():
    crawler = _make_crawler({})  # no pattern configured at all
    mw = FlightRSCMiddleware.from_crawler(crawler)

    req = Request("https://example.com/anything", meta={"nextflight_rsc": True})
    mw.process_request(req, None)
    assert req.headers.to_unicode_dict()["Rsc"] == "1"


def test_rsc_middleware_explicit_opt_out_overrides_matching_pattern():
    crawler = _make_crawler({"NEXTFLIGHT_RSC_URL_PATTERN": r"/product/"})
    mw = FlightRSCMiddleware.from_crawler(crawler)

    req = Request("https://example.com/product/123", meta={"nextflight_rsc": False})
    mw.process_request(req, None)
    assert "Rsc" not in req.headers.to_unicode_dict()


def test_rsc_middleware_does_not_overwrite_explicit_headers():
    crawler = _make_crawler({})
    mw = FlightRSCMiddleware.from_crawler(crawler)

    req = Request(
        "https://example.com/x",
        meta={"nextflight_rsc": True},
        headers={"Next-Url": "/custom/route"},
    )
    mw.process_request(req, None)
    assert req.headers.to_unicode_dict()["Next-Url"] == "/custom/route"


def test_rsc_response_is_readable_via_flight():
    # The whole point: a response fetched with the RSC header set is a
    # raw RSC stream, not HTML -- .flight must still work on it via the
    # library's existing raw-RSC auto-detection.
    FlightMiddleware()
    raw_rsc = '0:{"title":"RSC page","price":5}\n'
    resp = HtmlResponse(url="https://example.com/product/1", body=raw_rsc.encode())
    assert resp.flight.find_by_keys(["title", "price"]) == {"title": "RSC page", "price": 5}


# -- FlightDedupeMiddleware ---------------------------------------------


def test_dedupe_middleware_disabled_by_default():
    from scrapy.exceptions import NotConfigured

    crawler = _make_crawler({})
    with pytest.raises(NotConfigured):
        FlightDedupeMiddleware.from_crawler(crawler)


def test_dedupe_middleware_drops_exact_duplicate_dicts_across_calls():
    crawler = _make_crawler({"NEXTFLIGHT_DEDUPE_ACROSS_PAGES": True})
    mw = FlightDedupeMiddleware.from_crawler(crawler)

    page1 = list(mw.process_spider_output(None, iter([{"id": 1, "title": "A"}]), None))
    page2 = list(mw.process_spider_output(
        None, iter([{"id": 1, "title": "A"}, {"id": 2, "title": "B"}]), None
    ))
    assert page1 == [{"id": 1, "title": "A"}]
    assert page2 == [{"id": 2, "title": "B"}]  # the id=1 duplicate from a LATER page/call is dropped


def test_dedupe_middleware_passes_through_non_dict_items_untouched():
    crawler = _make_crawler({"NEXTFLIGHT_DEDUPE_ACROSS_PAGES": True})
    mw = FlightDedupeMiddleware.from_crawler(crawler)

    req = Request("https://example.com/next")
    result = list(mw.process_spider_output(
        None, iter([{"id": 1}, req, {"id": 1}]), None
    ))
    # The dict duplicate is dropped, but the Request must never be
    # treated as a duplicate or dropped, regardless of how many times
    # something that isn't a dict shows up.
    assert result == [{"id": 1}, req]


def test_dedupe_middleware_with_dedupe_key_ignores_other_field_changes():
    crawler = _make_crawler({"NEXTFLIGHT_DEDUPE_ACROSS_PAGES": True, "NEXTFLIGHT_DEDUPE_KEY": "id"})
    mw = FlightDedupeMiddleware.from_crawler(crawler)

    items = [
        {"id": 1, "title": "A", "last_seen": "t1"},
        {"id": 1, "title": "A", "last_seen": "t2"},  # same id, different field -- still a dup
        {"id": 2, "title": "B", "last_seen": "t1"},
    ]
    result = list(mw.process_spider_output(None, iter(items), None))
    assert result == [
        {"id": 1, "title": "A", "last_seen": "t1"},
        {"id": 2, "title": "B", "last_seen": "t1"},
    ]


def test_dedupe_middleware_without_dedupe_key_distinguishes_different_content():
    crawler = _make_crawler({"NEXTFLIGHT_DEDUPE_ACROSS_PAGES": True})
    mw = FlightDedupeMiddleware.from_crawler(crawler)

    items = [
        {"id": 1, "title": "A"},
        {"id": 1, "title": "A (actually different)"},
    ]
    result = list(mw.process_spider_output(None, iter(items), None))
    assert len(result) == 2  # not deduped -- whole-item fingerprint differs


# -- recommended_settings() --------------------------------------------


def test_recommended_settings_baseline():
    settings = recommended_settings()
    assert settings["DOWNLOADER_MIDDLEWARES"] == {
        "nextflight.scrapy_middleware.FlightMiddleware": 543,
    }
    assert settings["NEXTFLIGHT_REPAIR"] is True
    assert "SPIDER_MIDDLEWARES" not in settings


def test_recommended_settings_all_flags():
    settings = recommended_settings(
        streaming=True, rsc=True, dedupe_across_pages=True, retry=True, stats=True,
    )
    dm = settings["DOWNLOADER_MIDDLEWARES"]
    assert "nextflight.scrapy_middleware.FlightMiddleware" in dm
    assert "nextflight.scrapy_middleware.FlightStreamingMiddleware" in dm
    assert "nextflight.scrapy_middleware.FlightRSCMiddleware" in dm
    assert "nextflight.scrapy_middleware.FlightRetryMiddleware" in dm
    assert settings["NEXTFLIGHT_STREAMING"] is True
    assert settings["SPIDER_MIDDLEWARES"] == {
        "nextflight.scrapy_middleware.FlightDedupeMiddleware": 543,
    }
    assert settings["NEXTFLIGHT_DEDUPE_ACROSS_PAGES"] is True
    assert settings["EXTENSIONS"] == {
        "nextflight.scrapy_middleware.NextflightStatsExtension": 543,
    }


def test_recommended_settings_retry_and_stats_off_by_default():
    settings = recommended_settings()
    dm = settings["DOWNLOADER_MIDDLEWARES"]
    assert "nextflight.scrapy_middleware.FlightRetryMiddleware" not in dm
    assert "EXTENSIONS" not in settings


# -- NextflightStatsExtension ---------------------------------------------


def _make_stats_extension():
    from scrapy.statscollectors import StatsCollector

    class _FakeSignals:
        def connect(self, *args, **kwargs):
            pass

    class _FakeCrawler:
        pass

    fc = _FakeCrawler()
    fc.settings = Settings({})
    fc.signals = _FakeSignals()
    fc.stats = StatsCollector(fc)
    return NextflightStatsExtension.from_crawler(fc), fc.stats


def test_stats_extension_works_without_flight_middleware_installed():
    # NextflightStatsExtension reads Flight data through its own internal
    # path (_flight_property), not through response.flight -- it must
    # work even if FlightMiddleware itself was never constructed *in this
    # test*. (Note: this can't assert `response.flight` itself is
    # unavailable, because FlightMiddleware patches scrapy.http.Response
    # at the class level -- once any earlier test in the same process has
    # constructed it, the property stays installed for the rest of the
    # process; see test_flight_middleware_patch_persists_for_process_lifetime
    # below, which pins that behavior explicitly instead of fighting it here.)
    ext, stats = _make_stats_extension()
    req = Request("https://example.com")
    resp = HtmlResponse(url=req.url, body=_push_html('0:{"a":1}'), request=req)
    ext.response_received(resp, req, None)
    assert stats.get_value("nextflight/responses_with_flight_data") == 1


def test_flight_middleware_patch_persists_for_process_lifetime():
    # FlightMiddleware installs `.flight` on the scrapy.http.Response
    # CLASS, not per-instance -- there is deliberately no "uninstall"
    # method, so once any code in a process has constructed
    # FlightMiddleware() even once (a prior crawl run, a `scrapy shell`
    # session, an earlier test), `response.flight` keeps working for
    # every Response for the rest of that process's lifetime, including
    # ones built before this specific test ran. This is expected,
    # documented behavior (see docs/scrapy.md's "Which approach should I
    # use?" note) -- pinned here explicitly so a future refactor can't
    # silently change it without a test noticing.
    FlightMiddleware()
    resp = HtmlResponse(url="https://example.com", body=_push_html('0:{"a":1}'))
    assert resp.flight.resolve_chunk("0") == {"a": 1}
    assert hasattr(Response, "flight")


def test_stats_extension_counts_responses_with_and_without_flight_data():
    FlightMiddleware()
    ext, stats = _make_stats_extension()

    req1 = Request("https://example.com/1")
    resp1 = HtmlResponse(url=req1.url, body=_push_html('0:{"a":1}'), request=req1)
    ext.response_received(resp1, req1, None)

    req2 = Request("https://example.com/2")
    resp2 = HtmlResponse(url=req2.url, body=b"<html>no flight data</html>", request=req2)
    ext.response_received(resp2, req2, None)

    assert stats.get_value("nextflight/responses_with_flight_data") == 1
    assert stats.get_value("nextflight/responses_without_flight_data") == 1


def test_stats_extension_tracks_average_parse_confidence():
    FlightMiddleware()
    ext, stats = _make_stats_extension()

    for i in range(3):
        req = Request(f"https://example.com/{i}")
        resp = HtmlResponse(url=req.url, body=_push_html('0:{"a":1}'), request=req)
        ext.response_received(resp, req, None)

    assert stats.get_value("nextflight/avg_parse_confidence") == 1.0


def test_stats_extension_tracks_version_hint_counts():
    FlightMiddleware()
    ext, stats = _make_stats_extension()

    req = Request("https://example.com/1")
    resp = HtmlResponse(url=req.url, body=_push_html('0:{"a":1}'), request=req)
    ext.response_received(resp, req, None)

    total = sum(v for k, v in stats.get_stats().items() if k.startswith("nextflight/version_hint/"))
    assert total == 1


def test_stats_extension_counts_stream_early_stops():
    FlightMiddleware()
    ext, stats = _make_stats_extension()

    req = Request("https://example.com/1")
    resp = HtmlResponse(url=req.url, body=_push_html('0:{"a":1}'), request=req)
    resp.meta["nextflight_stopped_early"] = True
    ext.response_received(resp, req, None)

    assert stats.get_value("nextflight/stream/stopped_early") == 1


# -- FlightRetryMiddleware -------------------------------------------------


def _make_retry_crawler(settings: dict):
    from scrapy.statscollectors import StatsCollector

    class _FakeSignals:
        def connect(self, *args, **kwargs):
            pass

    class _FakeCrawler:
        pass

    fc = _FakeCrawler()
    fc.settings = Settings(settings)
    fc.signals = _FakeSignals()
    fc.stats = StatsCollector(fc)
    return fc


def _retry_spider(crawler):
    spider = Spider(name="test")
    spider.crawler = crawler
    return spider


def test_retry_middleware_disabled_gracefully_when_unavailable(monkeypatch):
    import nextflight.scrapy_middleware as sm
    from scrapy.exceptions import NotConfigured

    monkeypatch.setattr(sm, "_RETRY_AVAILABLE", False)
    with pytest.raises(NotConfigured):
        FlightRetryMiddleware()


def test_retry_middleware_from_crawler_reads_settings():
    crawler = _make_retry_crawler({
        "NEXTFLIGHT_RETRY_MIN_CONFIDENCE": 0.75,
        "NEXTFLIGHT_RETRY_MAX_TIMES": 5,
    })
    mw = FlightRetryMiddleware.from_crawler(crawler)
    assert mw.min_confidence == 0.75
    assert mw.max_retry_times == 5


def test_retry_middleware_retries_low_confidence_response():
    FlightMiddleware()
    crawler = _make_retry_crawler({"NEXTFLIGHT_RETRY_MIN_CONFIDENCE": 0.9, "RETRY_TIMES": 3})
    mw = FlightRetryMiddleware.from_crawler(crawler)
    spider = _retry_spider(crawler)

    # A bare "$"-style marker row materializes as a raw string, not real
    # JSON, dragging parse_confidence below the 0.9 threshold.
    req = Request("https://example.com")
    resp = HtmlResponse(url=req.url, body=_push_html("0:X"), request=req)

    result = mw.process_response(req, resp, spider)
    assert isinstance(result, Request)
    assert result.url == req.url


def test_retry_middleware_passes_through_clean_response():
    FlightMiddleware()
    crawler = _make_retry_crawler({"NEXTFLIGHT_RETRY_MIN_CONFIDENCE": 0.5, "RETRY_TIMES": 3})
    mw = FlightRetryMiddleware.from_crawler(crawler)
    spider = _retry_spider(crawler)

    req = Request("https://example.com")
    resp = HtmlResponse(url=req.url, body=_push_html('0:{"a":1}'), request=req)

    result = mw.process_response(req, resp, spider)
    assert result is resp


def test_retry_middleware_never_retries_zero_chunk_response():
    FlightMiddleware()
    crawler = _make_retry_crawler({"NEXTFLIGHT_RETRY_MIN_CONFIDENCE": 0.99, "RETRY_TIMES": 3})
    mw = FlightRetryMiddleware.from_crawler(crawler)
    spider = _retry_spider(crawler)

    req = Request("https://example.com")
    resp = HtmlResponse(url=req.url, body=b"<html>nothing here</html>", request=req)

    result = mw.process_response(req, resp, spider)
    assert result is resp


def test_retry_middleware_gives_up_after_max_retries():
    FlightMiddleware()
    crawler = _make_retry_crawler({
        "NEXTFLIGHT_RETRY_MIN_CONFIDENCE": 0.9,
        "NEXTFLIGHT_RETRY_MAX_TIMES": 1,
    })
    mw = FlightRetryMiddleware.from_crawler(crawler)
    spider = _retry_spider(crawler)

    req = Request("https://example.com")
    resp = HtmlResponse(url=req.url, body=_push_html("0:X"), request=req)

    first = mw.process_response(req, resp, spider)
    assert isinstance(first, Request)
    # Exhaust the single allowed retry -- the next attempt must give up
    # and return the (still low-confidence) response as-is rather than
    # retrying forever.
    second = mw.process_response(first, resp, spider)
    assert second is resp


def test_concurrent_access_does_not_corrupt_fallback_cache():
    # Scrapy runs many requests concurrently via Twisted's single-threaded
    # reactor (cooperative, not pre-emptive) -- genuine OS-thread
    # concurrency isn't how Scrapy itself exercises this code. This test
    # is a deliberately stronger check than that: real OS threads
    # hammering the same fallback cache simultaneously, to catch any
    # accidental shared-mutable-state bug the reactor's cooperative
    # scheduling would otherwise never surface.
    #
    # Uses a minimal, portably-slotted fake object and calls
    # `_flight_property` directly (rather than a `Response` subclass's
    # `.flight` property) for the same reason as
    # `test_fallback_cache_entries_are_garbage_collected` above:
    # `object.__setattr__` bypasses any subclass's own `__setattr__`
    # override, so whether a `Response` subclass actually forces the
    # fallback path is Scrapy-version-dependent, not something a
    # `__setattr__` override can reliably control.
    class LockedFakeResponse:
        __slots__ = ("body", "url", "__weakref__")

        def __init__(self, url, body):
            self.url = url
            self.body = body

    responses = [
        LockedFakeResponse(url=f"https://example.com/{i}", body=_push_html(f'0:{{"id":{i}}}'))
        for i in range(50)
    ]
    errors = []

    def worker(resp, idx):
        try:
            for _ in range(20):
                page = _flight_property(resp)
                assert page.resolve_chunk("0") == {"id": idx}
        except Exception as e:  # pragma: no cover - failure path
            errors.append(e)

    threads = [
        threading.Thread(target=worker, args=(resp, i))
        for i, resp in enumerate(responses)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    # All 50 responses should have gone through the fallback path (none
    # of them can hold a `_nextflight_cache` attribute at all), so this
    # is also a real check that concurrent fallback-cache access across
    # threads didn't corrupt or lose entries.
    assert len(_fallback_cache) >= 50
