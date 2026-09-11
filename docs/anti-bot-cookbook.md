# Anti-bot cookbook

`nextflight` only parses HTML/RSC payloads you already have — it has no
opinion on how you got them, and does nothing to help you evade
detection. This page is about not shooting yourself in the foot with
*parsing* errors that look like blocking, and about where an actual
blocker (Cloudflare, PerimeterX, etc.) tends to intersect with this
library's `from_url`/`from_rsc_url` helpers.

## First: is it actually a blocker, or a parsing gap?

Before assuming Cloudflare/PerimeterX/etc. is involved, check:

```python
page = extract(response.text)
page.stats()             # 0 chunks? keep reading below
page.parse_confidence()  # low score? see repair=True
detect_next_router(response.text)  # "unknown"? might not be Next.js-rendered at all
```

A `0`-chunk result from a real Next.js page is more often one of:
- A challenge/interstitial page (see below) instead of the real page.
- A response your HTTP client silently decompressed incorrectly (rare
  with `from_url`/`from_rsc_url`, which handle gzip/deflate/br
  transparently, but possible with a bespoke client).
- The site using the Pages Router (`__NEXT_DATA__`) instead of the App
  Router — check `detect_next_router()`.

## Common blockers and what actually helps

- **Cloudflare (challenge page / "Verify you are human")**: `from_url`'s
  stdlib request has no JS engine, so it cannot solve a JS challenge.
  Either fetch with a real browser (Playwright/Selenium — see the
  Playwright integration proposal in the README's roadmap) and hand
  `nextflight` the resulting `page.content()`, or use a
  challenge-solving proxy/service upstream of your request. `nextflight`
  itself has no role here beyond parsing whatever HTML you eventually get.
- **PerimeterX / DataDome / similar bot-management**: same shape of
  problem as Cloudflare — these gate the *response*, not the payload
  format. Get past the gate with your own tooling first; parse
  afterward.
- **Rate limiting / IP-based blocking**: not something this library can
  or should help with directly; use your own proxy/rate-limiting
  strategy. `FlightExtractor.from_url`'s `timeout`/`headers` params cover
  basic customization, but a serious crawl should use a real HTTP client
  (`requests`/`httpx`) with retries and proxy rotation, and pass
  `response.text` to `extract()`.

## RSC-specific gotchas

- `from_rsc_url`'s `RSC: 1` header request is itself sometimes treated as
  more suspicious than a normal page load by bot-management (it doesn't
  look like a browser navigating, it looks like an API call). If
  `from_rsc_url` gets blocked where a normal page load doesn't, fall back
  to `from_url` and pay the cost of the full HTML.
- Some deployments require a `Next-Router-State-Tree` header matching the
  specific route being "navigated" to/from; without it you may get a
  200 response with zero useful chunks rather than an explicit block.
  Grab this header once from a real browser's network tab (Network tab →
  the RSC fetch request → Request Headers) and reuse it — it's stable per
  route.

## What `nextflight` will not do

- It does not solve JS challenges, rotate proxies, spoof TLS fingerprints,
  or otherwise evade bot detection. Those are separate, harder problems
  with their own tooling; conflating them with payload parsing makes both
  halves worse to reason about and debug independently.
