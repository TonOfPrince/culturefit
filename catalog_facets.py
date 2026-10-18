#!/usr/bin/env python3
"""Discover real operator-catalogue tags before searching CultureFit.

This module consumes validated in-memory catalogue Candidate objects. Places
are city-specific; brands and artists are shared across cities. No URLs are
fetched and no external provider is queried or represented as verified.
"""
from __future__ import annotations

from collections import Counter

LANES = ("place", "brand", "artist")
MAX_RETURNED_TAGS = 80


class FacetError(ValueError):
    """The requested browse scope is not an acceptable city."""


def discover_facets(candidates: tuple, *, city: str) -> dict:
    """Return bounded, exact source-tag frequencies per eligible catalogue lane.

    Validated catalogue rows are supplied by curated_catalog.read_catalog; this
    function never opens paths or accesses their source URLs. Each record counts
    once per tag, even if a non-parser caller repeats that tag in an item.
    """
    if not isinstance(city, str):
        raise FacetError("city must be text")
    location = city.strip()
    if (not location or len(location) > 300 or
            any(ord(ch) < 32 or 0xD800 <= ord(ch) <= 0xDFFF for ch in location)):
        raise FacetError("invalid city")

    lanes = {}
    total_eligible = 0
    for lane in LANES:
        eligible = [record for record in candidates
                    if record.lane == lane and
                    (lane != "place" or record.city.casefold() == location.casefold())]
        total_eligible += len(eligible)
        counts = Counter(tag for record in eligible for tag in set(record.tags))
        ordered = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
        lanes[lane] = {
            "record_count": len(eligible),
            "distinct_tag_count": len(ordered),
            "tags_truncated": len(ordered) > MAX_RETURNED_TAGS,
            "tags": [{"tag": name, "record_count": n}
                     for name, n in ordered[:MAX_RETURNED_TAGS]],
        }
    return {
        "format": "culturefit_operator_facets_v1",
        "basis": "frequencies_of_user_authored_tags_not_provider_rankings",
        "source_status": "user_supplied_unverified",
        "provider_independently_verified": False,
        "provider_calls": 0,
        "city": location,
        "eligible_record_count": total_eligible,
        "lanes": lanes,
    }
