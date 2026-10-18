#!/usr/bin/env python3
"""Read-only two-city comparison of an operator-owned cultural catalogue.

Each claim in the report comes from the caller's already-authored CSV; shared
brands and artists are not misrepresented as local to either city. No URLs are
visited, no provider is contacted, and the source CSV is never modified.
"""
from __future__ import annotations

import argparse
import json
import sys

from catalog_coverage import coverage_report
from curated_catalog import Candidate, CatalogError, read_catalog, shortlist


def compare_cities(
    candidates: tuple[Candidate, ...], *, first_city: str, second_city: str,
    interests: str, per_city: int = 3,
) -> dict:
    """Contrast place coverage and real ranked records for two different cities.

    Brand and artist records in this CSV have no local place restrictions; their
    ranked records are returned once in shared_lanes, not duplicated as evidence
    that either city hosts them. Counts use the whole catalogue, while top_places
    are deliberately capped and ranked by the existing shortlist behavior.
    """
    if type(per_city) is not int or not 1 <= per_city <= 20:
        raise CatalogError("per-city place count must be between 1 and 20")

    first = shortlist(candidates, city=first_city, interests=interests, per_lane=per_city)
    second = shortlist(candidates, city=second_city, interests=interests, per_lane=per_city)
    if first["city"].casefold() == second["city"].casefold():
        raise CatalogError("two different cities are required")

    reports = [coverage_report(candidates, city=q["city"], interests=interests)
               for q in (first, second)]
    views = []
    for query, report in zip((first, second), reports):
        places = report["lanes"]["place"]
        views.append({
            "city": query["city"],
            "eligible_place_records": places["eligible_records"],
            "places_matching_any_interest": places["matching_any_interest"],
            "places_matching_all_interests": places["matching_all_interests"],
            "place_matches_by_interest": places["per_interest"],
            "missing_place_interests": [term for term in query["interests"]
                                        if places["per_interest"][term] == 0],
            "top_places": query["lanes"]["place"],
        })

    return {
        "format": "culturefit_operator_city_comparison_v1",
        "basis": "exact_matches_to_operator_authored_tags_only",
        "source_status": "user_supplied_unverified",
        "provider_independently_verified": False,
        "provider_calls": 0,
        "total_catalogue_records": len(candidates),
        "interests": first["interests"],
        "top_place_limit_per_city": per_city,
        "cities": views,
        "first_minus_second_place_matches": (
            views[0]["places_matching_any_interest"] - views[1]["places_matching_any_interest"]
        ),
        "shared_lanes": {lane: first["lanes"][lane] for lane in ("brand", "artist")},
        "shared_lanes_note": "Brand and artist tags are not city-specific in this input schema",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Compare two cities using only a local, operator-authored cultural CSV")
    parser.add_argument("--catalog", required=True, help="Existing local operator CSV")
    parser.add_argument("--first-city", required=True, help="First city for place records")
    parser.add_argument("--second-city", required=True, help="Second city for place records")
    parser.add_argument("--interests", required=True, help="Comma-separated exact source tags")
    parser.add_argument("--per-city", type=int, default=3, help="Top 1-20 places per city")
    args = parser.parse_args(argv)
    try:
        comparison = compare_cities(
            read_catalog(args.catalog), first_city=args.first_city,
            second_city=args.second_city, interests=args.interests,
            per_city=args.per_city,
        )
    except (CatalogError, OSError) as exc:
        print(f"city comparison error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(comparison, ensure_ascii=False, allow_nan=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
