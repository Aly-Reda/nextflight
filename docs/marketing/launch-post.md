# Launch post draft — Show HN / r/Python

**Title options:**
- "Show HN: nextflight – parse Next.js's hidden JSON payloads without hardcoding array indices"
- "nextflight: a Python library for scraping Next.js App Router sites without breaking on every redeploy"

**Body:**

Next.js's App Router embeds page data as React Server Components
("Flight") payloads inside `<script>self.__next_f.push([...])</script>`
tags — not plain JSON, but a custom wire format with byte-length-prefixed
text rows and `$`-sigil references (`$3`, `$L41`, `$@20`) that Next.js
uses to dedupe repeated subtrees.

If you've ever scraped one of these pages, you've probably hardcoded
something like `data[3]["children"][0][3]["children"][3]["props"]` to get
at a price or title — and watched it break the next time the site
redeployed and the component tree shifted.

`nextflight` parses the actual row grammar (handling truncated payloads,
rows split across multiple `push()` calls, duplicate/empty chunk ids, all
observed on real production pages), resolves the `$`-refs, and lets you
search for the *shape* of data you want instead:

```python
from nextflight import extract

page = extract(response.text)
listing = page.find_by_keys({"price", "title"})
```

Zero required dependencies (stdlib only) — safe to drop into an existing
Scrapy/Zyte project without touching your dependency tree. Optional
extras (`orjson`, `pandas`, `httpx`, `pydantic`) unlock extra features if
you already have them installed.

Honest limitations up front: this tracks an undocumented, unversioned
wire format Next.js can change at any release. There's a
`next_version_hint()` / `known_formats.yaml` mechanism for detecting known
format variants, a `repair=True` mode for truncated responses, and a
`parse_confidence()` score so you can flag pages that need a closer look
— but "Next.js changed something and broke this" is a real, ongoing risk
of using any tool in this space, not just this one. Same-week patch
releases are the goal whenever that happens.

Repo: https://github.com/Aly-Reda/nextflight
Docs/README: (comparison with `njsparser` and other tools in the README)

Happy to answer questions about the wire format itself, or about specific
scraping use cases (price monitoring, job boards, SEO auditing).
