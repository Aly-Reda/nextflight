"""
Tests for the v0.4.4 P2 feature set:

  3.  Parallel/intercepting route recognition (is_route_slot_key,
      is_intercepting_route_segment) + verification that search already
      transparently walks into slot content.
  13. find_api_routes()
  15. FlightDedupeMiddleware(dedupe_keys=...)
  16. build_action_meta() / read_action_meta() (Scrapy meta convention)
  19. find_meta_tags()
  26. discover_urls_from_sitemap()
"""
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from nextflight import (
    extract,
    find_api_routes,
    find_meta_tags,
    is_route_slot_key,
    is_intercepting_route_segment,
    discover_urls_from_sitemap,
)


def _run_local_server(handler_cls):
    server = HTTPServer(("127.0.0.1", 0), handler_cls)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, server.server_port


# ------------------------------------------------------------------ #
# Item 3 -- parallel / intercepting routes
# ------------------------------------------------------------------ #

def test_is_route_slot_key():
    assert is_route_slot_key("@modal") is True
    assert is_route_slot_key("@analytics") is True
    assert is_route_slot_key("children") is False
    assert is_route_slot_key("price") is False
    assert is_route_slot_key(123) is False


def test_is_intercepting_route_segment():
    assert is_intercepting_route_segment("(.)") is True
    assert is_intercepting_route_segment("(..)") is True
    assert is_intercepting_route_segment("(...)") is True
    assert is_intercepting_route_segment("(..)(..)") is True
    assert is_intercepting_route_segment("(group)") is False
    assert is_intercepting_route_segment("photos") is False


def test_find_by_keys_transparently_walks_into_slot_content():
    # Content nested under a "@modal" parallel-route slot key is found by
    # find_by_keys() the same as any other nested content -- no special
    # slot-aware call needed, since search recurses by value regardless
    # of key name.
    html = (
        '<script>self.__next_f.push([1, '
        '"1:{\\"@modal\\":{\\"price\\":999,\\"title\\":\\"Quick view\\"},'
        '\\"@children\\":{\\"other\\":true}}"'
        '])</script>'
    )
    page = extract(html)
    match = page.find_by_keys({"price", "title"})
    assert match == {"price": 999, "title": "Quick view"}


# ------------------------------------------------------------------ #
# Item 13 -- find_api_routes()
# ------------------------------------------------------------------ #

def test_find_api_routes_basic():
    html = '<script>fetch("/api/products?page=2").then(r=>r.json())</script>'
    assert find_api_routes(html) == ["/api/products"]


def test_find_api_routes_dedupes_and_preserves_order():
    html = (
        "fetch('/api/a') fetch(\"/api/b\") fetch('/api/a?x=1')"
    )
    assert find_api_routes(html) == ["/api/a", "/api/b"]


def test_find_api_routes_empty_when_absent():
    assert find_api_routes("<html><body>no api calls here</body></html>") == []


# ------------------------------------------------------------------ #
# Item 19 -- find_meta_tags()
# ------------------------------------------------------------------ #

def test_find_meta_tags_property_then_content():
    html = (
        '<meta property="og:title" content="Cool Product">'
        '<meta property="og:image" content="https://example.com/1.jpg">'
        '<meta name="twitter:card" content="summary_large_image">'
    )
    tags = find_meta_tags(html)
    assert tags == {
        "og:title": "Cool Product",
        "og:image": "https://example.com/1.jpg",
        "twitter:card": "summary_large_image",
    }


def test_find_meta_tags_content_then_property():
    html = '<meta content="Reversed Order Title" property="og:title">'
    assert find_meta_tags(html) == {"og:title": "Reversed Order Title"}


def test_find_meta_tags_empty_dict_when_absent():
    assert find_meta_tags("<html><head></head><body></body></html>") == {}


def test_find_meta_tags_ignores_non_og_twitter_meta():
    html = '<meta name="description" content="not og or twitter">'
    assert find_meta_tags(html) == {}


# ------------------------------------------------------------------ #
# Item 26 -- discover_urls_from_sitemap()
# ------------------------------------------------------------------ #

def test_discover_urls_from_sitemap_flat():
    body = (
        b'<?xml version="1.0" encoding="UTF-8"?>'
        b'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        b"<url><loc>https://example.com/a</loc></url>"
        b"<url><loc>https://example.com/b</loc></url>"
        b"</urlset>"
    )

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "application/xml")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    server, port = _run_local_server(Handler)
    try:
        urls = discover_urls_from_sitemap(f"http://127.0.0.1:{port}")
        assert urls == ["https://example.com/a", "https://example.com/b"]
    finally:
        server.shutdown()


def test_discover_urls_from_sitemap_follows_index_nesting():
    index_body = (
        b'<?xml version="1.0" encoding="UTF-8"?>'
        b'<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        b"<sitemap><loc>SITEMAP_A_URL</loc></sitemap>"
        b"</sitemapindex>"
    )
    child_body = (
        b'<?xml version="1.0" encoding="UTF-8"?>'
        b'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        b"<url><loc>https://example.com/child-1</loc></url>"
        b"</urlset>"
    )

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/sitemap.xml":
                body = index_body.replace(b"SITEMAP_A_URL", self._child_url().encode())
            else:
                body = child_body
            self.send_response(200)
            self.send_header("Content-Type", "application/xml")
            self.end_headers()
            self.wfile.write(body)

        def _child_url(self):
            return f"http://127.0.0.1:{self.server.server_port}/sitemap-a.xml"

        def log_message(self, *a):
            pass

    server, port = _run_local_server(Handler)
    try:
        urls = discover_urls_from_sitemap(f"http://127.0.0.1:{port}")
        assert urls == ["https://example.com/child-1"]
    finally:
        server.shutdown()


def test_discover_urls_from_sitemap_returns_empty_on_failure():
    # nothing listening on this port
    assert discover_urls_from_sitemap("http://127.0.0.1:1", timeout=0.5) == []


def test_discover_urls_from_sitemap_accepts_direct_xml_url():
    body = (
        b'<?xml version="1.0" encoding="UTF-8"?>'
        b'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        b"<url><loc>https://example.com/direct</loc></url>"
        b"</urlset>"
    )

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    server, port = _run_local_server(Handler)
    try:
        urls = discover_urls_from_sitemap(f"http://127.0.0.1:{port}/custom-sitemap.xml")
        assert urls == ["https://example.com/direct"]
    finally:
        server.shutdown()


# ------------------------------------------------------------------ #
# Item 15 -- FlightDedupeMiddleware(dedupe_keys=...)  (Scrapy required)
# ------------------------------------------------------------------ #

scrapy = pytest.importorskip("scrapy")


def test_dedupe_middleware_multi_field_key():
    from nextflight.scrapy_middleware import FlightDedupeMiddleware

    mw = FlightDedupeMiddleware(dedupe_key=["id", "price"])
    assert mw.dedupe_keys == ("id", "price")
    assert mw.dedupe_key == "id"  # backward-compatible single accessor

    result = list(mw.process_spider_output(
        None,
        [
            {"id": 1, "price": 10, "note": "a"},
            {"id": 1, "price": 10, "note": "different note, same id+price"},
            {"id": 1, "price": 20, "note": "different price -- not a dup"},
        ],
        None,
    ))
    assert len(result) == 2


def test_dedupe_middleware_single_string_key_still_works():
    from nextflight.scrapy_middleware import FlightDedupeMiddleware

    mw = FlightDedupeMiddleware(dedupe_key="id")
    assert mw.dedupe_keys == ("id",)
    result = list(mw.process_spider_output(
        None, [{"id": 1, "note": "a"}, {"id": 1, "note": "b"}], None,
    ))
    assert len(result) == 1


def test_dedupe_middleware_from_crawler_rejects_both_settings():
    from scrapy.crawler import Crawler
    from scrapy.settings import Settings
    from scrapy.spiders import Spider
    from scrapy.exceptions import NotConfigured
    from nextflight.scrapy_middleware import FlightDedupeMiddleware

    settings = Settings({
        "NEXTFLIGHT_DEDUPE_ACROSS_PAGES": True,
        "NEXTFLIGHT_DEDUPE_KEY": "id",
        "NEXTFLIGHT_DEDUPE_KEYS": ["id", "price"],
    })
    crawler = Crawler(Spider, settings)
    crawler.settings = settings
    with pytest.raises(NotConfigured):
        FlightDedupeMiddleware.from_crawler(crawler)


# ------------------------------------------------------------------ #
# Item 16 -- build_action_meta() / read_action_meta()
# ------------------------------------------------------------------ #

def test_build_and_read_action_meta_round_trip():
    from nextflight.scrapy_middleware import build_action_meta, read_action_meta

    meta = build_action_meta("abc123", '["",{},null,null,true]', cursor="c2")
    request = scrapy.Request("https://example.com", meta=meta)
    ctx = read_action_meta(request)
    assert ctx["action_id"] == "abc123"
    assert ctx["cursor"] == "c2"


def test_read_action_meta_returns_empty_dict_when_absent():
    from nextflight.scrapy_middleware import read_action_meta

    request = scrapy.Request("https://example.com")
    assert read_action_meta(request) == {}
