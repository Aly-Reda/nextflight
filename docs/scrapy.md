# nextflight + Scrapy

## Why this beats hand-written XPath/regex for Next.js sites

A Next.js App Router page's real data almost never lives in clean,
selectable HTML -- it's serialized into `self.__next_f.push([...])`
script blocks, chunked and cross-referenced with `$`-sigils. That leaves
two common approaches, both fragile in ways `nextflight` isn't:

- **XPath/CSS selectors against rendered markup** -- works only for
  whatever ends up in visible HTML, which is often less than what the
  page actually has data for (metadata, related items, pricing history)
  and breaks the moment a redesign changes class names or DOM structure.
- **Regex against the raw `<script>` text** -- technically gets at the
  Flight data, but has to reinvent row-boundary detection, `$`-ref
  resolution, and truncation handling from scratch, and typically ends
  up hardcoding brittle array-index paths that break on the next
  redeploy's component-tree reshuffle.

`nextflight` parses the actual wire format and lets you search by
*shape* (`find_by_keys({"price", "title"})`) instead of by fragile
position -- and with `FlightMiddleware`, it's no more Scrapy boilerplate
than reading `response.text` already is.

## Which approach should I use?

`nextflight` ships several distinct ways to integrate with Scrapy, each
solving a different problem. **Recommended baseline for any project:
`FlightMiddleware` + `NextflightSpiderMixin`.** Layer the others on only
when you actually hit the specific problem each one solves -- none of
them are free, and stacking on ones you don't need just adds
configuration surface for no benefit.

**A note on independence**: `NextflightStatsExtension`,
`FlightRetryMiddleware`, and `FlightStreamingMiddleware` all work
correctly even if `FlightMiddleware` is *not* enabled -- they read
Flight data through their own internal path, not through
`response.flight`. What they *don't* give you without `FlightMiddleware`
is `response.flight` itself in your spider's own callback -- that
convenience property only exists once `FlightMiddleware` installs it.
In practice this means: it's fine to enable only
`NextflightStatsExtension` for crawl-health metrics without touching
your spiders' code at all, but if you want `response.flight` in
`parse()`, `FlightMiddleware` needs to be enabled too.

**A note on persistence**: `FlightMiddleware` installs `.flight` on the
`scrapy.http.Response` *class* itself, not per-instance -- there's no
"uninstall." Once anything in a given Python process has constructed
`FlightMiddleware()` even once (a prior crawl, an earlier `scrapy shell`
command, a test), `response.flight` keeps working for every `Response`
for the rest of that process's lifetime, including ones that existed
before. This is expected and harmless for a normal `scrapy crawl`
invocation (one process, one crawl, then it exits) -- it's only worth
knowing about if you're running multiple crawls or spiders inside one
long-lived Python process (a custom runner script, a notebook, a test
suite) and want to reason about exactly when the property became
available.

| Component | Use it when | Don't bother if |
|---|---|---|
| **`FlightMiddleware`** (downloader middleware) | Almost always -- the baseline for every project. Gives every response a lazy `.flight`. | Never, really -- this is the foundation everything else builds on. |
| **`NextflightSpiderMixin`** | Almost always -- `self.flight(response)` works with or without the middleware, and `log_version_hint()` gives you an early warning in normal Scrapy logs if a site's wire format ever looks unfamiliar. | You have a strong reason to avoid mixins in your spider hierarchy. |
| **Plain `extract(response.text)`** in a callback, no middleware at all | A single quick script, a one-off scrape, or a spider you want to keep completely dependency-free of this library's Scrapy layer. | You're already using `FlightMiddleware` elsewhere in the project -- stay consistent. |
| **`FlightStreamingMiddleware`** (streaming + early-stop) | Large pages (100s of KB+) where the field(s) you need are near the top of the page, and bandwidth/latency matters (large crawls, rate-limited sites, mobile-constrained infra). | Pages are small, or the data you need tends to be near the *end* of the page (early-stop can't help, and you're just paying the byte-watching overhead for nothing). |
| **`FlightRSCMiddleware`** (fetch RSC instead of HTML) | Leaf/detail pages you extract data from but never need to crawl outgoing links from, on a site whose RSC endpoint works with just an `RSC: 1` header (no extra discovery). | You need to follow links from the same response (RSC responses have no HTML to select links from), or the site needs a `_rsc=<id>`/`Next-Router-State-Tree` you'd have to hardcode anyway -- use `FlightExtractor.from_rsc_url(auto_discover=True)` directly in those cases instead. |
| **`FlightItemPipeline`** | A crawl where "every matching dict is an item" is literally true, and you want that declared once in settings rather than repeated in every spider's `parse()`. | Your extraction logic is more selective than "every dict with these keys" (most real crawls) -- call `find_by_keys`/`find_all_by_keys` directly instead. |
| **`FlightDedupeMiddleware`** | The same listing can legitimately appear on more than one page of the crawl (a "related items" widget, a listing appearing in two different category pages). | Duplicates only ever occur *within* a single page -- `find_all_by_keys(dedupe=True)` already covers that case for free, with no crawl-wide state to manage. |
| **`FlightRetryMiddleware`** | You've seen (or want to guard against) a target site returning a challenge/interstitial page with a normal HTTP 200 status -- Scrapy's own status-based retry logic wouldn't notice, but `parse_confidence()` would. | The target site reliably returns proper error status codes when something's wrong (Scrapy's built-in `RetryMiddleware` already covers that) -- adding this on top just risks retrying legitimately sparse-but-correct pages. |
| **`NextflightStatsExtension`** | You want visibility into parse health (average confidence, version-hint spread, early-stop counts) in Scrapy's own end-of-crawl stats dump, without adding separate monitoring. | A short, one-off scrape where you'll eyeball the output directly anyway. |
| **`follow_flight_urls()`** (spider mixin method) | The site navigates via `onClick`/client-side routing for at least some links, so Scrapy's HTML-based link following misses real targets. | The site uses real `<Link>`-rendered anchors everywhere -- `response.follow_all()` already sees everything, and this would just be redundant. |
| **`recommended_settings()`** | Bootstrapping a new project and want a sane, documented starting configuration rather than assembling settings by hand. | You already have an established settings.py and just want one piece -- add that one middleware/setting directly instead of pulling in a whole preset. |

A typical "serious crawl" project ends up combining several of these:
`FlightMiddleware` + `NextflightSpiderMixin` always; `FlightStreamingMiddleware`
for the handful of spiders hitting large pages; `FlightItemPipeline` for
simple key-set crawls and hand-written `find_by_keys` calls for
anything more selective; `FlightDedupeMiddleware` only if the same
project's spiders are known to revisit the same listings from multiple
entry points; `FlightRetryMiddleware` + `NextflightStatsExtension` once
a crawl is big/long-running enough that "did this actually work well"
needs an answer beyond spot-checking a few pages by hand.

## Setup

```bash
pip install nextflight[scrapy]
```

```python
# settings.py
DOWNLOADER_MIDDLEWARES = {
    "nextflight.scrapy_middleware.FlightMiddleware": 543,
}

# All optional -- shown with their defaults:
NEXTFLIGHT_STRICT = False   # raise FlightParseError on malformed rows instead of tolerating them
NEXTFLIGHT_REPAIR = False   # best-effort recover chunks truncated mid-response
NEXTFLIGHT_DEDUPE = False   # default for FlightItemPipeline / NextflightSpiderMixin dedupe

# Optional, for FlightItemPipeline:
ITEM_PIPELINES = {
    "nextflight.scrapy_middleware.FlightItemPipeline": 300,
}
NEXTFLIGHT_PIPELINE_KEYS = ["price", "title"]
```

Or start from the built-in preset instead of assembling settings by
hand -- see [`recommended_settings()`](#recommended_settings) below.

## Complete runnable example spider

```python
import scrapy
from nextflight import normalize_price, clean_text
from nextflight.scrapy_middleware import NextflightSpiderMixin


class ProductSpider(NextflightSpiderMixin, scrapy.Spider):
    name = "products"
    start_urls = ["https://example.com/category/electronics"]

    def parse(self, response):
        # Logs next_version_hint() once per domain, not once per
        # response -- shows up in Scrapy's own log output if this site's
        # wire format ever looks unfamiliar.
        self.log_version_hint(response)

        for listing in response.flight.find_all_by_keys(
            {"price", "title", "url"}, dedupe=True
        ):
            yield {
                "title": clean_text(listing.get("title")),
                "price": normalize_price(listing.get("price")),
                "url": response.urljoin(listing["url"]),
            }

        # Clean, typed extraction once you know a page's shape well
        # enough to write a schema for it:
        from dataclasses import dataclass

        @dataclass
        class ProductDetail:
            title: str
            price: int
            description: str

        detail = response.flight.extract_as(ProductDetail)
        if detail:
            yield {"detail": detail}

        # Follow pagination the normal Scrapy way -- nothing
        # Flight-specific about this part.
        next_page = response.flight.get("pagination.next_url")
        if next_page:
            yield response.follow(next_page, self.parse)
```

Notes on what's happening above:

- `response.flight` is provided by `FlightMiddleware` -- parsing only
  happens the first time it's accessed, so a response a callback never
  touches `.flight` on costs nothing extra.
- `self.flight(response)` (from `NextflightSpiderMixin`) is equivalent
  but also works if `FlightMiddleware` isn't installed for some reason
  -- handy for spiders shared across projects with different settings.
- `dedupe=True` matters on real pages: the same listing commonly appears
  both in a "related items" carousel and the main results grid.

## Using `FlightItemPipeline`

For the common case of "every matching dict on the page is an item,"
skip the manual `find_all_by_keys` call in `parse()` entirely. Register
it as a normal Scrapy item pipeline (so `process_item` participates in
the usual pipeline chain for whatever else your project does with
items) and construct your own instance for `matches_for_response`,
since `matches_for_response` is a plain, explicit method a spider calls
when it wants Flight-derived items -- it deliberately does not silently
intercept and mutate every item a spider yields, since that kind of
action-at-a-distance pipeline behavior tends to surprise people
debugging it months later:

```python
# settings.py
ITEM_PIPELINES = {"nextflight.scrapy_middleware.FlightItemPipeline": 300}
NEXTFLIGHT_PIPELINE_KEYS = ["price", "title"]
```

```python
# spider.py
import scrapy
from nextflight.scrapy_middleware import FlightItemPipeline

class ProductSpider(scrapy.Spider):
    name = "products"

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._flight_pipeline = FlightItemPipeline(required_keys=["price", "title"])

    def parse(self, response):
        yield from self._flight_pipeline.matches_for_response(response)
```

Or, to read `NEXTFLIGHT_PIPELINE_KEYS`/`NEXTFLIGHT_DEDUPE` from settings
instead of hardcoding `required_keys`, build it from the crawler in
`from_crawler` (Scrapy calls this automatically for spiders, same as it
does for the pipeline registered above):

```python
class ProductSpider(scrapy.Spider):
    name = "products"

    @classmethod
    def from_crawler(cls, crawler, *args, **kwargs):
        spider = super().from_crawler(crawler, *args, **kwargs)
        spider._flight_pipeline = FlightItemPipeline.from_crawler(crawler)
        return spider

    def parse(self, response):
        yield from self._flight_pipeline.matches_for_response(response)
```

```python
class ProductSpider(scrapy.Spider):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        from nextflight.scrapy_middleware import FlightItemPipeline
        self._flight_pipeline = FlightItemPipeline(required_keys=["price", "title"])

    def parse(self, response):
        yield from self._flight_pipeline.matches_for_response(response)
```

## Using `response.flight` in `scrapy shell`

Enable the middleware in your project's `settings.py` as above, then:

```
$ scrapy shell "https://example.com/product/123"
>>> response.flight.keys()
['0', '1', '3f', ...]
>>> response.flight.find_by_keys({"price", "title"})
{'price': 1999, 'title': 'Example Product'}
```

This works because `scrapy shell` constructs its `response` object
through the exact same `scrapy.http.Response` class `FlightMiddleware`
patches -- there's nothing spider-callback-specific about how the
`.flight` property gets installed, so exploring a new, unfamiliar site
interactively before writing a spider works exactly the same way.

## Processing a response while it's still downloading

For large pages, or a crawl where you only need one or two fields off
each page, `FlightStreamingMiddleware` processes bytes **as they arrive**
via Scrapy's `bytes_received` signal, instead of waiting for the full
download to finish:

```python
# settings.py
DOWNLOADER_MIDDLEWARES = {
    "nextflight.scrapy_middleware.FlightMiddleware": 543,
    "nextflight.scrapy_middleware.FlightStreamingMiddleware": 544,
}
NEXTFLIGHT_STREAMING = True
NEXTFLIGHT_STREAM_KEYS = ["price", "title"]   # optional -- see below
```

This buys two things, and only the first requires `NEXTFLIGHT_STREAM_KEYS`:

1. **Cancel the download the moment you have what you need.** Once a
   dict matching `NEXTFLIGHT_STREAM_KEYS` shows up in the bytes received
   so far, the rest of the download is cancelled outright (via Scrapy's
   `StopDownload`) — for a 3MB page where the price you want is in the
   first 50KB, this means not downloading the other ~2.95MB at all. The
   match is available immediately, with no extra parsing needed in the
   callback:

   ```python
   def parse(self, response):
       if response.meta.get("nextflight_stopped_early"):
           yield response.meta["nextflight_match"]
           return
       # normal, fully-downloaded-page handling
       yield response.flight.find_by_keys({"price", "title"})
   ```

2. **Overlap parsing with the download, even without early stop.** Leave
   `NEXTFLIGHT_STREAM_KEYS` unset and bytes still get decoded
   incrementally as they arrive rather than all at once afterward, and
   `response.flight` is pre-warmed from that by the time your callback
   runs. This benefit is real but modest — it overlaps decode/row-split
   work with network wait time, it doesn't reduce how much data comes
   over the wire — benefit 1 is the standout win when it applies.

Tuning knobs, all optional:

```python
NEXTFLIGHT_STREAM_MAX_BYTES = 10_000_000    # cap on bytes tracked for match-checking (default shown)
NEXTFLIGHT_STREAM_CHECK_INTERVAL = 8192      # bytes between match-checks (default shown)
NEXTFLIGHT_STREAM_ENCODING = "utf-8"         # decoding assumption (default shown; Next.js payloads are virtually always UTF-8)
```

`NEXTFLIGHT_STREAM_MAX_BYTES` only bounds *this middleware's own tracking
buffer* — if a page never matches and exceeds it, the download still
completes normally through Scrapy's own machinery, and `.flight` falls
through to parsing the complete `response.text` the ordinary way; no
data is lost, you just don't get the early-stop benefit for that
particular (larger-than-expected) page.

Requires Scrapy >= 2.6 (for `scrapy.exceptions.StopDownload`); on older
Scrapy this middleware disables itself via `NotConfigured`, the same way
Scrapy handles any other middleware whose dependencies aren't met — the
rest of `nextflight`'s Scrapy integration works fine without it.

Off by default: `NEXTFLIGHT_STREAMING` must be explicitly set to `True`.
Watching every byte as it arrives has some overhead, isn't needed for a
typical crawl, and cancelling downloads mid-flight is a real behavior
change a project should opt into deliberately.

**Compatibility note, verified against the actual package source, not
assumed:** `bytes_received` only fires for requests going through
Scrapy's own Twisted-based download handlers (HTTP/1.1, H2). It does
**not** fire for:

- Requests routed through **`scrapy-zyte-api`**'s automap/Addon (`ADDONS
  = {"scrapy_zyte_api.Addon": ...}`, or `meta={"zyte_api_automap": ...}`)
  — that download handler builds the `Response` directly from a
  completed Zyte API call rather than reading the target page's bytes
  through Scrapy's own protocol layer, so there's nothing to watch
  incrementally.
- Requests using **`scrapy-impersonate`**'s `meta={"impersonate": True}`
  — that handler fetches via `curl_cffi`'s own connection handling,
  entirely outside Twisted's protocol classes that emit the signal.

For both, `FlightStreamingMiddleware` doesn't error or misbehave — its
`bytes_received` handler simply never gets called for those specific
requests, so they silently get none of the early-stop/overlap benefit
and just complete as a normal, fully-downloaded response. If a project
mixes plain Scrapy requests with Zyte API/impersonated ones (common when
some domains need anti-bot handling and others don't), streaming still
helps the plain-HTTP subset — there's no need to disable it project-wide
just because some spiders use one of these.

## Fetching the lightweight RSC payload instead of full HTML

For leaf/detail pages you extract data from but don't need to crawl
outgoing links from, `FlightRSCMiddleware` transparently fetches the RSC
payload (Next.js's own client-side-navigation format) instead of the
full HTML page -- often a small fraction of the size, since it skips
the surrounding HTML document and everything that isn't Flight data:

```python
# settings.py
DOWNLOADER_MIDDLEWARES = {
    "nextflight.scrapy_middleware.FlightMiddleware": 543,
    "nextflight.scrapy_middleware.FlightRSCMiddleware": 542,
}
NEXTFLIGHT_RSC_URL_PATTERN = r"/product/"   # optional -- match a URL pattern crawl-wide
```

```python
# or opt in per request instead of (or in addition to) a URL pattern:
yield scrapy.Request(url, meta={"nextflight_rsc": True})
```

`response.flight` works completely unchanged on the result -- Flight
payload auto-detection already handles a raw RSC stream the same way it
handles the HTML-embedded form, so nothing about how you *read* the
response needs to change, only how it's fetched.

**Important**: the response for a rewritten request has no HTML in it
at all, so `response.css`/`.xpath`/link-following won't find anything —
only use this for pages you don't also need to crawl links from. Some
deployments also need a build-specific `_rsc=<id>` parameter or a
`Next-Router-State-Tree` header this middleware doesn't guess at (a
wrong guess would silently return the wrong data, which is worse than
not trying); for those sites, use
`FlightExtractor.from_rsc_url(url, auto_discover=True)` directly instead
— it already implements that discovery step (see the main README's "Raw
RSC fetches" section).

## Deduplicating listings across the whole crawl

`find_all_by_keys(dedupe=True)` only catches duplicates *within* a
single page. If the same listing can legitimately show up on more than
one page of the crawl — a "related items" widget appearing on many
detail pages, or a listing indexed under two categories —
`FlightDedupeMiddleware` catches that broader case:

```python
# settings.py
SPIDER_MIDDLEWARES = {
    "nextflight.scrapy_middleware.FlightDedupeMiddleware": 543,
}
NEXTFLIGHT_DEDUPE_ACROSS_PAGES = True
NEXTFLIGHT_DEDUPE_KEY = "id"   # optional -- fingerprint just this field, cheaper and more correct
                                # than the whole item if other fields (a "last seen" timestamp,
                                # say) can legitimately differ between two crawls of the same listing
```

Only dict-shaped items are ever considered for deduplication —
`scrapy.Request` objects and typed `Item`/dataclass instances a spider
yields pass through completely untouched, so this can never accidentally
drop a page still queued to be crawled.

## Retrying pages that look like a challenge/interstitial page

Some anti-bot systems return a normal HTTP 200 status for a
challenge/interstitial page instead of an error code — Scrapy's built-in
`RetryMiddleware` (status-code-based) won't notice, but `nextflight` can,
via `parse_confidence()`:

```python
# settings.py
DOWNLOADER_MIDDLEWARES = {
    "nextflight.scrapy_middleware.FlightMiddleware": 543,
    "nextflight.scrapy_middleware.FlightRetryMiddleware": 550,
}
NEXTFLIGHT_RETRY_MIN_CONFIDENCE = 0.5   # below this, retry
NEXTFLIGHT_RETRY_MAX_TIMES = 2          # optional -- falls back to Scrapy's own RETRY_TIMES
```

Uses Scrapy's own `get_retry_request` helper under the hood — the same
retry-count bookkeeping, backoff, and stats integration as the built-in
`RetryMiddleware` — so retried requests behave exactly like any other
Scrapy retry. A response with **zero** Flight chunks (a non-Next.js
page, or a genuinely blank error page) is never retried by this
middleware on purpose — `parse_confidence()` returns `1.0` when there's
nothing to have parsed badly, so a non-Next.js page won't loop here.
Requires Scrapy >= 2.5; disables itself via `NotConfigured` on older
Scrapy.

## Crawl-wide stats: parse confidence, version drift, early-stop counts

For visibility into parse health across a whole crawl — not just
spot-checking individual pages — enable the stats extension:

```python
# settings.py
EXTENSIONS = {
    "nextflight.scrapy_middleware.NextflightStatsExtension": 543,
}
```

This surfaces, in Scrapy's own end-of-crawl stats dump (and anywhere
else your project already exports Scrapy stats to):

```
nextflight/responses_with_flight_data: 842
nextflight/responses_without_flight_data: 12
nextflight/avg_parse_confidence: 0.97
nextflight/version_hint/14.1 - 14.2: 842
nextflight/stream/stopped_early: 310
```

A dropping `avg_parse_confidence` partway through a long crawl is an
early signal worth investigating — either the target site's wire format
shifted (check `nextflight/version_hint/*` for a new, unexpected range)
or responses are increasingly hitting a block/challenge page instead of
the real one. Requires `FlightMiddleware` (or some other way of
populating `response.flight`) to also be enabled.

### Wiring this into an existing alerting extension

Many real projects already have a custom Scrapy `Extension` connected to
`spider_closed` that alerts (Slack, Telegram, email, PagerDuty, ...) when
a spider scrapes **zero items**. That catches total breakage, but not
*partial* breakage — a spider that still yields items every run because
one field it always relied on quietly started coming back empty after a
site redesign, while everything else still looks fine. Extending that
same extension with a per-page confidence check catches this earlier,
without waiting for someone to notice a field is wrong in the exported
data days later:

```python
# in your existing extension class
def response_received(self, response, request, spider):
    flight = getattr(response, "flight", None)
    if flight is None or not flight.keys():
        return  # not a Next.js page, or FlightMiddleware not enabled here
    confidence = flight.parse_confidence()["score"]
    if confidence < 0.7:
        self.send_alert(
            f"⚠️ {spider.name}: low parse confidence ({confidence:.2f}) on {response.url}"
        )
```

Connect it the same way your extension already connects to
`spider_closed`/`item_scraped`: `crawler.signals.connect(self.response_received,
signal=signals.response_received)`. Pairing this with
`NextflightStatsExtension`'s crawl-level average (rather than alerting
per-page, which can be noisy on a site with genuinely sparse listings)
is usually the better signal-to-noise tradeoff for a long-running crawl
— check `nextflight/avg_parse_confidence` in `spider_closed` instead of
alerting on every individual low-confidence response.

## Following links that only exist in Flight JSON, not rendered HTML

A Next.js `<Link>` component almost always renders a real `<a href>` --
Scrapy's own `response.follow_all()`/`LinkExtractor` already sees those.
But plenty of common UI patterns navigate a different way: a card grid
using an `onClick` handler and client-side `router.push(...)`, a "load
more" cursor passed as page data for a client-side fetch, or a "related
items" widget whose targets only ever exist as data. Those URLs are
real and sitting right there in the Flight JSON -- just invisible to
anything that only looks at rendered HTML.

```python
class ProductSpider(NextflightSpiderMixin, scrapy.Spider):
    def parse(self, response):
        # Follows every string value found under an "href" key anywhere
        # in the page's Flight data, resolved against response.url the
        # same way response.follow() always does.
        yield from self.follow_flight_urls(
            response, keys={"href"}, callback=self.parse_detail,
        )
```

Restrict with `keys` (specific field names, e.g. `{"href", "url",
"next"}`) and/or `pattern` (a regex the URL itself must match, e.g.
`r"/product/"`) -- omitting both scans every URL-*shaped* string on the
page, which is easy to over-match (image CDN URLs, canonical tags,
external share links) on a real site. At least one is recommended
beyond quick exploration. The underlying extraction --
`FlightExtractor.find_urls()` -- works outside Scrapy too, returning
plain strings (not resolved against any base URL) for any other use.

## Building typed items with `extract_as`

`extract_as` works with a `scrapy.Item` subclass directly, not just
dataclasses/pydantic models — handy if your project already declares
items the traditional Scrapy way:

```python
class ListingItem(scrapy.Item):
    title = scrapy.Field()
    price = scrapy.Field()

def parse(self, response):
    item = response.flight.extract_as(ListingItem)
    if item:
        yield item
```

Extra keys on the matched dict that aren't declared `Field()`s on the
item are silently ignored (same behavior as with dataclasses); a
required field with no match raises the same way `ListingItem(**kwargs)`
already would for a missing required key.

## `recommended_settings()`

A starting-point settings preset for a new project, rather than
assembling the pieces above by hand:

```python
# settings.py
from nextflight.scrapy_middleware import recommended_settings

for key, value in recommended_settings(
    streaming=True, rsc=False, dedupe_across_pages=False, retry=True, stats=True,
).items():
    existing = globals().get(key)
    if isinstance(value, dict) and isinstance(existing, dict):
        existing.update(value)
    else:
        globals()[key] = value
```

The baseline (no flags) enables just `FlightMiddleware` with
`NEXTFLIGHT_REPAIR = True` — a reasonable default for a real crawl
against production sites, where a response truncated by a flaky proxy
is far more likely than in local testing. See
["Which approach should I use?"](#which-approach-should-i-use) above for
when to turn on each flag.

## Failure modes

`.flight` never raises for a non-Next.js response (a redirected error
page, a non-HTML response, an empty body) -- it simply resolves to an
extractor with zero chunks, same as pointing `extract()` at any other
page with no Flight data on it. The only way to get an exception out of
`.flight` is setting `NEXTFLIGHT_STRICT = True` and hitting a genuinely
malformed row (not merely an absent one).

## See also

- [`docs/cookbook.md`](cookbook.md) -- e-commerce, job-board, and SEO
  auditing examples (not Scrapy-specific, but directly usable from a
  `parse()` callback via `response.flight`).
- [`docs/anti-bot-cookbook.md`](anti-bot-cookbook.md) -- what `nextflight`
  can and can't help with when a site is behind Cloudflare/PerimeterX/etc.
- The main [README](../README.md#comparison-with-other-tools) for how
  `nextflight` compares to `njsparser` and other tools in this space.
