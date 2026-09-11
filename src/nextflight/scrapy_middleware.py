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

import logging
import weakref
from typing import Any, Iterable, Optional

try:
    from scrapy.http import Response
except ImportError as exc:  # pragma: no cover - exercised only without scrapy installed
    raise ImportError(
        "nextflight.scrapy_middleware requires Scrapy. Install it with "
        "`pip install scrapy` or `pip install nextflight[scrapy]`."
    ) from exc

from .extractor import FlightExtractor

logger = logging.getLogger(__name__)

_ATTR = "_nextflight_cache"

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
    try:
        object.__setattr__(self, _ATTR, cached)
    except AttributeError:
        _fallback_cache[self] = cached
    return cached


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
    instead of requiring separate, manual debugging."""

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


def _domain_of(url: str) -> str:
    from urllib.parse import urlparse
    return urlparse(url).netloc
