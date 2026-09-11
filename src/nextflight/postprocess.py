"""
nextflight.postprocess

Small, dependency-free normalization helpers for the messy strings that
commonly show up in resolved Flight payloads: prices with currency
symbols/thousands separators, HTML-entity-laden text, and loosely
formatted dates. Every downstream scraper ends up writing some version of
these -- having them here saves that reimplementation.
"""

from __future__ import annotations

import html
import re
from datetime import datetime
from typing import Optional, Union

_PRICE_RE = re.compile(
    r"""
    (?P<currency>[$€£¥₹]|[A-Z]{3}(?=[\s\d]))?   # optional leading symbol/ISO code
    \s*
    (?P<amount>\d[\d,.\s]*\d|\d)                  # the numeric part
    \s*
    (?P<trailing_currency>[A-Z]{3}|[$€£¥₹])?      # optional trailing ISO code/symbol
    """,
    re.VERBOSE,
)

_CURRENCY_SYMBOLS = {"$": "USD", "€": "EUR", "£": "GBP", "¥": "JPY", "₹": "INR"}

_WHITESPACE_RE = re.compile(r"\s+")


def normalize_price(value: Union[str, int, float, None]) -> Optional[dict]:
    """Parse a messy price string into ``{"amount": float, "currency": str
    | None}``. Handles leading/trailing currency symbols or ISO codes,
    thousands separators (comma OR period, disambiguated by whichever one
    appears last and is followed by exactly 1-2 digits -- the decimal
    convention), and surrounding whitespace/text. Returns ``None`` if no
    numeric price could be found at all.

        normalize_price("$1,299.00")   -> {"amount": 1299.0, "currency": "USD"}
        normalize_price("1.299,00 €")  -> {"amount": 1299.0, "currency": "EUR"}
        normalize_price("EUR 45")      -> {"amount": 45.0, "currency": "EUR"}
        normalize_price(1299)          -> {"amount": 1299.0, "currency": None}
        normalize_price("Contact us")  -> None
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return {"amount": float(value), "currency": None}
    text = str(value).strip()
    m = _PRICE_RE.search(text)
    if not m or not m.group("amount"):
        return None
    raw_amount = m.group("amount").strip()
    currency = None
    if m.group("currency"):
        sym = m.group("currency").strip()
        currency = _CURRENCY_SYMBOLS.get(sym, sym)
    elif m.group("trailing_currency"):
        sym = m.group("trailing_currency")
        currency = _CURRENCY_SYMBOLS.get(sym, sym)
    if currency is None:
        code_m = re.search(r"\b([A-Z]{3})\b", text)
        if code_m:
            currency = code_m.group(1)

    digits_only = raw_amount.replace(" ", "")
    last_comma = digits_only.rfind(",")
    last_dot = digits_only.rfind(".")
    if last_comma != -1 and last_dot != -1:
        # Whichever separator appears last is the decimal point; the
        # other is a thousands separator.
        if last_comma > last_dot:
            digits_only = digits_only.replace(".", "").replace(",", ".")
        else:
            digits_only = digits_only.replace(",", "")
    elif last_comma != -1:
        # Only a comma: treat as decimal only if 1-2 digits follow it
        # (matches "1.299,00"-style European formatting); otherwise it's
        # a thousands separator ("1,299").
        after = digits_only[last_comma + 1:]
        if len(after) in (1, 2):
            digits_only = digits_only.replace(",", ".")
        else:
            digits_only = digits_only.replace(",", "")
    try:
        amount = float(digits_only)
    except ValueError:
        return None
    return {"amount": amount, "currency": currency}


def clean_text(value: Optional[str], *, collapse_whitespace: bool = True) -> Optional[str]:
    """Decode HTML entities (``&amp;`` -> ``&``, ``&#39;`` -> ``'``, etc.)
    and, by default, collapse runs of whitespace (including newlines/tabs
    picked up from prerendered markup fragments) down to single spaces,
    then strip the result. Returns ``None`` unchanged for ``None`` input
    so it's safe to map over optional fields."""
    if value is None:
        return None
    text = html.unescape(str(value))
    if collapse_whitespace:
        text = _WHITESPACE_RE.sub(" ", text).strip()
    return text


# A conservative set of formats covering the vast majority of dates found
# in scraped e-commerce/listing/job-board data. Not exhaustive -- callers
# with a known, unusual format should just use `datetime.strptime` directly.
_DATE_FORMATS = (
    "%Y-%m-%d",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%dT%H:%M:%S%z",
    "%Y-%m-%dT%H:%M:%S.%f%z",
    "%Y/%m/%d",
    "%m/%d/%Y",
    "%d/%m/%Y",
    "%B %d, %Y",
    "%b %d, %Y",
    "%d %B %Y",
    "%d %b %Y",
)


def parse_date(value: Optional[str]) -> Optional[datetime]:
    """Best-effort parse of a loosely formatted date string against a
    conservative list of common formats (ISO 8601 variants, ``MM/DD/YYYY``
    vs ``DD/MM/YYYY``, ``"January 5, 2024"``, etc.). Returns ``None`` if
    `value` is ``None``/empty or matches none of them -- this is
    intentionally not a full natural-language date parser (that's what
    `dateutil`/`dateparser` are for); it covers the common, unambiguous
    cases without adding a dependency."""
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    iso_candidate = text.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(iso_candidate)
    except ValueError:
        pass
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None
