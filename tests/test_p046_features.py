"""
Tests for the v0.4.6 P3 feature set:

  8.  detect_locale()
  9.  bundler-aware next_version_hint()
  10. route-group (group) regression tests (verification only)
  17. FlightAutoThrottleMiddleware (confidence-driven throttling)
  18. NextflightSpiderMiddleware + schema_drift signal
"""
import json

import pytest

from nextflight import extract, detect_locale


def _push_html(*rows):
    payload = "\n".join(rows)
    return f'<script>self.__next_f.push([1,{json.dumps(payload)}])</script>'


# ------------------------------------------------------------------ #
# Item 8 -- detect_locale()
# ------------------------------------------------------------------ #

def test_detect_locale_from_html_lang_attribute():
    html = '<html lang="fr"><body>Bonjour</body></html>'
    assert detect_locale(html) == "fr"


def test_detect_locale_from_html_lang_with_region():
    html = '<html lang="pt-BR"><body>Ola</body></html>'
    assert detect_locale(html) == "pt-BR"


def test_detect_locale_from_url_path_prefix():
    assert detect_locale("https://example.com/en/products/1") == "en"


def test_detect_locale_from_url_path_prefix_with_region():
    assert detect_locale("https://example.com/en-US/products/1") == "en-US"


def test_detect_locale_from_subdomain():
    assert detect_locale("https://de.example.com/products/1") == "de"


def test_detect_locale_none_for_non_locale_path_segment():
    # "xx" isn't an ISO 639-1 code -- shouldn't be mistaken for a locale
    assert detect_locale("https://example.com/xx/products/1") is None


def test_detect_locale_none_for_plain_page():
    assert detect_locale("<html><body>hello</body></html>") is None


def test_detect_locale_bare_url_string_without_html():
    assert detect_locale("https://example.com/ar/listing/9") == "ar"


def test_detect_locale_html_lang_takes_priority_over_url():
    html = '<html lang="es">See <a href="https://example.com/en/other">link</a></html>'
    assert detect_locale(html) == "es"


# ------------------------------------------------------------------ #
# Item 9 -- bundler-aware next_version_hint()
# ------------------------------------------------------------------ #

def test_next_version_hint_reports_unknown_bundler_by_default():
    page = extract(_push_html('0:{"a":1}'))
    hint = page.next_version_hint()
    assert hint["bundler"] == "unknown"


def test_next_version_hint_detects_webpack():
    html = _push_html('0:{"a":1}') + "<script>self.webpackChunk_N_E=[]</script>"
    page = extract(html)
    assert page.next_version_hint()["bundler"] == "webpack"


def test_next_version_hint_detects_turbopack():
    html = _push_html('0:{"a":1}') + "<script>__turbopack_require__(123)</script>"
    page = extract(html)
    assert page.next_version_hint()["bundler"] == "turbopack"


def test_next_version_hint_bundler_present_even_with_no_version_match():
    page = extract("<html><body>not next.js</body></html>")
    hint = page.next_version_hint()
    assert hint == {"range": None, "notes": None, "matches": [], "bundler": "unknown"}


# ------------------------------------------------------------------ #
# Item 10 -- route groups "(group)" -- verification only
# ------------------------------------------------------------------ #

def test_route_group_key_does_not_interfere_with_find_by_keys():
    # "(marketing)" is a cosmetic App Router route-group segment -- it
    # should behave as an ordinary dict key, not break resolution or
    # search into its nested content.
    html = _push_html(
        '0:{"(marketing)":{"price":499,"title":"Landing page product"}}'
    )
    page = extract(html)
    assert page.find_by_keys({"price", "title"}) == {"price": 499, "title": "Landing page product"}


def test_route_group_key_visible_in_shape():
    html = _push_html('0:{"(marketing)":{"price":499}}')
    page = extract(html)
    shape = page.shape("0")
    assert "(marketing)" in shape


# ------------------------------------------------------------------ #
# Items 17 / 18 -- Scrapy-dependent (skipped if scrapy isn't installed)
# ------------------------------------------------------------------ #

scrapy = pytest.importorskip("scrapy")

from scrapy.http import HtmlResponse  # noqa: E402
from scrapy.settings import Settings  # noqa: E402
from scrapy.statscollectors import StatsCollector  # noqa: E402
from scrapy.spiders import Spider  # noqa: E402

from nextflight.scrapy_middleware import (  # noqa: E402
    FlightMiddleware,
    FlightAutoThrottleMiddleware,
    NextflightSpiderMiddleware,
    schema_drift,
)


class _FakeSignals:
    def __init__(self):
        self.sent = []

    def connect(self, *args, **kwargs):
        pass

    def send_catch_log(self, signal, **kwargs):
        self.sent.append((signal, kwargs))


class _FakeSlot:
    def __init__(self, delay=1.0):
        self.delay = delay


class _FakeDownloader:
    def __init__(self, slots):
        self.slots = slots


class _FakeEngine:
    def __init__(self, slots):
        self.downloader = _FakeDownloader(slots)


class _FakeCrawler:
    def __init__(self, settings=None, slots=None):
        self.settings = Settings(settings or {})
        self.signals = _FakeSignals()
        self.stats = StatsCollector(self)
        if slots is not None:
            self.engine = _FakeEngine(slots)


def _low_confidence_response(url="https://example.com/a"):
    # A bare "$"-style marker row materializes as a raw string, dragging
    # parse_confidence below 1.0.
    return HtmlResponse(url=url, body=_push_html("0:X").encode())


def _high_confidence_response(url="https://example.com/a"):
    return HtmlResponse(url=url, body=_push_html('0:{"a":1}').encode())


def test_autothrottle_middleware_slows_down_on_sustained_low_confidence():
    FlightMiddleware()
    slots = {"example.com": _FakeSlot(delay=1.0)}
    crawler = _FakeCrawler(
        {"NEXTFLIGHT_THROTTLE_WINDOW": 4, "NEXTFLIGHT_THROTTLE_MIN_CONFIDENCE": 0.9,
         "NEXTFLIGHT_THROTTLE_FACTOR": 2.0},
        slots=slots,
    )
    mw = FlightAutoThrottleMiddleware.from_crawler(crawler)
    spider = Spider(name="test")

    for _ in range(3):
        mw.process_response(None, _low_confidence_response(), spider)

    assert slots["example.com"].delay == 2.0


def test_autothrottle_middleware_relaxes_after_recovery():
    FlightMiddleware()
    slots = {"example.com": _FakeSlot(delay=1.0)}
    crawler = _FakeCrawler(
        {"NEXTFLIGHT_THROTTLE_WINDOW": 4, "NEXTFLIGHT_THROTTLE_MIN_CONFIDENCE": 0.9,
         "NEXTFLIGHT_THROTTLE_FACTOR": 2.0},
        slots=slots,
    )
    mw = FlightAutoThrottleMiddleware.from_crawler(crawler)
    spider = Spider(name="test")

    for _ in range(3):
        mw.process_response(None, _low_confidence_response(), spider)
    assert slots["example.com"].delay == 2.0

    # Push enough high-confidence responses to fully flush the low ones
    # out of the rolling window (maxlen == window).
    for _ in range(mw.window):
        mw.process_response(None, _high_confidence_response(), spider)
    assert slots["example.com"].delay == 1.0


def test_autothrottle_middleware_does_nothing_without_engine():
    FlightMiddleware()
    crawler = _FakeCrawler({})  # no .engine attribute at all
    mw = FlightAutoThrottleMiddleware.from_crawler(crawler)
    spider = Spider(name="test")
    # should not raise even though crawler.engine doesn't exist
    for _ in range(5):
        result = mw.process_response(None, _low_confidence_response(), spider)
    assert result is not None


def test_autothrottle_middleware_ignores_zero_chunk_responses():
    FlightMiddleware()
    slots = {"example.com": _FakeSlot(delay=1.0)}
    crawler = _FakeCrawler({"NEXTFLIGHT_THROTTLE_WINDOW": 2}, slots=slots)
    mw = FlightAutoThrottleMiddleware.from_crawler(crawler)
    spider = Spider(name="test")
    resp = HtmlResponse(url="https://example.com/a", body=b"<html>not next.js</html>")
    mw.process_response(None, resp, spider)
    assert slots["example.com"].delay == 1.0  # untouched


def test_spider_middleware_fires_schema_drift_signal_after_baseline():
    FlightMiddleware()
    crawler = _FakeCrawler({"NEXTFLIGHT_DRIFT_MIN_SAMPLES": 3, "NEXTFLIGHT_DRIFT_THRESHOLD": 0.3})
    mw = NextflightSpiderMiddleware.from_crawler(crawler)
    spider = Spider(name="test")

    # Establish a clean baseline (confidence 1.0) for 3 responses.
    for _ in range(3):
        mw.process_spider_input(_high_confidence_response(), spider)

    # A low-confidence response should now trigger the signal.
    mw.process_spider_input(_low_confidence_response(), spider)

    assert len(crawler.signals.sent) == 1
    signal, kwargs = crawler.signals.sent[0]
    assert signal is schema_drift
    assert kwargs["domain"] == "example.com"
    assert kwargs["baseline"] == pytest.approx(1.0)
    assert kwargs["confidence"] < 1.0


def test_spider_middleware_no_signal_before_baseline_established():
    FlightMiddleware()
    crawler = _FakeCrawler({"NEXTFLIGHT_DRIFT_MIN_SAMPLES": 5})
    mw = NextflightSpiderMiddleware.from_crawler(crawler)
    spider = Spider(name="test")

    # Fewer than min_samples responses -- no baseline yet, so a
    # low-confidence response shouldn't fire anything.
    for _ in range(2):
        mw.process_spider_input(_high_confidence_response(), spider)
    mw.process_spider_input(_low_confidence_response(), spider)

    assert crawler.signals.sent == []


def test_spider_middleware_no_signal_when_confidence_stable():
    FlightMiddleware()
    crawler = _FakeCrawler({"NEXTFLIGHT_DRIFT_MIN_SAMPLES": 3, "NEXTFLIGHT_DRIFT_THRESHOLD": 0.3})
    mw = NextflightSpiderMiddleware.from_crawler(crawler)
    spider = Spider(name="test")

    for _ in range(6):
        mw.process_spider_input(_high_confidence_response(), spider)

    assert crawler.signals.sent == []


def test_spider_middleware_ignores_zero_chunk_responses():
    FlightMiddleware()
    crawler = _FakeCrawler({"NEXTFLIGHT_DRIFT_MIN_SAMPLES": 1})
    mw = NextflightSpiderMiddleware.from_crawler(crawler)
    spider = Spider(name="test")
    resp = HtmlResponse(url="https://example.com/a", body=b"<html>not next.js</html>")
    result = mw.process_spider_input(resp, spider)
    assert result is None
    assert crawler.signals.sent == []
