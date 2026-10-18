#!/usr/bin/env python3
"""Query an explicitly user-curated cultural catalogue without external services.

This is an independent, credential-free product component. It does not query,
represent, or attest to any third-party provider. The operator supplies every
candidate and source URL; the program does not dereference those URLs.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

LANES = ("place", "brand", "artist")
COLUMNS = ("lane", "name", "city", "tags", "source_url", "source_label")
MAX_CATALOG_BYTES = 2_000_000
MAX_ROWS = 5_000
MAX_INTERESTS = 12


class CatalogError(ValueError):
    """Input cannot support a source-attributed catalogue result."""


@dataclass(frozen=True)
class Candidate:
    lane: str
    name: str
    city: str
    tags: tuple[str, ...]
    source_url: str
    source_label: str


def _value(raw: object, field: str, *, required: bool = True) -> str:
    if not isinstance(raw, str):
        raise CatalogError(f"missing {field}")
    value = raw.strip()
    if ((required and not value) or len(value) > 300 or
            any(ord(ch) < 32 or 0xD800 <= ord(ch) <= 0xDFFF for ch in value)):
        raise CatalogError(f"invalid {field}")
    return value


def _url(raw: object) -> str:
    value = _value(raw, "source_url")
    try:
        p = urlsplit(value)
        valid = (p.scheme == "https" and p.hostname is not None and
                 p.username is None and p.password is None and not any(c.isspace() for c in value))
        if not valid or p.port == 0:
            raise CatalogError("source_url must be an HTTPS URL without credentials")
    except ValueError as exc:
        raise CatalogError("invalid source_url") from exc
    return value


def read_catalog(path: str | Path) -> tuple[Candidate, ...]:
    """Load all operator-authored rows; fail closed on ambiguous records."""
    path = Path(path)
    if not path.is_file() or path.stat().st_size > MAX_CATALOG_BYTES:
        raise CatalogError("catalogue missing or exceeds size limit")
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle, strict=True)
            headers = reader.fieldnames
            if not headers or len(set(headers)) != len(headers) or set(headers) != set(COLUMNS):
                raise CatalogError("expected exactly: " + ",".join(COLUMNS))
            records: list[Candidate] = []
            identities: set[tuple[str, str, str]] = set()
            for index, row in enumerate(reader, 2):
                if index > MAX_ROWS + 1:
                    raise CatalogError("too many catalogue records")
                lane = _value(row.get("lane"), "lane").casefold()
                if lane not in LANES:
                    raise CatalogError(f"row {index}: unsupported lane")
                name = _value(row.get("name"), "name")
                city = _value(row.get("city"), "city", required=(lane == "place"))
                tags = tuple(dict.fromkeys(
                    _value(tag, "tag").casefold()
                    for tag in _value(row.get("tags"), "tags").split(";")
                ))
                source_url = _url(row.get("source_url"))
                source_label = _value(row.get("source_label"), "source_label")
                identity = (lane, name.casefold(), city.casefold())
                if identity in identities:
                    raise CatalogError(f"row {index}: duplicate candidate identity")
                identities.add(identity)
                records.append(Candidate(lane, name, city, tags, source_url, source_label))
            return tuple(records)
    except (OSError, UnicodeError, csv.Error) as exc:
        raise CatalogError("catalogue cannot be read as valid UTF-8 CSV") from exc


def _tag_terms(value: str, field: str, *, optional: bool = False) -> tuple[str, ...]:
    if not isinstance(value, str):
        raise CatalogError(f"invalid {field}")
    if optional and not value.strip():
        return ()
    terms = tuple(dict.fromkeys(_value(part, field).casefold() for part in value.split(",")))
    if not 1 <= len(terms) <= MAX_INTERESTS:
        raise CatalogError(f"{field} count out of range")
    return terms


def shortlist(candidates: tuple[Candidate, ...], *, interests: str, city: str,
              per_lane: int = 3, required_tags: str = "",
              excluded_tags: str = "") -> dict:
    """Exact user-authored tag ranking with optional hard eligibility constraints."""
    location = _value(city, "city")
    terms = _tag_terms(interests, "interest")
    required = _tag_terms(required_tags, "required tag", optional=True)
    excluded = _tag_terms(excluded_tags, "excluded tag", optional=True)
    if set(required).intersection(excluded):
        raise CatalogError("conflicting tag constraints")
    if not 1 <= per_lane <= 20:
        raise CatalogError("result count out of range")

    groups: dict[str, list[dict]] = {lane: [] for lane in LANES}
    for candidate in candidates:
        if candidate.lane == "place" and candidate.city.casefold() != location.casefold():
            continue
        if any(tag not in candidate.tags for tag in required):
            continue
        if any(tag in candidate.tags for tag in excluded):
            continue
        matched = tuple(term for term in terms if term in candidate.tags)
        if not matched:
            continue
        groups[candidate.lane].append({
            "lane": candidate.lane,
            "name": candidate.name,
            "city": candidate.city or None,
            "matched_interests": list(matched),
            "match_count": len(matched),
            "source_url": candidate.source_url,
            "source_label": candidate.source_label,
            "source_status": "user_supplied_unverified",
        })

    for lane in LANES:
        groups[lane].sort(key=lambda x: (-x["match_count"], x["name"].casefold(), x["source_url"]))
        groups[lane] = groups[lane][:per_lane]
    return {
        "basis": "exact_matches_to_user_authored_tags",
        "source_status": "user_supplied_unverified",
        "provider_calls": 0,
        "city": location,
        "interests": list(terms),
        "required_tags": list(required),
        "excluded_tags": list(excluded),
        "lanes": groups,
        "missing_lanes": [lane for lane in LANES if not groups[lane]],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Search a user-supplied cultural reference catalogue")
    parser.add_argument("--catalog", required=True, help="UTF-8 CSV with columns: " + ",".join(COLUMNS))
    parser.add_argument("--city", required=True)
    parser.add_argument("--interests", required=True, help="Comma-separated exact tags")
    parser.add_argument("--per-lane", type=int, default=3)
    parser.add_argument("--required-tags", default="", help="Comma-separated must-have tags")
    parser.add_argument("--excluded-tags", default="", help="Comma-separated disallowed tags")
    args = parser.parse_args(argv)
    try:
        result = shortlist(read_catalog(args.catalog), city=args.city,
                           interests=args.interests, per_lane=args.per_lane,
                           required_tags=args.required_tags, excluded_tags=args.excluded_tags)
    except CatalogError as exc:
        print(f"catalogue error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
