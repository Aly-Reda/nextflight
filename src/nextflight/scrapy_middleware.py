"""
nextflight.scrapy_middleware

Optional Scrapy integration: a downloader middleware that attaches a
`.flight` attribute to every `Response` it sees, so spiders can write
``response.flight.find_by_keys(...)`` directly instead of calling
``nextflight.extract(response)`` by hand in every callback.

Requires Scrapy (``pip install nextflight[scrapy]`` or just have Scrapy
already installed, as any project using this middleware necessarily
does) -- importing this module without Scrapy installed raises a clear
`ImportError` rather than a confusing one, but the rest of `nextflight`
has no dependency on this module at all.

Usage
-----
Add to your project's ``settings.py``::

    DOWNLOADER_MIDDLEWARES = {
        "nextflight.scrapy_middleware.FlightMiddleware": 543,
    }

    # All optional -- shown with their defaults:
    NEXTFLIGHT_STRICT = False
    NEXTFLIGHT_REPAIR = False
    NEXTFLIGHT_DEDUPE = False

Then in any spider callback::

    def parse(self, response):
        listing = response.flight.find_by_keys({"price", "title"})

`response.flight` is a lazily-constructed `FlightExtractor` -- for a
response, HTML parsing only happens the first time `.flight` is
accessed, so responses whose spider never reads Flight data pay no
parsing cost at all. It also works unmodified in `scrapy shell` once the
middleware is enabled in settings, since `scrapy shell` builds its
`response` object through the same `Response` class this middleware
patches -- there's nothing spider-callback-specific about how the
property is installed.

See also `FlightItemPipeline` (auto-yield items matching a required key
set) and `NextflightSpiderMixin` (`self.flight(response)` shorthand +
per-domain version-hint logging) below.
"""

from __future__ import annotations

import codecs
import logging
import re
import weakref
from typing import Any, Iterable, Optional
from urllib.parse import urlparse

try:
    from scrapy.http import Response
    from scrapy.exceptions import NotConfigured
except ImportError as exc:  # pragma: no cover - exercised only without scrapy installed
    raise ImportError(
        "nextflight.scrapy_middleware requires Scrapy. Install it with "
        "`pip install scrapy` or `pip install nextflight[scrapy]`."
    ) from exc

from scrapy import signals as _scrapy_signals

try:
    from scrapy.exceptions import StopDownload
    _STREAMING_AVAILABLE = True
except ImportError:  # pragma: no cover - Scrapy < 2.6 lacks bytes_received/StopDownload
    _STREAMING_AVAILABLE = False

try:
    from scrapy.downloadermiddlewares.retry import get_retry_request
    _RETRY_AVAILABLE = True
except ImportError:  # pragma: no cover - Scrapy < 2.5 lacks get_retry_request
    _RETRY_AVAILABLE = False

from .extractor import FlightExtractor

logger = logging.getLogger(__name__)

_ATTR = "_nextflight_cache"
_UNSET = object()

# Cache for responses where the primary path -- stashing the extractor
# directly on the response via `object.__setattr__` -- doesn't work. This
# is NOT a rare edge case: Scrapy's own `Response` (and `HtmlResponse`,
# `TextResponse`, ...) define `__slots__` and reject arbitrary new
# instance attributes on *every* normal response, so this fallback is
# actually the path real Scrapy crawls take essentially all the time --
# the "primary" path above mainly covers custom `Response` subclasses
# a project defines without `__slots__`, or future Scrapy versions that
# might not slot-restrict attributes the same way.
#
# Keyed by the response object itself via a *weak*-keyed mapping, not by
# `id(response)`: a plain `{id(response): extractor}` dict was the
# original implementation here, and it had two real bugs that would have
# hit on every single response given how common this path turns out to
# be -- (1) entries were never removed, so memory grows unbounded for the process lifetime, and (2) once a response was
# garbage collected, Python is free to reuse its `id()` for a completely
# unrelated later object, which would then silently receive some other
# page's cached `FlightExtractor` -- a correctness bug, not just a leak.
# A `WeakKeyDictionary` keyed on the response itself sidesteps both:
# entries are dropped automatically the moment the response is collected
# (verified in tests/test_scrapy_middleware.py), and the key is real
# object identity, so there's no id-reuse hazard. This dict is shared
# module state accessed from Twisted's (single-threaded, cooperatively
# multitasked) reactor thread; concurrent *requests* are safe here the
# same way any other single-threaded-event-loop shared state is --
# there's no pre-emption between the get/set below, only between
# `Deferred` callbacks.
_fallback_cache: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()


def _flight_property(self: Response, strict: bool = False, repair: bool = False) -> FlightExtractor:
    """Lazily build (and cache) a `FlightExtractor` for `self`. A
    response with no Flight data on it at all (redirected error page,
    non-HTML response, empty body, ...) still returns a valid extractor
    here -- just one with zero chunks -- rather than raising, unless
    `strict=True` turns a genuinely malformed *row* (not merely an
    absent one) into `FlightParseError`, same as passing the response to
    `FlightExtractor` directly would."""
    cached = getattr(self, _ATTR, None)
    if cached is not None:
        return cached
    try:
        cached = _fallback_cache.get(self)
    except TypeError:
        # `self` isn't weakly referenceable at all (e.g. a `Response`
        # subclass using `__slots__` without `__weakref__` -- note this
        # is about `__weakref__` specifically, not `__slots__` in
        # general: Scrapy's own `Response` uses `__slots__` but does
        # include `__weakref__`, which is why the common case above
        # works fine) -- there is no safe place to cache it, so every
        # access recomputes. Slower for that (genuinely rare) case, but
        # never leaks and never risks an identity collision.
        return FlightExtractor(self, strict=strict, repair=repair)
    if cached is not None:
        return cached
    cached = FlightExtractor(self, strict=strict, repair=repair)
    _install_cached_extractor(self, cached)
    return cached


def _install_cached_extractor(response: Response, extractor: FlightExtractor) -> None:
    """Store `extractor` wherever `_flight_property` will find it again
    for this exact response: as a real instance attribute if the
    response allows it, falling back to `_fallback_cache` (see above)
    otherwise. Shared by `_flight_property` itself and by
    `FlightStreamingMiddleware`, which pre-warms this cache from a
    response's bytes as they arrive rather than waiting to be asked."""
    try:
        object.__setattr__(response, _ATTR, extractor)
    except AttributeError:
        try:
            _fallback_cache[response] = extractor
        except TypeError:
            pass  # not weakly referenceable either -- nowhere safe to cache; give up silently


class FlightMiddleware:
    """Downloader middleware that installs a lazy `.flight` property on
    every `Response` object passed through it, so spiders never need to
    call `nextflight.extract(response)` explicitly.

    Installing this as a class-level property on `scrapy.http.Response`
    (rather than setting an instance attribute per response in
    `process_response`) means it also works for `Response` subclasses
    users construct themselves (e.g. in tests, or via
    `response.follow()`/`response.replace()`), not just the exact
    instances this middleware happens to see.

    Reads three optional settings, so per-spider behavior doesn't need to
    be repeated in every callback: ``NEXTFLIGHT_STRICT``,
    ``NEXTFLIGHT_REPAIR`` (both booleans, default `False`, forwarded to
    every `FlightExtractor` this middleware constructs), and
    ``NEXTFLIGHT_DEDUPE`` (default `False`) -- dedupe is a per-call
    `find_all_by_keys` argument rather than a page-parsing setting, so
    this middleware doesn't use it directly; it's exposed as
    `self.dedupe_default` so `FlightItemPipeline` and
    `NextflightSpiderMixin` can default to it instead of every call site
    repeating `dedupe=True`.

    The property is (re)installed on every `FlightMiddleware()`
    construction -- not guarded behind `hasattr(Response, "flight")` --
    so that if settings differ between two middleware instances in the
    same process (e.g. across tests), the most recently constructed one
    wins, rather than silently keeping whichever settings happened to be
    used first."""

    def __init__(self, strict: bool = False, repair: bool = False, dedupe_default: bool = False) -> None:
        self.strict = strict
        self.repair = repair
        self.dedupe_default = dedupe_default
        # Per-domain cache for `next_version_hint()`: within one crawl,
        # every page of the same Next.js build has the same answer (the
        # wire-format markers it's fingerprinted against don't vary
        # page-to-page on a given deployment), so recomputing it -- a
        # handful of substring scans over the *entire* page HTML each
        # time -- for every single response of a large crawl is pure
        # waste. Computed lazily, once per domain, on first request via
        # `version_hint_for_response`.
        self._version_hint_cache: dict = {}
        Response.flight = property(  # type: ignore[attr-defined]
            lambda resp: _flight_property(resp, strict, repair)
        )

    @classmethod
    def from_crawler(cls, crawler: Any) -> "FlightMiddleware":
        settings = crawler.settings
        return cls(
            strict=settings.getbool("NEXTFLIGHT_STRICT", False),
            repair=settings.getbool("NEXTFLIGHT_REPAIR", False),
            dedupe_default=settings.getbool("NEXTFLIGHT_DEDUPE", False),
        )

    def version_hint_for_response(self, response: Response) -> dict:
        """`next_version_hint()` for `response`'s domain, computed once
        per domain and reused for every subsequent response to that same
        domain in this crawl, rather than rescanning each page's full
        HTML for wire-format markers every single time."""
        domain = _domain_of(response.url)
        cached = self._version_hint_cache.get(domain)
        if cached is None:
            cached = _flight_property(response, self.strict, self.repair).next_version_hint()
            self._version_hint_cache[domain] = cached
        return cached

    def process_response(self, request: Any, response: Response, spider: Any) -> Response:
        # The `.flight` property (installed in __init__, at the class
        # level) already covers every response; nothing per-request
        # needs to happen here. This hook exists so the middleware has
        # somewhere valid to live in the downloader middleware chain and
        # so Scrapy's normal enable/disable/ordering machinery applies to
        # it like any other middleware.
        return response


class NextflightStatsExtension:
    """Scrapy `Extension` that surfaces nextflight-level metrics in the
    crawl's own stats collector (`crawler.stats`) -- visible in the
    normal end-of-crawl stats dump Scrapy prints, and in whatever
    stats-export pipeline a project already has (Prometheus, a stats
    database, etc.), rather than needing separate, manual
    instrumentation to notice things like "most pages barely parsed
    cleanly" or "half the crawl looks like an unrecognized Next.js
    version" while a crawl is running or after the fact.

    Enable via::

        EXTENSIONS = {"nextflight.scrapy_middleware.NextflightStatsExtension": 543}

    Tracked stats, all under the ``nextflight/`` prefix:

    - ``nextflight/responses_with_flight_data`` -- count of responses
      where `.flight` found at least one chunk.
    - ``nextflight/responses_without_flight_data`` -- count where it
      didn't (redirected error pages, non-Next.js sites, non-HTML
      responses, or a genuinely empty page).
    - ``nextflight/avg_parse_confidence`` -- running average of
      `.parse_confidence()["score"]` across responses with flight data.
      A dropping average partway through a crawl is an early signal that
      either the site's wire format shifted (see `next_version_hint()`)
      or that responses are increasingly hitting a challenge/block page
      instead of the real one.
    - ``nextflight/version_hint/<range>`` -- count of responses matching
      each known Next.js version range from `known_formats.yaml`, plus
      ``nextflight/version_hint/unknown`` for pages matching no known
      range at all.
    - ``nextflight/stream/stopped_early`` -- count of responses where
      `FlightStreamingMiddleware` cancelled the download early (only
      ever incremented if that middleware is also enabled).

    Requires `FlightMiddleware` (or some other way of populating
    `response.flight`) to also be enabled -- this extension reads
    `response.flight` itself via the `response_received` signal, it
    doesn't install the property."""

    def __init__(self, crawler: Any) -> None:
        self.crawler = crawler
        self.stats = crawler.stats
        self._confidence_count = 0
        self._confidence_sum = 0.0
        crawler.signals.connect(self.response_received, signal=_scrapy_signals.response_received)

    @classmethod
    def from_crawler(cls, crawler: Any) -> "NextflightStatsExtension":
        return cls(crawler)

    def response_received(self, response: Response, request: Any, spider: Any) -> None:
        flight = _flight_property(response)
        if not flight.keys():
            self.stats.inc_value("nextflight/responses_without_flight_data")
            return
        self.stats.inc_value("nextflight/responses_with_flight_data")

        confidence = flight.parse_confidence()["score"]
        self._confidence_count += 1
        self._confidence_sum += confidence
        self.stats.set_value(
            "nextflight/avg_parse_confidence",
            round(self._confidence_sum / self._confidence_count, 4),
        )

        hint = flight.next_version_hint()
        self.stats.inc_value(f"nextflight/version_hint/{hint['range'] or 'unknown'}")

        if response.meta.get("nextflight_stopped_early"):
            self.stats.inc_value("nextflight/stream/stopped_early")


class _StreamBuffer:
    """Accumulates a single response's bytes as they arrive (from
    Scrapy's `bytes_received` signal) into decoded text, so
    `FlightStreamingMiddleware` can cheaply check whether a match has
    appeared yet without waiting for the full download.

    Decodes with `codecs.getincrementaldecoder` rather than re-decoding
    the whole accumulated byte buffer on every call, so a multi-byte
    UTF-8 character split across two `bytes_received` calls doesn't
    raise or corrupt the text -- the incremental decoder holds onto a
    dangling partial byte sequence internally until the rest arrives.

    Assumes UTF-8 by default (configurable via `NEXTFLIGHT_STREAM_ENCODING`)
    since that's virtually universal for Next.js's own payload content;
    `errors="replace"` means a wrong encoding assumption degrades to a
    few mangled characters rather than raising, but note this means
    match-checking against a non-UTF-8 page's *early* bytes could
    theoretically miss or misfire on the garbled portion -- an accepted
    tradeoff for an opt-in performance feature, not a default-on one."""

    def __init__(self, encoding: str = "utf-8") -> None:
        self._decoder = codecs.getincrementaldecoder(encoding)(errors="replace")
        self.text = ""
        self.total_bytes = 0
        self.capped = False
        self._checked_at = 0

    def feed(self, data: bytes) -> None:
        if self.capped:
            return
        self.total_bytes += len(data)
        self.text += self._decoder.decode(data)

    def cap(self) -> None:
        """Stop growing the buffer (bounds peak memory for a large page
        that never matches) -- `text` from this point on no longer
        reflects the full response, so callers must not treat it as
        authoritative once capped unless an early-stop match already
        happened before the cap was hit."""
        self.capped = True

    def ready_for_check(self, min_interval: int) -> bool:
        if self.total_bytes - self._checked_at < min_interval:
            return False
        self._checked_at = self.total_bytes
        return True

    def find_by_keys(self, keys: Iterable[str], *, repair: bool = True) -> Any:
        if not self.text:
            return None
        return FlightExtractor(self.text, repair=repair).find_by_keys(keys)

    def to_extractor(self, *, repair: bool = True) -> Optional[FlightExtractor]:
        return FlightExtractor(self.text, repair=repair) if self.text else None


class FlightStreamingMiddleware:
    """Processes a response's bytes AS THEY ARRIVE, via Scrapy's
    `bytes_received` signal, instead of waiting for the full download to
    finish -- two distinct benefits, both opt-in:

    1. **Early stop ("verify faster")**: set `NEXTFLIGHT_STREAM_KEYS` to
       the keys you're looking for, and the moment a matching dict shows
       up in the bytes received so far, the download is cancelled via
       Scrapy's `StopDownload` -- for a 3MB page where the price you want
       is in the first 50KB, this means not downloading the other
       2.95MB at all. The match is available immediately as
       `response.meta["nextflight_match"]`, and `response.meta["nextflight_stopped_early"]`
       is `True` so a spider can tell the response body is intentionally
       truncated.
    2. **Overlap parsing with the download**: even without
       `NEXTFLIGHT_STREAM_KEYS` set, bytes are decoded incrementally as
       they arrive rather than all at once after the download completes,
       and `response.flight` is pre-warmed from that -- so parsing work
       happens concurrently with network I/O instead of strictly after
       it. This second benefit is real but modest (it overlaps the
       decode/row-split step with I/O wait time; it does not change how
       much data has to come over the wire) -- benefit 1 is the
       standout win when it applies.

    Enable in settings.py::

        DOWNLOADER_MIDDLEWARES = {
            "nextflight.scrapy_middleware.FlightMiddleware": 543,
            "nextflight.scrapy_middleware.FlightStreamingMiddleware": 544,
        }
        NEXTFLIGHT_STREAMING = True
        NEXTFLIGHT_STREAM_KEYS = ["price", "title"]   # optional -- omit for benefit 2 only
        NEXTFLIGHT_STREAM_MAX_BYTES = 10_000_000       # optional, default shown
        NEXTFLIGHT_STREAM_CHECK_INTERVAL = 8192        # optional, default shown
        NEXTFLIGHT_STREAM_ENCODING = "utf-8"           # optional, default shown

    `NEXTFLIGHT_STREAM_KEYS` can also be overridden per request, for a
    spider crawling several URL patterns that each need different fields
    watched for, via `request.meta["nextflight_stream_keys"]` -- set it
    to a different key list for that one request, or to `False`/`None`
    to opt that request out of early-stop entirely even when a global
    default is configured::

        yield scrapy.Request(url, meta={"nextflight_stream_keys": ["sku"]})
        yield scrapy.Request(url, meta={"nextflight_stream_keys": False})  # opt out

    Off by default (`NEXTFLIGHT_STREAMING` must be explicitly `True`):
    watching every byte as it arrives has some overhead, isn't needed
    for typical crawl sizes, and truncating downloads via `StopDownload`
    is a meaningful behavior change a project should opt into
    deliberately rather than get implicitly from installing
    `FlightMiddleware` alone. Requires Scrapy >= 2.6 (for
    `scrapy.exceptions.StopDownload`); on older Scrapy, `from_crawler`
    raises `NotConfigured` and Scrapy disables this middleware the same
    way it does for any other middleware whose dependencies aren't met.

    A response that never matches (or that isn't a Next.js page at all)
    downloads completely as normal -- this middleware only ever cuts a
    download short on an explicit, positive match against
    `NEXTFLIGHT_STREAM_KEYS`, never on a timeout or heuristic."""

    def __init__(self, crawler: Any, *, stream_keys: Optional[Iterable[str]] = None,
                 max_bytes: int = 10_000_000, min_check_interval: int = 8192,
                 repair: bool = True, encoding: str = "utf-8") -> None:
        self.crawler = crawler
        self.stream_keys = set(stream_keys) if stream_keys else None
        self.max_bytes = max_bytes
        self.min_check_interval = min_check_interval
        self.repair = repair
        self.encoding = encoding
        # Keyed by Request (a distinct object per download attempt,
        # including per redirect hop) via a *weak*-keyed mapping for the
        # same reason `_fallback_cache` is one -- see that comment above.
        self._buffers: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()
        self._matches: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()
        crawler.signals.connect(self.bytes_received, signal=_scrapy_signals.bytes_received)

    @classmethod
    def from_crawler(cls, crawler: Any) -> "FlightStreamingMiddleware":
        if not _STREAMING_AVAILABLE:
            raise NotConfigured(
                "FlightStreamingMiddleware requires Scrapy >= 2.6 "
                "(scrapy.exceptions.StopDownload / bytes_received signal)."
            )
        settings = crawler.settings
        if not settings.getbool("NEXTFLIGHT_STREAMING", False):
            raise NotConfigured("NEXTFLIGHT_STREAMING is not enabled")
        return cls(
            crawler,
            stream_keys=settings.getlist("NEXTFLIGHT_STREAM_KEYS", None) or None,
            max_bytes=settings.getint("NEXTFLIGHT_STREAM_MAX_BYTES", 10_000_000),
            min_check_interval=settings.getint("NEXTFLIGHT_STREAM_CHECK_INTERVAL", 8192),
            repair=settings.getbool("NEXTFLIGHT_REPAIR", True),
            encoding=settings.get("NEXTFLIGHT_STREAM_ENCODING", "utf-8"),
        )

    def bytes_received(self, data: bytes, request: Any, spider: Any) -> None:
        buf = self._buffers.get(request)
        if buf is None:
            buf = _StreamBuffer(encoding=self.encoding)
            self._buffers[request] = buf
        if buf.capped:
            return
        buf.feed(data)
        if buf.total_bytes >= self.max_bytes:
            buf.cap()
            return
        keys = self._stream_keys_for(request)
        if not keys or not buf.ready_for_check(self.min_check_interval):
            return
        match = buf.find_by_keys(keys, repair=self.repair)
        if match is not None:
            self._matches[request] = match
            raise StopDownload(fail=False)

    def _stream_keys_for(self, request: Any) -> Optional[set]:
        """Per-request override of `NEXTFLIGHT_STREAM_KEYS`, via
        `request.meta["nextflight_stream_keys"]` -- lets a spider crawling
        several URL patterns in one project watch for different fields on
        each, or set `False` to opt a specific request out of early-stop
        entirely even when a global default is configured."""
        override = request.meta.get("nextflight_stream_keys", _UNSET)
        if override is _UNSET:
            return self.stream_keys
        if not override:
            return None
        return set(override)

    def process_response(self, request: Any, response: Response, spider: Any) -> Response:
        buf = self._buffers.pop(request, None)
        match = self._matches.pop(request, None)
        if match is not None:
            # Early-stop happened: response.body IS genuinely truncated
            # to what we buffered, so our buffer is authoritative for
            # this response -- pre-warm .flight from it rather than
            # letting it lazily re-decode the (identical) truncated body
            # later.
            if buf is not None and buf.text:
                extractor = buf.to_extractor(repair=self.repair)
                if extractor is not None:
                    _install_cached_extractor(response, extractor)
            response.meta["nextflight_match"] = match
            response.meta["nextflight_stopped_early"] = True
        elif buf is not None and not buf.capped and buf.text:
            # Full response streamed in without ever matching or hitting
            # the memory cap -- our buffer reflects the complete
            # response, so pre-warm .flight from it instead of a
            # redundant re-decode of response.text from scratch.
            extractor = buf.to_extractor(repair=self.repair)
            if extractor is not None:
                _install_cached_extractor(response, extractor)
        # else: buffer was capped before completion with no match found
        # -- .flight falls through to its normal lazy path over the
        # full response.text Scrapy assembled itself, which IS complete
        # even though our own tracking buffer isn't.
        return response


class FlightRSCMiddleware:
    """Downloader middleware that transparently rewrites eligible
    requests to fetch a Next.js page's lightweight RSC payload (sending
    an `RSC: 1` header, the same way Next.js's own client-side
    navigation does) instead of full HTML -- often a small fraction of
    the size of the complete page, since it skips the surrounding HTML
    document, inline `<style>`/`<script>` tags, and anything else not
    part of the Flight data itself.

    Opt in per request (recommended -- see the caveat below)::

        yield scrapy.Request(url, meta={"nextflight_rsc": True})

    Or match a URL pattern crawl-wide via settings::

        DOWNLOADER_MIDDLEWARES = {"nextflight.scrapy_middleware.FlightRSCMiddleware": 542}
        NEXTFLIGHT_RSC_URL_PATTERN = r"/product/"

    `response.flight` works unchanged on the resulting response --
    `FlightExtractor`/`extract()` already auto-detects a raw RSC payload
    (no HTML wrapper at all) the same way it detects the
    HTML-embedded form, so nothing else about how you read the response
    needs to change.

    **Important caveat**: the response for a rewritten request is the
    *raw RSC stream*, not a full HTML page -- `response.css`/`.xpath`
    and Scrapy's own link extraction will find nothing useful on it,
    since there's no HTML document to select against. Use this only for
    "leaf" pages you extract data from and don't also need to crawl
    outgoing links from (typical for a product/listing detail page);
    for an index/listing page you still need to follow links from,
    fetch it normally instead.

    Some Next.js deployments additionally require a build-specific
    `_rsc=<id>` query parameter or a `Next-Router-State-Tree` header to
    route an RSC request correctly, neither of which this middleware
    guesses at (a wrong guess would silently return the wrong data
    rather than erroring, which is worse than not attempting it at all).
    If a target site 404s or returns unexpected data with just the
    `RSC`/`Next-Url` headers this middleware adds, grab the additional
    header(s) once from a real browser's network tab and set them
    yourself via `meta={"nextflight_rsc": True}` plus your own
    `headers=`/query param on the `scrapy.Request` -- or use
    `FlightExtractor.from_rsc_url(url, auto_discover=True)` directly for
    that specific site instead of this middleware, since it already
    implements that discovery step (see the README's "Raw RSC fetches"
    section)."""

    def __init__(self, url_pattern: Optional[str] = None) -> None:
        self.url_pattern = re.compile(url_pattern) if url_pattern else None

    @classmethod
    def from_crawler(cls, crawler: Any) -> "FlightRSCMiddleware":
        return cls(url_pattern=crawler.settings.get("NEXTFLIGHT_RSC_URL_PATTERN"))

    def process_request(self, request: Any, spider: Any) -> None:
        if not self._eligible(request):
            return None
        request.headers.setdefault(b"RSC", b"1")
        request.headers.setdefault(b"Next-Url", urlparse(request.url).path.encode("utf-8") or b"/")
        request.meta["nextflight_rsc_request"] = True
        return None

    def _eligible(self, request: Any) -> bool:
        flag = request.meta.get("nextflight_rsc")
        if flag is False:
            return False
        if flag is True:
            return True
        return bool(self.url_pattern and self.url_pattern.search(request.url))


class FlightRetryMiddleware:
    """Downloader middleware that retries a response whose Flight data
    parsed with suspiciously low confidence (see
    `FlightExtractor.parse_confidence()`) -- a strong signal the page
    actually received was a challenge/interstitial page, a
    partially-rendered error page, or a response mangled by a flaky
    proxy, even though the HTTP status code itself looked like a normal
    200 (so Scrapy's own status-code-based `RetryMiddleware` wouldn't
    have caught it).

    Enable via::

        DOWNLOADER_MIDDLEWARES = {
            "nextflight.scrapy_middleware.FlightMiddleware": 543,
            "nextflight.scrapy_middleware.FlightRetryMiddleware": 550,
        }
        NEXTFLIGHT_RETRY_MIN_CONFIDENCE = 0.5   # below this, retry
        NEXTFLIGHT_RETRY_MAX_TIMES = 2          # optional -- Scrapy's own default otherwise

    Uses Scrapy's own `get_retry_request` helper -- the same retry-count
    bookkeeping, backoff, and `stats` integration as the built-in
    `RetryMiddleware` -- rather than reimplementing retry logic; this
    middleware only adds the *trigger* (low parse confidence), not a
    separate retry mechanism. Requires Scrapy >= 2.5 (for
    `get_retry_request`); disables itself via `NotConfigured` on older
    Scrapy, same as `FlightStreamingMiddleware` does for its own
    version requirement.

    A response with ZERO chunks (no Flight data on it at all -- a
    non-Next.js page, or a completely blank error page) is deliberately
    **not** retried by this middleware: `parse_confidence()` returns
    `1.0` for zero chunks (there's nothing to have parsed badly), so a
    genuinely non-Next.js page won't loop here. If a project also wants
    to retry on an *empty* response, that's a distinct signal from a
    *malformed* one and is better handled via `response.flight.keys()`
    in the spider's own callback, or Scrapy's ordinary HTTP-status-based
    retry configuration."""

    def __init__(self, min_confidence: float = 0.5, max_retry_times: Optional[int] = None) -> None:
        if not _RETRY_AVAILABLE:
            raise NotConfigured(
                "FlightRetryMiddleware requires Scrapy >= 2.5 "
                "(scrapy.downloadermiddlewares.retry.get_retry_request)."
            )
        self.min_confidence = min_confidence
        self.max_retry_times = max_retry_times

    @classmethod
    def from_crawler(cls, crawler: Any) -> "FlightRetryMiddleware":
        settings = crawler.settings
        return cls(
            min_confidence=settings.getfloat("NEXTFLIGHT_RETRY_MIN_CONFIDENCE", 0.5),
            max_retry_times=settings.getint("NEXTFLIGHT_RETRY_MAX_TIMES", 0) or None,
        )

    def process_response(self, request: Any, response: Response, spider: Any) -> Any:
        flight = _flight_property(response)
        if not flight.keys():
            return response  # nothing parsed at all -- see docstring
        confidence = flight.parse_confidence()["score"]
        if confidence >= self.min_confidence:
            return response
        reason = f"low nextflight parse confidence ({confidence:.2f} < {self.min_confidence})"
        retry_request = get_retry_request(
            request, spider=spider, reason=reason, max_retry_times=self.max_retry_times,
        )
        return retry_request if retry_request is not None else response


class FlightDedupeMiddleware:
    """Spider middleware that deduplicates dict items across the WHOLE
    crawl, not just within one page -- `find_all_by_keys(dedupe=True)`
    only catches the same object serialized twice on the *same* page
    (e.g. a "related items" carousel repeating something from the main
    grid); it can't catch the same listing appearing again on a
    different page later in the crawl (a paginated index plus a
    "related items" widget showing up on many separate detail pages, for
    example). This middleware catches that broader case by remembering
    every dict-shaped item's fingerprint for the lifetime of the crawl.

    Enable via::

        SPIDER_MIDDLEWARES = {"nextflight.scrapy_middleware.FlightDedupeMiddleware": 543}
        NEXTFLIGHT_DEDUPE_ACROSS_PAGES = True
        NEXTFLIGHT_DEDUPE_KEY = "id"   # optional -- fingerprint just this field instead of the whole item

    Only dict-shaped items are considered for deduplication -- `Request`
    objects and Scrapy `Item`/dataclass instances a spider yields for
    further crawling or as typed results pass through completely
    untouched, so this never accidentally drops a page still queued to
    be crawled.

    `NEXTFLIGHT_DEDUPE_KEY` fingerprints just that one field (e.g. a
    stable listing id) instead of the whole item -- much cheaper for
    large items, and correctly treats two crawls of the same listing
    with an incidentally-changed field (a "last seen" timestamp, say) as
    the same item rather than as two different ones."""

    def __init__(self, dedupe_key: Optional[str] = None) -> None:
        self.dedupe_key = dedupe_key
        self._seen: set = set()

    @classmethod
    def from_crawler(cls, crawler: Any) -> "FlightDedupeMiddleware":
        settings = crawler.settings
        if not settings.getbool("NEXTFLIGHT_DEDUPE_ACROSS_PAGES", False):
            raise NotConfigured("NEXTFLIGHT_DEDUPE_ACROSS_PAGES is not enabled")
        return cls(dedupe_key=settings.get("NEXTFLIGHT_DEDUPE_KEY"))

    def process_spider_output(self, response: Any, result: Any, spider: Any) -> Any:
        for item in result:
            if isinstance(item, dict):
                fingerprint = self._fingerprint(item)
                if fingerprint in self._seen:
                    continue
                self._seen.add(fingerprint)
            yield item

    def _fingerprint(self, item: dict) -> Any:
        if self.dedupe_key is not None:
            return (self.dedupe_key, item.get(self.dedupe_key))
        from .extractor import _json_dumps_sorted
        try:
            return _json_dumps_sorted(item)
        except TypeError:
            return repr(item)


def recommended_settings(*, streaming: bool = False, rsc: bool = False,
                          dedupe_across_pages: bool = False, retry: bool = False,
                          stats: bool = False) -> dict:
    """Returns a settings dict implementing this project's recommended
    `nextflight` + Scrapy wiring, layered onto whatever a project already
    has -- update `settings.py` with it rather than replacing it::

        # settings.py
        from nextflight.scrapy_middleware import recommended_settings
        for key, value in recommended_settings(streaming=True, stats=True).items():
            if isinstance(value, dict) and isinstance(globals().get(key), dict):
                globals()[key].update(value)
            else:
                globals()[key] = value

    The baseline (no flags) enables just `FlightMiddleware` with
    `NEXTFLIGHT_REPAIR = True` -- a reasonable default for a real crawl
    against production sites, where a response truncated by a flaky
    proxy is far more likely than in local testing. Add `streaming=True`
    for large pages / bandwidth-sensitive crawls, `rsc=True` for
    leaf/detail pages you don't need to crawl outgoing links from,
    `dedupe_across_pages=True` for a crawl where the same listing can
    legitimately appear on more than one page, `retry=True` to
    automatically retry responses whose Flight data parsed with
    suspiciously low confidence (often a challenge/interstitial page
    masquerading as a normal 200), and `stats=True` to surface
    nextflight-level metrics (parse confidence, version-hint spread,
    early-stop counts) in Scrapy's own end-of-crawl stats dump. See
    `docs/scrapy.md`'s "Which approach should I use?" section for when
    each is worth turning on."""
    downloader_middlewares = {"nextflight.scrapy_middleware.FlightMiddleware": 543}
    settings: dict = {
        "DOWNLOADER_MIDDLEWARES": downloader_middlewares,
        "NEXTFLIGHT_REPAIR": True,
    }
    if streaming:
        downloader_middlewares["nextflight.scrapy_middleware.FlightStreamingMiddleware"] = 544
        settings["NEXTFLIGHT_STREAMING"] = True
    if rsc:
        downloader_middlewares["nextflight.scrapy_middleware.FlightRSCMiddleware"] = 542
    if retry:
        downloader_middlewares["nextflight.scrapy_middleware.FlightRetryMiddleware"] = 550
    if dedupe_across_pages:
        settings["SPIDER_MIDDLEWARES"] = {"nextflight.scrapy_middleware.FlightDedupeMiddleware": 543}
        settings["NEXTFLIGHT_DEDUPE_ACROSS_PAGES"] = True
    if stats:
        settings["EXTENSIONS"] = {"nextflight.scrapy_middleware.NextflightStatsExtension": 543}
    return settings


class FlightItemPipeline:
    """Optional item pipeline: declare a required key set once (via
    ``NEXTFLIGHT_PIPELINE_KEYS`` in settings.py, or by constructing with
    `required_keys=`), then call `matches_for_response(response)` from a
    spider to get every matching dict on that page without repeating
    `find_all_by_keys(...)` boilerplate in each callback::

        # settings.py
        ITEM_PIPELINES = {"nextflight.scrapy_middleware.FlightItemPipeline": 300}
        NEXTFLIGHT_PIPELINE_KEYS = ["price", "title"]

        # spider.py
        def parse(self, response):
            pipeline = self.crawler.engine.scraper.itemproc.middlewares[...]  # see docs/scrapy.md
            # or, simpler -- construct one directly from settings:
            pipeline = FlightItemPipeline.from_crawler(self.crawler)
            for item in pipeline.matches_for_response(response):
                yield item

    This intentionally does not try to intercept every yielded item and
    silently attach unrelated Flight matches to it -- that kind of
    implicit, action-at-a-distance item mutation is exactly the kind of
    surprising pipeline behavior Scrapy projects tend to regret later.
    `process_item` here is a pass-through; the actual value is
    `matches_for_response`, a plain, explicit method a spider calls when
    (and only when) it wants Flight-derived items."""

    def __init__(self, required_keys: Optional[Iterable[str]] = None, dedupe: bool = False) -> None:
        self.required_keys = list(required_keys) if required_keys else None
        self.dedupe = dedupe

    @classmethod
    def from_crawler(cls, crawler: Any) -> "FlightItemPipeline":
        settings = crawler.settings
        keys = settings.getlist("NEXTFLIGHT_PIPELINE_KEYS", [])
        return cls(
            required_keys=keys or None,
            dedupe=settings.getbool("NEXTFLIGHT_DEDUPE", False),
        )

    def process_item(self, item: Any, spider: Any) -> Any:
        # Pass-through: this pipeline never drops or mutates a spider's
        # own item. See `matches_for_response` for the actual feature.
        return item

    def matches_for_response(self, response: Response) -> list:
        """Every dict on `response` matching this pipeline's configured
        `required_keys`, using whatever `.flight` extractor is already
        cached on the response (built via `FlightMiddleware` if it's
        enabled; a fresh, uncached one otherwise)."""
        if not self.required_keys:
            return []
        flight = _flight_property(response)
        return flight.find_all_by_keys(self.required_keys, dedupe=self.dedupe)


class NextflightSpiderMixin:
    """Mix into a Scrapy `Spider` subclass for a couple of small
    conveniences beyond what `FlightMiddleware` alone provides::

        class MySpider(NextflightSpiderMixin, scrapy.Spider):
            name = "my_spider"

            def parse(self, response):
                page = self.flight(response)
                self.log_version_hint(response)
                ...

    `self.flight(response)` is equivalent to `response.flight` when
    `FlightMiddleware` is enabled, but also works (falling back to a
    fresh, uncached `FlightExtractor`) if the middleware isn't installed
    -- handy for spiders shared across projects that may or may not have
    it configured.

    `self.log_version_hint(response)` logs `next_version_hint()` once per
    domain (not once per response -- that would be far too noisy on a
    real crawl) via the spider's own logger, so a wire-format drift on a
    site under active crawl shows up in Scrapy's normal log output
    instead of requiring separate, manual debugging.

    `self.follow_flight_urls(response, ...)` follows URLs found in the
    page's Flight JSON itself -- not just rendered HTML links -- for
    sites where navigation happens via `onClick`/client-side routing
    rather than real `<a href>` tags. See `FlightExtractor.find_urls()`
    for the underlying extraction and why this is sometimes necessary."""

    def flight(self, response: Response) -> FlightExtractor:
        return _flight_property(response)

    def log_version_hint(self, response: Response) -> None:
        if not hasattr(self, "_nextflight_logged_domains"):
            self._nextflight_logged_domains: set = set()
        domain = _domain_of(response.url)
        if domain in self._nextflight_logged_domains:
            return
        self._nextflight_logged_domains.add(domain)
        hint = self.flight(response).next_version_hint()
        log = getattr(self, "logger", logger)
        if hint["range"] is None:
            log.info("nextflight: no known wire-format markers matched for %s", domain)
        else:
            log.info("nextflight: %s looks like Next.js %s (%s)", domain, hint["range"], hint["notes"])

    def follow_flight_urls(self, response: Response, *, keys: Optional[Iterable[str]] = None,
                            pattern: Optional[Any] = None, callback: Optional[Any] = None,
                            **kwargs: Any) -> Iterable[Any]:
        """Yield `response.follow(url, callback=callback, **kwargs)` for
        every URL found via `self.flight(response).find_urls(keys=keys,
        pattern=pattern)` -- the Flight-JSON equivalent of Scrapy's own
        `response.follow_all()`, for navigation that never rendered as a
        real `<a href>` (see `FlightExtractor.find_urls()`'s docstring
        for when that happens). Relative URLs are resolved against
        `response.url` the same way `response.follow()` always does, so
        callers don't need to call `response.urljoin()` themselves.

            def parse(self, response):
                yield from self.follow_flight_urls(
                    response, keys={"href"}, callback=self.parse_detail,
                )

        Restrict with `keys`/`pattern` the same way `find_urls()` does --
        omitting both scans every URL-shaped string on the page, which
        is easy to over-match (image URLs, canonical tags, external
        links) on a real site; passing at least one is recommended for
        anything beyond quick exploration."""
        urls = self.flight(response).find_urls(keys=keys, pattern=pattern)
        for url in urls:
            yield response.follow(url, callback=callback, **kwargs)


def _domain_of(url: str) -> str:
    from urllib.parse import urlparse
    return urlparse(url).netloc
