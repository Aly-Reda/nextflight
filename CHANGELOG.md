# Changelog

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
