# Cookbook

Realistic, worked examples of common `nextflight` use cases. Each starts
from an already-fetched page (`response.text`, or an HTML file) — see the
README for fetching options (`FlightExtractor.from_url`, Scrapy, raw RSC
fetches, streaming).

## E-commerce price scraping

```python
from nextflight import extract, normalize_price

page = extract(response.text)

for product in page.find_all_by_keys({"price", "title"}, dedupe=True):
    price = normalize_price(product["price"])
    print(product["title"], price["amount"] if price else None, price["currency"] if price else None)
```

`dedupe=True` matters here: the same product often appears both in a
"related items" carousel and the main results grid, serialized twice at
different tree positions — without it you'd double-count.

For monitoring price changes over time:

```python
from nextflight import FlightExtractor, diff_pages

old = FlightExtractor.from_url(url)
# ...re-fetch after some time...
new = FlightExtractor.from_url(url)

diff_pages(old, new, id_key="sku")
# {"changed": {"items[sku='ABC123'].price": (2999, 2499)}, ...}
```

## Job-board scraping

Job boards commonly vary field names slightly between listing types
(full-time vs. contract, different regions) — `find_any_keys` handles
that better than requiring an exact key set:

```python
listings = page.find_any_keys({"salary", "compensation", "pay_range"})

for job in listings:
    title = job.get("title") or job.get("role_title")
    comp = job.get("salary") or job.get("compensation") or job.get("pay_range")
```

If a search comes back thinner than expected, check for a typo or a
build-specific field name first:

```python
page.suggest_similar_keys({"salary"})
# {"salary": ["salary_range"]}
```

## SEO / metadata auditing

Structured `<script type="application/ld+json">` data is often more
stable across redesigns than Flight data, since it's part of the site's
public SEO contract rather than an internal implementation detail — worth
trying before reaching into Flight chunks at all:

```python
from nextflight import find_json_ld

products = find_json_ld(response.text, type_="Product")
breadcrumbs = find_json_ld(response.text, type_="BreadcrumbList")
```

For a full audit across many pages, combine both: `find_json_ld` for
what's officially declared, `extract(...).find_by_keys(...)` for anything
the page actually renders with but doesn't declare in structured data —
diffing the two surfaces gaps in a site's SEO markup.

## Typed extraction for a known page shape

Once you know a site's shape well enough to write a schema for it,
`extract_as` skips the manual `dict.get(...)` calls:

```python
from dataclasses import dataclass
from nextflight import extract

@dataclass
class Listing:
    title: str
    price: int
    url: str

listing = extract(response.text).extract_as(Listing)
```
