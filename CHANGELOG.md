# Changelog

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
