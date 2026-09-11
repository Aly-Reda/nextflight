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
