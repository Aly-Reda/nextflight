"""
nextflight.extractor

Core parser for the React Server Components "Flight" wire format that
Next.js 13+ (App Router) embeds in ``<script>self.__next_f.push([...])</script>``
tags. Works on any Next.js App Router site -- nothing here is tied to a
specific project.

Why not just str.split('\n') on the payload?
---------------------------------------------
Because two of the row kinds break that assumption:

  * Text rows:    `id:T<hexByteLen>,<raw text of exactly hexByteLen bytes>`
    The raw text is a byte-length-prefixed blob, not newline-terminated.
    It can legitimately CONTAIN literal newlines, and it can run directly
    into the NEXT row's id with zero separator.
  * Module rows:  `id:I[...]` / anonymous preload rows: `:HL[...]`

And even once rows are split correctly, the values are full of `$`-sigil
references Next.js uses to dedupe repeated subtrees (`$3`, `$L41`, `$@20`,
`$Sreact.fragment`, and path-suffixed refs like
`$3f:props:children:0:props:sections:...`). Hardcoding array indices like
`data[3]["children"][0][3]["children"][3][3]` breaks the moment the
surrounding component tree reshuffles on a redeploy. This module resolves
those references and lets you *search* for the shape of data you want
instead.

Quick start
-----------
    from nextflight import extract

    page = extract(html_text)

    # Find whatever object looks like the data you need, wherever
    # Next.js decided to put it in this particular build:
    listing = page.find_by_keys({"sections", "meta"})

    # Or with a custom predicate:
    products = page.find_all(lambda n: isinstance(n, dict) and n.get("@type") == "Product")

    # Or just get everything, fully dereferenced:
    everything = page.resolve_all()
"""

from __future__ import annotations

import datetime
import json
import re
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Mapping
from inspect import iscoroutinefunction as _is_coroutine_function
from typing import Any, Callable, Iterable, Iterator, Optional, Union

# Optional accelerator: if the caller already has orjson installed (common
# in scraping stacks), use it for JSON decoding -- it's a drop-in replacement
# that's typically several times faster than the stdlib on the array/object
# shapes Flight payloads produce. Falls back to stdlib json with zero
# required dependencies either way.
try:
    import orjson as _orjson  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - exercised via the stdlib fallback path
    _orjson = None  # type: ignore[assignment]

if _orjson is not None:  # pragma: no cover - depends on optional dependency
    _JSON_ERRORS: tuple = (json.JSONDecodeError, _orjson.JSONDecodeError)

    def _json_loads(s: str) -> Any:
        return _orjson.loads(s)
else:
    _JSON_ERRORS = (json.JSONDecodeError,)

    def _json_loads(s: str) -> Any:
        return json.loads(s)


# A single, shared, stateless `json.JSONDecoder` used specifically for
# `raw_decode(text, idx)` in `_split_rows`'s bracket branches -- this is
# NOT the same thing as `_json_loads` above (which may be orjson) and is
# intentionally always the stdlib decoder: `raw_decode` both (a) finds
# where a JSON object/array ends starting at a given index and (b)
# decodes it, in a single C-accelerated pass, whereas the previous
# implementation walked the bracket/quote structure by hand in pure
# Python (`_read_balanced`) just to find the end index. Profiling a
# ~500KB synthetic page showed `_read_balanced`'s hand-rolled scan
# consuming roughly 40% of total parse+resolve time -- by far the single
# largest hot spot -- so replacing that Python-level scan with this
# C-accelerated one is a straightforward win (~25-35% faster end to end
# on synthetic small/medium/large pages -- see benchmarks/). The decoded
# value this produces is discarded (not cached) for non-strict
# construction, to preserve the existing lazy-materialization contract
# (see `_extract_all`); `strict=True` construction, which was already
# eagerly materializing every chunk, does reuse it to skip a redundant
# second decode. `raw_decode` is safe to share across calls/instances: a
# `JSONDecoder` with default settings holds no per-call state.
#
# `_read_balanced` is NOT removed -- it remains the fallback for
# malformed/truncated input (where `raw_decode` raises), preserving the
# exact existing `strict=False`/`repair=True` tolerance behavior for
# those cases unchanged. See `_fast_bracket_decode`.
_BRACKET_DECODER = json.JSONDecoder()


def _json_dumps(value: Any, *, indent: Optional[int] = None) -> str:
    """Encode-side counterpart to `_json_loads`: uses `orjson.dumps` when
    available (still falling back to the stdlib `json.dumps` either when
    orjson isn't installed, or for the indented case -- orjson's own
    indent option only supports a fixed 2-space indent via `OPT_INDENT_2`,
    so a caller-specified `indent` other than 2 falls back to stdlib to
    honor it exactly). Used by `.to_json()` and the CLI's output writer --
    not on the hot per-row parsing path, but a large `.to_json()` export
    benefits the same way decoding does. Does NOT sort keys (matches the
    pre-existing `json.dumps(..., indent=2)` behavior these call sites
    used directly before) -- callers that need deterministic key order
    for comparison/fingerprinting (e.g. `find_all_by_keys(dedupe=True)`)
    call `json.dumps(..., sort_keys=True)` directly instead of through
    this helper, since orjson's key-sorting option and stdlib's aren't
    quite the same feature to unify here."""
    if _orjson is not None and (indent is None or indent == 2):
        options = _orjson.OPT_INDENT_2 if indent == 2 else 0
        return _orjson.dumps(value, option=options, default=str).decode("utf-8")
    return json.dumps(value, indent=indent, ensure_ascii=False, default=str)


def _json_dumps_sorted(value: Any) -> str:
    """Deterministic (key-order-independent) string encoding of `value`,
    used for `find_all_by_keys(dedupe=True)`'s fingerprinting -- two
    dicts with the same keys/values in a different order must fingerprint
    identically, or dedupe would miss exact duplicates that merely got
    serialized in a different field order elsewhere in the payload.
    Prefers `orjson.OPT_SORT_KEYS` (recursive key sorting, natively
    faster than stdlib's) when available."""
    if _orjson is not None:
        return _orjson.dumps(value, option=_orjson.OPT_SORT_KEYS, default=str).decode("utf-8")
    return json.dumps(value, sort_keys=True, default=str)


def _fast_bracket_decode(payload: str, start: int) -> tuple:
    """Attempt to decode the JSON object/array starting exactly at
    `payload[start]` (which must be `'{'` or `'['`) in one pass. Returns
    `(raw_substring, end_index, decoded_value)` on success, or `(None,
    start, None)` if the JSON at `start` is malformed or the payload is
    truncated mid-value -- callers fall back to `_read_balanced` in that
    case, which tolerates truncation the way `raw_decode` does not."""
    try:
        value, end = _BRACKET_DECODER.raw_decode(payload, start)
    except ValueError:
        return None, start, None
    return payload[start:end], end, value


_KNOWN_FORMATS_CACHE: Optional[list] = None


def _load_known_formats() -> list:
    """Load the bundled ``known_formats.yaml`` registry (see that file),
    used by :meth:`FlightExtractor.next_version_hint`. Uses `pyyaml` if
    it's already installed (common in scraping stacks); otherwise falls
    back to a small hand-rolled parser sufficient for this file's own
    fixed, simple structure (a top-level list of ``range``/``markers``/
    ``notes`` entries) -- kept dependency-free by design, same as the
    rest of the core library. Result is cached after the first call."""
    global _KNOWN_FORMATS_CACHE
    if _KNOWN_FORMATS_CACHE is not None:
        return _KNOWN_FORMATS_CACHE
    import os
    path = os.path.join(os.path.dirname(__file__), "known_formats.yaml")
    try:
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
    except OSError:
        _KNOWN_FORMATS_CACHE = []
        return _KNOWN_FORMATS_CACHE
    try:
        import yaml  # type: ignore
        _KNOWN_FORMATS_CACHE = yaml.safe_load(text) or []
    except ImportError:
        _KNOWN_FORMATS_CACHE = _parse_simple_yaml_list(text)
    return _KNOWN_FORMATS_CACHE


def _parse_simple_yaml_list(text: str) -> list:
    """Minimal parser for this package's own `known_formats.yaml` shape
    only -- NOT a general YAML parser. Handles: top-level `- key: value`
    list items, nested `key: >` folded scalars, and nested `- item`
    sub-lists (used for `markers`, whose values may themselves contain
    colons, e.g. `- ":HL["` -- those must NOT be parsed as a nested
    `key: value` pair). Good enough to avoid a hard `pyyaml` dependency
    for a file this project fully controls the shape of."""
    entries: list = []
    current: Optional[dict] = None
    current_list_key: Optional[str] = None
    current_list_indent: Optional[int] = None
    folded_key: Optional[str] = None
    folded_lines: list = []

    def flush_folded() -> None:
        nonlocal folded_key, folded_lines
        if current is not None and folded_key:
            current[folded_key] = " ".join(line.strip() for line in folded_lines if line.strip())
        folded_key = None
        folded_lines = []

    for raw_line in text.splitlines():
        if not raw_line.strip() or raw_line.strip().startswith("#"):
            continue
        indent = len(raw_line) - len(raw_line.lstrip(" "))
        stripped = raw_line.strip()
        if folded_key and indent > 2:
            folded_lines.append(stripped)
            continue
        flush_folded()

        is_top_level_entry = stripped.startswith("- ") and indent == 0
        is_nested_list_item = (
            stripped.startswith("- ")
            and current_list_key is not None
            and not is_top_level_entry
            and (current_list_indent is None or indent >= current_list_indent)
        )

        if is_top_level_entry:
            current = {}
            entries.append(current)
            stripped = stripped[2:]
            current_list_key = None
            current_list_indent = None
        elif is_nested_list_item:
            # A `- <value>` line under an already-open `key:` list --
            # the value may itself contain a colon (e.g. a marker like
            # ":HL["), so this branch must run BEFORE any generic
            # colon-splitting below, not after.
            if current is not None and current_list_key is not None:
                current[current_list_key].append(stripped[2:].strip().strip('"'))
            continue

        if current is None:
            continue
        if ":" in stripped:
            key, _, val = stripped.partition(":")
            key = key.strip()
            val = val.strip()
            if val == ">":
                folded_key = key
                folded_lines = []
                current_list_key = None
            elif val == "":
                current[key] = []
                current_list_key = key
                current_list_indent = indent + 1
            else:
                current[key] = val.strip('"')
                current_list_key = None
    flush_folded()
    return entries


class FlightParseError(Exception):
    """Raised only when ``strict=True`` and a row cannot be parsed at all."""


class FlightRequestError(Exception):
    """Base class for errors raised while making a request on the
    caller's behalf (currently: :func:`call_server_action`). Distinct
    from :class:`FlightParseError`, which is about a row's *content*
    being malformed, not the HTTP exchange that fetched it."""


class ActionNotFoundError(FlightRequestError):
    """Raised by :func:`call_server_action` when the server reports it
    can no longer find the requested Server Action id -- almost always
    because a redeploy regenerated action ids since this one was
    scraped from an earlier page load. Without this, the failure
    surfaces as an opaque 500 (or a 200 rendering a generic error
    component) that looks identical to any other request-gone-wrong,
    even though the fix (re-discover the id with
    :func:`find_server_action_ids` against a fresh page) is completely
    different from what you'd do for, say, a timeout."""



class _LazyRawChunks(Mapping):
    """Dict-like view over a page's chunks that decodes each chunk's raw
    JSON lazily, on first access, rather than all up front at parse time.

    Splitting a payload into rows (finding where each chunk starts and
    ends) is cheap and always needs to happen -- it's how chunk ids get
    discovered at all. But actually JSON-decoding each row's *value* isn't
    free, and a page can have dozens of chunks a caller never touches
    (e.g. wanting just `price`/`title` off one listing among forty). This
    defers that decode to whichever chunk is actually looked at, via
    `FlightExtractor._materialize_chunk`, and caches the result so
    repeated access doesn't re-decode.

    Supports the same read interface as a plain dict (`in`, `len`,
    iteration, `[key]`, `.keys()`/`.values()`/`.items()`/`.get()` via the
    `Mapping` ABC) so existing code that treats `raw_chunks` as a dict
    keeps working unchanged; only assignment isn't supported, since
    materialization is meant to happen internally.
    """

    __slots__ = ("_extractor",)

    def __init__(self, extractor: "FlightExtractor"):
        self._extractor = extractor

    def __getitem__(self, key: str) -> Any:
        if key not in self._extractor._row_types:
            raise KeyError(key)
        return self._extractor._materialize_chunk(key)

    def __iter__(self):
        return iter(self._extractor._row_types)

    def __len__(self) -> int:
        return len(self._extractor._row_types)

    def __repr__(self) -> str:
        return f"<LazyRawChunks {len(self)} chunk(s)>"


def _coerce_html(source: Any) -> str:
    """Accept a raw HTML string/bytes, or a response-like object (Scrapy's
    ``Response``, ``requests.Response``, httpx, etc.) and return plain text.

    This means both of these just work:

        FlightExtractor(response)          # Scrapy / requests response
        FlightExtractor(response.text)     # or the plain string, as before
    """
    if isinstance(source, str):
        return source
    if isinstance(source, (bytes, bytearray)):
        return bytes(source).decode("utf-8", errors="replace")
    text_attr = getattr(source, "text", None)
    if isinstance(text_attr, str):
        return text_attr
    body_attr = getattr(source, "body", None)
    if isinstance(body_attr, (bytes, bytearray)):
        return bytes(body_attr).decode("utf-8", errors="replace")
    raise TypeError(
        "Expected an HTML string, bytes, or a response-like object with a "
        ".text or .body attribute (e.g. a Scrapy or requests Response); "
        f"got {type(source).__name__}"
    )


def _read_urllib_response_text(resp) -> str:
    """Read and decode an ``http.client.HTTPResponse`` from ``urlopen``,
    handling `Content-Encoding` (gzip/deflate/br) transparently.

    ``urllib`` doesn't advertise gzip support by default, so most servers
    respond uncompressed -- but some (CDNs in particular) compress
    unconditionally regardless of what the client asked for. `from_url` /
    `from_rsc_url` also send ``Accept-Encoding: identity`` to ask for
    plain text up front, but this decodes whatever actually comes back
    either way, so a server that ignores that header doesn't leave the
    caller holding raw compressed bytes."""
    raw = resp.read()
    encoding = (resp.headers.get("Content-Encoding") or "").lower()
    if encoding == "gzip":
        import gzip
        raw = gzip.decompress(raw)
    elif encoding == "deflate":
        import zlib
        raw = zlib.decompress(raw)
    elif encoding == "br":
        try:
            import brotli  # type: ignore
        except ImportError:
            try:
                import brotlicffi as brotli  # type: ignore
            except ImportError:
                raise RuntimeError(
                    "Response is brotli-encoded but neither `brotli` nor "
                    "`brotlicffi` is installed. Install one "
                    "(`pip install brotli`), or pass "
                    "headers={'Accept-Encoding': 'identity'} explicitly if "
                    "the server still ignores the default identity request."
                )
        raw = brotli.decompress(raw)
    charset = resp.headers.get_content_charset() or "utf-8"
    return raw.decode(charset, errors="replace")


_BUNDLER_MARKERS = (
    ("turbopack", ("__turbopack_require__", "__turbopack_context__", "/_next/static/chunks/[turbopack]")),
    ("webpack", ("__webpack_require__", "webpackChunk_N_E", "self.webpackChunk")),
)


def _detect_bundler(html: str) -> str:
    """Best-effort guess at which bundler produced a page's chunks --
    `"webpack"`, `"turbopack"`, or `"unknown"` -- based on runtime
    identifier strings each bundler's output leaves in the page. Used by
    :meth:`FlightExtractor.next_version_hint` so a `parse_confidence()`
    drop caused by a bundler switch (a real, if uncommon, event -- e.g.
    a project opting into Turbopack for dev or, in Next.js 15+, for
    production builds) can be correctly attributed to that instead of
    being mistaken for an actual wire-format break."""
    for name, markers in _BUNDLER_MARKERS:
        if any(marker in html for marker in markers):
            return name
    return "unknown"


class FlightExtractor:
    """Parses and searches the Next.js Flight payloads embedded in a page.

    Parameters
    ----------
    html:
        The full HTML of a server-rendered Next.js App Router page. Also
        accepts bytes, or a response-like object with a `.text`/`.body`
        attribute (Scrapy's `Response`, `requests.Response`, etc.).
    strict:
        If True, raise :class:`FlightParseError` when a row's value isn't
        valid JSON and doesn't look like a bare `$`-reference marker.
        Default False: such rows are kept as raw strings so a handful of
        odd rows never take down extraction of everything else on the page.
    repair:
        If True, additionally attempt to heuristically recover chunks
        whose JSON body fails to decode outright (as opposed to merely
        being an unrecognized bare marker) -- e.g. a proxy/CDN that cut
        the response short mid-object. This closes unbalanced brackets
        and quotes and retries the decode; chunks that still can't be
        salvaged fall back to the same raw-string behavior as
        `strict=False`. Mutually exclusive with `strict=True` (repairing
        implies tolerating malformed input, which is what `strict` exists
        to forbid); combining both raises `ValueError`. Has no effect on
        already-well-formed payloads. See :meth:`parse_confidence` to
        check how much of a repaired page was actually salvaged.
    decode_rsc_values:
        If True (the default), decode Flight's sigil-prefixed value
        encodings for types plain JSON can't represent -- currently
        `$D<isoString>` (``datetime.datetime``), `$Q<ref>` (``Map`` ->
        `dict`), and `$W<ref>` (``Set`` -> `list`) -- into native Python
        values wherever they appear, instead of leaving them as raw
        strings like ``"$D2024-01-05T00:00:00.000Z"``. Set to False to
        get the pre-0.4.2 behavior back (raw sigil strings passed
        through unresolved) if existing code depends on the old shape.
    """

    _REF_RE = re.compile(r"^\$(?P<sigil>[A-Z@]{0,2})(?P<id>[^:\s]+)(?::(?P<path>.+))?$")
    _PUSH_CALL_RE = re.compile(r"self\.__next_f\.push\(")
    _ROW_START_RE = re.compile(r"[0-9a-zA-Z_\-]*:")
    _NEXT_ROW_RE = re.compile(r"\n[0-9a-zA-Z_\-]*:")
    _HTML_TAG_RE = re.compile(r"<[a-zA-Z!/][^>\n]{0,300}>")
    _RAW_RSC_ROW_RE = re.compile(r"^[0-9a-zA-Z_\-]+:")

    def __init__(self, html: Any, *, strict: bool = False, repair: bool = False,
                 decode_rsc_values: bool = True):
        if strict and repair:
            raise ValueError(
                "strict=True and repair=True are mutually exclusive: "
                "strict asks to fail loudly on malformed rows, repair asks "
                "to recover them heuristically instead."
            )
        self.html = _coerce_html(html)
        self.strict = strict
        self.repair = repair
        self.decode_rsc_values = decode_rsc_values
        # Populated by `_materialize_chunk` when `repair=True` salvages (or
        # fails to salvage) a chunk -- see `parse_confidence()`.
        self._repair_outcomes: dict[str, bool] = {}
        # Counters for `.stats()`'s cache visibility -- incremented in
        # `resolve_chunk`. A cache-miss simply means "this chunk id
        # hadn't been resolved yet", not an error; a page touched once
        # via `resolve_all()` will show mostly misses (first-time
        # resolves) and hits only from chunks reached more than once via
        # different `$`-ref paths, whereas a page probed repeatedly via
        # `.get()`/`.select()` on the same paths should show a high hit
        # ratio after the first call.
        self._cache_hits = 0
        self._cache_misses = 0
        # Cache for `_push_call_index_map()` / `streamed_chunks()` -- see
        # that method's docstring. `None` means "not computed yet",
        # distinct from an empty `dict` (a page with a single push call,
        # nothing streamed), so it's only ever built once.
        self._push_call_index_cache: Optional[dict] = None
        # Lazily-decoded view over each chunk's raw JSON -- see
        # _LazyRawChunks and _materialize_chunk. The raw row text and row
        # kind are what actually get populated during parsing (cheap);
        # the JSON decode itself happens on first access per chunk.
        self.raw_chunks: Mapping[str, Any] = _LazyRawChunks(self)
        # Which Flight row kind each chunk id came from ("text", "json",
        # "module", or "preload") -- powers .kind()/.json_keys()/.html_keys().
        self._row_types: dict[str, str] = {}
        # The chunk's raw, still-undecoded row text, keyed by chunk id.
        self._raw_row_text: dict[str, str] = {}
        # Cache of already-materialized (JSON-decoded) chunk values.
        self._materialized: dict[str, Any] = {}
        self._resolved_cache: dict[str, Any] = {}
        self._resolving: set[str] = set()
        # Guards re-entrant resolution of a *specific path* into a chunk
        # that is itself still mid-resolution (see _resolve_ref_string).
        # Keyed on (chunk_id, path) so distinct sibling paths into the same
        # in-progress chunk don't block each other.
        self._resolving_paths: set[tuple[str, str]] = set()
        self._extract_all()

    def __repr__(self) -> str:
        return f"<FlightExtractor chunks={len(self.raw_chunks)}>"

    def __len__(self) -> int:
        return len(self.raw_chunks)

    def __iter__(self):
        return iter(self.raw_chunks)

    def __contains__(self, key: str) -> bool:
        return key in self.raw_chunks

    def __getitem__(self, key: str) -> Any:
        """``page[key]`` is shorthand for ``page.resolve_chunk(key)``, but
        raises KeyError (like a normal dict) instead of returning None for
        a key that was never pushed onto the page at all."""
        if key not in self.raw_chunks:
            raise KeyError(key)
        return self.resolve_chunk(key)

    def keys(self) -> list:
        """Every chunk id found on the page, in the order they were pushed.
        This is 'step 1': see what's there before deciding what to resolve."""
        return list(self.raw_chunks.keys())

    def kind(self, chunk_id: str) -> Optional[str]:
        """Which Flight row kind a chunk came from: ``"json"``, ``"text"``,
        ``"module"``, or ``"preload"`` -- ``None`` if the id doesn't exist.
        This is what :meth:`json_keys` / :meth:`html_keys` / :meth:`text_keys`
        filter on."""
        return self._row_types.get(chunk_id)

    def json_keys(self) -> list:
        """Chunk ids whose raw value is structured JSON (a dict or list) --
        the ones you almost always want, as opposed to raw text blobs or
        module/preload bookkeeping rows that didn't parse as JSON. Order
        matches :meth:`keys`."""
        return [
            k for k, v in self.raw_chunks.items() if isinstance(v, (dict, list))
        ]

    def text_keys(self) -> list:
        """Chunk ids that came from a Flight 'T' (text) row -- raw,
        byte-length-prefixed strings. Next.js uses these for anything that
        isn't itself JSON: translated copy, prerendered markup fragments,
        inlined SVGs, etc. See also :meth:`html_keys` for the subset that
        actually looks like markup."""
        return [k for k in self.raw_chunks if self._row_types.get(k) == "text"]

    def html_keys(self) -> list:
        """Chunk ids from 'T' (text) rows whose content looks like an HTML
        fragment (contains at least one tag), e.g. suspense fallbacks, error
        boundaries, or inlined SVG/markup Next.js streams as raw text rather
        than JSON. A stricter subset of :meth:`text_keys` -- plain copy or
        translated strings without any tags are excluded."""
        return [
            k for k in self.text_keys()
            if isinstance(self.raw_chunks[k], str)
            and self._HTML_TAG_RE.search(self.raw_chunks[k])
        ]

    # ------------------------------------------------------------------ #
    # Construction helpers
    # ------------------------------------------------------------------ #
    @classmethod
    def from_url(cls, url: str, *, timeout: float = 15.0, headers: Optional[dict] = None,
                 strict: bool = False) -> "FlightExtractor":
        """Fetch a URL with the stdlib (no extra dependencies) and parse it.

        For anything beyond quick, one-off exploration -- retries, proxies,
        rendering JS, respecting robots.txt -- fetch the page with your own
        HTTP client / Scrapy / Zyte and pass ``response.text`` to the
        normal constructor instead.
        """
        req = urllib.request.Request(
            url,
            headers=headers or {
                "User-Agent": "Mozilla/5.0 (nextflight)",
                "Accept-Encoding": "identity",
            },
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            html = _read_urllib_response_text(resp)
        return cls(html, strict=strict)

    @classmethod
    async def from_url_async(cls, url: str, *, timeout: float = 15.0,
                              headers: Optional[dict] = None, strict: bool = False) -> "FlightExtractor":
        """Async counterpart to :meth:`from_url`, for concurrent multi-page
        crawls, e.g.::

            pages = await asyncio.gather(
                *(FlightExtractor.from_url_async(u) for u in urls)
            )

        Requires ``httpx`` (``pip install httpx``) -- optional, not a
        required dependency of this library. Raises ``ImportError`` with a
        clear message if it isn't installed.
        """
        try:
            import httpx  # type: ignore
        except ImportError as e:
            raise ImportError(
                "from_url_async() requires httpx -- install it with `pip install httpx`."
            ) from e
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(
                url, headers=headers or {"User-Agent": "Mozilla/5.0 (nextflight)"}
            )
            resp.raise_for_status()
            html = resp.text
        return cls(html, strict=strict)

    _RSC_PARAM_RE = re.compile(r"[?&]_rsc=([A-Za-z0-9_-]+)")

    @classmethod
    def from_rsc_url(cls, url: str, *, timeout: float = 15.0, headers: Optional[dict] = None,
                      cookies: Optional[dict] = None, strict: bool = False,
                      auto_discover: bool = True) -> "FlightExtractor":
        """Fetch a Next.js App Router page's raw RSC payload directly --
        a lighter-weight alternative to :meth:`from_url` that skips
        downloading the full HTML page, the same way Next.js's own
        client-side navigation does it: with an ``RSC: 1`` request header.

        Uses only the stdlib (no ``requests`` dependency). Pass
        `headers`/`cookies` to add request headers/cookies your target
        site needs (session cookies, etc. -- copy them from a real browser
        request if the bare ``RSC: 1`` header alone gets rejected or
        redirected to the full HTML page instead).

        Two of the fiddlier headers/params are handled automatically:

        - ``Next-Url`` is set to `url`'s own path, since that's always
          derivable and some deployments check it.
        - If `url` doesn't already have a ``_rsc=<id>`` query parameter
          and `auto_discover` is true (the default), this does one
          ordinary GET of `url` first and looks for a ``_rsc=`` value in
          any prefetch links Next.js embedded in the page (it reuses the
          *same* build-specific id for every page in that deployment, so
          this is genuinely the right value, not a guess) -- not every
          page embeds one, so this is best-effort and silently does
          nothing if none is found. Pass `auto_discover=False` to skip
          this extra request (e.g. if you already know none exists, or
          are supplying your own `_rsc` value in `url`/`headers`).

        A matching ``Next-Router-State-Tree`` header is *not* reconstructed
        automatically -- it's a serialized representation of the specific
        route being navigated to/from, and some deployments require it to
        match exactly while others don't need it at all. If the bare
        request comes back with 0 chunks (check with :meth:`stats` or
        `len(page)`), inspect a real browser's network tab for that header
        and pass it via `headers`.
        """
        if auto_discover and "_rsc=" not in url:
            discovered = cls._discover_rsc_id(url, timeout=timeout)
            if discovered:
                sep = "&" if "?" in url else "?"
                url = f"{url}{sep}_rsc={discovered}"
        req_headers = {
            "User-Agent": "Mozilla/5.0 (nextflight)",
            "RSC": "1",
            "Accept": "*/*",
            "Accept-Encoding": "identity",
            "Next-Url": urllib.parse.urlparse(url).path or "/",
        }
        if headers:
            req_headers.update(headers)
        if cookies:
            req_headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in cookies.items())
        req = urllib.request.Request(url, headers=req_headers)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            text = _read_urllib_response_text(resp)
        return cls(text, strict=strict)

    @classmethod
    def _discover_rsc_id(cls, url: str, *, timeout: float) -> Optional[str]:
        """Best-effort: fetch `url` normally (no RSC header) and look for
        a `_rsc=<id>` value already embedded in the page (e.g. in a
        Next.js prefetch `<link>`). Returns None on any failure or if
        nothing is found -- this is an optimization, not something the
        caller should depend on succeeding."""
        try:
            plain_req = urllib.request.Request(
                url, headers={"User-Agent": "Mozilla/5.0 (nextflight)", "Accept-Encoding": "identity"}
            )
            with urllib.request.urlopen(plain_req, timeout=timeout) as resp:
                html = _read_urllib_response_text(resp)
        except Exception:
            return None
        m = cls._RSC_PARAM_RE.search(html)
        return m.group(1) if m else None

    @classmethod
    def from_page(cls, page: Any, *, strict: bool = False, repair: bool = False) -> "FlightExtractor":
        """Build an extractor from a Playwright `Page` object's *current,
        fully-rendered* HTML -- for sites that only populate later
        `self.__next_f.push(...)` chunks after client-side JS runs
        (Suspense boundaries resolving, client-side data fetches, etc.),
        where `from_url`'s plain HTTP GET would only see the initial
        server-rendered chunks.

            from playwright.sync_api import sync_playwright
            from nextflight import FlightExtractor

            with sync_playwright() as p:
                browser = p.chromium.launch()
                page = browser.new_page()
                page.goto("https://example.com/product/123")
                page.wait_for_load_state("networkidle")  # let streamed chunks finish arriving
                extractor = FlightExtractor.from_page(page)
                browser.close()

        Works with Playwright's sync API (`page.content()` returns a
        `str` directly, as above) and its async API (`await
        page.content()`) transparently -- this method itself stays a
        plain classmethod either way; only the caller's `await` differs::

            extractor = FlightExtractor.from_page(await async_page.content())

        Actually, simpler and more robust than trying to detect
        sync-vs-async `Page` objects internally (which would need to
        special-case two different Playwright APIs and would break if a
        future Playwright version changes its internals): pass the
        *already-fetched* HTML string directly, from either API's
        `.content()` call -- `from_page` also accepts a plain string for
        exactly this reason, in which case it's simply
        `FlightExtractor(html, ...)`.  Passing an object with a
        synchronous `.content()` method (Playwright's sync `Page`) is
        also accepted directly, since that's the common case and needs
        no `await` gymnastics from the caller.

        No Playwright dependency is required to use the rest of
        `nextflight` -- this only needs Playwright installed
        (`pip install playwright` -- not bundled in any `nextflight`
        extra, since it also requires a separate `playwright install`
        browser-download step that isn't a normal pip dependency at all)
        if you actually call this method. A Selenium equivalent isn't
        provided here since Selenium's `driver.page_source` needs no
        `nextflight`-specific wrapping at all -- just call
        `FlightExtractor(driver.page_source)` directly.
        """
        if isinstance(page, str):
            html = page
        elif hasattr(page, "content") and not _is_coroutine_function(page.content):
            # Playwright's sync API: `page.content()` returns `str`
            # directly, no `await` needed.
            html = page.content()
        else:
            raise TypeError(
                "from_page() expects a Playwright sync Page (with a "
                "synchronous .content() method) or a plain HTML string. "
                "For Playwright's async API, await page.content() "
                "yourself and pass the resulting string: "
                "FlightExtractor.from_page(await page.content())."
            )
        return cls(html, strict=strict, repair=repair)

    # ------------------------------------------------------------------ #
    # Step 1 -- find every push([...]) call, bracket/quote aware, so it
    # doesn't matter how many <script> tags they're spread across.
    # ------------------------------------------------------------------ #
    def _iter_push_payloads(self) -> Iterator[str]:
        found_wrapped = False
        for m in self._PUSH_CALL_RE.finditer(self.html):
            found_wrapped = True
            array_text, _end, value = _fast_bracket_decode(self.html, m.end())
            if array_text is None:
                # Fall back to the tolerant hand-rolled scan (handles
                # malformed/truncated push() calls the same way this did
                # before this optimization) -- then still needs its own
                # decode, since `_read_balanced` only finds boundaries.
                array_text, _end = self._read_balanced(self.html, m.end(), "[", "]")
                if array_text is None:
                    continue
                try:
                    value = _json_loads(array_text)
                except _JSON_ERRORS:
                    continue
            # push([0]) is an init call with no payload string; the ones we
            # want look like push([1, "...rows..."])
            if isinstance(value, list) and len(value) > 1 and isinstance(value[1], str):
                yield value[1]
        if not found_wrapped and self._looks_like_raw_rsc_payload(self.html):
            # Next.js's App Router returns the raw Flight row stream
            # directly as the response body -- no HTML, no
            # self.__next_f.push() wrapper -- when a request carries the
            # RSC fetch header (see FlightExtractor.from_rsc_url). Treat
            # the whole input as a single payload in that case.
            yield self.html

    @classmethod
    def _looks_like_raw_rsc_payload(cls, text: str) -> bool:
        """Heuristic: does `text` look like a raw Flight row stream on its
        own, with no surrounding HTML and no `self.__next_f.push(...)`
        wrapper? That's what an RSC fetch response looks like -- see
        :meth:`from_rsc_url`.

        Matching just an `id:` prefix on the first line isn't enough --
        ordinary text like `"name: John"` or a timestamped log line like
        `"12:34:56 INFO started"` also happens to start that way. The
        extra check here is that the row *value* right after the colon
        must itself look like a genuine Flight row value: a quoted
        string, array, object, module/preload row, text row, bare
        `$`-ref, or a bare number that is the *entire* rest of the line
        (not just starts with a digit) -- `"key: value"` fails this
        because of the space, and `"12:34:56 ..."` fails it because
        `"34:56 INFO started"` isn't a bare number on its own.
        """
        stripped = text.lstrip()
        if not stripped:
            return False
        first_line = stripped.split("\n", 1)[0]
        m = cls._RAW_RSC_ROW_RE.match(first_line)
        if not m:
            return False
        if "<html" in text[:200].lower():
            return False
        rest = first_line[m.end():]
        if not rest:
            return False
        if rest[0] in "\"[{$":
            return True
        if rest.startswith("I[") or rest.startswith("HL["):
            return True
        if re.match(r"^T[0-9a-fA-F]+,", rest):
            return True
        if rest in ("null", "true", "false"):
            return True
        if re.match(r"^-?\d+(\.\d+)?$", rest):
            return True
        return False

    @staticmethod
    def _read_balanced(text: str, start: int, open_ch: str, close_ch: str):
        """Find `open_ch` at/after `start`, then return (substring, end_idx)
        where substring spans to its matching `close_ch`, respecting quoted
        strings and backslash escapes inside them. (None, start) if no
        balanced match exists (e.g. truncated HTML)."""
        i = start
        n = len(text)
        while i < n and text[i] != open_ch:
            i += 1
        if i >= n:
            return None, start
        begin = i
        depth = 0
        in_str = False
        esc = False
        while i < n:
            ch = text[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
            else:
                if ch == '"':
                    in_str = True
                elif ch == open_ch:
                    depth += 1
                elif ch == close_ch:
                    depth -= 1
                    if depth == 0:
                        return text[begin:i + 1], i + 1
            i += 1
        return None, n

    # ------------------------------------------------------------------ #
    # Step 2 -- split a payload string into (chunk_id, row_type, raw_value)
    # honouring the real row grammar (this is what a '\n'.split() breaks).
    # ------------------------------------------------------------------ #
    def _split_rows(self, payload: str) -> Iterator[tuple]:
        """Yields ``(chunk_id, row_type, raw_value, precomputed_value)``
        for each row. `precomputed_value` is the already-decoded JSON
        value when the fast path (`_fast_bracket_decode`) successfully
        decoded it inline, or `None` when it wasn't attempted or fell
        back to the tolerant `_read_balanced` path -- callers that don't
        care about the precomputed value (this is purely an optimization,
        not part of the row grammar) can simply ignore the 4th element."""
        i = 0
        n = len(payload)
        while i < n:
            if payload[i] == "\n":
                i += 1
                continue
            m = self._ROW_START_RE.match(payload, i)
            if not m:
                break
            chunk_id = m.group(0)[:-1]
            i = m.end()
            if i >= n:
                # Truncated payload: an id: was matched but nothing follows
                # it (e.g. the row got cut off at a chunk boundary, which
                # happens with proxies/CDNs that truncate responses).
                # Nothing more to parse.
                break
            if payload[i] == "T":
                comma_idx = payload.find(",", i + 1)
                if comma_idx == -1:
                    # Truncated payload: a text row's "T<hexLen>," header
                    # never got its comma (response cut off mid-header).
                    if self.strict:
                        raise FlightParseError(
                            f"chunk {chunk_id!r}: truncated text-row header "
                            f"(no comma found): {payload[i:i + 40]!r}"
                        )
                    break
                j = comma_idx
                try:
                    hex_len = int(payload[i + 1:j], 16)
                except ValueError:
                    if self.strict:
                        raise FlightParseError(
                            f"chunk {chunk_id!r}: invalid hex length in text "
                            f"row: {payload[i + 1:j]!r}"
                        )
                    break
                body_start = j + 1
                text_str, got_bytes, truncated = self._read_text_row_body(
                    payload, body_start, hex_len
                )
                if truncated:
                    # Truncated payload: fewer bytes remain than the header
                    # promised (response cut off mid text-row body).
                    if self.strict:
                        raise FlightParseError(
                            f"chunk {chunk_id!r}: text row body truncated "
                            f"(expected {hex_len} bytes, got {got_bytes})"
                        )
                    yield chunk_id, "text", text_str, None
                    break
                i = body_start + len(text_str)
                yield chunk_id, "text", text_str, None
            elif payload[i:i + 2] == "HL":
                val, end, decoded = _fast_bracket_decode(payload, i + 2)
                if val is None:
                    val, end = self._read_balanced(payload, i + 2, "[", "]")
                    if val is None:
                        break
                i = end
                yield chunk_id, "preload", val, decoded
            elif payload[i] == "I":
                val, end, decoded = _fast_bracket_decode(payload, i + 1)
                if val is None:
                    val, end = self._read_balanced(payload, i + 1, "[", "]")
                    if val is None:
                        break
                i = end
                yield chunk_id, "module", val, decoded
            else:
                decoded = None
                if payload[i] in "[{":
                    close_ch = "]" if payload[i] == "[" else "}"
                    val, end, decoded = _fast_bracket_decode(payload, i)
                    if val is not None:
                        i = end
                    else:
                        val, end = self._read_balanced(payload, i, payload[i], close_ch)
                        if val is None:
                            val, i = payload[i:], n
                        else:
                            i = end
                elif payload[i] == '"':
                    val = self._read_quoted_string(payload, i)
                    i += len(val)
                else:
                    # Bare, unbracketed value (a number, boolean, or bare
                    # `$`-ref marker): read to the next row start. Search
                    # in place with a `pos` argument rather than slicing
                    # `payload[i:]` -- slicing copies the remaining payload
                    # on every such row, which is O(n) per row and O(n^2)
                    # across a payload with many of them.
                    nxt = self._NEXT_ROW_RE.search(payload, i)
                    end = nxt.start() if nxt else n
                    val, i = payload[i:end], end
                yield chunk_id, "json", val, decoded

    @staticmethod
    def _read_text_row_body(payload: str, body_start: int, hex_len: int) -> tuple:
        """Return ``(text, bytes_found, truncated)`` for the next `hex_len`
        UTF-8-encoded bytes of `payload` starting at `body_start`.

        The naive approach -- ``payload[body_start:].encode("utf-8")`` then
        slicing off the first `hex_len` bytes -- re-encodes the ENTIRE rest
        of the payload on every single text row. That's O(payload length)
        per row, so a page with many text rows (translated copy, repeated
        card fragments, etc.) parses in O(n^2). This instead encodes only
        as much of the payload as the row actually needs.

        Fast path: for pure-ASCII text (the common case), `hex_len`
        characters are exactly `hex_len` bytes, so the first slice already
        has the right length and this returns after a single encode call.
        Multi-byte characters right at the boundary are handled by growing
        one character at a time (bounded by a handful of extra characters,
        not the rest of the payload).
        """
        n = len(payload)
        if hex_len == 0:
            return "", 0, False
        end = min(body_start + hex_len, n)
        chunk = payload[body_start:end]
        encoded = chunk.encode("utf-8")
        while len(encoded) < hex_len and end < n:
            ch = payload[end]
            encoded += ch.encode("utf-8")
            chunk += ch
            end += 1
        if len(encoded) < hex_len:
            # Truncated payload: ran out of characters before hex_len bytes.
            return chunk, len(encoded), True
        if len(encoded) > hex_len:
            # Overshot: the last multi-byte character pushed us past the
            # target. `hex_len` counts complete UTF-8 bytes by construction,
            # so trimming to exactly `hex_len` bytes always lands back on a
            # character boundary.
            chunk = encoded[:hex_len].decode("utf-8")
        return chunk, hex_len, False

    @staticmethod
    def _read_quoted_string(text: str, start: int) -> str:
        i, n, esc = start + 1, len(text), False
        while i < n:
            ch = text[i]
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                return text[start:i + 1]
            i += 1
        return text[start:]

    def _extract_all(self) -> None:
        # Next.js's Flight stream doesn't guarantee one row (or even one
        # complete row) per self.__next_f.push() call: for a page with
        # enough data, the browser-side buffer for a given stream gets
        # flushed mid-string, so a single logical row's raw text can be
        # split across two (or more) separate push() calls with NO
        # separator between the pieces -- concatenating them is required
        # to reconstruct the real, continuous row stream before it can be
        # split into rows at all. Treating each push() call as an
        # independently complete set of rows (the previous approach) works
        # by coincidence on smaller pages where every row happens to fit
        # in one push() call, but silently produces garbage chunk ids
        # (fragments of URLs, etc.) on larger real-world pages where it
        # doesn't -- confirmed against production Next.js pages where over
        # half of all push() calls were mid-row continuations.
        combined = "".join(self._iter_push_payloads())
        dup_counts: dict[str, int] = {}
        for chunk_id, row_type, raw_value, precomputed in self._split_rows(combined):
            key = chunk_id
            if key in self._row_types:
                # Duplicate chunk id. Confirmed on real pages: Next.js
                # deliberately emits many `HL` (preload) rows with a
                # completely empty id (":HL[\"/path.css\",\"style\"]") since
                # nothing ever needs to `$`-ref them individually -- on one
                # real page, 43 separate preload rows all shared the empty
                # id. Silently overwriting would keep only the last one and
                # under-report the true row count. The FIRST occurrence
                # keeps its original id untouched (so `$`-ref resolution
                # into it is unaffected); every later occurrence gets a
                # synthesized, clearly-marked unique key instead -- it was
                # never uniquely `$`-ref-addressable anyway once its id
                # collided, so nothing that used to work stops working.
                n = dup_counts.get(chunk_id, 1) + 1
                # A real chunk id can only contain [0-9a-zA-Z_-] (see
                # _ROW_START_RE), so it can never itself contain "#" --
                # a synthesized key can't collide with a genuine one. This
                # loop is defensive housekeeping in case that ever changes,
                # not a case that can currently be hit.
                while f"{chunk_id}#{n}" in self._row_types:
                    n += 1
                dup_counts[chunk_id] = n
                key = f"{chunk_id}#{n}"
            self._row_types[key] = row_type
            self._raw_row_text[key] = raw_value
            if self.strict:
                # strict=True means "tell me immediately if anything on
                # this page is malformed" -- materialize (and so
                # validate) every row right away rather than waiting for
                # something to access it, so a chunk nobody ever looks at
                # can still fail construction as before. Since strict
                # mode already eagerly materializes every chunk by
                # design, reusing `precomputed` here (the value
                # `_split_rows` already decoded while finding this row's
                # boundary -- see `_fast_bracket_decode`) whenever it's
                # available avoids a second, redundant decode -- this is
                # NOT a laziness violation, since strict mode was never
                # lazy about materialization to begin with.
                if precomputed is not None:
                    self._materialized[key] = precomputed
                else:
                    self._materialize_chunk(key)
            # else: `precomputed` is deliberately discarded here even
            # though we already have it for free. Construction must stay
            # honestly lazy in the non-strict (default) case: a page with
            # many chunks a caller never looks at (the whole point of
            # `_LazyRawChunks`) should not have every chunk's decoded
            # Python object materialized and held in memory just because
            # finding row boundaries happened to decode it along the way.
            # `_materialize_chunk` re-decodes from `raw_value` on first
            # real access instead, same as before this optimization.

    def _materialize_chunk(self, chunk_id: str) -> Any:
        """JSON-decode a single chunk's raw row text (see
        :class:`_LazyRawChunks`), caching the result. This is where the
        per-row-kind parsing logic that used to run unconditionally in
        `_extract_all` for every chunk now runs, but only for chunks that
        are actually looked at."""
        if chunk_id in self._materialized:
            return self._materialized[chunk_id]
        row_type = self._row_types[chunk_id]
        raw_value = self._raw_row_text[chunk_id]
        if row_type == "text":
            value = raw_value
        elif row_type in ("module", "preload"):
            try:
                value = _json_loads(raw_value)
            except _JSON_ERRORS:
                if self.strict:
                    raise FlightParseError(
                        f"chunk {chunk_id!r}: invalid {row_type} JSON: {raw_value[:80]!r}"
                    )
                value = raw_value
        else:
            if raw_value == "$undefined":
                value = None
            else:
                try:
                    value = _json_loads(raw_value)
                except _JSON_ERRORS:
                    if self.repair and raw_value[:1] in "[{":
                        repaired = self._attempt_repair(raw_value)
                        if repaired is not None:
                            self._repair_outcomes[chunk_id] = True
                            value = repaired
                        else:
                            self._repair_outcomes[chunk_id] = False
                            value = raw_value
                    elif self.strict and not raw_value.startswith("$"):
                        raise FlightParseError(
                            f"chunk {chunk_id!r}: invalid JSON: {raw_value[:80]!r}"
                        )
                    else:
                        value = raw_value  # bare marker e.g. "X"
        self._materialized[chunk_id] = value
        return value

    @staticmethod
    def _attempt_repair(raw_value: str) -> Any:
        """Best-effort recovery for a JSON-looking row that failed to
        decode -- most commonly because the surrounding HTML response was
        truncated mid-chunk by a proxy/CDN. Heuristically closes unbalanced
        quotes and brackets/braces (tracking string state so bracket
        characters inside string literals aren't miscounted), then retries
        the decode, backing off one trailing partial token at a time if it
        still doesn't parse. Returns the decoded value, or ``None`` if
        nothing recoverable could be produced.

        This is deliberately conservative: it never guesses at *missing*
        data (e.g. it won't invent a value for a truncated key), it only
        closes what's already open so however much of the structure did
        arrive intact can still be decoded."""
        text = raw_value
        for _ in range(3):
            candidate = FlightExtractor._close_unbalanced(text)
            try:
                return _json_loads(candidate)
            except _JSON_ERRORS:
                # Trim the last partial token (likely a truncated key or
                # value fragment) and try again.
                trimmed = re.sub(r'[,:]?\s*"[^"]*$', "", text)
                trimmed = re.sub(r",\s*$", "", trimmed)
                if trimmed == text or not trimmed:
                    break
                text = trimmed
        return None

    @staticmethod
    def _close_unbalanced(text: str) -> str:
        stack: list = []
        in_string = False
        escape = False
        for ch in text:
            if in_string:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == '"':
                    in_string = False
                continue
            if ch == '"':
                in_string = True
            elif ch in "[{":
                stack.append("]" if ch == "[" else "}")
            elif ch in "]}":
                if stack and stack[-1] == ch:
                    stack.pop()
        closing = ""
        if in_string:
            if escape:
                # The text ends on a dangling, unconsumed escape
                # backslash (e.g. text ends `..."\`) -- naively closing
                # with just a `"` would produce `\"`, which JSON parses
                # as an *escaped* quote inside the string, not a
                # terminator, leaving the string open. Complete the
                # dangling escape as a literal backslash first (`\\`)
                # so the quote that follows actually closes the string.
                closing += "\\"
            closing += '"'
        closing += "".join(reversed(stack))
        return text + closing

    # ------------------------------------------------------------------ #
    # Step 3 -- resolve '$'-sigil references into real values, recursively.
    # ------------------------------------------------------------------ #
    def resolve_chunk(self, chunk_id: str) -> Any:
        """Resolve a single chunk (by its id) with all `$`-refs dereferenced."""
        if chunk_id in self._resolved_cache:
            self._cache_hits += 1
            return self._resolved_cache[chunk_id]
        self._cache_misses += 1
        if chunk_id in self._resolving or chunk_id not in self.raw_chunks:
            return None
        self._resolving.add(chunk_id)
        resolved = self._resolve_value(self.raw_chunks[chunk_id])
        self._resolving.discard(chunk_id)
        self._resolved_cache[chunk_id] = resolved
        return resolved

    def _resolve_value(self, value: Any) -> Any:
        if isinstance(value, str):
            return self._resolve_ref_string(value)
        if isinstance(value, list):
            return [self._resolve_value(v) for v in value]
        if isinstance(value, dict):
            return {k: self._resolve_value(v) for k, v in value.items()}
        return value

    def _resolve_ref_string(self, s: str) -> Any:
        if s == "$undefined":
            return None
        if not s.startswith("$"):
            return s
        if s.startswith("$$"):  # escaped literal '$...'
            return s[1:]
        if self.decode_rsc_values:
            # These two sigils encode the *entire* value inline after the
            # sigil letter (an ISO date string, a decimal digit string) --
            # they are NOT chunk references at all, unlike every other
            # sigil handled below. That matters because `_REF_RE`'s `id`
            # group stops at the first ':', and an ISO date string
            # (`2024-01-05T00:00:00.000Z`) contains one -- matching against
            # `_REF_RE` first would misparse the date into a bogus
            # "ref_id:path" split instead of decoding it. So these are
            # checked, and consumed, before `_REF_RE` ever sees the string.
            if s.startswith("$D"):
                return self._parse_rsc_date(s[2:])
            if s.startswith("$n"):
                try:
                    return int(s[2:])
                except ValueError:
                    pass  # not actually a BigInt -- fall through below
        m = self._REF_RE.match(s)
        if not m:
            return s
        sigil, ref_id, path = m.group("sigil"), m.group("id"), m.group("path")
        if sigil == "S":
            return {"__symbol__": ref_id}
        if self.decode_rsc_values and sigil in ("Q", "W") and not path:
            # "$Q<ref>" -> Map, "$W<ref>" -> Set. The referenced chunk is a
            # plain array (of [key, value] pairs for a Map, of values for
            # a Set) -- resolve it exactly like an ordinary reference, then
            # convert the result into the native container the sigil
            # denotes: a `dict` for Map, a `list` for Set (per the RSC
            # decoding policy documented on `FlightExtractor.__init__`).
            # Only handled when there's no trailing path segment: a
            # path-addressed ref (rare for these two sigils in practice)
            # is almost certainly reaching *into* the raw pairs/values
            # array rather than asking for the wrapped container, so it's
            # left to the generic path below instead of being converted.
            if ref_id not in self.raw_chunks:
                return s
            if ref_id in self._resolving:
                return {} if sigil == "Q" else []  # genuine cycle -- bail safely
            items = self.resolve_chunk(ref_id)
            if sigil == "Q":
                try:
                    return dict(items)
                except (TypeError, ValueError):
                    return items  # didn't look like [key, value] pairs after all
            return list(items) if isinstance(items, (list, tuple)) else items
        if sigil == "@":
            # Async/Suspense placeholder marker (e.g. "$@5"): during
            # streaming, this slot's real value arrives on a later chunk
            # while this one renders a fallback in the meantime. By the
            # time we're parsing a *completed* page or payload, chunk
            # `ref_id` has already arrived along with everything else, so
            # this resolves exactly like an ordinary reference ("$5")
            # once that chunk is looked up below -- no special handling
            # needed beyond documenting that this is intentional, not
            # coincidental.
            pass
        if ref_id not in self.raw_chunks:
            return s
        if ref_id in self._resolving:
            # `ref_id` is the chunk currently being resolved further up the
            # call stack. If this ref has no path, it's a genuine reference
            # to the *whole* chunk-in-progress -- a true cycle, so bail with
            # None rather than recursing forever.
            #
            # But if it has a path (e.g. "$0:children:1"), it's pointing at
            # a different, already-materialized part of that chunk's raw
            # structure -- not actually circular. The raw data for it is
            # already sitting in self.raw_chunks, so walk and resolve just
            # that slice directly instead of deferring to resolve_chunk
            # (which would hit the guard above and return None). A second
            # guard, keyed on (ref_id, path), still catches a genuine cycle
            # that loops back through the exact same path.
            if not path:
                return None
            guard_key = (ref_id, path)
            if guard_key in self._resolving_paths:
                return None
            raw = self._walk_path(self.raw_chunks[ref_id], path.split(":"))
            self._resolving_paths.add(guard_key)
            try:
                return self._resolve_value(raw)
            finally:
                self._resolving_paths.discard(guard_key)
        value = self.resolve_chunk(ref_id)
        if path:
            value = self._walk_path(value, path.split(":"))
        return value

    @staticmethod
    def _parse_rsc_date(date_str: str) -> Any:
        """Parse an RSC-encoded `$D<isoString>` date value into a native
        `datetime.datetime`. Accepts a trailing 'Z' (UTC) the way
        `datetime.fromisoformat` alone doesn't on Python versions before
        3.11, by swapping it for an explicit `+00:00` offset first.
        Falls back to returning the original sigil-prefixed string
        unchanged if `date_str` doesn't actually parse as a date --
        better to surface an obviously-still-encoded value than raise an
        exception out of an otherwise-successful page parse."""
        normalized = date_str[:-1] + "+00:00" if date_str.endswith("Z") else date_str
        try:
            return datetime.datetime.fromisoformat(normalized)
        except ValueError:
            return f"$D{date_str}"

    @staticmethod
    def _walk_path(value: Any, parts: list, default: Any = None) -> Any:
        for part in parts:
            if (
                part == "props"
                and isinstance(value, list)
                and len(value) >= 4
                and value[0] == "$"
            ):
                # React element quad: ["$", type, key, props]. Flight ref
                # paths address this shape with a symbolic "props" segment
                # (e.g. "29:3:props:adMetrics:0:tags") even though the props
                # dict is really just index 3 of the raw array -- there's no
                # dict literally keyed "props" here. Special-case it so path
                # traversal matches the wire format's own addressing scheme.
                value = value[3]
                continue
            if isinstance(value, list):
                try:
                    value = value[int(part)]
                except (ValueError, IndexError):
                    return default
            elif isinstance(value, dict):
                if part not in value:
                    return default
                value = value[part]
            else:
                return default
        return value

    def resolve_all(self) -> dict:
        """Every chunk on the page, fully dereferenced."""
        return {cid: self.resolve_chunk(cid) for cid in list(self.raw_chunks)}

    def resolve_json(self) -> dict:
        """Resolve only the chunks whose raw value is structured JSON (see
        :meth:`json_keys`) -- skips top-level text/HTML-only chunks that
        aren't referenced from any JSON chunk. On a page that embeds a lot
        of raw text/markup rows alongside a smaller amount of actual data,
        this avoids walking and copying subtrees you almost certainly don't
        want, without you having to filter `resolve_all()`'s output
        yourself. Chunks that a JSON chunk references *via* a `$`-ref are
        still resolved as part of walking that chunk, same as always --
        this only changes which chunks you start from."""
        return {cid: self.resolve_chunk(cid) for cid in self.json_keys()}

    def resolve_html(self) -> dict:
        """Resolve only the :meth:`html_keys` chunks. Since these are raw
        text rows they don't usually contain further `$`-refs, so this
        mostly returns the HTML strings themselves, but goes through the
        same resolution path for consistency (e.g. the rare `$$`-escaped
        literal case)."""
        return {cid: self.resolve_chunk(cid) for cid in self.html_keys()}

    def resolve_text(self) -> dict:
        """Resolve only the :meth:`text_keys` chunks (HTML-looking or
        not) -- broader than :meth:`resolve_html`, narrower than
        :meth:`resolve_all`."""
        return {cid: self.resolve_chunk(cid) for cid in self.text_keys()}

    def iter_resolved(self) -> Iterator[tuple]:
        """Yield ``(chunk_id, resolved_value)`` pairs one at a time instead
        of building the whole :meth:`resolve_all` dict up front. Useful on
        very large pages when you only need a handful of chunks and want
        to stop early -- e.g. ``next(v for k, v in page.iter_resolved() if
        ...)``. Each value is still cached the same as :meth:`resolve_chunk`,
        so mixing this with other lookups is fine."""
        for chunk_id in self.raw_chunks:
            yield chunk_id, self.resolve_chunk(chunk_id)

    def shape(self, chunk_id: Optional[str] = None, *, max_depth: int = 3) -> Any:
        """A compact summary of the resolved data's *structure* -- key
        names and value types, with actual values collapsed to their type
        name -- for getting a feel for an unfamiliar site's page without
        scrolling through megabytes of :meth:`resolve_all` output. Lists
        are summarized by their first element only (real pages tend to
        have homogeneous lists). Pass a specific `chunk_id` to summarize
        just that chunk; omit it to summarize every chunk on the page.
        `max_depth` bounds how deep the walk goes before collapsing the
        rest of a branch to its type name."""

        def _walk(value: Any, depth: int) -> Any:
            if depth >= max_depth:
                return type(value).__name__
            if isinstance(value, dict):
                return {k: _walk(v, depth + 1) for k, v in value.items()}
            if isinstance(value, list):
                if not value:
                    return []
                return [_walk(value[0], depth + 1)]
            return type(value).__name__

        if chunk_id is not None:
            return _walk(self.resolve_chunk(chunk_id), 0)
        return {cid: _walk(v, 0) for cid, v in self.resolve_all().items()}

    def diff(self, other: "FlightExtractor", *, id_key: Optional[str] = None) -> dict:
        """Compare this page against another crawl (presumably of the same
        URL) and report what changed. Shorthand for
        :func:`diff_pages(self, other, id_key=id_key) <diff_pages>` -- see
        there for details, `id_key`, and caveats."""
        return diff_pages(self, other, id_key=id_key)

    # ------------------------------------------------------------------ #
    # Step 4 -- schema-free search over the fully resolved data.
    # ------------------------------------------------------------------ #
    def find_all(self, predicate: Callable[[Any], bool], root: Any = None,
                 max_results: Optional[int] = None, include_source: bool = False) -> list:
        """Walk the whole resolved tree and collect every node matching
        `predicate`. Set `include_source=True` to get `(node, source)`
        tuples instead of bare nodes, where `source` is the top-level
        chunk id the match was found under (`None` if a custom `root` was
        given instead of searching the whole page) -- handy for tracing a
        match back to roughly where it came from, or for re-fetching just
        that chunk on a future crawl.

        With no `root`, the search starts at each chunk's *value* (the
        synthetic `{chunk_id: value}` wrapper from :meth:`resolve_all`
        itself is never tested against `predicate`, since it isn't really
        part of the page). With an explicit `root`, `root` itself is a
        real node and IS tested against `predicate` before its children,
        the same as any node found while walking the whole page.
        """
        results: list = []
        seen: set = set()

        def emit(node: Any, source: Any) -> None:
            results.append((node, source) if include_source else node)

        def walk(node: Any, source: Any) -> None:
            if max_results is not None and len(results) >= max_results:
                return
            if id(node) in seen:
                return
            if isinstance(node, (dict, list)):
                seen.add(id(node))
            if predicate(node):
                emit(node, source)
                if max_results is not None and len(results) >= max_results:
                    return
            if isinstance(node, dict):
                for v in node.values():
                    walk(v, source)
            elif isinstance(node, list):
                for v in node:
                    walk(v, source)

        if root is None:
            # Searching the whole page: track which top-level chunk id
            # each branch came from, for `include_source`. The wrapper
            # dict itself is a page-level artifact, not a real node, so
            # only its values are walked/tested, not the wrapper itself.
            # Uses `iter_resolved()` rather than `resolve_all()` so that
            # `max_results` (e.g. `find_one`/`find_by_keys`, which pass
            # max_results=1) can stop *resolving* further chunks the
            # moment enough matches are found, not just stop searching --
            # resolving a chunk isn't free, and on a page where the match
            # is near the front this can skip resolving most of the page.
            # Iterate manually (rather than a plain `for`) so the
            # max_results check runs *before* asking the generator for the
            # next chunk -- a plain `for` loop always pulls (and resolves)
            # one chunk ahead of the loop body, which would resolve one
            # extra chunk past where we actually needed to stop.
            resolved_iter = self.iter_resolved()
            while True:
                if max_results is not None and len(results) >= max_results:
                    break
                try:
                    k, v = next(resolved_iter)
                except StopIteration:
                    break
                walk(v, k)
        else:
            # An explicit root IS a real node: test and descend from it.
            walk(root, None)
        return results

    def find_one(self, predicate: Callable[[Any], bool], root: Any = None,
                 include_source: bool = False) -> Any:
        """Like :meth:`find_all` but returns just the first match (or
        `None`). See `include_source` there."""
        r = self.find_all(predicate, root=root, max_results=1, include_source=include_source)
        return r[0] if r else None

    def find_by_keys(self, required_keys: Iterable[str], root: Any = None,
                      include_source: bool = False) -> Any:
        """Find the first dict containing ALL of `required_keys` -- the
        pattern you almost always want: 'give me whatever object looks
        like the data I need', regardless of where this build's component
        tree happened to put it."""
        required_keys = set(required_keys)
        return self.find_one(
            lambda n: isinstance(n, dict) and required_keys <= n.keys(),
            root=root, include_source=include_source,
        )

    def find_all_by_keys(self, required_keys: Iterable[str], root: Any = None,
                          include_source: bool = False, dedupe: bool = False) -> list:
        """Like :meth:`find_by_keys` but returns every matching dict, not
        just the first -- useful for pages with repeated cards/listings
        that all share the same shape (product cards, search results, ...).

        `dedupe`: Flight payloads commonly serialize the same underlying
        object twice at different tree positions (e.g. a listing embedded
        both in a carousel and a full results grid). When True, collapse
        matches that are equal after JSON-serialization (so key order
        doesn't cause false negatives) down to the first occurrence,
        instead of surfacing every redundant copy. Off by default to keep
        existing call sites' output unchanged; `include_source=True`
        results are deduped on the matched value only, keeping each kept
        result's original `(value, source)` pair."""
        required_keys = set(required_keys)
        results = self.find_all(
            lambda n: isinstance(n, dict) and required_keys <= n.keys(),
            root=root, include_source=include_source,
        )
        if not dedupe:
            return results
        seen: set = set()
        deduped = []
        for r in results:
            value = r[0] if include_source else r
            try:
                fingerprint = _json_dumps_sorted(value)
            except TypeError:
                fingerprint = repr(value)
            if fingerprint in seen:
                continue
            seen.add(fingerprint)
            deduped.append(r)
        return deduped

    def find_any_keys(self, any_keys: Iterable[str], root: Any = None,
                       include_source: bool = False) -> list:
        """Like :meth:`find_all_by_keys` but matches a dict containing ANY
        of `any_keys` rather than requiring all of them -- for sites where
        the "same kind" of card uses slightly different field names across
        categories (e.g. a marketplace with different attributes per listing
        type)."""
        any_keys = set(any_keys)
        return self.find_all(
            lambda n: isinstance(n, dict) and bool(any_keys & n.keys()),
            root=root, include_source=include_source,
        )

    def find_by_key_pattern(self, pattern, root: Any = None,
                             include_source: bool = False) -> list:
        """Find every dict with at least one key matching `pattern` (a
        regex string or compiled pattern) -- for sites whose field names
        vary by a suffix/prefix (e.g. `price_usd`, `price_aed`) rather than
        being fixed strings you can pass to :meth:`find_all_by_keys`."""
        compiled = re.compile(pattern) if isinstance(pattern, str) else pattern
        return self.find_all(
            lambda n: isinstance(n, dict) and any(compiled.search(k) for k in n.keys()),
            root=root, include_source=include_source,
        )

    def find_by_type(self, type_value: str, *, key: str = "@type", root: Any = None,
                      include_source: bool = False) -> list:
        """Find every dict whose `key` field equals `type_value` (default key
        "@type", matching schema.org-style typed objects Next.js often embeds
        e.g. {"@type": "Product", ...})."""
        return self.find_all(
            lambda n: isinstance(n, dict) and n.get(key) == type_value,
            root=root, include_source=include_source,
        )

    _URL_LIKE_RE = re.compile(r"^(?:https?://\S+|/[^\s\"'<>]*)$")

    def find_urls(self, *, keys: Optional[Iterable[str]] = None,
                   pattern: Optional[Any] = None, root: Any = None) -> list:
        """Find navigable URLs sitting in the Flight JSON itself, not
        just whatever ends up in rendered `<a href>` tags.

        This matters specifically for Next.js sites: a `<Link>` component
        almost always renders a real anchor tag (so an HTML-based link
        extractor like Scrapy's `LinkExtractor`/`response.follow_all()`
        already sees it) -- but plenty of common UI patterns don't use
        `<Link>` at all: a card grid navigating via an `onClick`
        handler and `router.push(...)`, a "load more" cursor/URL passed
        as page data for a client-side fetch, or a "related items"
        widget whose targets are only present as data, not markup. Those
        URLs are real, present in the page's JSON, and invisible to
        anything that only looks at rendered HTML.

        `keys`: restrict to string values found under specifically these
        key names (e.g. `{"href", "url", "link", "next"}`) -- much more
        precise than scanning every string on the page, since a page can
        easily contain URL-*shaped* strings that aren't links a crawler
        should follow (a canonical-tag value duplicated into page data,
        an image CDN URL, an external share-link). If omitted, scans
        every string value on the page for anything URL-shaped
        (absolute `http(s)://` or a root-relative `/path`), which is
        noisier but doesn't require knowing the site's field names ahead
        of time.

        `pattern`: an additional regex (str or compiled) the URL string
        itself must match -- e.g. `r"/product/"` to only follow product
        detail links, ignoring navigation chrome.

        Returns distinct URLs in the order first seen; relative URLs are
        returned as-is (still relative) -- resolve them yourself (e.g.
        `response.urljoin(url)` in Scrapy) since this method has no
        notion of the page's own base URL.

            page.find_urls(keys={"href"})
            page.find_urls(pattern=r"^https://")          # absolute only
            page.find_urls(keys={"next_page_url"})         # pagination cursor

        See `NextflightSpiderMixin.follow_flight_urls()` for a Scrapy
        convenience that wraps this and yields `response.follow(...)`
        for each result directly."""
        compiled_pattern = None
        if pattern is not None:
            compiled_pattern = re.compile(pattern) if isinstance(pattern, str) else pattern
        key_set = set(keys) if keys is not None else None
        data = self.resolve_all() if root is None else root
        matches: list = []
        seen: set = set()

        def is_url_like(s: str) -> bool:
            return bool(self._URL_LIKE_RE.match(s)) and s not in ("/",)

        def consider(value: Any) -> None:
            if isinstance(value, str) and value not in seen and is_url_like(value):
                if compiled_pattern is not None and not compiled_pattern.search(value):
                    return
                seen.add(value)
                matches.append(value)

        def walk(node: Any) -> None:
            if isinstance(node, dict):
                for k, v in node.items():
                    if key_set is not None:
                        if k in key_set:
                            consider(v)
                    else:
                        consider(v)
                    walk(v)
            elif isinstance(node, list):
                for v in node:
                    walk(v)

        walk(data)
        return matches

    def find_text(self, pattern, root: Any = None) -> list:
        """Regex-search every string value in the resolved tree and return
        the distinct whole string values that contain a match (this is a
        substring search, like `re.search`, not an exact-match filter), in
        the order first seen. Handy for pulling emails, phone numbers,
        prices, or SKUs out of a page without having to know which object
        they live on.

            page.find_text(r"^\\$[\\d,]+(\\.\\d{2})?$")   # dollar amounts
            page.find_text(re.compile(r"[\\w.+-]+@[\\w-]+\\.\\w+"))  # emails
        """
        compiled = re.compile(pattern) if isinstance(pattern, str) else pattern
        data = self.resolve_all() if root is None else root
        matches: list = []
        seen: set = set()

        def walk(node: Any):
            if isinstance(node, str):
                if node not in seen and compiled.search(node):
                    seen.add(node)
                    matches.append(node)
            elif isinstance(node, dict):
                for v in node.values():
                    walk(v)
            elif isinstance(node, list):
                for v in node:
                    walk(v)

        walk(data)
        return matches

    def get(self, path: str, default: Any = None, sep: str = ".") -> Any:
        """Navigate the resolved page with a dotted path of dict keys
        and/or list indices, e.g. ``page.get("3f.props.product.price")`` or
        ``page.get("items.0.name")``. Returns `default` if any segment is
        missing, instead of raising -- meant for quick, tolerant lookups
        once you already know roughly where something lives on this site.

        Also understands the same symbolic ``"props"`` segment that
        `$`-ref paths use to address a React element's props (e.g.
        ``"29.3.props.adMetrics.0.title"``, where index 3 of that
        four-element ``["$", type, key, props]`` list *is* props) -- see
        `_walk_path` -- so navigating resolved data by hand behaves the
        same way references into it do internally.

        Only the chunk the path actually starts from (`"3f"` above) gets
        resolved -- not every chunk on the page -- so looking up a handful
        of known paths with `.get()`/:meth:`select` doesn't pay to resolve
        chunks the path never touches."""
        parts = path.split(sep)
        if not parts or parts[0] not in self.raw_chunks:
            return default
        current: Any = self.resolve_chunk(parts[0])
        return self._walk_path(current, parts[1:], default=default)

    def select(self, *paths: str, default: Any = None, sep: str = ".") -> dict:
        """Resolve just the specific dotted paths you ask for --
        ``page.select("3f.props.price", "3f.props.title", "9.currency")``
        -- instead of the whole page. Returns ``{path: value}`` (using
        `default` for any path that doesn't exist, same as :meth:`get`).

        This is the general form of :meth:`resolve_json` /
        :meth:`resolve_html` / :meth:`resolve_text`, which give you a
        whole *kind* of chunk; `select` gives you exactly the fields you
        name, however many chunks that happens to touch, and nothing
        else -- the natural tool once you know precisely where the data
        you want lives (e.g. from a first exploratory pass with
        :meth:`shape` or :meth:`find_by_keys`)."""
        return {p: self.get(p, default=default, sep=sep) for p in paths}

    def stats(self) -> dict:
        """A quick diagnostic snapshot -- handy the first time you point
        this at a new site and want a feel for what's on the page before
        writing search predicates."""
        row_types: dict[str, int] = {}
        for value in self.raw_chunks.values():
            kind = type(value).__name__
            row_types[kind] = row_types.get(kind, 0) + 1
        flight_row_kinds: dict[str, int] = {}
        for k in self._row_types.values():
            flight_row_kinds[k] = flight_row_kinds.get(k, 0) + 1
        return {
            "chunk_count": len(self.raw_chunks),
            "chunk_ids": self.keys(),
            "value_type_counts": row_types,
            "flight_row_kind_counts": flight_row_kinds,
            "json_chunk_count": len(self.json_keys()),
            "html_chunk_count": len(self.html_keys()),
            "html_size_bytes": len(self.html.encode("utf-8")),
            "resolve_cache_hits": self._cache_hits,
            "resolve_cache_misses": self._cache_misses,
        }

    def _push_call_index_map(self) -> dict:
        """Cached mapping of `chunk_id -> index` of the `self.__next_f
        .push(...)` call (0-based, in document order) it first appears
        in -- the basis for :meth:`streamed_chunks`. Computed once per
        instance and cached, since it requires a full pass over every
        push call's raw text (`_split_rows` per call), same cost class as
        the eager materialization `parse_confidence()` already triggers.

        This relies on a real, if informal, invariant of how Next.js
        emits Flight data: the *first* `push()` call carries the initial
        SSR shell (everything ready before any `<Suspense>` boundary had
        to wait), and each subsequent, separate `push()` call is
        appended to the document only once its corresponding boundary's
        data becomes ready -- so a chunk's presence in call index 0 vs.
        a later index reflects genuine arrival order, not just
        incidental ordering, even when parsing an already-complete,
        fully-buffered page rather than a live stream."""
        if self._push_call_index_cache is None:
            index_map: dict = {}
            for call_index, payload in enumerate(self._iter_push_payloads()):
                for chunk_id, _row_type, _raw_value, _pre in self._split_rows(payload):
                    index_map.setdefault(chunk_id, call_index)
            self._push_call_index_cache = index_map
        return self._push_call_index_cache

    def streamed_chunks(self) -> list:
        """Chunk ids that arrived after the initial shell -- i.e. in a
        `self.__next_f.push(...)` call other than the first one on the
        page -- distinguishing "was present in the initial HTML" from
        "arrived later via streaming" once a `<Suspense>` boundary's data
        became ready. Useful for a scraper that wants to know whether it
        needs to wait/poll for slower-loading data, versus data that's
        guaranteed present in the very first response.

        Returns ids in the order they first appear in `.keys()` (page
        order), not grouped by which push call they arrived in -- use
        :meth:`is_ppr_page`/:meth:`static_vs_dynamic_chunks` instead if
        what's actually wanted is a static/dynamic split rather than an
        arrival-order one; the two are related but not identical
        concepts (PPR's static shell and the streaming shell aren't
        always the same boundary set).

        This is a best-effort inference from `push()` call boundaries in
        the already-fully-buffered HTML (see `_push_call_index_map`'s
        docstring for why that's reliable), not a live measurement --
        for a genuinely live stream where exact timing matters, use
        `from_stream()` instead, which yields chunks as they actually
        arrive."""
        boundaries = self._push_call_index_map()
        return [cid for cid in self.raw_chunks if boundaries.get(cid, 0) > 0]

    def is_ppr_page(self) -> bool:
        """Best-effort detector for a Partial Prerendering (PPR) page --
        one that mixes a static shell with dynamic "holes" filled in at
        request time. There's no single definitive wire-format marker
        for PPR (it's a rendering strategy, not a distinct payload
        format), so this checks for the combination of signals that, in
        practice, only co-occur on a PPR page: more than one `push()`
        call (i.e. :meth:`streamed_chunks` is non-empty -- something
        streamed in after an initial shell) *and* at least one chunk
        resolving through an async placeholder marker (`$@<id>`) in the
        raw, unresolved row text -- PPR's dynamic holes are wired up
        through the same Suspense/streaming machinery as an ordinary
        `loading.tsx` boundary, so this can't perfectly distinguish "PPR"
        from "an ordinary streamed page with a real Suspense boundary"
        in every case; treat a `True` result as "likely PPR or at least
        genuinely streamed," not as a certainty."""
        if not self.streamed_chunks():
            return False
        for chunk_id in self._row_types:
            raw_text = self._raw_row_text.get(chunk_id, "")
            if isinstance(raw_text, str) and re.search(r'"\$@\d+"', raw_text):
                return True
        return False

    def static_vs_dynamic_chunks(self) -> dict:
        """Split chunk ids into `{"static": [...], "dynamic": [...]}`,
        using :meth:`streamed_chunks` as the split point -- chunks
        present in the page's very first `push()` call are treated as
        the static shell (safe to cache aggressively across requests to
        the same route), everything that arrived in a later `push()`
        call as dynamically rendered per-request content (re-fetch on
        every request). This is the same underlying signal as
        :meth:`streamed_chunks`, just returned as a ready-made two-way
        split instead of a single list, for a crawler that specifically
        wants to treat the two groups differently (e.g. cache one, skip
        re-parsing the other) rather than just knowing which ids are
        which."""
        streamed = set(self.streamed_chunks())
        static: list = []
        dynamic: list = []
        for cid in self.raw_chunks:
            (dynamic if cid in streamed else static).append(cid)
        return {"static": static, "dynamic": dynamic}

    def is_fallback_skeleton(self) -> bool:
        """Heuristic check for an ISR `fallback: true`/`'blocking'`
        loading skeleton -- a first request to a not-yet-generated path
        that returns HTTP 200 with a near-empty loading placeholder
        instead of real content. Easily confused with "the format broke"
        by `parse_confidence()`, which scores this page as perfectly
        clean (every row *is* valid JSON) even though there's
        essentially no data on it -- the two signals are checking
        different things and neither alone tells the whole story.

        There's no universal wire-format marker for this state -- it's a
        loading UI a specific app renders, not a Next.js primitive -- so
        this is deliberately a coarse heuristic: a page that parses with
        full confidence but resolves to only a handful of total keys/
        items across all of its JSON chunks combined. Real listing/
        product pages almost always carry more structured data than
        that; a page this sparse that still parsed perfectly is a
        stronger signal of "nothing rendered yet" than of "a small page
        that's just naturally simple," but false positives are possible
        on a genuinely minimal page (e.g. a bare confirmation screen) --
        treat this as a hint to retry-after-a-delay, not a certainty.

        Wire into `FlightRetryMiddleware`-style logic as: treat a
        skeleton hit as "retry after N seconds" rather than "low
        confidence, retry immediately," since an immediate retry likely
        hits the same unfinished generation."""
        json_keys = self.json_keys()
        if not json_keys:
            return False
        if self.parse_confidence()["score"] < 0.99:
            return False  # a real parse problem, not a skeleton -- different signal
        total_items = 0
        for cid in json_keys:
            resolved = self.resolve_chunk(cid)
            if isinstance(resolved, (dict, list)):
                total_items += len(resolved)
            else:
                total_items += 1
        return total_items <= 2

    def parse_confidence(self) -> dict:
        """A score/summary of how cleanly this page's rows parsed, so you
        can flag pages that need investigation instead of manually
        re-reading every row. Returns::

            {
                "score": 0.0-1.0,           # clean rows / total rows
                "total_chunks": int,
                "clean_chunks": int,        # decoded as real JSON/text
                "raw_string_chunks": int,   # fell back to a raw string
                "repaired_chunks": int,     # recovered via repair=True
                "failed_repair_chunks": int,
            }

        A raw-string fallback isn't necessarily a bug -- bare `$`-ref
        markers and symbol names are *expected* to stay as strings -- but
        a page where most chunks fell back is a strong signal something
        about its wire format isn't being recognized (see
        :meth:`next_version_hint`)."""
        total = len(self.raw_chunks)
        raw_string_count = 0
        for cid in self.raw_chunks:
            self._materialize_chunk(cid)  # ensure repair/fallback has run
            row_type = self._row_types[cid]
            if row_type == "text":
                continue
            if isinstance(self._materialized.get(cid), str):
                raw_string_count += 1
        repaired = sum(1 for ok in self._repair_outcomes.values() if ok)
        failed_repair = sum(1 for ok in self._repair_outcomes.values() if not ok)
        clean = total - raw_string_count
        return {
            "score": (clean / total) if total else 1.0,
            "total_chunks": total,
            "clean_chunks": clean,
            "raw_string_chunks": raw_string_count,
            "repaired_chunks": repaired,
            "failed_repair_chunks": failed_repair,
        }

    def next_version_hint(self) -> dict:
        """Best-effort guess at which Next.js version range produced this
        page's Flight payload, based on wire-format markers checked
        against the bundled :mod:`nextflight` ``known_formats.yaml``
        registry (see that file for the underlying, citable reference).

        Returns ``{"range": str | None, "notes": str | None, "matches":
        [str, ...], "bundler": str}`` -- `matches` lists every candidate
        range whose markers were found, since marker sets can overlap
        between adjacent versions; `range`/`notes` are simply the last
        (i.e. most recent) match, a reasonable default when several
        match. `bundler` (**new in 0.4.6**) is `"webpack"`, `"turbopack"`,
        or `"unknown"` -- distinguishing the two matters because they can
        produce structurally different chunk naming, which would
        otherwise show up as a `parse_confidence()` drop that looks like
        a wire-format break but is really just a bundler switch (e.g. a
        project opting into Turbopack). Returns an all-``None``/empty
        `range`/`notes`/`matches` (bundler is still checked and reported)
        if no known version markers were found at all -- not an error,
        just "this library doesn't have a version fingerprint for
        whatever produced this page yet." Parsing itself does not depend
        on this result; it degrades gracefully either way."""
        registry = _load_known_formats()
        matches = []
        for entry in registry:
            markers = entry.get("markers") or []
            if markers and all(marker in self.html for marker in markers):
                matches.append(entry)
        bundler = _detect_bundler(self.html)
        if not matches:
            return {"range": None, "notes": None, "matches": [], "bundler": bundler}
        best = matches[-1]
        return {
            "range": best.get("range"),
            "notes": (best.get("notes") or "").strip() or None,
            "matches": [m.get("range") for m in matches],
            "bundler": bundler,
        }

    def suggest_similar_keys(self, required_keys: Iterable[str], *, cutoff: float = 0.6,
                              max_suggestions: int = 5) -> dict:
        """When :meth:`find_by_keys`/:meth:`find_all_by_keys` comes back
        empty, fuzzy-match `required_keys` against every key actually
        present anywhere in the resolved tree, to help debug "why didn't
        this match" instead of silently getting `None`/`[]` back.

        Returns ``{key: [similar_key, ...]}`` for each of `required_keys`,
        ordered by similarity (best first). A key with no reasonably
        similar match anywhere on the page gets an empty list -- that's a
        much stronger signal ("this field probably isn't on this page/this
        build at all") than a fuzzy near-miss is."""
        import difflib

        present: set = set()

        def walk(node: Any) -> None:
            if isinstance(node, dict):
                present.update(node.keys())
                for v in node.values():
                    walk(v)
            elif isinstance(node, list):
                for v in node:
                    walk(v)

        walk(self.resolve_all())
        present_list = sorted(present)
        return {
            key: difflib.get_close_matches(key, present_list, n=max_suggestions, cutoff=cutoff)
            for key in required_keys
        }

    def extract_as(self, model: type, *, root: Any = None,
                    required_keys: Optional[Iterable[str]] = None) -> Any:
        """Find the first dict matching `required_keys` (defaulting to the
        target's own field names) and coerce it into `model` -- a stdlib
        `dataclasses.dataclass`, a pydantic `BaseModel` (optional
        `pydantic` extra), or a Scrapy `Item` subclass (detected only if
        Scrapy is already installed -- this does not add a hard
        dependency on Scrapy). Extra keys present in the matched dict but
        not on `model` are ignored; missing keys that have no default
        raise the model's own validation error, so a shape mismatch
        fails loudly rather than silently returning a half-populated
        object.

            @dataclass
            class Listing:
                title: str
                price: float

            listing = page.extract_as(Listing)

            # Or, in a Scrapy project:
            class ListingItem(scrapy.Item):
                title = scrapy.Field()
                price = scrapy.Field()

            listing = page.extract_as(ListingItem)

        Returns `None` if no matching dict is found at all. This is a
        thin, optional convenience on top of :meth:`find_by_keys` --
        nothing about the core extraction path depends on it, and no
        schema library is required unless you actually call this."""
        import dataclasses

        model_fields = getattr(model, "model_fields", None)
        is_pydantic = model_fields is not None
        is_dataclass = dataclasses.is_dataclass(model)
        is_scrapy_item = False
        if not (is_pydantic or is_dataclass):
            try:
                import scrapy as _scrapy_pkg
                is_scrapy_item = isinstance(model, type) and issubclass(model, _scrapy_pkg.Item)
            except ImportError:
                pass
        if not (is_pydantic or is_dataclass or is_scrapy_item):
            raise TypeError(
                f"{model!r} is neither a dataclass, a pydantic BaseModel, "
                "nor a scrapy.Item subclass"
            )
        if required_keys is None:
            if is_pydantic:
                assert model_fields is not None
                required_keys = list(model_fields.keys())
            elif is_scrapy_item:
                required_keys = list(model.fields.keys())  # type: ignore[attr-defined]
            else:
                required_keys = [f.name for f in dataclasses.fields(model)]
        match = self.find_by_keys(required_keys, root=root)
        if match is None:
            return None
        if is_pydantic:
            return model(**match)
        if is_scrapy_item:
            field_names = set(model.fields.keys())  # type: ignore[attr-defined]
            return model(**{k: v for k, v in match.items() if k in field_names})
        field_names = {f.name for f in dataclasses.fields(model)}
        return model(**{k: v for k, v in match.items() if k in field_names})

    @classmethod
    def from_stream(cls, chunks: Iterable[Union[str, bytes]], *, strict: bool = False,
                     repair: bool = False) -> Iterator[tuple]:
        """Incrementally parse an iterable of HTML fragments/bytes (e.g. a
        `requests`/`httpx` streaming response body, read chunk-by-chunk)
        and yield ``(chunk_id, resolved_value)`` pairs as soon as each
        Flight row completes, without requiring the full page in memory
        first.

        This is necessarily coarser than parsing a complete page: `$`-ref
        resolution for a chunk can only be fully accurate once every chunk
        it might point to has arrived (a ref to a not-yet-seen chunk id
        resolves to `None` for a stream, whereas a completed-page
        `FlightExtractor` would resolve it correctly) -- rows are yielded
        best-effort, in arrival order, re-resolving already-yielded ids as
        later chunks that complete them arrive is deliberately NOT done,
        since redoing that lazily on a live stream would mean re-yielding
        the same id repeatedly. For a use case where getting every
        cross-reference exactly right matters more than seeing data as it
        streams in, buffer the full response and use the regular
        `FlightExtractor` constructor instead; this is for cases where
        acting on data as it arrives (e.g. a live progress indicator, or
        stopping a slow crawl early once the wanted field shows up) is
        the actual goal.

        Performance note: each new piece triggers a fresh re-scan of the
        entire buffer accumulated so far (necessary for correctness --
        `push()` calls and rows can only be recognized once complete, and
        something that looks like the start of one near the end of a
        piece might only actually complete in a later one). This makes
        total work quadratic in the number of pieces for a given total
        size: fine for a normal page streamed in the tens to low hundreds
        of pieces typical of a real HTTP response (a ~200KB page streamed
        in ~200-byte pieces finishes in well under a second), but a poor
        fit for a very large page deliberately split into many hundreds
        of tiny fragments -- that case should buffer and use the regular
        constructor instead.
        """
        buffer = ""
        extractor = cls.__new__(cls)
        extractor.html = ""
        extractor.strict = strict
        extractor.repair = repair
        extractor.raw_chunks = _LazyRawChunks(extractor)
        extractor._row_types = {}
        extractor._raw_row_text = {}
        extractor._materialized = {}
        extractor._resolved_cache = {}
        extractor._resolving = set()
        extractor._resolving_paths = set()
        extractor._repair_outcomes = {}
        extractor._cache_hits = 0
        extractor._cache_misses = 0
        extractor._push_call_index_cache = None
        seen_ids: set = set()
        for piece in chunks:
            if isinstance(piece, (bytes, bytearray)):
                piece = bytes(piece).decode("utf-8", errors="replace")
            buffer += piece
            extractor.html = buffer
            # Recompute from the accumulated HTML each time a new piece
            # arrives: only *complete* self.__next_f.push(...) calls (and
            # complete rows within their concatenated payload) show up
            # here at all -- `_iter_push_payloads`/`_split_rows` both stop
            # cleanly at whatever's still in flight, so this never yields
            # a row before it's fully arrived. Re-scanning the whole
            # buffer on every piece is O(n^2) over a very long-lived
            # stream, which is an acceptable trade for correctness in the
            # common case (a normal page response streamed in a handful
            # of chunks) -- callers with pathologically large/long-lived
            # streams should buffer and use the regular constructor
            # instead, per the docstring above.
            combined = "".join(extractor._iter_push_payloads())
            for chunk_id, row_type, raw_value, _precomputed in extractor._split_rows(combined):
                if chunk_id in seen_ids:
                    continue
                seen_ids.add(chunk_id)
                extractor._row_types[chunk_id] = row_type
                extractor._raw_row_text[chunk_id] = raw_value
                yield chunk_id, extractor.resolve_chunk(chunk_id)

    def to_json(self, path: Optional[str] = None, *, indent: int = 2) -> Optional[str]:
        """Dump the fully resolved page as JSON. Writes to `path` if given
        (returns None), otherwise returns the JSON string."""
        text = _json_dumps(self.resolve_all(), indent=indent)
        if path is None:
            return text
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        return None

    def to_dataframe(self, records: Optional[Iterable[dict]] = None, *,
                      required_keys: Optional[Iterable[str]] = None):
        """Build a pandas DataFrame from a list of dict records -- typically
        the output of :meth:`find_all_by_keys`. Pass `required_keys`
        instead of `records` to run `find_all_by_keys` for you in one call.

        Requires ``pandas`` (``pip install pandas``) -- optional, not a
        required dependency of this library. Raises ``ImportError`` with a
        clear message if it isn't installed."""
        records = self._resolve_records_arg(records, required_keys)
        try:
            import pandas as pd  # type: ignore
        except ImportError as e:
            raise ImportError(
                "to_dataframe() requires pandas -- install it with `pip install pandas`."
            ) from e
        return pd.DataFrame(records)

    def to_csv(self, path: str, records: Optional[Iterable[dict]] = None, *,
               required_keys: Optional[Iterable[str]] = None) -> None:
        """Write `records` (or the result of
        `find_all_by_keys(required_keys)`) to a CSV file at `path`. Uses
        pandas if it's installed (handles ragged/nested records more
        gracefully); falls back to the stdlib ``csv`` module otherwise, so
        this always works even without pandas."""
        records = self._resolve_records_arg(records, required_keys)
        try:
            import pandas as pd  # type: ignore
            pd.DataFrame(records).to_csv(path, index=False)
            return
        except ImportError:
            pass
        import csv
        if not records:
            open(path, "w", encoding="utf-8").close()
            return
        fieldnames = sorted({k for r in records for k in r.keys()})
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(records)

    def _resolve_records_arg(self, records: Optional[Iterable[dict]],
                              required_keys: Optional[Iterable[str]]) -> list:
        if records is not None:
            return list(records)
        if required_keys is None:
            raise ValueError("Pass either `records` or `required_keys` to build them from the page.")
        return self.find_all_by_keys(required_keys)


class AsyncFlightExtractor(FlightExtractor):
    """Thin, discoverable alias for the async construction path.

    `FlightExtractor` already supports async fetching via
    ``await FlightExtractor.from_url_async(url)`` (see that method) -- the
    class itself has no async state, only its *construction* can be
    async, since parsing a page you already hold in memory is pure CPU
    work. `AsyncFlightExtractor` exists purely so that code (and search
    results, and IDE autocomplete) reaching for "the async one" for
    concurrent multi-page crawling finds it under the name it's looking
    for; it is otherwise byte-for-byte the same class::

        pages = await asyncio.gather(*(
            AsyncFlightExtractor.from_url_async(u) for u in urls
        ))

    Requires the optional `httpx` dependency (``pip install
    nextflight[async]``); see :meth:`FlightExtractor.from_url_async`,
    inherited here unchanged (subclassing preserves `cls`-based
    dispatch, so this correctly constructs `AsyncFlightExtractor`
    instances rather than plain `FlightExtractor` ones)."""


def find_json_ld(html: Any, type_: Optional[str] = None) -> list:
    """Parse any <script type="application/ld+json"> blocks on the page,
    independent of Flight data and often more stable across redesigns --
    worth trying first for structured product/article/breadcrumb data.

    `html` accepts a raw string/bytes, or a response-like object (Scrapy's
    `Response`, `requests.Response`, etc.). `type_` optionally filters
    results by their "@type" (e.g. "Product")."""
    html = _coerce_html(html)
    blocks = re.findall(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html, re.DOTALL,
    )
    out = []
    for b in blocks:
        try:
            parsed = _json_loads(b)
        except _JSON_ERRORS:
            continue
        for c in (parsed if isinstance(parsed, list) else [parsed]):
            if not isinstance(c, dict):
                continue
            types = c.get("@type")
            if type_ is None or types == type_ or (
                isinstance(types, list) and type_ in types
            ):
                out.append(c)
    return out


_NEXT_DATA_RE = re.compile(
    r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>', re.DOTALL,
)


def find_next_data(html: Any) -> Optional[dict]:
    """Parse the Pages Router ``__NEXT_DATA__`` JSON blob
    (``<script id="__NEXT_DATA__" type="application/json">...</script>``),
    Next.js's data-embedding mechanism from before the App Router / Flight
    format. Unlike Flight, this is already plain JSON -- no `$`-ref
    resolution needed, so there's no extractor class for it, just this
    function. Typically has top-level keys like ``props``, ``page``,
    ``query``, ``buildId``.

    `html` accepts a raw string/bytes, or a response-like object. Returns
    `None` if the page doesn't have this block at all -- e.g. it's an App
    Router page instead, in which case try :func:`extract` there. See also
    :func:`detect_next_router` if you're not sure which one a given page
    uses."""
    html = _coerce_html(html)
    m = _NEXT_DATA_RE.search(html)
    if not m:
        return None
    try:
        return _json_loads(m.group(1))
    except _JSON_ERRORS:
        return None


def find_page_props(html: Any) -> Optional[dict]:
    """Convenience for the single most repeated line across Pages Router
    scrapers: ``json.loads(<script id="__NEXT_DATA__">.text())['props']['pageProps']``.
    Returns that `pageProps` dict directly, or `None` -- gracefully, not
    via a raised exception -- if the page has no `__NEXT_DATA__` block at
    all, or if it does but doesn't have the expected `props.pageProps`
    shape (some Next.js pages nest data differently, e.g. under `page`
    instead, or have an empty `props`).

    This matters beyond just saving a line: the naive
    ``json.loads(selector('#__NEXT_DATA__').text())['props']['pageProps']``
    raises `IndexError`/`KeyError`/`json.JSONDecodeError` uncaught on any
    page where the script tag is missing or the shape is slightly off
    (a redirected error page, a locale variant with a different data
    shape, a temporarily broken deploy) -- which kills that request's
    entire `parse()` callback rather than letting the spider log a
    warning and move on to the next request.

        page_props = find_page_props(response.text)
        if page_props is None:
            self.logger.warning("no page data found: %s", response.url)
            return
        car = page_props.get("vehicleListingData")

    For the full raw `__NEXT_DATA__` blob (not just `pageProps` -- e.g.
    you also need `buildId` or `query`), use :func:`find_next_data`
    directly instead."""
    data = find_next_data(html)
    if not isinstance(data, dict):
        return None
    props = data.get("props")
    if not isinstance(props, dict):
        return None
    page_props = props.get("pageProps")
    return page_props if isinstance(page_props, dict) else None


_SERVER_ACTION_ID_RE = re.compile(r'createServerReference\)\("([0-9a-f]{16,64})"')


# ISO 639-1 two-letter language codes -- used by detect_locale() to avoid
# treating an arbitrary two-letter path segment (a product SKU prefix, an
# abbreviated category slug) as a locale just because it happens to be
# the right shape. Not exhaustive of every possible locale a site could
# invent, but covers the common case this function targets.
_ISO_639_1_CODES = frozenset({
    "aa", "ab", "ae", "af", "ak", "am", "an", "ar", "as", "av", "ay", "az",
    "ba", "be", "bg", "bh", "bi", "bm", "bn", "bo", "br", "bs",
    "ca", "ce", "ch", "co", "cr", "cs", "cu", "cv", "cy",
    "da", "de", "dv", "dz",
    "ee", "el", "en", "eo", "es", "et", "eu",
    "fa", "ff", "fi", "fj", "fo", "fr", "fy",
    "ga", "gd", "gl", "gn", "gu", "gv",
    "ha", "he", "hi", "ho", "hr", "ht", "hu", "hy", "hz",
    "ia", "id", "ie", "ig", "ii", "ik", "io", "is", "it", "iu",
    "ja", "jv",
    "ka", "kg", "ki", "kj", "kk", "kl", "km", "kn", "ko", "kr", "ks", "ku", "kv", "kw", "ky",
    "la", "lb", "lg", "li", "ln", "lo", "lt", "lu", "lv",
    "mg", "mh", "mi", "mk", "ml", "mn", "mr", "ms", "mt", "my",
    "na", "nb", "nd", "ne", "ng", "nl", "nn", "no", "nr", "nv", "ny",
    "oc", "oj", "om", "or", "os",
    "pa", "pi", "pl", "ps", "pt",
    "qu",
    "rm", "rn", "ro", "ru", "rw",
    "sa", "sc", "sd", "se", "sg", "si", "sk", "sl", "sm", "sn", "so", "sq", "sr", "ss", "st", "su", "sv", "sw",
    "ta", "te", "tg", "th", "ti", "tk", "tl", "tn", "to", "tr", "ts", "tt", "tw", "ty",
    "ug", "uk", "ur", "uz",
    "ve", "vi", "vo",
    "wa", "wo",
    "xh",
    "yi", "yo",
    "za", "zh", "zu",
})

_HTML_LANG_RE = re.compile(r'<html[^>]+lang=["\']([a-zA-Z0-9_-]+)["\']', re.I)
_LOCALE_PATH_SEGMENT_RE = re.compile(r'^/([a-zA-Z]{2}(?:-[a-zA-Z]{2,4})?)(?=/|$)')
_LOCALE_SUBDOMAIN_RE = re.compile(r'^([a-zA-Z]{2})\.')
_EMBEDDED_URL_RE = re.compile(r'https?://[^\s"\'<>]+')


def detect_locale(html_or_url: Any) -> Optional[str]:
    """Best-effort detection of a page's locale, checking three signals
    in order of reliability:

    1. The rendered `<html lang="...">` attribute -- the most reliable
       signal when present, since it's Next.js's own (or the app's own)
       stated locale rather than an inference from the URL shape.
    2. A path-prefixed locale segment (`/en/...`, `/ar/...`), validated
       against a list of ISO 639-1 language codes so an arbitrary
       two-letter path segment (a SKU prefix, an abbreviated slug) isn't
       mistaken for one.
    3. A locale-coded subdomain (`en.example.com`), same validation.

    `html_or_url` accepts an HTML string, a response-like object (its
    `.text`/`.body` is checked for `<html lang>` and for an embedded
    absolute URL to test against signals 2/3), or a bare URL string
    directly. Returns the locale code as found (e.g. `"en"`, `"en-US"`,
    `"pt-BR"`) preserving its original casing, or `None` if none of the
    three signals matched. This is deliberately simple (per the common
    `/en/` path-prefix pattern seen on most multi-region sites) rather
    than a full BCP 47 validator -- a site using an unusual locale
    scheme may need its own check."""
    text = html_or_url if isinstance(html_or_url, str) else _coerce_html(html_or_url)

    m = _HTML_LANG_RE.search(text)
    if m:
        return m.group(1)

    url_match = _EMBEDDED_URL_RE.search(text)
    candidate = url_match.group(0) if url_match else text
    if "://" in candidate:
        parsed = urllib.parse.urlsplit(candidate)
        path, netloc = parsed.path, parsed.netloc
    else:
        path, netloc = candidate, ""

    m = _LOCALE_PATH_SEGMENT_RE.match(path)
    if m and m.group(1).split("-")[0].lower() in _ISO_639_1_CODES:
        return m.group(1)

    m = _LOCALE_SUBDOMAIN_RE.match(netloc)
    if m and m.group(1).lower() in _ISO_639_1_CODES:
        return m.group(1)

    return None


def find_server_action_ids(html: Any) -> list:
    """Find Next.js Server Action ids embedded in a page or JS chunk's
    text -- the hex ids bound via `createServerReference(...)` in the
    client bundle, which the browser later invokes with a POST request
    carrying a `Next-Action: <id>` header instead of a normal navigation.
    Some sites use server actions for *data fetching* (not just
    mutations), in which case this id is the only way to make the
    equivalent request yourself without a real browser -- there's no
    corresponding GET endpoint to discover any other way.

        action_ids = find_server_action_ids(chunk_response.text)
        for action_id in action_ids:
            yield scrapy.Request(
                page_url, method="POST",
                headers={"Next-Action": action_id},
                body=json.dumps([...]),   # the action's expected argument shape
                callback=self.parse_action_response,
            )

    Server actions are typically defined in a specific `/_next/static/
    chunks/...js` bundle referenced from the page rather than in the
    page's own HTML -- see :func:`find_next_chunk_urls` for locating that
    bundle first. Returns distinct ids in the order found; an empty list
    (not an error) if none are present, which is the common case for
    pages that don't use server actions for data fetching at all.

    This is deliberately a plain regex over raw text, not part of the
    Flight/`$`-ref parsing machinery -- server action wiring lives in
    ordinary (if minified) JavaScript source, not in a `self.__next_f.push`
    row, so there's no row grammar to parse here."""
    html = _coerce_html(html)
    seen: set = set()
    ids: list = []
    for m in _SERVER_ACTION_ID_RE.finditer(html):
        action_id = m.group(1)
        if action_id not in seen:
            seen.add(action_id)
            ids.append(action_id)
    return ids


_API_ROUTE_RE = re.compile(r'fetch\(\s*["\'](/api/[^"\'?\s]+)')


def find_api_routes(html: Any) -> list:
    """Scan a page or JS chunk's raw text for `fetch("/api/...")`-shaped
    calls to a Next.js App Router Route Handler (`app/api/.../route.ts`)
    -- a separate mechanism from Server Actions: a plain REST-ish
    endpoint, no `Next-Action` header required to call it. Rounds out
    data-fetching discovery alongside :func:`find_server_action_ids` and
    :func:`find_next_chunk_urls`.

    This is deliberately a plain regex over raw text, not part of the
    Flight/`$`-ref parsing machinery -- these calls live in ordinary (if
    minified) client JavaScript, not in a `self.__next_f.push` row.
    Query strings are stripped from results (the route itself is what
    matters for discovery; a specific call's query params usually aren't
    reusable as-is). Returns distinct paths in the order found; an empty
    list if none are present, which is the common case for a site that
    only fetches data server-side (already embedded in the Flight
    payload) rather than via a client-side call to its own API route."""
    html = _coerce_html(html)
    seen: set = set()
    routes: list = []
    for m in _API_ROUTE_RE.finditer(html):
        route = m.group(1)
        if route not in seen:
            seen.add(route)
            routes.append(route)
    return routes


_ROUTE_SLOT_KEY_RE = re.compile(r"^@[A-Za-z0-9_-]+$")
_INTERCEPTING_ROUTE_RE = re.compile(r"^\((?:\.{1,3}|\.\.\)\(\.\.)\)$")


def is_route_slot_key(key: Any) -> bool:
    """Whether `key` looks like an App Router parallel-route slot name
    (`@modal`, `@analytics`, ...) rather than an ordinary data field.

    **This does not gate what `find_by_keys()`/`find_all()`/
    `resolve_all()` can see** -- those already walk into a slot's nested
    content transparently, the same as any other dict value, since they
    recurse by *value* rather than by key name. There's no separate
    "slot-aware" search mode to opt into; a `find_by_keys({"price"})`
    call already finds `price` whether it's nested under an ordinary key
    or a `@modal` slot. This helper is for the opposite direction --
    telling a slot key apart from a real data field when *displaying*
    `.shape()` output or writing code that specifically needs to know
    "is this key a parallel-route slot.\""""
    return isinstance(key, str) and bool(_ROUTE_SLOT_KEY_RE.match(key))


def is_intercepting_route_segment(segment: Any) -> bool:
    """Whether a route segment string looks like an App Router
    intercepting-route convention marker -- `(.)`, `(..)`, `(...)`, or
    the two-level `(..)(..)` form -- rather than an ordinary path
    segment. Like :func:`is_route_slot_key`, this is for *identifying*
    the convention (e.g. for a diagnostic tool or `.shape()`-style
    display), not a gate on search: these segments are just ordinary
    string keys/values to every `find_*`/`resolve_*` method already."""
    return isinstance(segment, str) and bool(_INTERCEPTING_ROUTE_RE.match(segment))


_OG_META_RE = re.compile(
    r'<meta\s+(?:property|name)=["\'](og:[\w:.-]+|twitter:[\w:.-]+)["\']\s+content=["\']([^"\']*)["\']',
    re.I,
)
_OG_META_RE_REVERSED = re.compile(
    r'<meta\s+content=["\']([^"\']*)["\']\s+(?:property|name)=["\'](og:[\w:.-]+|twitter:[\w:.-]+)["\']',
    re.I,
)


def find_meta_tags(html: Any) -> dict:
    """Extract Open Graph (`og:*`) and Twitter Card (`twitter:*`)
    `<meta>` tags into a flat dict, e.g. `{"og:title": "...",
    "og:image": "...", "twitter:card": "summary_large_image"}` -- a third
    structured-data fallback source (Flight -> JSON-LD -> meta tags)
    alongside :func:`find_json_ld`. Listing/product sites often duplicate
    title/image/price data in these tags for social-preview purposes,
    which is useful when the Flight data is incomplete or a specific
    field is missing from it.

    Handles both common attribute orders (`property`/`name` before
    `content`, or after) since real-world markup isn't consistent about
    it. Returns an empty dict (not an error) if no matching tags are
    present. When both orders somehow set the same property (unusual),
    the first occurrence in document order wins."""
    html = _coerce_html(html)
    tags: dict = {}
    for m in _OG_META_RE.finditer(html):
        tags.setdefault(m.group(1).lower(), m.group(2))
    for m in _OG_META_RE_REVERSED.finditer(html):
        tags.setdefault(m.group(2).lower(), m.group(1))
    return tags


def discover_urls_from_sitemap(base_url: str, session: Any = None, *, timeout: float = 15.0,
                                _depth: int = 0, _max_depth: int = 3) -> list:
    """Fetch a sitemap and return every `<loc>` URL in it, following one
    level of `<sitemapindex>` nesting automatically -- the most reliable
    way to seed `start_urls` for a full-site crawl on an App Router site
    that generates its sitemap dynamically (`app/sitemap.ts`). This is
    plain XML, not Flight data, but sits alongside this library's other
    fetch helpers since it solves the same "don't hand-roll this per
    project" problem.

    `base_url`: either the site's origin (`"https://example.com"` --
    `/sitemap.xml` is appended automatically) or a direct sitemap/
    sitemap-index URL (anything ending in `.xml` is used as-is).

    `session`, if given, is anything exposing a `requests.Session`-
    compatible `.get(url, timeout=)` method returning a response with
    `.text`. Omitted (the default), the stdlib (`urllib.request`) is used
    instead -- no hard dependency on `requests`. Pure `xml.etree`, no new
    dependency either way.

    Returns an empty list (not an error) if the sitemap can't be fetched
    or parsed at all -- a missing or malformed sitemap shouldn't take
    down a crawl that has other ways to discover URLs (following links,
    a known listing-index page, etc.)."""
    import xml.etree.ElementTree as ET

    url = base_url if base_url.rstrip("/").lower().endswith(".xml") else f"{base_url.rstrip('/')}/sitemap.xml"
    try:
        if session is not None:
            resp = session.get(url, timeout=timeout)
            text = resp.text
        else:
            request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (nextflight)"})
            with urllib.request.urlopen(request, timeout=timeout) as resp_obj:
                text = _read_urllib_response_text(resp_obj)
        root = ET.fromstring(text)
    except Exception:
        return []

    def _local_name(tag: str) -> str:
        return tag.rsplit("}", 1)[-1]

    locs = [el.text.strip() for el in root.iter() if _local_name(el.tag) == "loc" and el.text]
    if _local_name(root.tag) == "sitemapindex" and _depth < _max_depth:
        urls: list = []
        for loc in locs:
            urls.extend(discover_urls_from_sitemap(
                loc, session, timeout=timeout, _depth=_depth + 1, _max_depth=_max_depth,
            ))
        return urls
    return locs


_NEXT_CHUNK_SRC_RE = re.compile(r'src="([^"]*/_next/static/chunks/[^"]+\.js)"')


def detect_base_path(html: Any) -> str:
    """Best-effort detection of a site's `next.config.js` `basePath` (or
    a multi-zone rewrite prefix) -- the part of the URL that comes
    *before* `/_next/...` in asset paths, e.g. `"/docs"` for a site whose
    chunks are served from `/docs/_next/static/chunks/...`. Returns `""`
    (no prefix) for an ordinary default-configured site, which is also
    what's returned if no `/_next/static/chunks/*.js` reference is found
    at all -- this is a heuristic based on the *first* such reference
    seen, not a guarantee, so a mid-migration or genuinely multi-zone
    page mixing more than one prefix may not be handled perfectly."""
    html = _coerce_html(html)
    m = _NEXT_CHUNK_SRC_RE.search(html)
    if not m:
        return ""
    full = m.group(1)
    idx = full.find("/_next/static/chunks/")
    return full[:idx]


def find_next_chunk_urls(html: Any, *, pattern: Optional[Any] = None,
                          base_path: Optional[str] = None) -> list:
    """Find `/_next/static/chunks/*.js` script URLs referenced by a page
    -- useful for locating the specific bundle a page's server action ids
    or other build-time-generated identifiers live in (see
    :func:`find_server_action_ids`), since that's rarely in the page's
    own HTML.

    `pattern`: an additional regex (str or compiled) the URL itself must
    match, e.g. a route-specific chunk naming pattern -- omit to get
    every chunk URL referenced by the page, which is usually dozens and
    mostly irrelevant to any one task.

    `base_path`: restrict results to URLs under this prefix -- needed on
    a site using `next.config.js`'s `basePath`, or a multi-zone
    deployment (several Next.js apps behind one domain via rewrites),
    where assets aren't served under a plain `/_next/...` path and more
    than one prefix might appear on the same page. Leave as `None`
    (default) to return every chunk URL found regardless of prefix --
    which, as of this version, includes the prefix itself where present
    (e.g. `"/docs/_next/static/chunks/123.js"`), unlike earlier versions
    that only matched a bare `/_next/...` path and silently returned
    nothing at all for a non-default `basePath`. Use
    :func:`detect_base_path` first if you need to know the prefix in use
    without hardcoding it.

    Returns URLs (root-relative, prefix included when present) in the
    order found; resolve against the page's own URL yourself (e.g.
    `response.urljoin(url)` in Scrapy)."""
    html = _coerce_html(html)
    compiled = None
    if pattern is not None:
        compiled = re.compile(pattern) if isinstance(pattern, str) else pattern
    normalized_base = base_path.rstrip("/") if base_path is not None else None
    seen: set = set()
    urls: list = []
    for m in _NEXT_CHUNK_SRC_RE.finditer(html):
        url = m.group(1)
        if url in seen:
            continue
        if normalized_base is not None and not url.startswith(f"{normalized_base}/_next/"):
            continue
        if compiled is not None and not compiled.search(url):
            continue
        seen.add(url)
        urls.append(url)
    return urls


def detect_middleware_rewrite(response: Any) -> Optional[str]:
    """Read Next.js Edge Middleware's rewrite/redirect signaling headers
    to surface the *actual* URL a response was silently served for, so a
    crawler doesn't store the response's data under the wrong logical
    URL. `NextResponse.rewrite(...)` in middleware serves different
    content at the *same* URL the crawler requested, with no HTTP
    redirect for `requests`/Scrapy to follow -- only a response header
    reveals it happened at all.

    Checks (in order) `x-middleware-rewrite`, then `x-nextjs-rewrite` (an
    older/alternate header name seen on some deployments). `response`
    accepts anything exposing a `.headers` mapping (a `requests.Response`
    or Scrapy `Response`) or a plain `dict` of headers directly. Returns
    the rewritten URL, or `None` if neither header is present."""
    headers = response if isinstance(response, dict) else getattr(response, "headers", None)
    if headers is None:
        return None
    for name in ("x-middleware-rewrite", "x-nextjs-rewrite"):
        value = _header_get(headers, name)
        if value:
            return value
    return None


def _header_get(headers: Any, name: str) -> Optional[str]:
    """Case-insensitive header lookup that works across a plain `dict`
    (any casing), `requests.Response.headers` (already
    case-insensitive), and Scrapy's `Headers` (bytes keys/values,
    case-insensitive `.get`). Tries a handful of common castings first
    (cheap, no scan needed for the containers that are already
    case-insensitive), then falls back to a linear case-insensitive scan
    over `.items()` for a plain dict with unpredictable key casing."""
    for candidate in (name, name.title(), name.upper(), name.lower(),
                      name.encode("utf-8"), name.title().encode("utf-8")):
        try:
            value = headers.get(candidate)
        except Exception:
            continue
        if value is not None:
            if isinstance(value, (list, tuple)) and value:
                value = value[0]
            if isinstance(value, bytes):
                value = value.decode("utf-8", errors="replace")
            return value
    try:
        items = headers.items()
    except Exception:
        return None
    target = name.lower()
    for key, value in items:
        key_str = key.decode("utf-8", errors="replace") if isinstance(key, bytes) else key
        if key_str.lower() == target:
            if isinstance(value, (list, tuple)) and value:
                value = value[0]
            if isinstance(value, bytes):
                value = value.decode("utf-8", errors="replace")
            return value
    return None


def get_cache_status(response: Any) -> dict:
    """Surface Next.js's ISR/cache-related response headers in one call,
    for smarter re-fetch scheduling in `diff_pages`/`--watch`-style
    polling instead of hitting every URL on a fixed interval regardless
    of whether it's actually due to change.

    Returns a dict with:

    - ``"cache"``: the `x-nextjs-cache` header value verbatim (typically
      ``"HIT"``, ``"MISS"``, or ``"STALE"``), or `None` if absent (common
      for non-Vercel deployments, which may not set this header at all).
    - ``"s_maxage"`` / ``"stale_while_revalidate"``: parsed `int` values
      from the `Cache-Control` header's `s-maxage=<n>` and
      `stale-while-revalidate=<n>` directives, or `None` if not present.
    - ``"age"``: the `Age` header as an `int` (seconds since the response
      was generated at the cache), or `None`.

    `response` accepts anything exposing a `.headers` mapping, or a plain
    `dict` of headers directly."""
    headers = response if isinstance(response, dict) else getattr(response, "headers", None)
    cache_control = _header_get(headers, "cache-control") if headers is not None else None
    result: dict[str, Any] = {
        "cache": _header_get(headers, "x-nextjs-cache") if headers is not None else None,
        "s_maxage": None,
        "stale_while_revalidate": None,
        "age": None,
    }
    if cache_control:
        for directive, key in (("s-maxage", "s_maxage"), ("stale-while-revalidate", "stale_while_revalidate")):
            m = re.search(rf"{directive}=(\d+)", cache_control)
            if m:
                result[key] = int(m.group(1))
    if headers is not None:
        age = _header_get(headers, "age")
        if age is not None:
            try:
                result["age"] = int(age)
            except ValueError:
                pass
    return result


def get_rate_limit_headers(response: Any) -> dict:
    """Surface common rate-limit-signaling response headers in one call
    -- many sites (or the CDN/WAF in front of them) hint at an impending
    block via headers before an outright 429/403, and this is the same
    "read a few well-known headers into a plain dict" family as
    :func:`get_cache_status`.

    Returns a dict with ``"retry_after"`` (seconds, `int`, from
    `Retry-After` -- HTTP-date values are left as the raw string since
    parsing them isn't this function's job), ``"remaining"`` (from
    `X-RateLimit-Remaining` or `RateLimit-Remaining`), and ``"reset"``
    (from `X-RateLimit-Reset` or `RateLimit-Reset`) -- each `None` if not
    present. `response` accepts anything exposing a `.headers` mapping,
    or a plain `dict` of headers directly."""
    headers = response if isinstance(response, dict) else getattr(response, "headers", None)
    if headers is None:
        return {"retry_after": None, "remaining": None, "reset": None}
    retry_after: Any = _header_get(headers, "retry-after")
    if retry_after is not None:
        try:
            retry_after = int(retry_after)
        except ValueError:
            pass  # HTTP-date form -- left as the raw string
    remaining = _header_get(headers, "x-ratelimit-remaining") or _header_get(headers, "ratelimit-remaining")
    reset = _header_get(headers, "x-ratelimit-reset") or _header_get(headers, "ratelimit-reset")
    return {"retry_after": retry_after, "remaining": remaining, "reset": reset}


def get_edge_geo_headers(response: Any) -> dict:
    """Surface Vercel's Edge geolocation headers (used by many sites to
    branch pricing/currency/inventory on apparent request origin) in one
    call, so a crawl can log which geo variant of a page it actually
    received. `response` accepts anything exposing a `.headers` mapping,
    or a plain `dict` of headers directly. Returns a dict with
    ``"country"``, ``"city"``, ``"region"`` -- each `None` if the
    corresponding header isn't present (most non-Vercel deployments won't
    set these at all)."""
    headers = response if isinstance(response, dict) else getattr(response, "headers", None)
    if headers is None:
        return {"country": None, "city": None, "region": None}
    return {
        "country": _header_get(headers, "x-vercel-ip-country"),
        "city": _header_get(headers, "x-vercel-ip-city"),
        "region": _header_get(headers, "x-vercel-ip-region"),
    }


_DRAFT_MODE_COOKIE_NAMES = ("__prerender_bypass", "__next_preview_data")


def _extract_cookie_names(response: Any) -> set:
    """Best-effort extraction of cookie *names* present on/for a
    response-like object, across a few different shapes: a plain
    `dict`/`set`/`list` of names or name->value pairs, a
    `requests.Response` (`.cookies` is an iterable of cookie objects with
    `.name`, or a plain mapping), or a Scrapy `Response` (cookie names
    only discoverable via `Set-Cookie` response headers, not a `.cookies`
    attribute). Used by :func:`detect_draft_mode`."""
    if isinstance(response, dict):
        return set(response.keys())
    if isinstance(response, (list, tuple, set, frozenset)):
        return set(response)
    names: set = set()
    cookies_attr = getattr(response, "cookies", None)
    if cookies_attr is not None:
        try:
            names.update(cookies_attr.keys())  # dict-like
        except AttributeError:
            try:
                for c in cookies_attr:  # iterable of Cookie-like objects
                    name = getattr(c, "name", None)
                    if name:
                        names.add(name)
            except TypeError:
                pass
    headers = getattr(response, "headers", None)
    if headers is not None:
        set_cookie_values = []
        getlist = getattr(headers, "getlist", None)
        if callable(getlist):
            for key in ("Set-Cookie", b"Set-Cookie"):
                try:
                    values = getlist(key)
                except Exception:
                    values = None
                if values:
                    set_cookie_values = values
                    break
        else:
            value = _header_get(headers, "set-cookie")
            if value:
                set_cookie_values = [value]
        for raw in set_cookie_values:
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8", errors="replace")
            name = raw.split("=", 1)[0].strip()
            if name:
                names.add(name)
    return names


def detect_draft_mode(response: Any) -> bool:
    """Check whether a response carries Next.js Draft Mode's cookies
    (`__prerender_bypass` / `__next_preview_data`) -- if a scraping
    session picks one of these up (a stray redirect through a preview
    link, a shared cookie jar), Draft Mode bypasses the ISR cache and
    every subsequent request on that session silently serves different
    (preview/draft) content instead of the normal published page, with
    no other visible signal that anything changed.

    `response` accepts a `requests.Response`, a Scrapy `Response`, or a
    plain `dict`/`set`/`list` of cookie names -- see
    :func:`_extract_cookie_names`. See also `FlightSession`'s
    `strip_draft_cookies` option, and `FlightMiddleware`'s
    `NEXTFLIGHT_STRIP_DRAFT_COOKIES` setting, to actively remove these
    instead of just detecting them."""
    return bool(_extract_cookie_names(response) & set(_DRAFT_MODE_COOKIE_NAMES))


_ERROR_DIGEST_RE = re.compile(r'"digest"\s*:\s*"([a-zA-Z0-9_-]+)"')


def find_error_digest(html: Any) -> Optional[str]:
    """Extract a Server Component error's `digest` correlation id from a
    rendered error page, when present -- not scraping data, but useful
    when *diagnosing* a scrape that came back empty: logging
    `"parse failed, digest=abc123"` is far more actionable than just
    "empty page" when reporting an issue against the target site or
    searching your own crawl/server logs for the matching error. Returns
    `None` if no digest is found, which is the common case for a page
    that simply doesn't have Next.js data at all rather than one that
    errored server-side."""
    html = _coerce_html(html)
    m = _ERROR_DIGEST_RE.search(html)
    return m.group(1) if m else None


_PAGINATION_KEY_SETS = (
    {"hasNextPage", "endCursor"},
    {"hasNextPage", "cursor"},
    {"hasNextPage", "nextCursor"},
    {"nextCursor"},
    {"endCursor"},
)


def find_pagination_action(page: Any) -> Optional[dict]:
    """Look for the common cursor-pagination shape (`hasNextPage`,
    `cursor`/`endCursor`/`nextCursor` keys) anywhere in a page's Flight
    data, via `find_by_keys` under the hood -- pre-packaged so each
    project doesn't reinvent this detection per site. Many listing sites
    paginate via a Server Action or an RSC fetch keyed off a cursor
    rather than a plain `?page=2` URL, which `find_urls()` alone won't
    catch since there's no URL string to find at all.

    `page` accepts a `FlightExtractor` (or anything with a
    `find_by_keys`/`find_any_keys` method), or raw HTML/a response-like
    object, which is parsed with :func:`extract` first.

    Returns the first matching dict found (checked in the order listed
    above, most-specific shape first), or `None` if nothing matching any
    of the known shapes is present."""
    extractor = page if hasattr(page, "find_by_keys") else extract(page)
    for key_set in _PAGINATION_KEY_SETS:
        match = extractor.find_by_keys(key_set)
        if match is not None:
            return match
    return None


def detect_next_router(html: Any) -> str:
    """Best-effort guess at which Next.js router rendered a page:

    - ``"app"`` -- Flight data found, either the usual
      ``self.__next_f.push(...)`` wrapper embedded in HTML, or a raw RSC
      fetch response (see :meth:`FlightExtractor.from_rsc_url`); use
      :func:`extract` either way.
    - ``"pages"`` -- a ``__NEXT_DATA__`` block found; use
      :func:`find_next_data`.
    - ``"both"`` -- both patterns found (rare -- e.g. a Pages Router page
      embedding an App Router island, or a mid-migration site).
    - ``"static_export"`` -- `output: 'export'` build. A Pages Router
      export still has `__NEXT_DATA__` but flags it with a top-level
      `"nextExport": true` field and no server-side data-fetching
      methods; an App Router export has *no* Flight/`__NEXT_DATA__` at
      all by design, so this falls back to a chunk-URL fingerprint
      (`/_next/static/...` present in the HTML with neither of the other
      two markers) to tell "static export" apart from "not Next.js at
      all". Parsing "failure" against a `static_export` page isn't a
      bug -- there's no server-rendered data payload to parse, ever.
    - ``"unknown"`` -- none of the above found. Could mean the page isn't
      server-rendered by Next.js at all, or uses a wire format version
      this library doesn't recognize yet.

    `html` accepts a raw string/bytes, or a response-like object."""
    html = _coerce_html(html)
    has_flight = bool(FlightExtractor._PUSH_CALL_RE.search(html))
    has_raw_rsc = (not has_flight) and FlightExtractor._looks_like_raw_rsc_payload(html)
    next_data_match = _NEXT_DATA_RE.search(html)
    has_next_data = bool(next_data_match)
    if (has_flight or has_raw_rsc) and has_next_data:
        return "both"
    if has_flight or has_raw_rsc:
        return "app"
    if has_next_data:
        assert next_data_match is not None
        try:
            parsed = _json_loads(next_data_match.group(1))
        except _JSON_ERRORS:
            parsed = None
        if isinstance(parsed, dict) and parsed.get("nextExport") is True:
            return "static_export"
        return "pages"
    if _NEXT_CHUNK_SRC_RE.search(html):
        return "static_export"
    return "unknown"


def _flatten_for_diff(data: Any, prefix: str = "", id_key: Optional[str] = None) -> dict:
    """Flatten a nested dict/list into ``{path: scalar_or_empty}``,
    recursing into non-empty dicts/lists and stopping at scalars (or empty
    containers, which are kept as leaf values so an emptied-out list/dict
    still shows up as a change). Internal helper for :func:`diff_pages`.

    When `id_key` is given and a list's elements are all dicts containing
    that key, list elements are addressed by `[id_key=value]` instead of
    by position (`adMetrics[listing_id=7165546].price` instead of
    `adMetrics.3.price`) -- see :func:`diff_pages` for why that matters."""
    flat: dict = {}
    if isinstance(data, dict) and data:
        for k, v in data.items():
            child_prefix = f"{prefix}.{k}" if prefix else str(k)
            flat.update(_flatten_for_diff(v, child_prefix, id_key))
    elif isinstance(data, list) and data:
        use_id = bool(id_key) and all(
            isinstance(item, dict) and id_key in item for item in data
        )
        for i, v in enumerate(data):
            if use_id:
                child_prefix = f"{prefix}[{id_key}={v[id_key]!r}]"
            else:
                child_prefix = f"{prefix}.{i}" if prefix else str(i)
            flat.update(_flatten_for_diff(v, child_prefix, id_key))
    else:
        flat[prefix or "$"] = data
    return flat


def diff_pages(old: "FlightExtractor", new: "FlightExtractor", *, id_key: Optional[str] = None) -> dict:
    """Compare two crawls of (presumably) the same URL and report what
    changed, scalar-by-scalar, by path -- e.g. for a price/stock
    monitoring pipeline that re-scrapes a page periodically.

    Returns ``{"added": {path: value}, "removed": {path: value}, "changed":
    {path: (old_value, new_value)}}``.

    By default, list elements are addressed by position (`items.3.price`),
    which means inserting or removing a single element shifts every
    later index and makes everything after it look "changed" even though
    it didn't. If your data has a stable identifier -- a `listing_id`,
    `id`, `sku`, etc. -- pass it as `id_key` and lists of dicts containing
    that key are addressed by value instead (`items[listing_id=123].price`),
    so reordering or inserting elsewhere in the list no longer produces
    spurious diffs for unrelated items. Only applies to lists where every
    element is a dict containing `id_key`; lists that don't match (mixed
    types, or missing the key on some elements) still fall back to
    positional indexing.

    Caveat: Flight chunk ids are arbitrary per build (a redeploy can
    renumber them), and this diffs *fully resolved* data by path within
    each chunk id, not raw chunk ids. If the surrounding component tree
    reshuffled between crawls (a redeploy, not just new data), paths may
    not line up 1:1 and spurious add/remove pairs can appear alongside the
    real changes. Treat this as a "what looks different" signal to
    triage, not a guaranteed clean diff across arbitrary redeploys -- it's
    most reliable when comparing two crawls close together in time (e.g.
    polling the same live build every few minutes)."""
    old_flat = _flatten_for_diff(old.resolve_all(), id_key=id_key)
    new_flat = _flatten_for_diff(new.resolve_all(), id_key=id_key)
    old_keys, new_keys = old_flat.keys(), new_flat.keys()
    return {
        "added": {k: new_flat[k] for k in new_keys - old_keys},
        "removed": {k: old_flat[k] for k in old_keys - new_keys},
        "changed": {
            k: (old_flat[k], new_flat[k])
            for k in old_keys & new_keys
            if old_flat[k] != new_flat[k]
        },
    }


def extract(html: Any, *, strict: bool = False) -> FlightExtractor:
    """Shorthand for ``FlightExtractor(html)``. `html` accepts a raw
    string/bytes, or a response-like object (Scrapy's `Response`,
    `requests.Response`, etc.) -- you can pass `response` straight from a
    Scrapy `parse()` method without writing `response.text` yourself."""
    return FlightExtractor(html, strict=strict)


# ------------------------------------------------------------------------ #
# `/_next/image` URL helpers
# ------------------------------------------------------------------------ #

def resolve_next_image_url(next_image_url: str) -> str:
    """Decode a Next.js `/_next/image?url=...&w=...&q=...` image-optimizer
    proxy URL back to the original source URL it's serving a resized copy
    of. Handles both a root-relative source (`url=%2Fphotos%2F1.jpg`) and
    an absolute one (`url=https%3A%2F%2Fcdn.example.com%2F1.jpg`) --
    resolve the (possibly root-relative) result against the page's own
    URL yourself if you need an absolute one (e.g. `response.urljoin(...)`
    in Scrapy). Pure `urllib.parse` -- no new dependency.

    Raises `ValueError` if `next_image_url` has no `url` query parameter
    at all, which usually means it's already an original, un-proxied
    image URL rather than a `/_next/image` link."""
    parsed = urllib.parse.urlsplit(next_image_url)
    params = urllib.parse.parse_qs(parsed.query)
    if "url" not in params or not params["url"]:
        raise ValueError(
            f"{next_image_url!r} doesn't look like a Next.js /_next/image "
            "proxy URL -- no 'url' query parameter found."
        )
    return params["url"][0]


def build_next_image_url(base_url: str, image_url: str, *, width: int, quality: int = 75) -> str:
    """Build a Next.js `/_next/image?url=...&w=...&q=...` proxy URL for a
    specific width/quality, for cases where a scraper wants a particular
    resolution rather than whatever size happened to render on the page
    it found `image_url` on. `base_url` is the site's own origin (e.g.
    `"https://example.com"`, no trailing slash required); `image_url` is
    the original source image URL (root-relative or absolute) to request
    a resized copy of.

    The result round-trips through :func:`resolve_next_image_url` back to
    `image_url` exactly, since both use `urllib.parse`'s matching
    encode/decode pair under the hood."""
    query = urllib.parse.urlencode({"url": image_url, "w": width, "q": quality})
    return f"{base_url.rstrip('/')}/_next/image?{query}"


def resolve_next_image_srcset(html_or_tag: Any) -> list:
    """Parse an `<img srcset="...">` (or a bare `srcset` attribute value)
    into `{"width": int, "url": str}` entries, decoding each candidate
    through :func:`resolve_next_image_url` -- Next.js's `<Image>`
    component typically emits several resolutions in one `srcSet`, and
    :func:`resolve_next_image_url` alone only handles one URL at a time.
    Non-`/_next/image` candidates in the set (rare, but possible with a
    custom loader) are skipped rather than raising. Accepts either a full
    HTML fragment/page (the first `srcset="..."` found is used) or a bare
    `srcset` attribute value string directly."""
    html = _coerce_html(html_or_tag) if not isinstance(html_or_tag, str) else html_or_tag
    m = re.search(r'srcset="([^"]+)"', html) if "srcset=" in html else None
    srcset = m.group(1) if m else html
    entries = []
    for candidate in srcset.split(","):
        candidate = candidate.strip()
        if not candidate:
            continue
        parts = candidate.split()
        url = parts[0]
        width = None
        if len(parts) > 1 and parts[1].endswith("w"):
            try:
                width = int(parts[1][:-1])
            except ValueError:
                width = None
        try:
            resolved = resolve_next_image_url(urllib.parse.unquote(url) if "&amp;" not in url
                                               else resolve_next_image_url(url.replace("&amp;", "&")))
        except ValueError:
            continue
        entries.append({"width": width, "url": resolved})
    return entries


# ------------------------------------------------------------------------ #
# Bot-mitigation / access-wall fingerprinting
# ------------------------------------------------------------------------ #

_CHALLENGE_FINGERPRINTS = (
    ("cloudflare", re.compile(
        r"cf-browser-verification|Attention Required! \| Cloudflare|"
        r"cf-chl-|Just a moment\.\.\.|__cf_chl_", re.I)),
    ("akamai", re.compile(r"akamai(?:ghost|-bot-manager)|_abck=|ak_bmsc", re.I)),
    ("datadome", re.compile(r"datadome|dd_cookie_test_|geo\.captcha-delivery\.com", re.I)),
    ("perimeterx", re.compile(r"_px3=|perimeterx|px-captcha", re.I)),
)


def detect_challenge_page(response: Any) -> Optional[str]:
    """Best-effort fingerprint check for a bot-mitigation "challenge"
    page (Cloudflare, Akamai, DataDome, PerimeterX) served with an HTTP
    200 status -- the request technically "succeeded" but the body is an
    interstitial/CAPTCHA page, not real site content.

    This is a different signal from a low `parse_confidence()` score:
    confidence only tells you the page *isn't* well-formed Flight data,
    not *why* -- a genuinely truncated response and a deliberate block
    both score low, but call for opposite reactions (retry plainly vs.
    rotate proxy/User-Agent). Wire this into `FlightRetryMiddleware` (or
    a spider's own `process_response`) to tell the two apart.

    `response` accepts a raw HTML string/bytes, or a response-like object
    (checked for both `.headers` and body text when available -- some
    challenge vendors set a fingerprintable header even when the body
    itself is truncated). Returns a short vendor label or `None`; `None`
    does not guarantee the page is legitimate, only that it didn't match
    a signature this function currently checks for."""
    html = _coerce_html(response)
    headers = getattr(response, "headers", None)
    header_blob = ""
    if headers is not None:
        try:
            header_blob = " ".join(f"{k}:{v}" for k, v in dict(headers).items())
        except Exception:
            header_blob = str(headers)
    haystack = html[:20000] + " " + header_blob
    for vendor, pattern in _CHALLENGE_FINGERPRINTS:
        if pattern.search(haystack):
            return vendor
    return None


_VERCEL_PROTECTION_RE = re.compile(
    r"_vercel_sso_nonce|vercel\.com/sso-api|Vercel Authentication|"
    r"Authentication Required[\s\S]{0,200}Vercel",
    re.I,
)


def detect_deployment_protection(response: Any) -> bool:
    """Fingerprint check for Vercel Deployment Protection's login-wall
    page -- a preview (or protected production) deployment sitting behind
    Vercel's own auth gate returns an HTTP 200 whose body is a generic
    "Authentication Required" page, easy to silently misread as "the page
    was just mostly empty" rather than "we were blocked before reaching
    the app at all." Same spirit as :func:`detect_challenge_page` but
    checked separately since it's a distinct, Vercel-specific signature
    rather than a general-purpose WAF product's."""
    html = _coerce_html(response)
    return bool(_VERCEL_PROTECTION_RE.search(html[:20000]))


# ------------------------------------------------------------------------ #
# Server Action invocation
# ------------------------------------------------------------------------ #

def _is_file_like(value: Any) -> bool:
    return hasattr(value, "read") and callable(getattr(value, "read"))


def _encode_action_args(args: list) -> tuple:
    """Return `(body_bytes, content_type)` for POSTing Server Action
    arguments the way React's own client runtime does: a plain JSON
    array (`Content-Type: text/plain;charset=UTF-8`) when every argument
    is JSON-serializable, or `multipart/form-data` (one part per
    argument, synthetic field names `"0"`, `"1"`, ...) when any argument
    is a file/Blob-like object (anything with a `.read()` method)."""
    if not any(_is_file_like(a) for a in args):
        try:
            return _json_dumps(list(args)).encode("utf-8"), "text/plain;charset=UTF-8"
        except TypeError:
            pass  # some arg isn't JSON-serializable either -- fall through
    boundary = uuid.uuid4().hex
    chunks = []
    for i, arg in enumerate(args):
        name = str(i)
        if _is_file_like(arg):
            filename = getattr(arg, "name", f"blob{i}")
            data = arg.read()
            if isinstance(data, str):
                data = data.encode("utf-8")
            chunks.append(
                (f'--{boundary}\r\n'
                 f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'
                 f'Content-Type: application/octet-stream\r\n\r\n').encode("utf-8")
                + data + b"\r\n"
            )
        else:
            value = arg if isinstance(arg, str) else _json_dumps(arg)
            chunks.append(
                (f'--{boundary}\r\n'
                 f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
                 f'{value}\r\n').encode("utf-8")
            )
    chunks.append(f"--{boundary}--\r\n".encode("utf-8"))
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


def capture_router_state_tree_hint(html: Any = "") -> str:
    """Best-effort guess at a page's `Next-Router-State-Tree` header
    value, for use with :func:`call_server_action` when no real
    browser-captured header is available for the route.

    **This is unreliable and deliberately just a fallback, not a real
    solution.** The actual router state tree encodes the full parallel-
    route segment tree as seen by a real browser session -- including any
    `@slot` structure, dynamic segment values, and loading/error boundary
    markers -- and there is no general way to reconstruct that from
    static HTML alone. Next.js may reject a mismatched tree (surfacing as
    `ActionNotFoundError` or a stale/incorrect render) even when the
    action id itself is perfectly valid. For anything beyond the
    simplest single-segment route, capture the real header once from a
    browser's Network tab per distinct route shape and pass it explicitly
    via `router_state_tree=` instead of relying on this.

    Returns a JSON-encoded string for the simplest possible route shape
    (a single root segment, no active children) -- correct for that case
    and nothing more elaborate. `html` is currently unused (reserved for
    a future, smarter heuristic) but accepted so call sites can pass a
    page's HTML now and benefit later without an API change."""
    return json.dumps(["", {}, None, None, True])


def call_server_action(url: str, action_id: str, args: Iterable = (), *,
                        router_state_tree: str, session: Any = None,
                        headers: Optional[dict] = None, cookies: Optional[dict] = None,
                        timeout: float = 15.0, strict: bool = False) -> FlightExtractor:
    """Invoke a Next.js Server Action over plain HTTP and parse the
    response -- the missing counterpart to `find_server_action_ids()`,
    which only *discovers* an action id, not calls it.

    Posts to `url`, which must be **the page's own URL** -- there is no
    separate action-invocation endpoint in Next.js, the framework
    dispatches based on the `Next-Action` request header alone. This is
    the single most common mistake when calling this function by hand.

    `args` is encoded the way React's client runtime encodes them (see
    `_encode_action_args`): a JSON array when everything in it is plain
    JSON-serializable data, or `multipart/form-data` if anything looks
    like a file (has a `.read()` method).

    `router_state_tree` is required and cannot generally be derived from
    HTML alone -- it's the router's current segment tree as a real
    browser sees it. Capture it once per distinct route shape from a
    browser's Network tab (look for the `Next-Router-State-Tree` request
    header on any client-side navigation), or pass the output of
    `capture_router_state_tree_hint()` as an explicitly-labeled,
    unreliable best-effort fallback.

    `session`, if given, is any object exposing a `requests.Session`-
    compatible `.post(url, data=, headers=, cookies=, timeout=)` method
    returning a response with `.text` (and, ideally, `.status_code`).
    When omitted (the default), the request is made with the stdlib
    (`urllib.request`) instead -- no hard dependency on `requests`.

    The response may contain both the action's own direct return value
    and a revalidated render (from `revalidatePath`/`revalidateTag`
    inside the action) as separate chunks in the same payload -- both
    are present in the returned `FlightExtractor` (e.g. via
    `.resolve_all()`), not just one.

    Raises `ActionNotFoundError` (a `FlightRequestError` subclass) if the
    server reports it can't find `action_id` -- almost always because a
    redeploy regenerated action ids since this one was scraped from an
    earlier page load; re-run `find_server_action_ids()` against a fresh
    page to get a current id."""
    body, content_type = _encode_action_args(list(args))
    req_headers = {
        "Accept": "text/x-component",
        "Next-Action": action_id,
        "Next-Router-State-Tree": urllib.parse.quote(router_state_tree),
        "Content-Type": content_type,
    }
    if headers:
        req_headers.update(headers)

    status = None
    if session is not None:
        resp = session.post(url, data=body, headers=req_headers, cookies=cookies, timeout=timeout)
        text = resp.text
        status = getattr(resp, "status_code", None)
    else:
        request = urllib.request.Request(url, data=body, headers=req_headers, method="POST")
        if cookies:
            request.add_header("Cookie", "; ".join(f"{k}={v}" for k, v in cookies.items()))
        try:
            with urllib.request.urlopen(request, timeout=timeout) as resp_obj:
                text = _read_urllib_response_text(resp_obj)
                status = resp_obj.status
        except urllib.error.HTTPError as e:
            text = e.read().decode("utf-8", errors="replace")
            status = e.code

    if "Failed to find Server Action" in text:
        raise ActionNotFoundError(
            f"Server Action {action_id!r} was not found at {url!r} "
            f"(HTTP {status}). This almost always means a redeploy "
            "regenerated action ids since this one was scraped -- "
            "re-discover a current id with find_server_action_ids() "
            "against a fresh copy of the page."
        )
    return FlightExtractor(text, strict=strict)


class FlightSession:
    """Thin session wrapper for chasing Next.js Server Actions across
    multiple requests -- the same role `requests.Session` plays for
    cookies, but for `Next-Router-State-Tree` continuity instead.

    `call_server_action()` (module-level) handles one isolated call.
    Real multi-step workflows (load a listing page, then call its
    "load more" action, then call it again with the next cursor) need
    cookies *and* the router state tree carried forward automatically
    instead of the caller manually threading both through spider
    instance attributes or script-level globals::

        session = FlightSession()
        page = session.get("https://example.com/listings")
        next_page = session.call_action(
            "https://example.com/listings", action_id, [cursor],
        )

    `.get()` remembers the (best-effort) router state tree per route
    automatically after each call, via `capture_router_state_tree_hint()`
    -- see that function's docstring for why this is a *best-effort*
    fallback, not a guarantee. Pass `router_state_tree=` explicitly to
    `.call_action()` to override it with a real, browser-captured value
    whenever accuracy matters.

    By default this uses only the stdlib (`urllib.request` + a
    `http.cookiejar.CookieJar` for cookie persistence) -- no `requests`
    dependency required. Pass `backend=` a `requests.Session` (or
    anything duck-typing its `.get()`/`.post()` interface) to use that
    instead, e.g. for connection pooling, retries, or proxy support
    already configured on it.

    `strip_draft_cookies=True` (the default) actively removes Next.js
    Draft Mode's `__prerender_bypass`/`__next_preview_data` cookies from
    the session after every `.get()` -- see :func:`detect_draft_mode`'s
    docstring for why letting one of these linger on a scraping session
    is dangerous (it silently makes every subsequent request bypass the
    ISR cache and serve draft content instead of the published page).
    Pass `False` if a workflow genuinely needs Draft Mode active on
    purpose."""

    def __init__(self, *, backend: Any = None, headers: Optional[dict] = None,
                 timeout: float = 15.0, strict: bool = False,
                 strip_draft_cookies: bool = True) -> None:
        self._backend = backend
        self._cookiejar = None
        self._opener = None
        if backend is None:
            import http.cookiejar
            self._cookiejar = http.cookiejar.CookieJar()
            self._opener = urllib.request.build_opener(
                urllib.request.HTTPCookieProcessor(self._cookiejar)
            )
        self.headers = headers or {
            "User-Agent": "Mozilla/5.0 (nextflight)",
            "Accept-Encoding": "identity",
        }
        self.timeout = timeout
        self.strict = strict
        self.strip_draft_cookies = strip_draft_cookies
        self._router_state_trees: dict = {}

    @staticmethod
    def _route_key(url: str) -> str:
        return urllib.parse.urlsplit(url).path or "/"

    def _cookies_dict(self) -> Optional[dict]:
        if self._cookiejar is None:
            return None
        return {c.name: c.value for c in self._cookiejar}

    def _strip_draft_cookies_from_jar(self) -> None:
        if self._cookiejar is None or not self.strip_draft_cookies:
            return
        for cookie in list(self._cookiejar):
            if cookie.name in _DRAFT_MODE_COOKIE_NAMES:
                self._cookiejar.clear(cookie.domain, cookie.path, cookie.name)

    def get(self, url: str, *, headers: Optional[dict] = None) -> FlightExtractor:
        """GET `url`, parse it, and remember a best-effort router-state-
        tree hint for its route for a later `.call_action()`. Cookies set
        on the response are retained for subsequent calls on this
        session, same as `requests.Session` -- except for Draft Mode's
        cookies, which are stripped immediately afterward unless
        `strip_draft_cookies=False` was passed to the constructor (see
        class docstring)."""
        req_headers = dict(self.headers)
        if headers:
            req_headers.update(headers)
        if self._backend is not None:
            resp = self._backend.get(url, headers=req_headers, timeout=self.timeout)
            text = resp.text
            if self.strip_draft_cookies:
                jar = getattr(self._backend, "cookies", None)
                if jar is not None:
                    for name in _DRAFT_MODE_COOKIE_NAMES:
                        try:
                            del jar[name]
                        except KeyError:
                            pass
        else:
            assert self._opener is not None
            request = urllib.request.Request(url, headers=req_headers)
            with self._opener.open(request, timeout=self.timeout) as resp_obj:
                text = _read_urllib_response_text(resp_obj)
            self._strip_draft_cookies_from_jar()
        self._router_state_trees[self._route_key(url)] = capture_router_state_tree_hint(text)
        return FlightExtractor(text, strict=self.strict)

    def call_action(self, url: str, action_id: str, args: Iterable = (), *,
                     router_state_tree: Optional[str] = None,
                     headers: Optional[dict] = None) -> FlightExtractor:
        """Call a Server Action at `url`, reusing this session's cookies
        and (unless overridden) its remembered router-state-tree hint for
        that route -- see :func:`call_server_action` for the underlying
        request semantics. Pass `router_state_tree=` explicitly with a
        real, browser-captured value whenever the best-effort hint isn't
        good enough for the target route's shape."""
        tree = (
            router_state_tree
            or self._router_state_trees.get(self._route_key(url))
            or capture_router_state_tree_hint()
        )
        result = call_server_action(
            url, action_id, args,
            router_state_tree=tree,
            session=self._backend,
            headers={**self.headers, **(headers or {})},
            cookies=self._cookies_dict(),
            timeout=self.timeout,
            strict=self.strict,
        )
        self._router_state_trees[self._route_key(url)] = tree
        return result

