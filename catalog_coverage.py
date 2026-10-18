#!/usr/bin/env python3
"""Summarize coverage of an operator-authored cultural source catalogue.

Counts observed user-provided tags only. This module never contacts a source,
performs a provider request, or claims independent verification.
"""
from __future__ import annotations

import argparse
import json
import sys

from curated_catalog import CatalogError, LANES, Candidate, read_catalog, shortlist


def coverage_report(candidates: tuple[Candidate, ...], *, city: str, interests: str) -> dict:
    """Count eligible records and exact-interest coverage without result truncation."""
    # Validate inputs using the same public search behavior as the catalogue CLI.
    request = shortlist(candidates, city=city, interests=interests, per_lane=1)
    terms = request["interests"]
    location = request["city"]
    coverage = {}
    absent = []
    for lane in LANES:
        eligible = [c for c in candidates if c.lane == lane and
                    (lane != "place" or c.city.casefold() == location.casefold())]
        by_interest = {term: sum(term in c.tags for c in eligible) for term in terms}
        any_count = sum(any(term in c.tags for term in terms) for c in eligible)
        all_count = sum(all(term in c.tags for term in terms) for c in eligible)
        coverage[lane] = {
            "eligible_records": len(eligible),
            "matching_any_interest": any_count,
            "matching_all_interests": all_count,
            "per_interest": by_interest,
        }
        absent.extend({"lane": lane, "interest": term}
                      for term in terms if by_interest[term] == 0)
    return {
        "format": "culturefit_operator_catalog_coverage_v1",
        "basis": "counts_of_operator_authored_tags_only",
        "source_status": "user_supplied_unverified",
        "provider_independently_verified": False,
        "provider_calls": 0,
        "city": location,
        "interests": terms,
        "total_records": len(candidates),
        "lanes": coverage,
        "uncovered_lane_interests": absent,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Inspect real operator-supplied catalogue coverage")
    parser.add_argument("--catalog", required=True, help="Operator-authored UTF-8 CSV")
    parser.add_argument("--city", required=True, help="City for place coverage only")
    parser.add_argument("--interests", required=True, help="Comma-separated exact operator tags")
    args = parser.parse_args(argv)
    try:
        output = coverage_report(read_catalog(args.catalog), city=args.city,
                                 interests=args.interests)
    except (CatalogError, OSError) as exc:
        print(f"coverage error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(output, ensure_ascii=False, allow_nan=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
