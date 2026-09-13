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
page.is_fallback_skeleton()         # near-empty but perfectly parsed? likely an ISR cold path, not a block
```

If a low `parse_confidence()` score is what tipped you off, check *why*
before reaching for anti-bot tooling — it could be a genuine bot-mitigation
page, but it's just as often something else entirely with a completely
different fix:

```python
from nextflight import detect_challenge_page, detect_deployment_protection, detect_draft_mode

vendor = detect_challenge_page(response)          # "cloudflare" / "akamai" / "datadome" / "perimeterx" / None
protected = detect_deployment_protection(response) # Vercel's own login wall, not a WAF at all
drafting = detect_draft_mode(response)             # your OWN session picked up a stray preview cookie
```

These three fingerprint checks (and `is_fallback_skeleton()` above) all
return an HTTP 200 with real-looking headers but the wrong body, and are
easy to conflate with each other or with a genuine parsing bug if you
only look at `parse_confidence()`. They call for entirely different
fixes:

| Signal | What it actually means | What helps |
|---|---|---|
| `detect_challenge_page()` returns a vendor | A real WAF/bot-mitigation interstitial | See "Common blockers" below — this is the genuine anti-bot case |
| `detect_deployment_protection()` is `True` | Vercel's own auth wall on a preview/protected deployment | Not a WAF at all — you need deployment-protection bypass credentials, or you're crawling the wrong (protected) environment |
| `detect_draft_mode()` is `True` | Your *own* scraping session picked up a `__prerender_bypass` cookie | Not a block at all — strip the cookie (`FlightSession(strip_draft_cookies=True)`, the default, or `NEXTFLIGHT_STRIP_DRAFT_COOKIES` on `FlightMiddleware`) |
| `is_fallback_skeleton()` is `True` | ISR `fallback:true`/`'blocking'` cold path — page just hasn't finished generating | Retry after a short delay, not immediately, and not with a different proxy/UA |

A `0`-chunk result from a real Next.js page is more often one of:
- A challenge/interstitial page (see below) instead of the real page.
- A response your HTTP client silently decompressed incorrectly (rare
  with `from_url`/`from_rsc_url`, which handle gzip/deflate/br
  transparently, but possible with a bespoke client).
- The site using the Pages Router (`__NEXT_DATA__`) instead of the App
  Router — check `detect_next_router()`.
- The site is a **static export** (`output: 'export'`) — `detect_next_router()`
  returns `"static_export"` for these; there's no server-rendered data
  payload to parse at all, by design, so a 0-chunk result here isn't a
  bug or a block, just plain HTML to scrape with ordinary selectors.

## Common blockers and what actually helps

- **Cloudflare (challenge page / "Verify you are human")**: `from_url`'s
  stdlib request has no JS engine, so it cannot solve a JS challenge.
  Either fetch with a real browser (Playwright/Selenium — see the
  Playwright integration proposal in the README's roadmap) and hand
  `nextflight` the resulting `page.content()`, or use a
  challenge-solving proxy/service upstream of your request. `nextflight`
  itself has no role here beyond parsing whatever HTML you eventually
  get — though `detect_challenge_page(response)` (0.4.2+) at least tells
  you *which* vendor you're looking at, so you're not guessing between
  "Cloudflare" and "a truncated response" from a bare confidence score.
- **PerimeterX / DataDome / similar bot-management**: same shape of
  problem as Cloudflare — these gate the *response*, not the payload
  format. Get past the gate with your own tooling first; parse
  afterward. `detect_challenge_page()` fingerprints all of these plus
  Akamai from the same call.
- **Rate limiting / IP-based blocking**: not something this library can
  or should help with directly; use your own proxy/rate-limiting
  strategy. `FlightExtractor.from_url`'s `timeout`/`headers` params cover
  basic customization, but a serious crawl should use a real HTTP client
  (`requests`/`httpx`) with retries and proxy rotation, and pass
  `response.text` to `extract()`. `get_rate_limit_headers(response)`
  (0.4.3+) at least surfaces `Retry-After`/`X-RateLimit-Remaining`
  before you hit a hard block, if the target sets them; pairing
  `FlightAutoThrottleMiddleware` (0.4.6, Scrapy) with a confidence-based
  early-warning signal is one option for slowing down proactively as
  responses start looking degraded, rather than only reacting to an
  outright 429/403 after the fact.

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

## Not a block at all: Draft Mode and deployment protection

Two "200 but not the real page" traps aren't anti-bot measures, and no
amount of proxy rotation or challenge-solving fixes them:

- **Draft Mode** (`detect_draft_mode()`, 0.4.3+): if a scraping session
  ever picks up a `__prerender_bypass`/`__next_preview_data` cookie (a
  stray redirect through a preview link, a shared cookie jar across
  requests), every subsequent request on that session silently bypasses
  the ISR cache and serves draft content instead of the published page —
  with no error, no unusual status code, nothing that looks like a
  block. `FlightSession` strips these automatically by default
  (`strip_draft_cookies=True`); `FlightMiddleware`'s
  `NEXTFLIGHT_STRIP_DRAFT_COOKIES` setting (also default `True`) does
  the same for a Scrapy cookie jar, with a priority-ordering caveat
  relative to `CookiesMiddleware` — see that setting's docstring.
- **Vercel Deployment Protection** (`detect_deployment_protection()`,
  0.4.2+): a preview (or protected production) deployment sitting behind
  Vercel's own login wall returns an HTTP 200 whose body is a generic
  "Authentication Required" page. This isn't a WAF and isn't solvable by
  anything in this section — you either need deployment-protection
  bypass credentials for that project, or you're pointed at the wrong
  (protected) environment entirely.

A related, non-blocking gotcha worth knowing about: Vercel Edge
Middleware commonly branches on request geolocation for pricing/currency/
inventory, so the *same* URL can legitimately return different data
depending on where the request appears to originate from.
`get_edge_geo_headers(response)` (0.4.3+) surfaces which geo variant a
given response actually was, so an unexpected price/currency difference
across a crawl doesn't get mistaken for a parsing bug or "the site is
inconsistent" when it's really just this.

## What `nextflight` will not do

- It does not solve JS challenges, rotate proxies, spoof TLS fingerprints,
  or otherwise evade bot detection. Those are separate, harder problems
  with their own tooling; conflating them with payload parsing makes both
  halves worse to reason about and debug independently.
