"""
nextflight
==========

Generic parser for the React Server Components "Flight" wire format that
Next.js 13+ (App Router) embeds in ``<script>self.__next_f.push([...])</script>``
tags. Works on any Next.js App Router site.

    from nextflight import extract

    page = extract(html_text)
    listing = page.find_by_keys({"sections", "meta"})
"""

import warnings

from .extractor import (
    FlightExtractor,
    FlightParseError,
    extract,
    find_json_ld,
    find_next_data,
    detect_next_router,
    diff_pages,
)

__all__ = [
    "FlightExtractor",
    "FlightParseError",
    "extract",
    "find_json_ld",
    "find_next_data",
    "detect_next_router",
    "diff_pages",
    "NextFlightExtractor",  # deprecated alias, see below
    "extract_json_ld",      # deprecated alias, see below
]

__version__ = "0.3.2"


# ---------------------------------------------------------------------- #
# Backwards-compatible aliases for the pre-rename API (nextjs_flight_extractor
# 0.1.x). These will be removed in a future major version -- switch to
# FlightExtractor / find_json_ld when convenient.
# ---------------------------------------------------------------------- #
class NextFlightExtractor(FlightExtractor):
    """Deprecated alias for :class:`FlightExtractor`. Use ``FlightExtractor`` instead."""

    def __init__(self, *args, **kwargs):
        warnings.warn(
            "NextFlightExtractor is deprecated, use nextflight.FlightExtractor instead",
            DeprecationWarning,
            stacklevel=2,
        )
        super().__init__(*args, **kwargs)

    def find_first(self, *args, **kwargs):
        warnings.warn(
            "find_first() is deprecated, use find_one() instead",
            DeprecationWarning,
            stacklevel=2,
        )
        return self.find_one(*args, **kwargs)


def extract_json_ld(html: str, schema_type=None) -> list:
    """Deprecated alias for :func:`find_json_ld`. Use ``find_json_ld`` instead."""
    warnings.warn(
        "extract_json_ld() is deprecated, use nextflight.find_json_ld() instead",
        DeprecationWarning,
        stacklevel=2,
    )
    return find_json_ld(html, type_=schema_type)
