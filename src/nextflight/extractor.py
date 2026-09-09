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
from typing import Any, Callable, Iterator, Optional


class FlightParseError(Exception):
    """Raised only when ``strict=True`` and a row cannot be parsed at all."""


class FlightExtractor:
    """Parses and searches the Next.js Flight payloads embedded in a page.

    Parameters
    ----------
    html:
        The full HTML of a server-rendered Next.js App Router page.
    strict:
        If True, raise :class:`FlightParseError` when a row's value isn't
        valid JSON and doesn't look like a bare `$`-reference marker.
        Default False: such rows are kept as raw strings so a handful of
        odd rows never take down extraction of everything else on the page.
    """

    _REF_RE = re.compile(r"^\$(?P<sigil>[A-Z@]{0,2})(?P<id>[^:\s]+)(?::(?P<path>.+))?$")
    _PUSH_CALL_RE = re.compile(r"self\.__next_f\.push\(")
    _ROW_START_RE = re.compile(r"[0-9a-zA-Z_\-]*:")

    def __init__(self, html: str, *, strict: bool = False):
        self.html = html
        self.strict = strict
        self.raw_chunks: dict[str, Any] = {}
        self._resolved_cache: dict[str, Any] = {}
        self._resolving: set[str] = set()
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
                value = json.loads(array_text)
            except json.JSONDecodeError:
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
                j = payload.index(",", i + 1)
                hex_len = int(payload[i + 1:j], 16)
                body_start = j + 1
                remaining_bytes = payload[body_start:].encode("utf-8")
                text_bytes = remaining_bytes[:hex_len]
                text_str = text_bytes.decode("utf-8")
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
                    nxt = re.search(r"\n[0-9a-zA-Z_\-]*:", payload[i:])
                    end = i + nxt.start() if nxt else n
                    val, i = payload[i:end], end
                yield chunk_id, "json", val

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
                if row_type == "text":
                    self.raw_chunks[chunk_id] = raw_value
                elif row_type in ("module", "preload"):
                    try:
                        self.raw_chunks[chunk_id] = json.loads(raw_value)
                    except json.JSONDecodeError:
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
                        self.raw_chunks[chunk_id] = json.loads(raw_value)
                    except json.JSONDecodeError:
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
        value = self.resolve_chunk(ref_id)
        if path:
            value = self._walk_path(value, path.split(":"))
        return value

    @staticmethod
    def _walk_path(value: Any, parts: list) -> Any:
        for part in parts:
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

    # ------------------------------------------------------------------ #
    # Step 4 -- schema-free search over the fully resolved data.
    # ------------------------------------------------------------------ #
    def find_all(self, predicate: Callable[[Any], bool], root: Any = None,
                 max_results: Optional[int] = None) -> list:
        """Walk the whole resolved tree and collect every node matching `predicate`."""
        data = self.resolve_all() if root is None else root
        results: list = []
        seen: set = set()

        def walk(node: Any):
            if max_results is not None and len(results) >= max_results:
                return
            if id(node) in seen:
                return
            if isinstance(node, (dict, list)):
                seen.add(id(node))
            if predicate(node):
                results.append(node)
                if max_results is not None and len(results) >= max_results:
                    return
            if isinstance(node, dict):
                for v in node.values():
                    walk(v)
            elif isinstance(node, list):
                for v in node:
                    walk(v)

        if isinstance(data, dict):
            for v in data.values():
                walk(v)
        else:
            walk(data)
        return results

    def find_one(self, predicate: Callable[[Any], bool], root: Any = None) -> Any:
        """Like :meth:`find_all` but returns just the first match, or None."""
        r = self.find_all(predicate, root=root, max_results=1)
        return r[0] if r else None

    def find_by_keys(self, required_keys, root: Any = None) -> Any:
        """Find the first dict containing ALL of `required_keys` -- the
        pattern you almost always want: 'give me whatever object looks
        like the data I need', regardless of where this build's component
        tree happened to put it."""
        required_keys = set(required_keys)
        return self.find_one(
            lambda n: isinstance(n, dict) and required_keys <= n.keys(), root=root
        )

    def find_by_type(self, type_value: str, *, key: str = "@type", root: Any = None) -> list:
        """Find every dict whose `key` field equals `type_value` (default key
        "@type", matching schema.org-style typed objects Next.js often embeds
        e.g. {"@type": "Product", ...})."""
        return self.find_all(
            lambda n: isinstance(n, dict) and n.get(key) == type_value, root=root
        )


def find_json_ld(html: str, type_: Optional[str] = None) -> list:
    """Parse any <script type="application/ld+json"> blocks on the page,
    independent of Flight data and often more stable across redesigns --
    worth trying first for structured product/article/breadcrumb data.

    `type_` optionally filters results by their "@type" (e.g. "Product")."""
    blocks = re.findall(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html, re.DOTALL,
    )
    out = []
    for b in blocks:
        try:
            parsed = json.loads(b)
        except json.JSONDecodeError:
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


def extract(html: str, *, strict: bool = False) -> FlightExtractor:
    """Shorthand for ``FlightExtractor(html)``."""
    return FlightExtractor(html, strict=strict)
