"""
Tests for the v0.4.3 P1 feature set:

  6.  detect_middleware_rewrite()
  7.  get_cache_status()
  12. find_pagination_action()
  21. static_export detection (detect_next_router)
  22. is_fallback_skeleton()
  23. detect_draft_mode() + FlightSession(strip_draft_cookies=...)
  25. basePath / multi-zone handling (find_next_chunk_urls, detect_base_path)

Bonus, small/cheap additions bundled alongside the above:
  28. get_rate_limit_headers()
  24. get_edge_geo_headers()
  30. find_error_digest()
"""
import json

from nextflight import (
    FlightExtractor,
    FlightSession,
    extract,
    detect_next_router,
    detect_base_path,
    find_next_chunk_urls,
    find_pagination_action,
    find_error_digest,
    detect_middleware_rewrite,
    detect_draft_mode,
    get_cache_status,
    get_rate_limit_headers,
    get_edge_geo_headers,
)


class _Headers(dict):
    """Minimal case-sensitive-but-close-enough stand-in for a
    requests-style headers mapping, for tests that don't need a real
    HTTP round trip."""


class _FakeResponse:
    def __init__(self, headers=None, text="", cookies=None):
        self.headers = headers or {}
        self.text = text
        self.cookies = cookies or {}


# ------------------------------------------------------------------ #
# Item 25 -- basePath / multi-zone handling
# ------------------------------------------------------------------ #

def test_find_next_chunk_urls_default_no_base_path():
    html = '<script src="/_next/static/chunks/123.js"></script>'
    assert find_next_chunk_urls(html) == ["/_next/static/chunks/123.js"]


def test_find_next_chunk_urls_with_custom_base_path():
    html = '<script src="/docs/_next/static/chunks/123.js"></script>'
    # previously this matched nothing at all -- now it's found, prefix included
    assert find_next_chunk_urls(html) == ["/docs/_next/static/chunks/123.js"]


def test_find_next_chunk_urls_filters_by_explicit_base_path():
    html = (
        '<script src="/docs/_next/static/chunks/123.js"></script>'
        '<script src="/_next/static/chunks/456.js"></script>'
    )
    assert find_next_chunk_urls(html, base_path="/docs") == ["/docs/_next/static/chunks/123.js"]
    assert find_next_chunk_urls(html, base_path="") == ["/_next/static/chunks/456.js"]


def test_detect_base_path_finds_prefix():
    html = '<script src="/docs/_next/static/chunks/123.js"></script>'
    assert detect_base_path(html) == "/docs"


def test_detect_base_path_empty_for_default_site():
    html = '<script src="/_next/static/chunks/123.js"></script>'
    assert detect_base_path(html) == ""


def test_detect_base_path_empty_when_no_chunks_found():
    assert detect_base_path("<html><body>hi</body></html>") == ""


# ------------------------------------------------------------------ #
# Item 21 -- static export detection
# ------------------------------------------------------------------ #

def test_detect_next_router_static_export_pages_router():
    html = (
        '<script id="__NEXT_DATA__" type="application/json">'
        '{"props":{"pageProps":{}},"nextExport":true,"page":"/"}'
        '</script>'
    )
    assert detect_next_router(html) == "static_export"


def test_detect_next_router_pages_router_normal_ssr_not_export():
    html = (
        '<script id="__NEXT_DATA__" type="application/json">'
        '{"props":{"pageProps":{}},"page":"/"}'
        '</script>'
    )
    assert detect_next_router(html) == "pages"


def test_detect_next_router_static_export_app_router_fallback():
    # No Flight push, no __NEXT_DATA__, but Next.js chunk URLs present.
    html = '<html><script src="/_next/static/chunks/123.js"></script></html>'
    assert detect_next_router(html) == "static_export"


def test_detect_next_router_unknown_for_non_nextjs_page():
    html = "<html><body>not a next.js page</body></html>"
    assert detect_next_router(html) == "unknown"


def test_detect_next_router_app_still_works():
    html = '<script>self.__next_f.push([1, "1:{\\"a\\":1}"])</script>'
    assert detect_next_router(html) == "app"


# ------------------------------------------------------------------ #
# Item 12 -- find_pagination_action()
# ------------------------------------------------------------------ #

def test_find_pagination_action_finds_has_next_page_shape():
    html = (
        '<script>self.__next_f.push([1, '
        '"1:{\\"listings\\":[1,2,3],\\"pageInfo\\":{\\"hasNextPage\\":true,\\"endCursor\\":\\"c2\\"}}"'
        '])</script>'
    )
    page = extract(html)
    result = find_pagination_action(page)
    assert result == {"hasNextPage": True, "endCursor": "c2"}


def test_find_pagination_action_accepts_raw_html():
    html = (
        '<script>self.__next_f.push([1, '
        '"1:{\\"nextCursor\\":\\"abc\\"}"'
        '])</script>'
    )
    result = find_pagination_action(html)
    assert result == {"nextCursor": "abc"}


def test_find_pagination_action_none_when_absent():
    html = '<script>self.__next_f.push([1, "1:{\\"title\\":\\"hi\\"}"])</script>'
    assert find_pagination_action(html) is None


# ------------------------------------------------------------------ #
# Item 22 -- is_fallback_skeleton()
# ------------------------------------------------------------------ #

def test_is_fallback_skeleton_true_for_near_empty_page():
    html = '<script>self.__next_f.push([1, "1:{\\"loading\\":true}"])</script>'
    page = extract(html)
    assert page.is_fallback_skeleton() is True


def test_is_fallback_skeleton_false_for_real_content():
    html = (
        '<script>self.__next_f.push([1, '
        '"1:{\\"title\\":\\"Product\\",\\"price\\":999,\\"sku\\":\\"abc\\",'
        '\\"tags\\":[\\"a\\",\\"b\\",\\"c\\"]}"'
        '])</script>'
    )
    page = extract(html)
    assert page.is_fallback_skeleton() is False


def test_is_fallback_skeleton_false_when_no_json_chunks():
    page = extract('<script>self.__next_f.push([1, "1:T5,hello"])</script>')
    assert page.is_fallback_skeleton() is False


def test_is_fallback_skeleton_false_on_low_confidence_page():
    # A bare, unresolvable marker row is a genuine parse problem, not a
    # skeleton -- different signal, shouldn't be conflated.
    page = extract('<script>self.__next_f.push([1, "1:X"])</script>')
    assert page.is_fallback_skeleton() is False


# ------------------------------------------------------------------ #
# Item 23 -- draft mode detection + FlightSession stripping
# ------------------------------------------------------------------ #

def test_detect_draft_mode_true_from_dict_cookies():
    assert detect_draft_mode({"__prerender_bypass": "xyz"}) is True


def test_detect_draft_mode_false_ordinary_cookies():
    assert detect_draft_mode({"sid": "abc123"}) is False


def test_detect_draft_mode_from_response_like_object_with_cookies_dict():
    resp = _FakeResponse(cookies={"__next_preview_data": "abc"})
    assert detect_draft_mode(resp) is True


def test_detect_draft_mode_from_set_cookie_header():
    resp = _FakeResponse(headers={"Set-Cookie": "__prerender_bypass=xyz; Path=/"})
    assert detect_draft_mode(resp) is True


def test_flight_session_strips_draft_cookies_by_default():
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Set-Cookie", "__prerender_bypass=xyz; Path=/")
            self.send_header("Set-Cookie", "sid=keepme; Path=/")
            self.end_headers()
            self.wfile.write(b"<html>draft page</html>")

        def log_message(self, *a):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        session = FlightSession()
        session.get(f"http://127.0.0.1:{server.server_port}/")
        names = {c.name for c in session._cookiejar}
        assert "__prerender_bypass" not in names
        assert "sid" in names
    finally:
        server.shutdown()


def test_flight_session_keeps_draft_cookies_when_disabled():
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Set-Cookie", "__prerender_bypass=xyz; Path=/")
            self.end_headers()
            self.wfile.write(b"<html>draft page</html>")

        def log_message(self, *a):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        session = FlightSession(strip_draft_cookies=False)
        session.get(f"http://127.0.0.1:{server.server_port}/")
        names = {c.name for c in session._cookiejar}
        assert "__prerender_bypass" in names
    finally:
        server.shutdown()


# ------------------------------------------------------------------ #
# Item 6 -- detect_middleware_rewrite()
# ------------------------------------------------------------------ #

def test_detect_middleware_rewrite_from_header():
    resp = _FakeResponse(headers={"x-middleware-rewrite": "https://example.com/real-page"})
    assert detect_middleware_rewrite(resp) == "https://example.com/real-page"


def test_detect_middleware_rewrite_none_when_absent():
    resp = _FakeResponse(headers={})
    assert detect_middleware_rewrite(resp) is None


def test_detect_middleware_rewrite_accepts_plain_dict():
    headers = {"x-middleware-rewrite": "/actual"}
    assert detect_middleware_rewrite(headers) == "/actual"


# ------------------------------------------------------------------ #
# Item 7 -- get_cache_status()
# ------------------------------------------------------------------ #

def test_get_cache_status_parses_headers():
    resp = _FakeResponse(headers={
        "x-nextjs-cache": "HIT",
        "Cache-Control": "s-maxage=60, stale-while-revalidate=300",
        "Age": "12",
    })
    status = get_cache_status(resp)
    assert status == {
        "cache": "HIT",
        "s_maxage": 60,
        "stale_while_revalidate": 300,
        "age": 12,
    }


def test_get_cache_status_all_none_when_absent():
    resp = _FakeResponse(headers={})
    status = get_cache_status(resp)
    assert status["cache"] is None
    assert status["s_maxage"] is None
    assert status["stale_while_revalidate"] is None
    assert status["age"] is None


# ------------------------------------------------------------------ #
# Bonus: get_rate_limit_headers(), get_edge_geo_headers(), find_error_digest()
# ------------------------------------------------------------------ #

def test_get_rate_limit_headers():
    resp = _FakeResponse(headers={
        "Retry-After": "30",
        "X-RateLimit-Remaining": "5",
        "X-RateLimit-Reset": "1717000000",
    })
    result = get_rate_limit_headers(resp)
    assert result == {"retry_after": 30, "remaining": "5", "reset": "1717000000"}


def test_get_edge_geo_headers():
    resp = _FakeResponse(headers={
        "x-vercel-ip-country": "US",
        "x-vercel-ip-city": "San%20Francisco",
        "x-vercel-ip-region": "CA",
    })
    result = get_edge_geo_headers(resp)
    assert result == {"country": "US", "city": "San%20Francisco", "region": "CA"}


def test_find_error_digest():
    html = '<html><body>Application error<script>{"digest":"abc123xyz"}</script></body></html>'
    assert find_error_digest(html) == "abc123xyz"


def test_find_error_digest_none_when_absent():
    assert find_error_digest("<html><body>fine</body></html>") is None
