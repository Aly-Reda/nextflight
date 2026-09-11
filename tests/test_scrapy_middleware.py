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

from scrapy.http import HtmlResponse, Response  # noqa: E402
from scrapy.crawler import Crawler  # noqa: E402
from scrapy.settings import Settings  # noqa: E402
from scrapy.spiders import Spider  # noqa: E402

from nextflight.extractor import FlightExtractor  # noqa: E402
from nextflight.scrapy_middleware import (  # noqa: E402
    FlightMiddleware,
    FlightItemPipeline,
    NextflightSpiderMixin,
    _fallback_cache,
)


def _push_html(*rows: str) -> bytes:
    payload = "\n".join(rows)
    return f"<script>self.__next_f.push([1,{json.dumps(payload)}])</script>".encode("utf-8")


def _make_crawler(settings: dict) -> Crawler:
    # A minimal stand-in exposing just `.settings`, which is all
    # `FlightMiddleware.from_crawler`/`FlightItemPipeline.from_crawler`
    # read -- avoids spinning up a full Crawler (reactor, engine, ...)
    # just to test settings plumbing.
    class _FakeCrawler:
        pass

    fc = _FakeCrawler()
    fc.settings = Settings(settings)
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
    # This forces the fallback path (a Response subclass that rejects
    # instance attribute assignment) and verifies the WeakKeyDictionary
    # actually drops the entry once the response itself is collected.
    class LockedResponse(HtmlResponse):
        __slots__ = ()

        def __setattr__(self, name, value):
            if name == "_nextflight_cache":
                raise AttributeError("locked")
            super().__setattr__(name, value)

    FlightMiddleware()
    resp = LockedResponse(url="https://example.com", body=_push_html('0:{"a":1}'))
    _ = resp.flight  # forces the fallback path
    assert len(_fallback_cache) >= 1

    resp_id = id(resp)
    del resp
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


def test_concurrent_access_does_not_corrupt_fallback_cache():
    # Scrapy runs many requests concurrently via Twisted's single-threaded
    # reactor (cooperative, not pre-emptive) -- genuine OS-thread
    # concurrency isn't how Scrapy itself exercises this code. This test
    # is a deliberately stronger check than that: real OS threads
    # hammering the same fallback cache simultaneously, to catch any
    # accidental shared-mutable-state bug the reactor's cooperative
    # scheduling would otherwise never surface.
    class LockedResponse(HtmlResponse):
        __slots__ = ()

        def __setattr__(self, name, value):
            if name == "_nextflight_cache":
                raise AttributeError("locked")
            super().__setattr__(name, value)

    FlightMiddleware()
    responses = [
        LockedResponse(url=f"https://example.com/{i}", body=_push_html(f'0:{{"id":{i}}}'))
        for i in range(50)
    ]
    errors = []

    def worker(resp, idx):
        try:
            for _ in range(20):
                page = resp.flight
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
