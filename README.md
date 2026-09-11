# nextflight

[![PyPI version](https://img.shields.io/pypi/v/nextflight.svg)](https://pypi.org/project/nextflight/)
[![Python versions](https://img.shields.io/pypi/pyversions/nextflight.svg)](https://pypi.org/project/nextflight/)
[![CI](https://github.com/Aly-Reda/nextflight/actions/workflows/ci.yml/badge.svg)](https://github.com/Aly-Reda/nextflight/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Typed](https://img.shields.io/badge/typing-typed-brightgreen.svg)](https://peps.python.org/pep-0561/)

**A Python library to scrape and parse Next.js (App Router) pages — decode React Server Components ("Flight") payloads and `self.__next_f.push()` data into clean, searchable JSON.**

`nextflight` parses the React Server Components ("Flight") payloads that
Next.js embeds in server-rendered HTML — the
`<script>self.__next_f.push([...])</script>` blocks, or the raw RSC
response you get back from a request sent with an `RSC: 1` header — and
turns them into clean, searchable Python dicts and lists. It works on
**any** Next.js 13+ App Router site out of the box, with no per-site
configuration, which makes it the go-to Python library for **scraping
Next.js websites**, **web scraping**, **crawling**, and structured
**data extraction** with Scrapy, requests, httpx, or the stdlib alone.

Next.js pages don't put their data in one obvious place — it's spread
across dozens of numbered chunks, cross-referenced with `$`-sigils, and
reshuffled every time the site redeploys. Hardcoding array paths like
`data[3]["children"][0][3]...` breaks the moment that happens.
`nextflight` resolves those references for you and lets you *search* for
the shape of data you want instead — `page.find_by_keys({"price",
"title"})` instead of a brittle index chain.

If you've ever searched for *"how to scrape a Next.js site"*, *"parse
self.__next_f.push in Python"*, or *"extract JSON from Next.js
__NEXT_DATA__ / Flight payload"* — this is that tool.

## Contents

- [Install](#install)
- [Quick start](#quick-start)
- [Usage](#usage) — [Scrapy](#in-a-scrapy-spider), [fetching a URL](#fetching-a-url-directly-no-scrapy-needed), [raw RSC fetches](#raw-rsc-fetches-no-html-at-all), [Pages Router](#pages-router-support), [monitoring/diffing](#monitoring-a-page-over-time), [CSV/DataFrame export](#exporting-to-a-dataframe-or-csv), [CLI](#command-line)
- [API reference](#api-reference)
- [Performance](#performance)
- [Optional dependencies](#optional-dependencies)
- [How it works](#how-it-works)
- [Comparison with other tools](#comparison-with-other-tools)
- [FAQ](#faq)
- [A note on maintenance](#a-note-on-maintenance)
- [Roadmap and design proposals](#roadmap-and-design-proposals)
- [License](#license)

## Install

```bash
pip install nextflight
```

No required dependencies — stdlib only, so it drops into any existing
Scrapy/Zyte project without touching your dependency tree. A few optional
extras unlock extra features automatically if installed; see
[Optional dependencies](#optional-dependencies).

## Quick start

Two steps: see what's on the page, then fetch the shape of data you want.

```python
from nextflight import extract

page = extract(html_text)      # a string, bytes, or response object

page.keys()                    # ['0', '1', '3f', '20', ...] -- what's here
page["3f"]                     # the resolved JSON for one specific chunk

# In practice, chunk ids are arbitrary per build (they change on
# redeploy), so search for the shape of data you want instead:
listing = page.find_by_keys({"price", "title"})       # first match
listings = page.find_all_by_keys({"price", "title"})  # every match
products = page.find_by_type("Product")               # by @type
everything = page.resolve_all()                        # everything, dereferenced
```

## Usage

### In a Scrapy spider

```python
import scrapy
from nextflight import extract

class MySpider(scrapy.Spider):
    name = "my_spider"

    def parse(self, response):
        page = extract(response.text)
        for item in page.find_all_by_keys({"price", "title"}):
            yield {
                "title": item.get("title"),
                "price": item.get("price"),
                "url": response.url,
            }
```

For a more idiomatic setup — a lazy `response.flight` on every response,
settings-driven `strict`/`repair`/`dedupe` defaults, an optional item
pipeline, and per-domain version-hint logging — see
[`docs/scrapy.md`](docs/scrapy.md):

```python
# settings.py
DOWNLOADER_MIDDLEWARES = {"nextflight.scrapy_middleware.FlightMiddleware": 543}
```

```python
def parse(self, response):
    listing = response.flight.find_by_keys({"price", "title"})
```

### Fetching a URL directly (no Scrapy needed)

```python
from nextflight import FlightExtractor

page = FlightExtractor.from_url("https://example.com/product/123")
product = page.find_by_keys({"price", "title"})
```

`from_url` uses only the stdlib, for quick exploration or lightweight
crawling. For anything needing retries, proxies, JS rendering, or
robots.txt handling, fetch the page with your own HTTP client and pass
`response.text` to `extract(...)` instead.

### Raw RSC fetches (no HTML at all)

Sending a request with an `RSC: 1` header — the way Next.js's own
client-side navigation does — returns the raw Flight row stream directly
as the response body, with no HTML wrapper. `extract()` detects and
parses this automatically, same as the HTML-embedded form:

```python
from nextflight import FlightExtractor

# Sets RSC:1 and Next-Url for you, and best-effort auto-discovers a
# build-specific _rsc=<id> from the page's own prefetch links
page = FlightExtractor.from_rsc_url("https://example.com/car/search?page=2")

# Or bring your own client:
import requests
resp = requests.get(
    "https://example.com/car/search",
    params={"page": "2", "_rsc": "1p28d"},   # a build-specific cache key
    headers={"RSC": "1", "Next-Url": "/en/car/search"},
)
page = FlightExtractor(resp.text)
```

If a site also requires a `Next-Router-State-Tree` header, grab it once
from a real browser's network tab and reuse it — it's stable for every
request to the *same route* regardless of query params, so it doesn't
need to be regenerated per request.

### Pages Router support

Older or mixed Next.js deployments use the Pages Router's `__NEXT_DATA__`
blob instead of Flight — already plain JSON, no `$`-refs to resolve:

```python
from nextflight import extract, find_next_data, detect_next_router

router = detect_next_router(html_text)   # "app" | "pages" | "both" | "unknown"

if router == "app":
    data = extract(html_text).find_by_keys({"price", "title"})
else:
    data = find_next_data(html_text)["props"]["pageProps"]
```

### Monitoring a page over time

`diff_pages` compares two crawls of the same URL and reports what
changed — handy for a price or stock watcher:

```python
from nextflight import FlightExtractor, diff_pages

old_page = FlightExtractor.from_url(url)
# ...re-fetch later...
new_page = FlightExtractor.from_url(url)

diff_pages(old_page, new_page)
# {"added": {...}, "removed": {...}, "changed": {"path.to.price": (100, 90)}}
```

For a list of records with a stable id, pass `id_key` — otherwise
inserting one new item shifts every later index and makes everything
after it look changed even though it didn't:

```python
diff_pages(old_page, new_page, id_key="listing_id")
# {"changed": {"items[listing_id=7165546].price": (929900, 899900)}, ...}
```

Or from the command line, polling continuously (`--rsc` for the
lighter-weight RSC payload instead of full HTML each poll):

```bash
nextflight https://example.com/product/123 --watch 60
nextflight https://example.com/car/search --rsc --watch 60
```

### Exporting to a DataFrame or CSV

```python
page = extract(html_text)

df = page.to_dataframe(required_keys={"id", "price"})       # requires pandas
page.to_csv("listings.csv", required_keys={"id", "price"})  # works either way
```

### Command line

```bash
nextflight page.html --keys sections,meta
nextflight https://example.com/product/123 --type Product
nextflight page.html --tree                 # shape summary, no full values
nextflight page.html --all > everything.json
```

## API reference

### `extract(html) -> FlightExtractor`

Shorthand constructor. `html` accepts a plain string, bytes, or a
response-like object (Scrapy's `Response`, `requests.Response`, etc.) —
pass `response` straight from a `parse()` method.

### `FlightExtractor(html, *, strict=False, repair=False)`

`strict=True` raises `FlightParseError` on a row that's neither valid
JSON nor a recognizable `$`-reference, instead of keeping it as a raw
string. Useful while developing a new scraper; leave off in production so
a handful of odd rows never take down extraction of everything else.

`repair=True` goes a step further for JSON rows that fail to decode
outright (not just bare markers) — typically a payload truncated
mid-chunk by a proxy/CDN cutting a response short. It heuristically
closes unbalanced brackets/quotes and retries the decode, so a page with
one truncated chunk doesn't lose that chunk's data entirely. Mutually
exclusive with `strict=True`. Check how much of a repaired page was
actually salvaged with `.parse_confidence()`.

**Exploring a page**

| Method | Returns | What it does |
|---|---|---|
| `.keys()` | `list[str]` | Every chunk id on the page, in order |
| `.kind(chunk_id)` | `str \| None` | Row kind: `"json"`, `"text"`, `"module"`, `"preload"` |
| `.json_keys()` | `list[str]` | Chunk ids holding structured JSON (dict/list) |
| `.html_keys()` | `list[str]` | Text-row chunk ids that look like HTML fragments |
| `.text_keys()` | `list[str]` | All text-row chunk ids, HTML-looking or not |
| `.shape(chunk_id=None, max_depth=3)` | structure summary | Key names + value types, not values — get a feel for a new site fast |
| `.stats()` | `dict` | Chunk count, row-kind breakdown, page size |
| `.parse_confidence()` | `dict` | Score + breakdown of cleanly-parsed vs. raw-fallback vs. repaired chunks |
| `.next_version_hint()` | `dict` | Best-effort guess at which Next.js version range produced this payload |

**Resolving data** (dereferencing `$`-refs)

| Method | Returns | What it does |
|---|---|---|
| `page["id"]` / `.resolve_chunk("id")` | resolved value | One chunk, fully dereferenced (`page[...]` raises `KeyError` if missing) |
| `.resolve_all()` | `dict` | Every chunk, fully dereferenced |
| `.resolve_json()` / `.resolve_html()` / `.resolve_text()` | `dict` | Only one kind of chunk — cheaper than `resolve_all()` when you don't need everything |
| `.iter_resolved()` | iterator | Like `resolve_all()` but lazy, one chunk at a time |
| `.get("path.to.value", default=None)` | value | Tolerant dotted-path lookup (dict keys, list indices, and React element `"props"`) |
| `.select(*paths, default=None)` | `dict` | Resolve just the named paths, e.g. `page.select("3f.props.price", "3f.props.title")` |

`"3f" in page` and `for k in page` also work, like a dict.

**Searching** (schema-free, works across redeploys)

| Method | Returns | What it does |
|---|---|---|
| `.find_by_keys(required_keys, root=None)` | dict or `None` | First dict containing *all* of `required_keys` |
| `.find_all_by_keys(required_keys, root=None, dedupe=False)` | `list` | Every matching dict — for repeated cards/listings. `dedupe=True` collapses the same object serialized twice at different tree positions |
| `.find_any_keys(any_keys, root=None)` | `list` | Every dict containing *any* of `any_keys` |
| `.find_by_key_pattern(pattern, root=None)` | `list` | Every dict with a key matching a regex, e.g. `r"^price_"` |
| `.find_by_type(type_value, key="@type", root=None)` | `list` | Every dict whose `key` field equals `type_value` |
| `.find_text(pattern, root=None)` | `list` | Distinct string values matching a regex (emails, SKUs, ...) |
| `.find_all(predicate, root=None, max_results=None)` | `list` | Fully custom predicate over every node |
| `.find_one(predicate, root=None)` | value or `None` | Like `find_all` but just the first match |
| `.suggest_similar_keys(required_keys)` | `dict` | When a `find_*` call comes back empty, fuzzy-match against keys actually present, e.g. `{"titl": ["title"]}` |
| `.extract_as(Model, root=None)` | instance or `None` | Find the first dict matching `Model`'s fields and coerce it into a dataclass or pydantic model |

Pass `include_source=True` on any `find_*` method to get `(node,
chunk_id)` tuples instead of bare nodes, so you can trace a match back to
where it came from. `find_all`/`find_one`/`find_by_keys` resolve chunks
lazily and stop the moment `max_results` is hit — they don't pay to
resolve chunks after a match is already found.

**Fetching**

| Classmethod | What it does |
|---|---|
| `.from_url(url, timeout=15.0, headers=None)` | Fetch and parse a URL, stdlib only |
| `.from_url_async(url, ...)` | Async version for `asyncio.gather(...)` crawls — requires `httpx`. `AsyncFlightExtractor` is a discoverable alias for the same class |
| `.from_rsc_url(url, headers=None, cookies=None, auto_discover=True)` | Fetch the raw RSC payload instead of full HTML — see "Raw RSC fetches" above |
| `.from_stream(chunks)` | Incrementally parse an iterable of HTML fragments/bytes, yielding `(chunk_id, value)` as rows complete — see "Streaming a response" below |
| `.from_page(playwright_page)` | Build from a Playwright page's fully-rendered HTML — for sites that only populate later chunks after client-side JS runs. Requires `playwright` (not bundled in any extra) |

`from_url`/`from_rsc_url` transparently decompress gzip/deflate/br
responses even if the server ignores the default `Accept-Encoding:
identity` request.

**Diffing and exporting**

| Method | Returns | What it does |
|---|---|---|
| `.diff(other_page, id_key=None)` | `dict` | Compare against another crawl — see "Monitoring a page over time" |
| `.to_json(path=None, indent=2)` | `str \| None` | Dump the fully resolved page to a file, or return as a string |
| `.to_dataframe(records=None, required_keys=None)` | `DataFrame` | Requires pandas |
| `.to_csv(path, records=None, required_keys=None)` | — | Falls back to the stdlib `csv` module without pandas |

### Module-level functions

- **`find_json_ld(html, type_=None) -> list`** — parse
  `<script type="application/ld+json">` blocks, optionally filtered by
  `@type`. Often more stable across redesigns than Flight data — worth
  trying first for product/article/breadcrumb structured data.
- **`find_next_data(html) -> dict | None`** — parse a Pages Router
  `__NEXT_DATA__` blob. `None` if the page doesn't have one.
- **`detect_next_router(html) -> str`** — `"app"`, `"pages"`, `"both"`, or
  `"unknown"`. Run this first if you're not sure which extractor to use.
- **`diff_pages(old, new, id_key=None) -> dict`** — module-level form of
  `.diff()`.
- **`normalize_price(value) -> dict | None`** — parse a messy price string
  (currency symbols, thousands separators, either comma or period as the
  decimal point) into `{"amount": float, "currency": str | None}`.
- **`clean_text(value) -> str | None`** — decode HTML entities and
  collapse whitespace.
- **`parse_date(value) -> datetime | None`** — best-effort parse against a
  conservative list of common date formats (ISO 8601, `MM/DD/YYYY`,
  `"January 5, 2024"`, ...).

### Command-line reference

```
nextflight <file-or-url>
  [--keys a,b | --all-by-keys a,b | --any-keys a,b
   | --type Product | --text PATTERN | --get path.to.value
   | --json-keys | --html-keys | --tree
   | --router | --next-data | --stats | --watch SECONDS | --all]
  [--rsc] [--redact] [--save out.json]
```

- `--tree` prints a `.shape()` summary instead of full values.
- `--router` / `--next-data` cover Pages Router pages.
- `--rsc` fetches the raw RSC payload instead of full HTML (URL sources
  only) — lighter weight, also works with `--watch`.
- `--watch SECONDS` polls a URL and prints only what changed since the
  last poll.
- `--redact` best-effort scrubs email/phone-shaped strings from output,
  for sharing debug dumps.

## Performance

`nextflight` parses and resolves a typical product/listing page in the
low single-digit milliseconds; see [`docs/performance.md`](docs/performance.md)
for the profiling methodology, before/after benchmark numbers, and why a
Rust/pyo3-accelerated tokenizer isn't currently justified (short version:
the actual hot spots turned out to be pure-Python inefficiencies with a
much cheaper fix). Run `python benchmarks/run_benchmarks.py` yourself
against small/medium/large synthetic pages.

## Optional dependencies

Nothing below is required to install or use `nextflight` — each is used
automatically if already present in your environment, and raises a clear
`ImportError` only if you call the one method that needs it.

| Package | Unlocks | Install |
|---|---|---|
| `orjson` | Faster JSON decoding everywhere | `pip install nextflight[fast]` |
| `pandas` | `.to_dataframe()`, nicer `.to_csv()` | `pip install nextflight[pandas]` |
| `httpx` | `.from_url_async()` / `AsyncFlightExtractor` | `pip install nextflight[async]` |
| `pydantic` | `.extract_as(SomePydanticModel)` (dataclasses work with no extra dependency) | `pip install nextflight[schema]` |
| `scrapy` | `nextflight.scrapy_middleware.FlightMiddleware` | `pip install nextflight[scrapy]` |
| `pyyaml` | Not required — `next_version_hint()` reads `known_formats.yaml` via a bundled fallback parser if `pyyaml` isn't installed | (optional, used automatically if present) |

Or `pip install nextflight[all]` for all of the above.

## How it works

Flight payloads aren't newline-delimited JSON — text rows
(`id:T<hexByteLen>,<raw bytes>`) are byte-length-prefixed and can contain
literal newlines or run straight into the next row with no separator, and
module/preload rows (`id:I[...]`, `id:HL[...]`) need bracket-aware
parsing. `nextflight` implements the actual row grammar rather than
splitting on `\n`, so it holds up on both well-formed pages and payloads
truncated mid-chunk (e.g. by a proxy that cuts a response short).

It also doesn't assume one `<script>self.__next_f.push(...)</script>`
call is one complete, self-contained set of rows. On large real-world
pages, Next.js's own streaming buffer can flush mid-string, splitting a
single row's raw text across two or more separate `push()` calls with no
separator between the pieces — all `push()` payloads are reassembled into
one continuous stream before being split into rows, so this doesn't
silently corrupt chunk ids on pages large enough to trigger it (confirmed
against production pages where over half of all `push()` calls turned out
to be mid-row continuations).

Chunk ids aren't guaranteed unique, either — Next.js deliberately emits
preload (`HL`) rows with a completely empty id (`:HL["/path.css","style"]`)
since nothing ever needs to `$`-ref them individually, and real pages have
had dozens of these sharing the same empty id. Rather than the later ones
silently overwriting the earlier ones, only the first occurrence of a
duplicated id keeps its real id; later ones get a synthesized, clearly
distinguishable key (`"id#2"`, `"id#3"`, ...) so nothing gets lost.

Parsing and resolution are both designed to scale roughly linearly with
page size: rows are split cheaply up front, each chunk's JSON is decoded
lazily on first access rather than all at once, and searches
(`find_one`/`find_by_keys`) stop resolving chunks the moment a match is
found instead of resolving the whole page first.

### Streaming a response

For very large pages, or use cases where acting on data as it arrives
matters more than a single final result (a live progress indicator,
stopping a slow crawl early once a wanted field shows up), parse an
iterable of HTML fragments/bytes incrementally instead of buffering the
whole page first:

```python
from nextflight import FlightExtractor

for chunk_id, value in FlightExtractor.from_stream(response.iter_content()):
    print(chunk_id, value)
```

`$`-refs are resolved best-effort as each row arrives — a reference to a
chunk that hasn't streamed in yet resolves to `None` rather than blocking,
unlike the regular constructor, which sees the whole page at once. Use
the regular constructor when getting every cross-reference exactly right
matters more than seeing data as it streams in.

### Recovering truncated pages

```python
from nextflight import FlightExtractor

page = FlightExtractor(html_text, repair=True)
page.parse_confidence()
# {"score": 0.92, "total_chunks": 40, "clean_chunks": 37,
#  "raw_string_chunks": 3, "repaired_chunks": 2, "failed_repair_chunks": 1}
```

### Debugging a search that came back empty

```python
page.find_by_keys({"price", "titel"})   # None -- typo, or wrong build?
page.suggest_similar_keys({"price", "titel"})
# {"price": ["price"], "titel": ["title"]}
```

### Typed extraction

```python
from dataclasses import dataclass
from nextflight import extract

@dataclass
class Listing:
    title: str
    price: int

page = extract(html_text)
listing = page.extract_as(Listing)   # or a pydantic BaseModel
```

### Cleaning up messy scraped values

```python
from nextflight import normalize_price, clean_text, parse_date

normalize_price("$1,299.00")       # {"amount": 1299.0, "currency": "USD"}
normalize_price("1.299,00 €")      # {"amount": 1299.0, "currency": "EUR"}
clean_text("  Cozy&nbsp;Studio  ") # "Cozy Studio"
parse_date("January 5, 2024")      # datetime(2024, 1, 5, 0, 0)
```

### Scrapy: automatic `.flight` on every response

Instead of calling `extract(response)` in every callback, install the
downloader middleware once and get a lazily-parsed `.flight` attribute on
every `Response`:

```python
# settings.py
DOWNLOADER_MIDDLEWARES = {
    "nextflight.scrapy_middleware.FlightMiddleware": 543,
}
```

```python
def parse(self, response):
    listing = response.flight.find_by_keys({"price", "title"})
```

Requires Scrapy (`pip install nextflight[scrapy]`, or just have Scrapy
installed already, as any project using this necessarily does).

## Comparison with other tools

Several tools touch the same "Next.js hides its data in a weird wire
format" problem space, but solve different parts of it — pick based on
what you're actually trying to do:

| Tool | What it's for | What it isn't |
|---|---|---|
| **`nextflight`** | Programmatic extraction in Python: parse, resolve `$`-refs, search, and re-use across a crawl of many pages | Not a browser tool or one-off manual paste target |
| [`njsparser`](https://pypi.org/project/njsparser/) | Also a Python Flight/RSC parser, closer to the wire format itself (lower-level primitives, less of a search-oriented API) | Doesn't provide `nextflight`'s search-by-shape (`find_by_keys`), diffing, Scrapy integration, or CLI |
| Burp Suite `nextjs-rsc-parser` extension | Inspecting Flight payloads interactively while proxying traffic through Burp, during manual security testing | Not a library — nothing to `import` or run unattended in a crawl/pipeline |
| [rsc-parser.vercel.app](https://rsc-parser.vercel.app) | Pasting one payload in a browser to eyeball its structure | No programmatic access, no batch/crawl use, nothing to search or diff |

If you need to *look at* one payload once, the web tool or Burp extension
is faster. If you need to *extract structured data reliably, from code,
across many pages and over time*, that's what `nextflight` is for.
`njsparser` is the closest direct alternative if you want a lower-level
parser and plan to build your own search/traversal layer on top yourself.

## FAQ

**How do I scrape data from a Next.js website in Python?**
Fetch the page's HTML (with `requests`, `httpx`, Scrapy, or
`FlightExtractor.from_url()`), then pass it to
[`extract()`](#quick-start). Search for the data you want by key names
(`find_by_keys`) rather than by guessing DOM structure or array
positions — see [Quick start](#quick-start) above.

**What is a Next.js Flight payload / RSC payload?**
It's the wire format Next.js's App Router uses to serialize React
Server Component output — rows of `id:json` (and a few other row kinds)
inside `self.__next_f.push([...])` script calls, with `$`-prefixed
references between rows so repeated data isn't duplicated. See
[How it works](#how-it-works) for the full grammar.

**How do I parse `self.__next_f.push` in Python?**
That's exactly what this library does — see [Install](#install) and
[Quick start](#quick-start). Hand-rolling a regex against `push()` calls
works until a chunk gets truncated, split across multiple `push()`
calls, or has a duplicated/empty id, all of which happen on real
production pages; `nextflight` implements the actual row grammar so
those cases don't silently corrupt your data.

**Does this work with the Next.js Pages Router (`__NEXT_DATA__`)?**
Yes — see [Pages Router support](#pages-router-support).
`detect_next_router()` tells you which one a page uses, and
`find_next_data()` parses the (already-plain-JSON) Pages Router blob
directly.

**Why not just use `BeautifulSoup`/XPath/regex on the raw HTML?**
Because the data you want usually isn't in the rendered HTML at all —
it's serialized separately in the Flight payload for client-side
hydration, so selecting DOM elements gets you at most a subset of what's
actually on the page. See [How it works](#how-it-works) and
[Comparison with other tools](#comparison-with-other-tools).

**Is scraping Next.js sites with this library legal?**
`nextflight` is a parser, not a bot-detection bypass tool or legal
opinion — it has no opinion on and provides no help with a target site's
terms of service, robots.txt, or applicable law in your jurisdiction,
which you're responsible for checking yourself. See
[`docs/anti-bot-cookbook.md`](docs/anti-bot-cookbook.md) for what it can
and can't help with technically.

**Does it work with Scrapy?**
Yes, natively — see [In a Scrapy spider](#in-a-scrapy-spider) and the
dedicated [`docs/scrapy.md`](docs/scrapy.md) for the middleware, item
pipeline, and spider mixin.

**What if Next.js changes this format?**
It's undocumented and unversioned, so it can change at any release —
see [A note on maintenance](#a-note-on-maintenance) for the disclosure
and mitigation (`next_version_hint()`, `parse_confidence()`, same-week
patch policy).

## A note on maintenance

This library's entire value depends on tracking an undocumented,
unversioned wire format that Next.js can change in any release. That's a
real risk to disclose up front, not just a disclaimer:

- New Next.js releases that change the wire format will get a same-week
  patch release with a clear changelog entry whenever the maintainer
  becomes aware of the change — see `next_version_hint()` and
  `known_formats.yaml` for the mechanism that tracks this.
- Bus factor: currently maintained by one person. If that changes,
  meaningfully, it'll be reflected here.
- If you rely on this in production, pin a version, watch releases, and
  consider `parse_confidence()` in your own monitoring — a dropping score
  over time on pages that used to parse cleanly is an early warning sign
  the format shifted before an official patch lands.

## Roadmap and design proposals

These are scoped as proposals, not shipped code, and are listed here so
the direction is visible rather than only living in an issue tracker:

- **Plugin system** for third-party row-type handlers and custom
  `$`-reference resolvers, so an unrecognized new row kind doesn't have to
  block on an official patch.
- **JS/TS port** for Node-based scraping stacks (Playwright/Puppeteer),
  sharing the same golden-file test corpus as this Python implementation
  so the parsing "spec" lives in language-agnostic tests rather than being
  owned by one codebase.
- **Hosted/self-hostable reference-crawl service**: periodically snapshot
  known sites' Flight formats and alert maintainers the moment Next.js
  changes something, closing the same-week patch loop automatically
  instead of relying on manual discovery.
- **Playwright/Selenium integration**: `FlightExtractor.from_page(page)`
  to grab fully-rendered HTML (including chunks populated after JS
  execution) directly from a live browser session.
- Anonymized-payload GitHub issue template, with accepted submissions
  wired into the regression test corpus.

## License

MIT
