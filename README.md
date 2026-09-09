# nextflight

A general-purpose parser for the data Next.js (App Router) embeds in
`<script>self.__next_f.push([...])</script>` tags — the React Server
Components "Flight" wire format. Works on **any** Next.js 13+ App Router
site, not just one particular project.

Instead of hardcoding array indices like `data[3]["children"][0][3]...`,
which break the moment a site's component tree reshuffles on redeploy,
`nextflight` resolves the `$`-sigil references Next.js uses internally
and lets you *search* for the shape of data you want.

## Install

```bash
pip install nextflight
```

## Quick start

The core workflow is two steps: hand it any HTML, see what keys are on
the page, then fetch the resolved JSON for whichever key you want.

```python
from nextflight import extract

# Step 1: send any HTML, get the list of keys (one per __next_f.push chunk)
page = extract(html_text)
print(page.keys())          # e.g. ['0', '1', '3f', '20', ...]

# Step 2: fetch the resolved JSON for a specific key
data = page["3f"]           # same as page.resolve_chunk("3f")
```

Chunk ids are arbitrary per build though (a redeploy can renumber them),
so in practice you'll usually skip straight to *searching* for the shape
of data you want instead of a specific id:

```python
from nextflight import extract

page = extract(html_text)

# Find the first object anywhere in the page that has all of these keys,
# wherever this build's component tree happened to put it:
listing = page.find_by_keys({"sections", "meta"})

# Find every node with a given @type (or any custom key):
products = page.find_by_type("Product")

# Or search with a fully custom predicate:
items = page.find_all(lambda n: isinstance(n, dict) and "price" in n)

# Or grab everything, fully dereferenced, and inspect by hand:
everything = page.resolve_all()
```

### Command line

For quick, no-script exploration of a page you've already saved (or a live URL):

```bash
nextflight page.html --keys sections,meta
nextflight https://example.com/product/123 --type Product
nextflight page.html --all > everything.json
```

### In a Scrapy / Zyte spider

```python
import scrapy
from nextflight import extract

class MySpider(scrapy.Spider):
    name = "my_spider"

    def parse(self, response):
        page = extract(response.text)

        items = page.find_all(
            lambda n: isinstance(n, dict) and "price" in n and "title" in n
        )
        for item in items:
            yield {
                "title": item.get("title"),
                "price": item.get("price"),
                "url": response.url,
            }
```

### Fetching a URL directly (no Scrapy needed)

```python
from nextflight import FlightExtractor

page = FlightExtractor.from_url("https://example.com/product/123")
product = page.find_by_keys({"price", "title"})
```

(`from_url` uses only the stdlib for quick one-off exploration. For
production crawling — retries, proxies, JS rendering, robots.txt — fetch
the page with your own HTTP client / Scrapy / Zyte and pass
`response.text` to `FlightExtractor(...)` / `extract(...)` instead.)

## API

- **`extract(html: str) -> FlightExtractor`** — shorthand constructor.
- **`FlightExtractor(html: str, *, strict: bool = False)`**
  - `.keys() -> list[str]` — every chunk id found on the page, in order.
  - `page["3f"]` / `.resolve_chunk("3f")` — the resolved JSON for one
    specific chunk id (`page[...]` raises `KeyError` if it doesn't exist;
    `resolve_chunk` returns `None`). `"3f" in page` and `for k in page`
    also work, like a dict.
  - `.resolve_all() -> dict` — every chunk, fully dereferenced.
  - `.find_all(predicate, root=None, max_results=None) -> list` — walk the
    resolved tree and collect every node matching `predicate`.
  - `.find_one(predicate, root=None) -> Any | None`
  - `.find_by_keys(required_keys, root=None) -> dict | None` — find the
    first dict containing all of `required_keys`.
  - `.find_by_type(type_value, key="@type", root=None) -> list` — find
    every dict whose `key` field equals `type_value`.
  - `.from_url(url, timeout=15.0, headers=None) -> FlightExtractor`
    (classmethod) — fetch and parse a URL using only the stdlib.
  - `strict=True` raises `FlightParseError` on a row that's neither valid
    JSON nor a recognizable `$`-reference marker, instead of silently
    keeping it as a raw string (useful while developing a new scraper;
    leave off in production so a handful of odd rows never take down
    extraction of everything else on the page).
- **`find_json_ld(html: str, type_: str | None = None) -> list`** — parse
  any `<script type="application/ld+json">` blocks on the page,
  optionally filtered by `@type`.
- **CLI**: `nextflight <file-or-url> [--keys a,b | --type Product | --all]`

No runtime dependencies — stdlib only (`json`, `re`, `urllib`, `argparse`)
— so it's safe to drop into any existing Scrapy/Zyte project without
touching the rest of your dependency tree.

### Upgrading from `nextjs-flight-extractor` / `NextFlightExtractor`

The old names still work but emit a `DeprecationWarning`:

| Old (0.1.x)                          | New (0.2.x)                     |
|---------------------------------------|----------------------------------|
| `from nextjs_flight_extractor import NextFlightExtractor` | `from nextflight import FlightExtractor` |
| `extractor.find_first(...)`           | `page.find_one(...)`            |
| `extract_json_ld(html, schema_type=…)`| `find_json_ld(html, type_=…)`   |

## Why not just `str.split('\n')`?

Two of the Flight row kinds break that assumption:

- **Text rows** (`id:T<hexByteLen>,<raw text>`) are byte-length-prefixed
  blobs, not newline-terminated, and can contain literal newlines or run
  directly into the next row's id with zero separator.
- **Module / preload rows** (`id:I[...]` / `:HL[...]`) need bracket-aware
  parsing.

`nextflight` implements the real row grammar, quote/escape aware, so it
holds up on both well-formed and truncated payloads (e.g. from a proxy
that cuts a response off mid-chunk).

## Building / publishing

### Manual (twine)

```bash
pip install build twine
python -m build              # produces dist/*.whl and dist/*.tar.gz
twine check dist/*           # validate metadata before uploading
twine upload dist/*          # publish to PyPI (or use --repository testpypi for a dry run)
```

### Automatic (GitHub Actions + PyPI Trusted Publishing)

This repo ships with `.github/workflows/ci.yml`, which:
- runs the test suite on every push/PR across Python 3.9–3.12,
- builds and validates the sdist/wheel,
- publishes to PyPI automatically whenever a tag like `v0.2.1` is pushed.

Publishing uses PyPI's **Trusted Publisher** flow — no API token stored in
GitHub secrets. One-time setup:

1. On [pypi.org](https://pypi.org), go to your project → *Publishing* →
   *Add a new publisher* (or, for a brand-new project name, do this from
   your PyPI account's "Trusted Publishers" management page before the
   project exists yet).
2. Fill in: Owner = your GitHub username/org, Repository = this repo's
   name, Workflow name = `ci.yml`, Environment name = `pypi`.
3. In your GitHub repo, go to *Settings → Environments*, create an
   environment named `pypi` (optionally require a manual approval before
   deploys, for extra safety).
4. Release a new version:
   ```bash
   # bump version in pyproject.toml and src/nextflight/__init__.py first
   git commit -am "Release v0.2.2"
   git tag v0.2.2
   git push origin main --tags
   ```
   The workflow builds, tests, and publishes automatically.


## License

MIT
