#!/usr/bin/env python3
"""Preview the impact of one real operator-authored record on local ranking.

The existing CSV is never changed. Attribution is supplied by the operator,
not inspected or verified by a provider. No external URLs are fetched.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile

from curated_catalog import COLUMNS, CatalogError, MAX_CATALOG_BYTES, read_catalog, shortlist


def _snapshot(path: Path) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise CatalogError("catalogue must be a regular, non-symlink CSV")
    try:
        with path.open("rb") as stream:
            raw = stream.read(MAX_CATALOG_BYTES + 1)
    except OSError as exc:
        raise CatalogError("catalogue unavailable") from exc
    if len(raw) > MAX_CATALOG_BYTES:
        raise CatalogError("catalogue exceeds size limit")
    return raw


def record_impact(
    catalogue: str | Path, *, row: dict[str, str], city: str,
    interests: str, per_lane: int = 3, required_tags: str = "",
    excluded_tags: str = "",
) -> dict:
    """Compare actual ranked results before/after one proposed CSV record.

    Only user-authored records are considered. The proposed row is validated
    by the exact same parser as actual imports; no write to catalogue occurs.
    """
    if (type(per_lane) is not int or not 1 <= per_lane <= 20 or
            not isinstance(row, dict) or set(row) != set(COLUMNS) or
            any(not isinstance(row[key], str) for key in COLUMNS)):
        raise CatalogError("invalid candidate or result limit")
    path = Path(catalogue)
    before_bytes = _snapshot(path)
    original = read_catalog(path)
    if _snapshot(path) != before_bytes:
        raise CatalogError("catalogue changed while previewing")

    before = shortlist(original, city=city, interests=interests, per_lane=per_lane,
                       required_tags=required_tags, excluded_tags=excluded_tags)
    # The validated source allows all six-column header permutations; preserve
    # its field order so the proposed name/city/source cannot be misattributed.
    with io.TextIOWrapper(io.BytesIO(before_bytes), encoding="utf-8-sig",
                          newline="") as header_stream:
        original_fields = next(csv.reader(header_stream, strict=True))
    encoded = io.StringIO(newline="")
    csv.DictWriter(encoded, fieldnames=original_fields, lineterminator="\n").writerow(row)
    addition = encoded.getvalue().encode("utf-8")
    delimiter = b"" if before_bytes.endswith((b"\n", b"\r")) else b"\n"
    proposed_bytes = before_bytes + delimiter + addition
    if len(proposed_bytes) > MAX_CATALOG_BYTES:
        raise CatalogError("appended catalogue exceeds size limit")

    # Parse the full proposed CSV using the production validator, rather than
    # accepting a separate weaker interpretation of a newly authored row.
    with tempfile.TemporaryDirectory(prefix="culturefit-impact-") as folder:
        proposed_path = Path(folder) / "candidate.csv"
        proposed_path.write_bytes(proposed_bytes)
        proposed = read_catalog(proposed_path)
    if len(proposed) != len(original) + 1 or proposed[:-1] != original:
        raise CatalogError("candidate did not preserve original catalogue")
    after = shortlist(proposed, city=city, interests=interests, per_lane=per_lane,
                      required_tags=required_tags, excluded_tags=excluded_tags)
    if _snapshot(path) != before_bytes:
        raise CatalogError("catalogue changed while previewing")

    added = proposed[-1]
    matched_lane = after["lanes"][added.lane]
    rank = next((idx for idx, item in enumerate(matched_lane, 1)
                 if item["name"].casefold() == added.name.casefold() and
                 (item["city"] or "").casefold() == added.city.casefold()), None)
    differences = {}
    for lane in ("place", "brand", "artist"):
        old, new = before["lanes"][lane], after["lanes"][lane]
        if old != new:
            differences[lane] = {"before": old, "after": new}
    return {
        "format": "culturefit_operator_record_impact_v1",
        "basis": "exact_matches_to_user_authored_tags_only",
        "source_status": "user_supplied_unverified",
        "provider_independently_verified": False,
        "provider_calls": 0,
        "original_catalogue_sha256": hashlib.sha256(before_bytes).hexdigest(),
        "original_record_count": len(original),
        "proposed_record_count": len(proposed),
        "proposed_candidate": {"lane": added.lane, "name": added.name,
                               "city": added.city or None,
                               "source_url": added.source_url,
                               "source_label": added.source_label,
                               "source_status": "user_supplied_unverified"},
        "candidate_rank_within_top": rank,
        "result_limit_per_lane": per_lane,
        "changed_lanes": differences,
        "no_change_to_top_ranked_results": not bool(differences),
        "city": after["city"],
        "interests": after["interests"],
        "required_tags": after["required_tags"],
        "excluded_tags": after["excluded_tags"],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Preview one authored record against an existing local CultureFit CSV")
    parser.add_argument("--catalog", required=True)
    parser.add_argument("--lane", required=True, choices=("place", "brand", "artist"))
    parser.add_argument("--name", required=True)
    parser.add_argument("--candidate-city", default="")
    parser.add_argument("--tags", required=True, help="Semicolon-separated operator tags")
    parser.add_argument("--source-url", required=True)
    parser.add_argument("--source-label", required=True)
    parser.add_argument("--city", required=True, help="Place city to search")
    parser.add_argument("--interests", required=True, help="Comma-separated interests")
    parser.add_argument("--per-lane", type=int, default=3)
    parser.add_argument("--required-tags", default="")
    parser.add_argument("--excluded-tags", default="")
    args = parser.parse_args(argv)
    row = dict(zip(COLUMNS, (args.lane, args.name, args.candidate_city,
                             args.tags, args.source_url, args.source_label)))
    try:
        result = record_impact(args.catalog, row=row, city=args.city,
                               interests=args.interests, per_lane=args.per_lane,
                               required_tags=args.required_tags,
                               excluded_tags=args.excluded_tags)
    except (CatalogError, OSError, UnicodeError, ValueError) as exc:
        print(f"catalogue impact error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, allow_nan=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
