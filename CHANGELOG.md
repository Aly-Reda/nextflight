# Changelog

## 0.4.6

The P3 tier: polish and crawl-health monitoring, the lowest-urgency items
from the original 26-item scoping pass. This closes out that entire
document — every item across all four parts now has a corresponding
release (0.4.2 through 0.4.6).

### New: locale and bundler detection

- `detect_locale(html_or_url) -> str | None`: best-effort locale
  detection checking, in order, the rendered `<html lang="...">`
  attribute, a path-prefixed locale segment (`/en/...`), and a
  locale-coded subdomain (`en.example.com`) — the latter two validated
  against an ISO 639-1 code list so an arbitrary two-letter path segment
  isn't mistaken for a locale.
- `FlightExtractor.next_version_hint()` gained a `"bundler"` field
  (`"webpack"`, `"turbopack"`, or `"unknown"`) alongside the existing
  Next.js version range, so a `parse_confidence()` drop caused by a
  bundler switch (e.g. a project opting into Turbopack) can be correctly
  attributed instead of being mistaken for an actual wire-format break.
  **Minor shape change**: the returned dict has one new key; code doing
  exact dict-equality comparisons against the old shape needs updating.

### New: crawl-health monitoring (Scrapy)

- `FlightAutoThrottleMiddleware`: adjusts Scrapy's own per-domain
  download-slot delay based on a rolling nextflight parse-confidence
  trend — slows requests to a domain proactively when confidence starts
  trending down (often an early sign of an intermittent challenge page
  or a site starting to rate-limit), and relaxes back off once it
  recovers. Layers on top of AutoThrottle/a fixed `DOWNLOAD_DELAY`
  rather than replacing either.
- `NextflightSpiderMiddleware` + the new `schema_drift` signal: fires
  when a response's parse confidence drops significantly below its own
  running per-domain baseline, for in-flight alerting/graceful-stop
  logic — complements `NextflightStatsExtension`'s end-of-crawl summary
  with a live early-warning path.

### Verified: route groups

- Added regression tests confirming `(group)`-style App Router route
  segments behave as ordinary dict keys throughout `.shape()`/
  `find_by_keys()`/resolution — no code changes were needed; this was
  already correct, now it's pinned.

### CLI and docs catch-up

- The CLI gained `--version-hint`, `--locale`, `--meta-tags`,
  `--api-routes`, `--base-path`, `--error-digest`, and `--pagination`,
  exposing the corresponding HTML-only functions added across
  0.4.2–0.4.6 for quick exploration without writing Python. (Functions
  that need real response headers rather than just HTML text —
  `get_cache_status`, `detect_draft_mode`, `detect_middleware_rewrite`,
  rate-limit/geo headers — remain Python-API-only for now, since the CLI
  only ever fetches and holds onto HTML text, not the response object.)
- `docs/anti-bot-cookbook.md` was updated to cover `detect_challenge_page`,
  `detect_deployment_protection`, `detect_draft_mode`, and
  `is_fallback_skeleton` — it previously only discussed anti-bot
  blockers in the abstract, predating all of the concrete detection
  helpers this project shipped to address exactly that gap.

## 0.4.5

Streaming/Suspense boundary visibility and Partial Prerendering (PPR)
awareness — held for this release since they needed the most new
golden-file fixture work of the P2 tier, per the original scoping
document's own release plan.

### New: streaming/Suspense boundary visibility

- `FlightExtractor.streamed_chunks() -> list[str]`: chunk ids that
  arrived after the initial shell (i.e. in a `self.__next_f.push(...)`
  call other than the first one on the page), distinguishing "was
  present in the initial HTML" from "arrived later via streaming" once a
  `<Suspense>` boundary's data became ready. Inferred from `push()` call
  boundaries in an already-fully-buffered page — a real, if informal,
  invariant of how Next.js emits Flight data (the first call is always
  the initial shell; each later, separate call is appended only once its
  boundary resolves), not a live measurement. For a genuinely live
  stream where exact timing matters, `from_stream()` (0.3.x) already
  yields chunks as they actually arrive.

### New: Partial Prerendering (PPR) awareness

- `FlightExtractor.is_ppr_page() -> bool`: best-effort detector combining
  `streamed_chunks()` being non-empty with the presence of an async
  placeholder marker (`$@<id>`) in the raw row text — the two signals
  that, in practice, only co-occur on a PPR page. Documented as
  best-effort: PPR's dynamic holes use the same Suspense/streaming
  machinery as an ordinary `loading.tsx` boundary, so this can't
  perfectly distinguish "PPR" from "an ordinary streamed page" in every
  case.
- `FlightExtractor.static_vs_dynamic_chunks() -> dict`: splits chunk ids
  into `{"static": [...], "dynamic": [...]}` using `streamed_chunks()`
  as the split point, for a crawler that wants to cache the static shell
  aggressively and only re-fetch the dynamic part.

New golden-file fixture (`tests/fixtures/streaming-ppr-0.4.5/`) added to
the corpus, exercising a static shell referencing an async placeholder
whose data arrives in a second, separate `push()` call.

All backward compatible — three new read-only methods, no changes to
any existing behavior or return shape.

## 0.4.4

The P2 tier: real gaps that come up less often than the 0.4.2/0.4.3
correctness issues, but are still worth closing without dedicated
per-project workarounds — parallel/intercepting route recognition, App
Router API route discovery, cross-page dedupe on multiple fields, a
documented `meta` convention for action-chasing spiders, Open
Graph/Twitter meta-tag fallback extraction, and sitemap-based URL
discovery.

### New: route conventions and discovery

- `is_route_slot_key(key) -> bool` / `is_intercepting_route_segment(segment) -> bool`:
  recognize App Router `@slot` parallel-route keys and `(.)`/`(..)`/
  `(...)`/`(..)(..)` intercepting-route segments. Worth noting: these are
  purely for *identifying* the convention (diagnostics, `.shape()`-style
  display) — `find_by_keys()`/`find_all()`/`resolve_all()` already walk
  into a slot's nested content transparently with no changes needed,
  since they recurse by value regardless of key name.
- `find_api_routes(html) -> list[str]`: finds `fetch("/api/...")`-shaped
  calls to App Router Route Handlers, a separate mechanism from Server
  Actions, rounding out data-fetching discovery alongside
  `find_server_action_ids()`/`find_next_chunk_urls()`.
- `find_pagination_action`, from 0.4.3, already covers the "how do I
  find the next-page cursor" half of item 12's ask; see that release's
  changelog entry.
- `discover_urls_from_sitemap(base_url, session=None) -> list[str]`:
  fetches `/sitemap.xml` (following one level of `<sitemapindex>`
  nesting), for seeding `start_urls` on an App Router site with a
  dynamically-generated sitemap. Stdlib-only.
- `find_meta_tags(html) -> dict`: extracts Open Graph/Twitter Card
  `<meta>` tags into a flat dict — a third structured-data fallback
  source (Flight → JSON-LD → meta tags) alongside `find_json_ld`.

### New: Scrapy integration

- `FlightDedupeMiddleware` now accepts `NEXTFLIGHT_DEDUPE_KEYS` (a list)
  in addition to the existing `NEXTFLIGHT_DEDUPE_KEY` (a single string)
  — fingerprint several fields together (e.g. `id` + `price` + `title`)
  instead of just one. Setting both raises `NotConfigured`. Fully
  backward compatible: `.dedupe_key` (singular) remains a valid read-only
  accessor for the first configured key.
- `build_action_meta(action_id, router_state_tree=None, **extra) -> dict`
  / `read_action_meta(request_or_response) -> dict`: the documented
  convention for passing Server Action context through
  `Request.meta`/`Response.meta` in a callback chain, for action-chasing
  spiders not using `FlightSession` (0.4.2).

## 0.4.3

Closes the P1 "silent wrong data" / misclassification traps identified
alongside 0.4.2's P0 set — deployment/rendering-mode edge cases and
response-header signals that otherwise look identical to a genuine parse
failure, plus the missing `basePath`/multi-zone handling that silently
no-ops `find_next_chunk_urls()` on any non-default-configured site.

### New: deployment/rendering-mode detection

- `detect_next_router()` now returns `"static_export"` (a new, distinct
  value) for `output: 'export'` builds — a Pages Router export via its
  `__NEXT_DATA__.nextExport: true` flag, an App Router export via a
  chunk-URL fingerprint when neither Flight nor `__NEXT_DATA__` is
  present at all. Parsing "failure" against a static-export page isn't a
  bug; this lets a spider branch cleanly instead of misreading it as one.
- `FlightExtractor.is_fallback_skeleton() -> bool`: heuristic detection
  of an ISR `fallback: true`/`'blocking'` cold-path loading skeleton — a
  page that parses with full confidence but resolves to almost no data,
  easily confused with "the format broke" by `parse_confidence()` alone.
- `detect_draft_mode(response) -> bool`: checks for Next.js Draft Mode's
  `__prerender_bypass`/`__next_preview_data` cookies, which silently
  bypass the ISR cache for every subsequent request on a session that
  picks one up. `FlightSession` now strips these automatically after
  every `.get()` (`strip_draft_cookies=True`, the default); `FlightMiddleware`
  gained the matching `NEXTFLIGHT_STRIP_DRAFT_COOKIES` setting (also
  default `True` — see its docstring for a priority-ordering caveat
  relative to Scrapy's `CookiesMiddleware`).

### New: response-header helpers

- `get_cache_status(response) -> dict`: surfaces `x-nextjs-cache`
  (`HIT`/`MISS`/`STALE`) plus `Cache-Control`'s `s-maxage`/
  `stale-while-revalidate` and the `Age` header, for smarter re-fetch
  scheduling in `diff_pages`/`--watch` workflows.
- `detect_middleware_rewrite(response) -> str | None`: reads
  `x-middleware-rewrite`/`x-nextjs-rewrite` to surface the real URL a
  response was silently rewritten to serve, so a crawler doesn't store
  data under the wrong logical URL.
- `get_rate_limit_headers(response) -> dict`: `Retry-After`/
  `X-RateLimit-Remaining`/`X-RateLimit-Reset` (or `RateLimit-*`
  equivalents) in one call.
- `get_edge_geo_headers(response) -> dict`: Vercel's
  `x-vercel-ip-country`/`-city`/`-region` headers in one call, for
  logging which geo variant of a page a crawl actually received.
- `find_error_digest(html) -> str | None`: extracts a Server Component
  error's `digest` correlation id from a rendered error page, for
  logging `"parse failed, digest=..."` instead of just "empty page."

### New: pagination and `basePath`/multi-zone support

- `find_pagination_action(page) -> dict | None`: finds the common
  cursor-pagination shape (`hasNextPage`, `cursor`/`endCursor`/
  `nextCursor`) via `find_by_keys` under the hood, pre-packaged so each
  project doesn't reinvent this detection per site.
- `find_next_chunk_urls()` gained a `base_path=` filter argument and now
  matches chunk URLs under *any* prefix by default (previously it only
  matched a bare `/_next/...` path and silently returned nothing at all
  on a site using `next.config.js`'s `basePath` or a multi-zone
  deployment). `detect_base_path(html) -> str` auto-detects the prefix in
  use.

All backward compatible. `find_next_chunk_urls()`'s broadened default
match is additive (more sites now return results instead of an empty
list) rather than a behavior change for sites it already worked on.

## 0.4.2

Closes the biggest functional/correctness gaps identified in a review of
real-world Scrapy/`requests` usage: calling (not just discovering) Server
Actions, `/_next/image` URL handling, distinguishing bot-mitigation
challenge pages and Vercel's deployment-protection wall from ordinary
parse failures, session continuity for multi-step Server Action
workflows, and a correctness fix for Flight's non-JSON value encodings.

### Fixed: RSC special-value decoding (was silently wrong)

- `FlightExtractor.resolve_chunk()`/`resolve_all()` previously passed
  Flight's sigil-encoded non-JSON values through as **raw, unresolved
  strings** — e.g. a `Date` field came back as the literal string
  `"$D2024-01-05T00:00:00.000Z"` instead of a `datetime.datetime`. Worse,
  the existing `$`-ref regex mis-split ISO date strings on their internal
  colons, so this wasn't just "unresolved," it was silently malformed.
  Now decoded automatically: `$D<isoString>` → `datetime.datetime`,
  `$Q<ref>` (Map) → `dict`, `$W<ref>` (Set) → `list`, `$n<digits>`
  (BigInt) → `int`.
- **This changes what already-shipped `resolve_*` calls return** for any
  page with a Date/Map/Set/BigInt field. Pass
  `FlightExtractor(html, decode_rsc_values=False)` to keep the pre-0.4.2
  raw-string behavior if existing code depends on it.
- New golden-file fixture (`tests/fixtures/rsc-values-0.4.2/`) pins the
  decoded output going forward.

### New: Server Action invocation

- `call_server_action(url, action_id, args=(), *, router_state_tree, session=None, headers=None, cookies=None)`:
  actually *calls* a Server Action over plain HTTP and parses the
  response — the missing counterpart to 0.4.1's
  `find_server_action_ids()`, which only discovers ids. POSTs to the
  page's own URL (there's no separate action endpoint) with the required
  `Accept`/`Next-Action`/`Next-Router-State-Tree` headers set
  automatically. Encodes `args` as a plain JSON array, or
  `multipart/form-data` if any argument looks like a file. Works with a
  bare stdlib request or a `requests.Session`-like `session=`.
- `ActionNotFoundError` (a new `FlightRequestError` base class): raised
  instead of an opaque HTTP 500 when the server no longer recognizes the
  action id — almost always a stale id from before a redeploy.
- `capture_router_state_tree_hint(html="")`: an explicitly-labeled,
  best-effort fallback for the one input `call_server_action` can't
  reliably derive from static HTML alone — a real browser capture of
  `Next-Router-State-Tree` is still the recommended source per route.
- `FlightSession`: wraps cookie *and* router-state-tree continuity across
  `.get()` → `.call_action()` → `.call_action()` chains, the same role
  `requests.Session` plays for cookies alone. Stdlib-only by default;
  accepts a `requests.Session`-compatible `backend=` for connection
  pooling/proxies/retries already configured on it.

### New: `/_next/image` URL helpers

- `resolve_next_image_url(next_image_url) -> str`: decode a
  `/_next/image?url=...&w=...&q=...` proxy URL back to its original
  source URL.
- `build_next_image_url(base_url, image_url, *, width, quality=75) -> str`:
  build a proxy URL for a specific resolution.
- `resolve_next_image_srcset(html_or_tag) -> list[dict]`: decode every
  candidate in an `<img srcset="...">` into `{"width", "url"}` entries.
- All three are pure `urllib.parse` — no new dependency.

### New: bot-mitigation / access-wall fingerprinting

- `detect_challenge_page(response) -> str | None`: fingerprints
  Cloudflare, Akamai, DataDome, and PerimeterX challenge/interstitial
  pages served with an HTTP 200 — a more actionable signal than a low
  `parse_confidence()` score alone, since it tells a retry policy *why*
  the page looks wrong (rotate proxy/UA) rather than just *that* it does
  (retry plainly).
- `detect_deployment_protection(response) -> bool`: fingerprints Vercel
  Deployment Protection's own login-wall page — the same "200 but not
  real content" trap, checked separately since it's a distinct,
  Vercel-specific signature rather than a general WAF product's.

All backward compatible except the `decode_rsc_values` behavior change
noted above, which is opt-out via a constructor flag.

## 0.4.1

Extends 0.4.0's Scrapy integration and adds Pages Router / Server Action
support, driven by patterns found reviewing a real production Scrapy
project (~119 spiders scraping Next.js car-listing sites). All backward
compatible — no changes to any existing public API.

### New: Pages Router & Server Actions

- `find_page_props(html)`: shortcut for the single most-repeated Pages
  Router line seen across real scrapers —
  `json.loads(<script id="__NEXT_DATA__">.text())['props']['pageProps']`
  — returning `None` gracefully instead of raising on a missing or
  malformed block.
- `find_server_action_ids(html)`: finds Next.js Server Action ids
  (`createServerReference("...")`) embedded in a page or JS chunk's raw
  text, for sites that fetch data via a server action (invoked with a
  `Next-Action: <id>` POST header) rather than a discoverable API route.
- `find_next_chunk_urls(html, pattern=None)`: finds `/_next/static/
  chunks/*.js` bundle URLs referenced by a page, for locating the
  specific chunk a server action id is defined in.

### New: link discovery beyond rendered HTML

- `FlightExtractor.find_urls(keys=None, pattern=None)`: finds
  URL-shaped strings in the Flight JSON itself — catches navigation for
  sites that route via `onClick`/client-side routing rather than a real
  `<a href>`, which Scrapy's own HTML-based `LinkExtractor` can't see.
- `NextflightSpiderMixin.follow_flight_urls(response, keys=, pattern=,
  callback=, **kwargs)`: Scrapy convenience wrapping the above, yielding
  `response.follow(...)` for each result with relative URLs resolved.

### New: Scrapy middleware family

- `FlightRetryMiddleware`: auto-retries a response whose Flight data
  parsed with suspiciously low `parse_confidence()` — catches
  challenge/interstitial pages masquerading as a normal HTTP 200, which
  status-code-based retry can't see. Uses Scrapy's own
  `get_retry_request` helper; never retries a response with zero Flight
  chunks. Requires Scrapy >= 2.5.
- `NextflightStatsExtension`: surfaces parse-health metrics
  (`nextflight/responses_with_flight_data`, `avg_parse_confidence`,
  `version_hint/<range>`, `stream/stopped_early`) in Scrapy's own
  end-of-crawl stats dump.
- `.extract_as()` now also accepts a `scrapy.Item` subclass, detected
  only if Scrapy is already installed (no new hard dependency).
- `recommended_settings()` gained `retry=`/`stats=` flags.
- Verified and documented: `NextflightStatsExtension`,
  `FlightRetryMiddleware`, and `FlightStreamingMiddleware` all work
  without `FlightMiddleware` also being enabled (they read Flight data
  through their own internal path). Also documented, and pinned with a
  test, that `FlightMiddleware` patches `scrapy.http.Response` at the
  class level with no "uninstall" — once constructed once in a process,
  `response.flight` keeps working for every response for that process's
  remaining lifetime.
- **Verified against actual package source** (not assumed):
  `FlightStreamingMiddleware`'s `bytes_received`-based early-stop does
  **not** engage for requests routed through `scrapy-zyte-api`'s
  Addon/automap or `scrapy-impersonate` — neither routes through the
  Twisted-based download handler that emits that signal. Documented as
  a compatibility note in `docs/scrapy.md` so projects using either
  aren't surprised when streaming doesn't speed those requests up.

### Testing

- Fixed a test-isolation bug in the Scrapy test suite discovered while
  writing the above verification: a `Response` subclass with a custom
  `__setattr__` override doesn't actually force the middleware's
  fallback-cache path, since `object.__setattr__()` bypasses a
  subclass's own `__setattr__` entirely — replaced with a genuinely
  slot-restricted class that forces the intended path deterministically
  on every Python/Scrapy version.
- Fuzz-tested all new functions (`find_page_props`, `find_server_action_ids`,
  `find_next_chunk_urls`, `find_urls`) against 500-5000 randomized/adversarial
  inputs each (empty strings, bytes, wrong types, malformed JSON,
  response-like objects) — zero crashes.
- Re-ran the existing hypothesis fuzz suite at 5000 examples (up from
  the default ~300) against the repair/parse_confidence/version_hint
  path with no new findings.

### Docs

- `docs/scrapy.md` gained sections for all of the above, plus a "Wiring
  this into an existing alerting extension" pattern for connecting
  `parse_confidence()` to a project's existing Slack/Telegram/email
  alerting rather than only Scrapy's own stats dump.

## 0.4.0

Large release, delivered in two rounds of work, covering parsing

robustness, performance, Scrapy-native ergonomics, and correctness
hardening. All backward compatible — no changes to `extract()`,
`FlightExtractor(html, strict=, repair=)`, `.find_all()`/`.find_one()`/
`.find_by_keys()`/`.find_all_by_keys()`/`.find_any_keys()`/
`.find_by_key_pattern()`/`.find_by_type()`/`.find_text()`/`.get()`/
`.select()`, `.resolve_chunk()`/`.resolve_all()`/`.resolve_json()`/
`.resolve_html()`/`.resolve_text()`, `.from_url()`/`.from_rsc_url()`/
`.from_stream()`, `.diff()`, `.shape()`, `.parse_confidence()`,
`.next_version_hint()`, `.suggest_similar_keys()`, `.extract_as()`,
`find_json_ld()`/`find_next_data()`/`detect_next_router()`/
`diff_pages()`, `normalize_price()`/`clean_text()`/`parse_date()`, or
`AsyncFlightExtractor`.

### Performance

- **~64-66% faster `resolve_all()`** and **~82-84% faster `find_by_keys()`
  time-to-first-match**, measured on synthetic small (~50KB)/medium
  (~530KB)/large (~3.3MB) pages — see `docs/performance.md` for full
  before/after numbers and profiling methodology. Root cause: two hot
  spots (`_split_rows`'s and `_iter_push_payloads`'s row/call-boundary
  detection) were hand-rolled, pure-Python character-by-character
  bracket scanners; both now try `json.JSONDecoder().raw_decode()` (a
  single C-accelerated pass that finds the boundary *and* decodes) first,
  falling back to the original scanner only for malformed/truncated
  input, so `strict`/`repair` tolerance is unchanged.
- Verified (not just reviewed) that `find_by_keys`/`find_one` skip
  resolving unrelated chunks on an early match, via new
  `resolve_cache_hits`/`resolve_cache_misses` counters exposed on
  `.stats()`.
- `orjson`-accelerated encoding (`_json_dumps`/`_json_dumps_sorted`) for
  `.to_json()` and `find_all_by_keys(dedupe=True)`'s fingerprinting, used
  automatically when `orjson` is installed.
- `benchmarks/` directory (`generate_pages.py`, `run_benchmarks.py`,
  `check_regression.py`, checked-in `baseline.json`) plus a CI job that
  runs the suite and flags a >25% regression on any page size.
- Rust/pyo3-accelerated tokenizer evaluated and **not adopted** for this
  release: the actual hot spots were pure-Python inefficiencies with a
  much cheaper fix (see above), not an inherent ceiling — see
  `docs/performance.md` for the full reasoning and what would change
  this recommendation.

### Parsing robustness

- `FlightExtractor(html, repair=True)`: best-effort recovery of chunks
  truncated mid-object, separate from and mutually exclusive with
  `strict=True`.
- `FlightExtractor.from_stream(chunks)`: incremental parsing of an
  iterable of HTML fragments/bytes, yielding `(chunk_id, value)` as rows
  complete.
- `FlightExtractor.from_page(playwright_page)`: build from a Playwright
  page's fully-rendered HTML, for sites that only populate later chunks
  after client-side JS runs. Accepts a Playwright sync `Page` or a plain
  HTML string (including `await page.content()` from the async API);
  raises a clear `TypeError` if handed an async `Page` object directly
  rather than silently misbehaving.
- `.next_version_hint()` + bundled `known_formats.yaml`: best-effort
  detection of which Next.js version range produced a given payload.
- Golden-file regression corpus under `tests/fixtures/` (13.0/13.4/14.1/
  15.0 synthetic fixtures pinned against current output) and
  `hypothesis`-based fuzz testing (`tests/test_fuzz.py`) targeting
  `_split_rows`, `_read_balanced`, `_attempt_repair`, `_close_unbalanced`.
  The fuzzer found and fixed a real bug: `_close_unbalanced` produced
  invalid JSON for a string ending in a dangling escape backslash
  (`..."\`), closing it with a bare `"` that JSON parses as an *escaped*
  quote rather than a terminator.

### Observability

- `.parse_confidence()`: score + breakdown of cleanly-parsed vs.
  raw-fallback vs. repaired chunks.

### API ergonomics

- `.suggest_similar_keys(required_keys)`: fuzzy-matches against keys
  actually present in the resolved tree when a search comes back empty.
- `.extract_as(Model)`: coerce the first matching dict into a
  `dataclasses.dataclass` or (optional `pydantic` extra) `BaseModel`.
- `dedupe=True` on `.find_all_by_keys()`: collapse the same object
  serialized twice at different tree positions.
- `AsyncFlightExtractor`: discoverable alias for the async construction
  path (`.from_url_async()` already existed on `FlightExtractor`).

### Scrapy integration

- `nextflight.scrapy_middleware.FlightMiddleware`: lazy `.flight`
  property on every `Response`; reads `NEXTFLIGHT_STRICT`/
  `NEXTFLIGHT_REPAIR`/`NEXTFLIGHT_DEDUPE` from settings.py. Also caches
  `next_version_hint()` per domain rather than recomputing it (a full
  page-HTML marker scan) on every response of a large same-site crawl.
- `nextflight.scrapy_middleware.FlightStreamingMiddleware` (new): opt-in
  (`NEXTFLIGHT_STREAMING = True`) processing of a response's bytes AS
  THEY ARRIVE, via Scrapy's `bytes_received` signal, instead of waiting
  for the full download to complete. With `NEXTFLIGHT_STREAM_KEYS` set,
  cancels the rest of the download the moment a match appears (via
  `scrapy.exceptions.StopDownload`) — e.g. a 3MB page where the wanted
  field is in the first 50KB no longer needs the other ~2.95MB
  downloaded at all; the match is exposed as
  `response.meta["nextflight_match"]`. Without stream keys configured,
  still pre-warms `response.flight` from bytes decoded incrementally
  during the download (a smaller but real win — parsing overlaps network
  I/O instead of strictly following it). Requires Scrapy >= 2.6; disables
  itself via `NotConfigured` on older Scrapy. Handles multi-byte UTF-8
  characters split across arbitrary byte-chunk boundaries correctly
  (verified with a byte-at-a-time feed test and 200 randomized
  chunk-size fuzz trials), and correctly falls back to parsing the full
  `response.text` if its own tracking buffer hits
  `NEXTFLIGHT_STREAM_MAX_BYTES` before finding a match, rather than
  silently returning a truncated result for a response that Scrapy
  actually finished downloading in full. Supports a per-request
  override of `NEXTFLIGHT_STREAM_KEYS` via
  `request.meta["nextflight_stream_keys"]` for spiders crawling several
  URL patterns that each need different fields watched for.
- `nextflight.scrapy_middleware.FlightRSCMiddleware` (new): transparently
  fetches a page's lightweight RSC payload (`RSC: 1` header) instead of
  full HTML for eligible requests (opt-in per-request via
  `meta={"nextflight_rsc": True}`, or crawl-wide via
  `NEXTFLIGHT_RSC_URL_PATTERN`) — often a fraction of a full page's
  size. `response.flight` works unchanged on the result via the
  library's existing raw-RSC auto-detection.
- `nextflight.scrapy_middleware.FlightDedupeMiddleware` (new): spider
  middleware that deduplicates dict items across the *whole* crawl
  (`NEXTFLIGHT_DEDUPE_ACROSS_PAGES = True`), catching duplicate listings
  that show up on more than one page — beyond what
  `find_all_by_keys(dedupe=True)`'s single-page dedupe can see.
  Non-dict items (`Request` objects, typed `Item`s) always pass through
  untouched.
- `nextflight.scrapy_middleware.recommended_settings()` (new): a
  starting-point settings preset (`streaming=`, `rsc=`,
  `dedupe_across_pages=` flags) for bootstrapping a new project's
  `nextflight` + Scrapy wiring.
- Fixed a latent bug (present since `FlightStreamingMiddleware` was
  added): `NotConfigured` was only importable inside a Scrapy
  >=2.6-gated `try` block, so on older Scrapy any *other* middleware
  raising it (`FlightDedupeMiddleware`, which has no such version
  requirement) would raise a local fake exception class Scrapy's real
  middleware-loading logic wouldn't recognize, crashing instead of
  gracefully skipping the middleware. `NotConfigured` is now imported
  unconditionally, independent of the streaming-specific availability
  check.
- `docs/scrapy.md` gained a "Which approach should I use?" decision
  guide comparing every Scrapy integration option (middleware, mixin,
  streaming, RSC, item pipeline, cross-page dedupe, plain `extract()`)
  by when each is actually worth using.
- `FlightItemPipeline`: declare a required key set once
  (`NEXTFLIGHT_PIPELINE_KEYS`), get every matching dict via
  `matches_for_response(response)` without repeating `find_all_by_keys`
  boilerplate in each callback.
- `NextflightSpiderMixin`: `self.flight(response)` shorthand (works with
  or without `FlightMiddleware` installed) and
  `self.log_version_hint(response)` (logs `next_version_hint()` once per
  domain via the spider's own logger).
- **Fixed a real memory-leak / correctness bug** in the middleware's
  fallback cache: it was a plain `{id(response): extractor}` dict that
  never shrank and could return a *different* page's cached extractor
  after `id()` reuse post-garbage-collection. Also discovered while
  fixing this that the "fallback" path isn't a rare edge case as
  originally documented — real Scrapy `Response`/`HtmlResponse` objects
  use `__slots__` and reject arbitrary instance attributes on every
  normal response, so this path is exercised on essentially every
  Scrapy crawl. Now a `weakref.WeakKeyDictionary`, verified via a GC test
  and a genuine multi-threaded concurrency test.
- Verified `.flight` never raises on non-Next.js responses (redirected
  error pages, non-HTML responses, empty bodies) unless `strict=True`.
- `docs/scrapy.md`: complete runnable example spider, item pipeline
  usage, `scrapy shell` support notes, and a "why this beats hand-written
  XPath/regex" pitch.

### Data quality

- `nextflight.normalize_price()`, `.clean_text()`, `.parse_date()`:
  dependency-free helpers for messy scraped values.

### Docs

- Comparison section against `njsparser`, the Burp Suite RSC parser
  extension, and `rsc-parser.vercel.app`.
- `docs/cookbook.md` (e-commerce, job-board, SEO auditing examples),
  `docs/anti-bot-cookbook.md`, `docs/performance.md`, `docs/scrapy.md`.
- Maintenance-sustainability note (bus factor, same-week patch policy).
- Roadmap/design-proposal section for a plugin system, a JS/TS port, and
  a hosted reference-crawl service — explicitly scoped as proposals, not
  shipped code.
- CI: added a `mypy` job (package now type-checks cleanly, not under
  `--strict`) and a `benchmark` job, and the test job now also runs the
  full suite with all optional extras (`orjson`, `pandas`, `httpx`,
  `pydantic`, `scrapy`) installed, not just the stdlib-only path.

### Explicitly out of scope for this release

Interactive TUI mode for the CLI (the CLI's existing `--watch` polling
mode was already present), standalone binary packaging (pyinstaller/
shiv), conda-forge/Homebrew packaging, and a JS/TS port sharing the
golden-file test corpus — all still listed as roadmap items in the
README, not silently dropped.

## 0.3.x

Prior releases — see git history.
