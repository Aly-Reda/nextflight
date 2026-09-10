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

import json
import re
import urllib.request
from typing import Any, Callable, Iterable, Iterator, Optional

# Optional accelerator: if the caller already has orjson installed (common
# in scraping stacks), use it for JSON decoding -- it's a drop-in replacement
# that's typically several times faster than the stdlib on the array/object
# shapes Flight payloads produce. Falls back to stdlib json with zero
# required dependencies either way.
try:
    import orjson as _orjson  # type: ignore
except ImportError:  # pragma: no cover - exercised via the stdlib fallback path
    _orjson = None

if _orjson is not None:  # pragma: no cover - depends on optional dependency
    _JSON_ERRORS: tuple = (json.JSONDecodeError, _orjson.JSONDecodeError)

    def _json_loads(s: str) -> Any:
        return _orjson.loads(s)
else:
    _JSON_ERRORS = (json.JSONDecodeError,)

    def _json_loads(s: str) -> Any:
        return json.loads(s)


class FlightParseError(Exception):
    """Raised only when ``strict=True`` and a row cannot be parsed at all."""


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
    """

    _REF_RE = re.compile(r"^\$(?P<sigil>[A-Z@]{0,2})(?P<id>[^:\s]+)(?::(?P<path>.+))?$")
    _PUSH_CALL_RE = re.compile(r"self\.__next_f\.push\(")
    _ROW_START_RE = re.compile(r"[0-9a-zA-Z_\-]*:")
    _NEXT_ROW_RE = re.compile(r"\n[0-9a-zA-Z_\-]*:")
    _HTML_TAG_RE = re.compile(r"<[a-zA-Z!/][^>\n]{0,300}>")

    def __init__(self, html: Any, *, strict: bool = False):
        self.html = _coerce_html(html)
        self.strict = strict
        self.raw_chunks: dict[str, Any] = {}
        # Which Flight row kind each chunk id came from ("text", "json",
        # "module", or "preload") -- powers .kind()/.json_keys()/.html_keys().
        self._row_types: dict[str, str] = {}
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
            headers=headers or {"User-Agent": "Mozilla/5.0 (nextflight)"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            charset = resp.headers.get_content_charset() or "utf-8"
            html = resp.read().decode(charset, errors="replace")
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

    # ------------------------------------------------------------------ #
    # Step 1 -- find every push([...]) call, bracket/quote aware, so it
    # doesn't matter how many <script> tags they're spread across.
    # ------------------------------------------------------------------ #
    def _iter_push_payloads(self) -> Iterator[str]:
        for m in self._PUSH_CALL_RE.finditer(self.html):
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
                    yield chunk_id, "text", text_str
                    break
                i = body_start + len(text_str)
                yield chunk_id, "text", text_str
            elif payload[i:i + 2] == "HL":
                val, end = self._read_balanced(payload, i + 2, "[", "]")
                if val is None:
                    break
                i = end
                yield chunk_id, "preload", val
            elif payload[i] == "I":
                val, end = self._read_balanced(payload, i + 1, "[", "]")
                if val is None:
                    break
                i = end
                yield chunk_id, "module", val
            else:
                if payload[i] in "[{":
                    close_ch = "]" if payload[i] == "[" else "}"
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
                yield chunk_id, "json", val

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
        for payload in self._iter_push_payloads():
            for chunk_id, row_type, raw_value in self._split_rows(payload):
                self._row_types[chunk_id] = row_type
                if row_type == "text":
                    self.raw_chunks[chunk_id] = raw_value
                elif row_type in ("module", "preload"):
                    try:
                        self.raw_chunks[chunk_id] = _json_loads(raw_value)
                    except _JSON_ERRORS:
                        if self.strict:
                            raise FlightParseError(
                                f"chunk {chunk_id!r}: invalid {row_type} JSON: {raw_value[:80]!r}"
                            )
                        self.raw_chunks[chunk_id] = raw_value
                else:
                    if raw_value == "$undefined":
                        self.raw_chunks[chunk_id] = None
                        continue
                    try:
                        self.raw_chunks[chunk_id] = _json_loads(raw_value)
                    except _JSON_ERRORS:
                        if self.strict and not raw_value.startswith("$"):
                            raise FlightParseError(
                                f"chunk {chunk_id!r}: invalid JSON: {raw_value[:80]!r}"
                            )
                        self.raw_chunks[chunk_id] = raw_value  # bare marker e.g. "X"

    # ------------------------------------------------------------------ #
    # Step 3 -- resolve '$'-sigil references into real values, recursively.
    # ------------------------------------------------------------------ #
    def resolve_chunk(self, chunk_id: str) -> Any:
        """Resolve a single chunk (by its id) with all `$`-refs dereferenced."""
        if chunk_id in self._resolved_cache:
            return self._resolved_cache[chunk_id]
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
        m = self._REF_RE.match(s)
        if not m:
            return s
        sigil, ref_id, path = m.group("sigil"), m.group("id"), m.group("path")
        if sigil == "S":
            return {"__symbol__": ref_id}
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
    def _walk_path(value: Any, parts: list) -> Any:
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
                    return None
            elif isinstance(value, dict):
                value = value.get(part)
            else:
                return None
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

    def diff(self, other: "FlightExtractor") -> dict:
        """Compare this page against another crawl (presumably of the same
        URL) and report what changed. Shorthand for
        :func:`diff_pages(self, other) <diff_pages>` -- see there for
        details and caveats."""
        return diff_pages(self, other)

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
                          include_source: bool = False) -> list:
        """Like :meth:`find_by_keys` but returns every matching dict, not
        just the first -- useful for pages with repeated cards/listings
        that all share the same shape (product cards, search results, ...)."""
        required_keys = set(required_keys)
        return self.find_all(
            lambda n: isinstance(n, dict) and required_keys <= n.keys(),
            root=root, include_source=include_source,
        )

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
        """Navigate the fully resolved page with a dotted path of dict keys
        and/or list indices, e.g. ``page.get("3f.props.product.price")`` or
        ``page.get("items.0.name")``. Returns `default` if any segment is
        missing, instead of raising -- meant for quick, tolerant lookups
        once you already know roughly where something lives on this site."""
        current: Any = self.resolve_all()
        for part in path.split(sep):
            if isinstance(current, dict):
                if part not in current:
                    return default
                current = current[part]
            elif isinstance(current, list):
                try:
                    current = current[int(part)]
                except (ValueError, IndexError):
                    return default
            else:
                return default
        return current

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
        }

    def to_json(self, path: Optional[str] = None, *, indent: int = 2) -> Optional[str]:
        """Dump the fully resolved page as JSON. Writes to `path` if given
        (returns None), otherwise returns the JSON string."""
        text = json.dumps(self.resolve_all(), indent=indent, ensure_ascii=False, default=str)
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


def detect_next_router(html: Any) -> str:
    """Best-effort guess at which Next.js router rendered a page:

    - ``"app"`` -- Flight data found (``self.__next_f.push(...)``); use
      :func:`extract`.
    - ``"pages"`` -- a ``__NEXT_DATA__`` block found; use
      :func:`find_next_data`.
    - ``"both"`` -- both patterns found (rare -- e.g. a Pages Router page
      embedding an App Router island, or a mid-migration site).
    - ``"unknown"`` -- neither pattern found. Could mean the page isn't
      server-rendered by Next.js at all, or uses a wire format version
      this library doesn't recognize yet.

    `html` accepts a raw string/bytes, or a response-like object."""
    html = _coerce_html(html)
    has_flight = bool(FlightExtractor._PUSH_CALL_RE.search(html))
    has_next_data = bool(_NEXT_DATA_RE.search(html))
    if has_flight and has_next_data:
        return "both"
    if has_flight:
        return "app"
    if has_next_data:
        return "pages"
    return "unknown"


def _flatten_for_diff(data: Any, prefix: str = "") -> dict:
    """Flatten a nested dict/list into ``{dotted.path: scalar_or_empty}``,
    recursing into non-empty dicts/lists and stopping at scalars (or empty
    containers, which are kept as leaf values so an emptied-out list/dict
    still shows up as a change). Internal helper for :func:`diff_pages`."""
    flat: dict = {}
    if isinstance(data, dict) and data:
        for k, v in data.items():
            flat.update(_flatten_for_diff(v, f"{prefix}.{k}" if prefix else str(k)))
    elif isinstance(data, list) and data:
        for i, v in enumerate(data):
            flat.update(_flatten_for_diff(v, f"{prefix}.{i}" if prefix else str(i)))
    else:
        flat[prefix or "$"] = data
    return flat


def diff_pages(old: "FlightExtractor", new: "FlightExtractor") -> dict:
    """Compare two crawls of (presumably) the same URL and report what
    changed, scalar-by-scalar, by dotted path -- e.g. for a price/stock
    monitoring pipeline that re-scrapes a page periodically.

    Returns ``{"added": {path: value}, "removed": {path: value}, "changed":
    {path: (old_value, new_value)}}``.

    Caveat: Flight chunk ids are arbitrary per build (a redeploy can
    renumber them), and this diffs *fully resolved* data by path within
    each chunk id, not raw chunk ids. If the surrounding component tree
    reshuffled between crawls (a redeploy, not just new data), paths may
    not line up 1:1 and spurious add/remove pairs can appear alongside the
    real changes. Treat this as a "what looks different" signal to
    triage, not a guaranteed clean diff across arbitrary redeploys -- it's
    most reliable when comparing two crawls close together in time (e.g.
    polling the same live build every few minutes)."""
    old_flat = _flatten_for_diff(old.resolve_all())
    new_flat = _flatten_for_diff(new.resolve_all())
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

