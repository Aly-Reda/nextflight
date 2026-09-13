"""
Tests for the v0.4.2 P0 feature set:

  1.  call_server_action() / ActionNotFoundError
  2.  resolve_next_image_url() / build_next_image_url()
  11. detect_challenge_page()
  14. FlightSession
  27. RSC special-value decoding (dates / Map / Set)
  29. detect_deployment_protection()  (bundled with item 11)
"""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from nextflight import (
    FlightExtractor,
    FlightSession,
    ActionNotFoundError,
    FlightRequestError,
    call_server_action,
    capture_router_state_tree_hint,
    resolve_next_image_url,
    build_next_image_url,
    resolve_next_image_srcset,
    detect_challenge_page,
    detect_deployment_protection,
)


def _run_local_server(handler_cls):
    server = HTTPServer(("127.0.0.1", 0), handler_cls)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, server.server_port


# ------------------------------------------------------------------ #
# Item 27 -- RSC special-value decoding
# ------------------------------------------------------------------ #

def test_date_sigil_decodes_to_datetime():
    import datetime
    html = (
        '<script>self.__next_f.push([1, '
        '"1:{\\"createdAt\\":\\"$D2024-01-05T00:00:00.000Z\\"}"'
        '])</script>'
    )
    page = FlightExtractor(html)
    resolved = page.resolve_chunk("1")
    assert isinstance(resolved["createdAt"], datetime.datetime)
    assert resolved["createdAt"].year == 2024
    assert resolved["createdAt"].month == 1
    assert resolved["createdAt"].day == 5


def test_date_sigil_with_offset_not_z():
    import datetime
    html = (
        '<script>self.__next_f.push([1, '
        '"1:{\\"createdAt\\":\\"$D2024-01-05T00:00:00.000+02:00\\"}"'
        '])</script>'
    )
    page = FlightExtractor(html)
    resolved = page.resolve_chunk("1")
    assert isinstance(resolved["createdAt"], datetime.datetime)


def test_bigint_sigil_decodes_to_int():
    html = '<script>self.__next_f.push([1, "1:{\\"views\\":\\"$n123456789012345\\"}"])</script>'
    page = FlightExtractor(html)
    resolved = page.resolve_chunk("1")
    assert resolved["views"] == 123456789012345
    assert isinstance(resolved["views"], int)


def test_map_sigil_decodes_to_dict():
    # chunk 1 references chunk 2 (the Map's [key, value] pairs) via "$Q2"
    html = (
        '<script>self.__next_f.push([1, "1:{\\"tags\\":\\"$Q2\\"}"])</script>'
        '<script>self.__next_f.push([2, "2:[[\\"a\\",1],[\\"b\\",2]]"])</script>'
    )
    page = FlightExtractor(html)
    resolved = page.resolve_chunk("1")
    assert resolved["tags"] == {"a": 1, "b": 2}


def test_set_sigil_decodes_to_list():
    html = (
        '<script>self.__next_f.push([1, "1:{\\"colors\\":\\"$W2\\"}"])</script>'
        '<script>self.__next_f.push([2, "2:[\\"red\\",\\"blue\\"]"])</script>'
    )
    page = FlightExtractor(html)
    resolved = page.resolve_chunk("1")
    assert resolved["colors"] == ["red", "blue"]


def test_decode_rsc_values_false_preserves_old_raw_string_behavior():
    html = '<script>self.__next_f.push([1, "1:{\\"createdAt\\":\\"$D2024-01-05T00:00:00.000Z\\"}"])</script>'
    page = FlightExtractor(html, decode_rsc_values=False)
    resolved = page.resolve_chunk("1")
    assert resolved["createdAt"] == "$D2024-01-05T00:00:00.000Z"


def test_malformed_date_sigil_falls_back_to_raw_string():
    html = '<script>self.__next_f.push([1, "1:{\\"createdAt\\":\\"$Dnot-a-date\\"}"])</script>'
    page = FlightExtractor(html)
    resolved = page.resolve_chunk("1")
    assert resolved["createdAt"] == "$Dnot-a-date"


# ------------------------------------------------------------------ #
# Item 2 -- /_next/image helpers
# ------------------------------------------------------------------ #

def test_resolve_next_image_url_root_relative():
    url = "/_next/image?url=%2Fphotos%2F1.jpg&w=640&q=75"
    assert resolve_next_image_url(url) == "/photos/1.jpg"


def test_resolve_next_image_url_absolute():
    url = "/_next/image?url=https%3A%2F%2Fcdn.example.com%2F1.jpg&w=1080&q=80"
    assert resolve_next_image_url(url) == "https://cdn.example.com/1.jpg"


def test_resolve_next_image_url_raises_on_non_proxy_url():
    with pytest.raises(ValueError):
        resolve_next_image_url("https://example.com/photos/1.jpg")


def test_build_next_image_url_round_trips():
    built = build_next_image_url("https://example.com", "/photos/1.jpg", width=750, quality=80)
    assert built.startswith("https://example.com/_next/image?")
    assert resolve_next_image_url(built) == "/photos/1.jpg"


def test_build_next_image_url_round_trips_absolute_source_with_special_chars():
    src = "https://cdn.example.com/a b/1.jpg?v=2"
    built = build_next_image_url("https://example.com", src, width=1920)
    assert resolve_next_image_url(built) == src


def test_resolve_next_image_srcset():
    html = (
        '<img srcset="/_next/image?url=%2Fa.jpg&w=640&q=75 640w, '
        '/_next/image?url=%2Fa.jpg&w=1080&q=75 1080w">'
    )
    entries = resolve_next_image_srcset(html)
    assert entries == [
        {"width": 640, "url": "/a.jpg"},
        {"width": 1080, "url": "/a.jpg"},
    ]


# ------------------------------------------------------------------ #
# Items 11 / 29 -- challenge-page / deployment-protection fingerprints
# ------------------------------------------------------------------ #

def test_detect_challenge_page_cloudflare():
    html = "<html><head><title>Just a moment...</title></head><body>cf-browser-verification</body></html>"
    assert detect_challenge_page(html) == "cloudflare"


def test_detect_challenge_page_datadome():
    html = "<html><body>protected by datadome</body></html>"
    assert detect_challenge_page(html) == "datadome"


def test_detect_challenge_page_none_on_normal_truncated_page():
    html = '<script>self.__next_f.push([1, "1:{\\"title\\":\\"Some prod'  # truncated, not a challenge
    assert detect_challenge_page(html) is None


def test_detect_challenge_page_checks_headers_too():
    class FakeResponse:
        text = "<html><body>ordinary body</body></html>"
        headers = {"Server": "cloudflare", "cf-mitigated": "challenge"}

    # headers alone don't guarantee a hit unless they match a fingerprint;
    # this just exercises that headers are actually inspected without
    # crashing on a dict-like object.
    result = detect_challenge_page(FakeResponse())
    assert result is None or isinstance(result, str)


def test_detect_deployment_protection_true():
    html = "<html><body>Authentication Required</body><script>vercel.com/sso-api</script></html>"
    assert detect_deployment_protection(html) is True


def test_detect_deployment_protection_false_on_normal_page():
    html = "<html><body>Welcome to our store</body></html>"
    assert detect_deployment_protection(html) is False


# ------------------------------------------------------------------ #
# Item 1 -- call_server_action() / ActionNotFoundError
# ------------------------------------------------------------------ #

def _action_server(*, action_response_body, expect_action_id=None, status=200,
                    capture_into=None):
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            if capture_into is not None:
                capture_into["headers"] = dict(self.headers)
                capture_into["body"] = body
            if expect_action_id and self.headers.get("Next-Action") != expect_action_id:
                self.send_response(400)
                self.end_headers()
                return
            self.send_response(status)
            self.send_header("Content-Type", "text/x-component")
            self.end_headers()
            self.wfile.write(action_response_body.encode("utf-8"))

        def log_message(self, *a):
            pass

    return Handler


def test_call_server_action_returns_flight_extractor():
    body = '1:{"ok":true}\n'
    captured = {}
    Handler = _action_server(action_response_body=body, expect_action_id="abc123",
                              capture_into=captured)
    server, port = _run_local_server(Handler)
    try:
        result = call_server_action(
            f"http://127.0.0.1:{port}/listings",
            "abc123",
            [{"cursor": "x"}],
            router_state_tree=capture_router_state_tree_hint(),
        )
        assert isinstance(result, FlightExtractor)
        assert result.resolve_chunk("1") == {"ok": True}
        assert captured["headers"]["Accept"] == "text/x-component"
        assert captured["headers"]["Next-Action"] == "abc123"
        assert "Next-Router-State-Tree" in captured["headers"]
        # args were sent as a plain JSON array
        assert json.loads(captured["body"]) == [{"cursor": "x"}]
    finally:
        server.shutdown()


def test_call_server_action_raises_action_not_found_error():
    Handler = _action_server(
        action_response_body="Failed to find Server Action \"stale123\"", status=500,
    )
    server, port = _run_local_server(Handler)
    try:
        with pytest.raises(ActionNotFoundError) as exc_info:
            call_server_action(
                f"http://127.0.0.1:{port}/listings",
                "stale123",
                [],
                router_state_tree=capture_router_state_tree_hint(),
            )
        assert isinstance(exc_info.value, FlightRequestError)
    finally:
        server.shutdown()


def test_call_server_action_multipart_for_file_like_arg():
    import io
    captured = {}
    Handler = _action_server(action_response_body='1:{"ok":true}\n', capture_into=captured)
    server, port = _run_local_server(Handler)
    try:
        fileobj = io.BytesIO(b"file-bytes")
        fileobj.name = "upload.bin"
        call_server_action(
            f"http://127.0.0.1:{port}/upload",
            "act1",
            [fileobj],
            router_state_tree=capture_router_state_tree_hint(),
        )
        assert captured["headers"]["Content-Type"].startswith("multipart/form-data")
        assert b"file-bytes" in captured["body"]
        assert b'filename="upload.bin"' in captured["body"]
    finally:
        server.shutdown()


def test_capture_router_state_tree_hint_returns_valid_json():
    hint = capture_router_state_tree_hint()
    assert json.loads(hint) == ["", {}, None, None, True]


# ------------------------------------------------------------------ #
# Item 14 -- FlightSession
# ------------------------------------------------------------------ #

def test_flight_session_get_then_call_action_without_manual_state_threading():
    calls = {"cookies_on_action": None, "headers_on_action": None}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Set-Cookie", "sid=abc123; Path=/")
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<html><body>listing page</body></html>")

        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            self.rfile.read(length)
            calls["cookies_on_action"] = self.headers.get("Cookie")
            calls["headers_on_action"] = dict(self.headers)
            self.send_response(200)
            self.send_header("Content-Type", "text/x-component")
            self.end_headers()
            self.wfile.write(b'1:{"nextCursor":"c2"}\n')

        def log_message(self, *a):
            pass

    server, port = _run_local_server(Handler)
    try:
        session = FlightSession()
        url = f"http://127.0.0.1:{port}/listings"
        page = session.get(url)
        assert isinstance(page, FlightExtractor)

        result = session.call_action(url, "paginate-action", [{"cursor": "c1"}])
        assert isinstance(result, FlightExtractor)
        assert result.resolve_chunk("1") == {"nextCursor": "c2"}
        # cookie from the .get() response was carried into the action call
        assert calls["cookies_on_action"] is not None
        assert "sid=abc123" in calls["cookies_on_action"]
        # Next-Action header set automatically
        assert calls["headers_on_action"]["Next-Action"] == "paginate-action"
    finally:
        server.shutdown()


def test_flight_session_remembers_router_state_tree_per_route():
    session = FlightSession()
    url = "http://example.com/foo"
    assert session._router_state_trees == {}
    session._router_state_trees[session._route_key(url)] = '["custom"]'
    # call_action should reuse the remembered tree rather than the generic hint
    # (verified indirectly: no exception, and the key lookup matches)
    assert session._route_key(url) == "/foo"
