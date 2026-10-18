#!/usr/bin/env python3
"""Explain one real operator-authored candidate's inclusion in a catalogue search.

Uses the same search function as the standalone catalogue. Reasons describe
only user-authored tags and city, never independent source or provider evidence.
"""
from __future__ import annotations

import argparse
import json
import sys

from curated_catalog import CatalogError, LANES, Candidate, _value, read_catalog, shortlist


def explain_candidate(candidates: tuple[Candidate, ...], *, lane: str, name: str,
                      city: str, interests: str, candidate_city: str | None = None,
                      per_lane: int = 3, required_tags: str = "",
                      excluded_tags: str = "") -> dict:
    """Diagnose a uniquely identified CSV record against the actual ranked query."""
    result = shortlist(candidates, city=city, interests=interests, per_lane=per_lane,
                       required_tags=required_tags, excluded_tags=excluded_tags)
    requested_lane = _value(lane, "lane").casefold()
    if requested_lane not in LANES:
        raise CatalogError("unsupported candidate lane")
    requested_name = _value(name, "candidate name").casefold()
    requested_city = (_value(candidate_city, "candidate city", required=False)
                      if candidate_city is not None else None)
    matches = [
        entry for entry in candidates
        if entry.lane == requested_lane and entry.name.casefold() == requested_name
        and (requested_city is None or entry.city.casefold() == requested_city.casefold())
    ]
    if not matches:
        raise CatalogError("candidate is absent from the operator catalogue")
    if len(matches) != 1:
        raise CatalogError("candidate name is ambiguous; specify --candidate-city")
    candidate = matches[0]
    tags = set(candidate.tags)
    matching = [tag for tag in result["interests"] if tag in tags]
    missing_required = [tag for tag in result["required_tags"] if tag not in tags]
    excluded_present = [tag for tag in result["excluded_tags"] if tag in tags]
    reasons: list[str] = []
    if candidate.lane == "place" and candidate.city.casefold() != result["city"].casefold():
        reasons.append("place_outside_requested_city")
    if missing_required:
        reasons.append("missing_required_tags")
    if excluded_present:
        reasons.append("excluded_tags_present")
    if not matching:
        reasons.append("no_matching_interests")
    rank = next(
        (index for index, card in enumerate(result["lanes"][candidate.lane], 1)
         if card["name"] == candidate.name and card["source_url"] == candidate.source_url
         and (card["city"] or "") == candidate.city),
        None,
    )
    if reasons:
        status = "ineligible"
    elif rank is None:
        status = "eligible_outside_result_limit"
        reasons.append("outside_per_lane_limit")
    else:
        status = "included"
    return {
        "format": "culturefit_operator_candidate_explanation_v1",
        "basis": "exact_matches_to_user_authored_tags",
        "source_status": "user_supplied_unverified",
        "provider_calls": 0,
        "provider_independently_verified": False,
        "query": {
            "city": result["city"],
            "interests": result["interests"],
            "required_tags": result["required_tags"],
            "excluded_tags": result["excluded_tags"],
            "per_lane": per_lane,
        },
        "candidate": {
            "lane": candidate.lane,
            "name": candidate.name,
            "city": candidate.city or None,
            "source_url": candidate.source_url,
            "source_label": candidate.source_label,
        },
        "matched_interests": matching,
        "missing_required_tags": missing_required,
        "excluded_tags_present": excluded_present,
        "status": status,
        "shortlist_rank": rank,
        "reasons": reasons,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Explain a real operator-catalogue shortlist decision")
    parser.add_argument("--catalog", required=True)
    parser.add_argument("--lane", required=True, choices=LANES)
    parser.add_argument("--name", required=True)
    parser.add_argument("--candidate-city", default=None,
                        help="Disambiguate the selected CSV entry, if necessary")
    parser.add_argument("--city", required=True, help="Requested city for places")
    parser.add_argument("--interests", required=True)
    parser.add_argument("--required-tags", default="")
    parser.add_argument("--excluded-tags", default="")
    parser.add_argument("--per-lane", type=int, default=3)
    args = parser.parse_args(argv)
    try:
        report = explain_candidate(
            read_catalog(args.catalog), lane=args.lane, name=args.name,
            candidate_city=args.candidate_city, city=args.city,
            interests=args.interests, required_tags=args.required_tags,
            excluded_tags=args.excluded_tags, per_lane=args.per_lane,
        )
    except CatalogError as exc:
        print(f"explanation error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(report, ensure_ascii=False, allow_nan=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
