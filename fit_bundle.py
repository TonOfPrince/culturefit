#!/usr/bin/env python3
"""Compose three-lane fit bundles from a local, operator-authored catalogue.

Every member and tag comes from the caller's own CSV. Results represent
transparent tag arithmetic, not verified sources, Qloo insights, or endorsements.
"""
from __future__ import annotations

import argparse
import itertools
import json
import sys

from curated_catalog import Candidate, CatalogError, LANES, read_catalog, shortlist

MAX_BUNDLES = 10


def compose_bundles(
    candidates: tuple[Candidate, ...], *, city: str, interests: str,
    per_lane: int = 8, max_bundles: int = 3,
    required_tags: str = "", excluded_tags: str = "",
    unique_places: bool = False,
) -> dict:
    """Rank real place/brand/artist triples by exact sourced-tag coverage.

    The bounded search is exhaustive *only within* the existing shortlist's
    top per_lane items for each lane, never the entire input catalogue.
    """
    if type(per_lane) is not int or not 1 <= per_lane <= 20:
        raise CatalogError("per-lane candidate pool must be between 1 and 20")
    if type(max_bundles) is not int or not 1 <= max_bundles <= MAX_BUNDLES:
        raise CatalogError("bundle count must be between 1 and 10")
    if type(unique_places) is not bool:
        raise CatalogError("unique_places must be a boolean")
    ranked = shortlist(
        candidates, city=city, interests=interests, per_lane=per_lane,
        required_tags=required_tags, excluded_tags=excluded_tags,
    )
    terms = ranked["interests"]
    groups = ranked["lanes"]
    result = {
        "basis": "exact_user_authored_tag_coverage_not_provider_fit",
        "source_status": "user_supplied_unverified",
        "provider_independently_verified": False,
        "provider_calls": 0,
        "city": ranked["city"],
        "interests": terms,
        "required_tags": ranked["required_tags"],
        "excluded_tags": ranked["excluded_tags"],
        "pool_limit_per_lane": per_lane,
        "unique_places": unique_places,
        "candidate_pool_counts": {lane: len(groups[lane]) for lane in LANES},
        "scoring_order": ["covered_interest_count", "shared_interest_count", "tag_observation_count"],
        "missing_lanes": ranked["missing_lanes"],
        "bundles": [],
    }
    if ranked["missing_lanes"]:
        return result

    scored: list[tuple[tuple, dict]] = []
    for members in itertools.product(*(groups[lane] for lane in LANES)):
        matched = [set(item["matched_interests"]) for item in members]
        covered = set().union(*matched)
        shared = {term for term in terms if sum(term in m for m in matched) >= 2}
        observations = sum(len(m) for m in matched)
        tie = tuple((item["name"].casefold(), (item["city"] or "").casefold(),
                     item["source_url"]) for item in members)
        score = (-len(covered), -len(shared), -observations, tie)
        scored.append((score, {
            "members": {lane: dict(item) for lane, item in zip(LANES, members)},
            "covered_interests": [term for term in terms if term in covered],
            "uncovered_interests": [term for term in terms if term not in covered],
            "shared_interests": [term for term in terms if term in shared],
            "covered_interest_count": len(covered),
            "shared_interest_count": len(shared),
            "tag_observation_count": observations,
        }))
    scored.sort(key=lambda pair: pair[0])
    if unique_places:
        # Preserve score ordering while presenting genuinely different places.
        used_places: set[tuple[str, str]] = set()
        for _, record in scored:
            place = record["members"]["place"]
            identity = (place["name"].casefold(), (place["city"] or "").casefold())
            if identity in used_places:
                continue
            used_places.add(identity)
            result["bundles"].append(record)
            if len(result["bundles"]) >= max_bundles:
                break
    else:
        result["bundles"] = [record for _, record in scored[:max_bundles]]
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Compose operator-sourced place/brand/artist bundles")
    parser.add_argument("--catalog", required=True, help="Local operator CSV, never fetched remotely")
    parser.add_argument("--city", required=True, help="Only place candidates are city-filtered")
    parser.add_argument("--interests", required=True, help="Comma-separated exact tags")
    parser.add_argument("--per-lane", type=int, default=8)
    parser.add_argument("--max-bundles", type=int, default=3)
    parser.add_argument("--unique-places", action="store_true",
                        help="Show each place once across returned bundles")
    parser.add_argument("--required-tags", default="")
    parser.add_argument("--excluded-tags", default="")
    args = parser.parse_args(argv)
    try:
        result = compose_bundles(
            read_catalog(args.catalog), city=args.city, interests=args.interests,
            per_lane=args.per_lane, max_bundles=args.max_bundles,
            required_tags=args.required_tags, excluded_tags=args.excluded_tags,
            unique_places=args.unique_places,
        )
    except CatalogError as exc:
        print(f"catalogue error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, allow_nan=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
