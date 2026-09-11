# Performance notes

## Methodology

Benchmarks live in `benchmarks/` (`generate_pages.py` builds synthetic
pages of three sizes; `run_benchmarks.py` times parse+resolve and
early-exit search with `timeit`; `check_regression.py` compares a run
against the checked-in `baseline.json`, used by CI's `benchmark` job).
Profiling was done with `cProfile` against the medium (~530KB) synthetic
page, 20 iterations, sorted by cumulative and internal time, to find
actual hot paths rather than guessing.

Pages are synthetic (structurally representative: numbered chunks,
`$`-refs, nested objects, occasional text rows) rather than captures of
a real site, since redistributing a real site's payload isn't something
this project can do. Real pages will vary, but the *shape* of the hot
path found here -- boundary-finding for bracketed JSON values -- is
inherent to the wire format itself, not an artifact of the synthetic
generator.

## What was actually slow

The first profiling pass (before any of this release's optimizations)
found `_read_balanced` -- a hand-rolled, pure-Python, character-by-
character bracket/quote-balance scanner used to find where a JSON
object/array row ends -- consuming roughly **40% of total parse+resolve
time** on the medium page. A second pass after fixing that found the
same pattern recurring in `_iter_push_payloads` (finding the boundary of
the outer `push([...])` call array), consuming most of what remained.

Neither was an algorithmic (O(n²)) bug -- both were a single O(n) scan
per row -- the cost was the *constant factor* of doing that scan in
interpreted Python, one character at a time, plus doing it as a
completely separate pass from the JSON decode that then had to happen
anyway afterward.

## The fix

Both call sites now try `json.JSONDecoder().raw_decode(text, idx)` first
-- the stdlib's own (C-accelerated) decoder, called in "find where this
value ends and decode it" mode -- and only fall back to the original
hand-rolled `_read_balanced` scan when `raw_decode` raises (malformed or
truncated input), which preserves the exact existing `strict`/`repair`
tolerance behavior for those cases unchanged. See `_fast_bracket_decode`
in `extractor.py`.

One design constraint this had to respect: the library explicitly (and
correctly) advertises and tests that constructing a `FlightExtractor`
does **not** eagerly decode every chunk's JSON -- a page with many
chunks a caller never looks at should not pay to decode all of them (see
`_LazyRawChunks`'s docstring, and
`tests/test_extractor.py::test_construction_does_not_eagerly_materialize_chunks`).
An early version of this optimization cached the decoded value
`raw_decode` produces as a side effect, which is free for *finding the
boundary* but would have meant every chunk on the page got decoded and
held in memory regardless of whether anything ever looked at it --
silently breaking that contract. The shipped version discards the
decoded value in the normal (non-strict) case and only reuses it when
`strict=True`, which was already eagerly materializing every chunk by
design, so no laziness is lost either way.

## Results

Measured full `resolve_all()` time and `find_by_keys()` time-to-first-
match, before vs. after, on the three benchmark sizes:

| Page size | Chunks | Size | `resolve_all()` before | after | speedup |
|---|---|---|---|---|---|
| small | 350 | 51 KB | 8.41 ms | 3.00 ms | ~64% faster |
| medium | 3,500 | 534 KB | 83.3 ms | 28.1 ms | ~66% faster |
| large | 21,000 | 3.3 MB | 530.3 ms | 178.4 ms | ~66% faster |

| Page size | `find_by_keys` (early match) before | after | speedup |
|---|---|---|---|
| small | 6.6 ms | 1.2 ms | ~82% faster |
| medium | 66.8 ms | 12.2 ms | ~82% faster |
| large | 442.8 ms | 72.9 ms | ~84% faster |

Throughput estimate for a Scrapy crawl (single-threaded parse cost per
response; Scrapy's own concurrency multiplies this across many in-flight
requests, so this is "cost per response," not a hard ceiling on
crawl-wide throughput): a typical product/listing page in the small-to-
medium range above now costs roughly **3-30ms of parse+resolve time**,
i.e. on the order of **35-300 pages/second** of pure parsing throughput
per worker before any network/IO time is even considered -- for
essentially every realistic Scrapy crawl, network latency and
target-site rate limits will dominate total crawl time long before
`nextflight`'s own parsing cost becomes the bottleneck.

Lazy `$`-ref resolution was verified (not just reviewed) to actually
skip resolving unrelated chunks: `find_by_keys` on the small page
resolves only 18 of 350 chunks before returning, confirmed via the new
`resolve_cache_hits`/`resolve_cache_misses` counters exposed on
`.stats()` (see
`tests/test_extractor.py::test_find_by_keys_lazy_resolution_measured_via_cache`).

## The Rust/pyo3 question

**Recommendation: not needed for this release, and not clearly justified
even as a future one.**

The two hot spots found were specific, addressable inefficiencies in
pure Python (a hand-rolled scanner duplicating work a C-accelerated
stdlib call already does) -- not a fundamental ceiling on pure-Python
performance for this workload. Fixing them purely in Python closed
60-85% of the gap depending on the operation, which is a larger jump
than a compiled-extension rewrite of the *same* algorithm would have
provided on top of the *unoptimized* baseline, and did so with zero new
build complexity, zero new wheels-per-platform to publish, and zero risk
to the "stdlib only, `pip install nextflight` just works everywhere"
promise that's core to this library's value proposition for a scraping
audience.

What remains as the dominant cost after this pass (`_resolve_value`'s
recursive `$`-ref walk, ~35-40% of remaining time per the profile) is
inherently tree-recursive, small-object-heavy Python work -- the kind of
workload where a Rust rewrite *can* help, but where the win is far more
modest than the "the current implementation duplicates work a C function
already does" class of bug just fixed, and where the packaging cost
(maintaining pyo3 builds across Python versions and platforms, or
falling back to a slower pure-Python path when a wheel isn't available
for some platform, defeating some of the purpose) is real and ongoing.

This should be revisited if: (a) a future profiling pass on a *real*,
large production page shows `_resolve_value` itself dominating by a wide
margin rather than being one cost among several, or (b) a user's
reported crawl-scale numbers show parsing genuinely bottlenecking a
crawl despite the numbers above (i.e., network/target-site limits are
*not* the bottleneck for their use case) -- but that hasn't been
demonstrated, and isn't the default expectation for a library whose
primary consumers are network-bound web scrapers.
