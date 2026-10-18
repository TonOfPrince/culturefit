#!/usr/bin/env python3
"""Append one operator-supplied cultural record to an existing local catalogue.

Works only on the caller's local CSV and never visits attribution URLs. Stop the
local web server and other writers before use: the final equality check is not
an operating-system compare-and-swap across unrelated processes.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import re
import sys
import tempfile

from curated_catalog import CatalogError, COLUMNS, MAX_CATALOG_BYTES, read_catalog
from catalog_diff import _read_bounded_bytes

DIGEST = re.compile(r"[0-9a-f]{64}\Z")


class CatalogueAddError(ValueError):
    """An operator-authored row cannot be safely appended to this CSV."""


def append_candidate(
    catalogue: str | Path, *, expected_sha256: str, lane: str,
    name: str, city: str, tags: str, source_url: str, source_label: str,
    confirm_add: bool = False,
) -> dict:
    """Append an individually validated row without rewriting old row bytes.

    Does not create a catalogue, overwrite other records, fetch URLs or contact
    third-party providers. A running server or concurrent CSV writer is not
    supported; stop other writers before calling.
    """
    if not confirm_add:
        raise CatalogueAddError("explicit --confirm-add is required")
    if not isinstance(expected_sha256, str) or DIGEST.fullmatch(expected_sha256) is None:
        raise CatalogueAddError("expected catalogue SHA-256 must be 64 lowercase hex characters")
    values = (lane, name, city, tags, source_url, source_label)
    if any(not isinstance(value, str) for value in values):
        raise CatalogueAddError("all candidate fields must be text")

    target = Path(catalogue).expanduser().absolute()
    if target.is_symlink() or not target.is_file():
        raise CatalogueAddError("catalogue must be an existing regular non-symlink file")
    try:
        if target.stat().st_size > MAX_CATALOG_BYTES:
            raise CatalogueAddError("catalogue exceeds the input size limit")
        original = _read_bounded_bytes(target)
        if len(original) > MAX_CATALOG_BYTES:
            raise CatalogueAddError("catalogue exceeds the input size limit")
        prior = read_catalog(target)
    except (OSError, CatalogError) as exc:
        raise CatalogueAddError("existing catalogue is unavailable or invalid") from exc
    try:
        observed = _read_bounded_bytes(target)
    except CatalogError as exc:
        raise CatalogueAddError("catalogue changed during validation") from exc
    if original != observed:
        raise CatalogueAddError("catalogue changed during validation")
    original_sha = hashlib.sha256(original).hexdigest()
    if original_sha != expected_sha256:
        raise CatalogueAddError("current catalogue SHA-256 differs from expected")

    # The existing parser accepts any permutation of the six named columns.
    # Append in that validated physical order, not the default COLUMNS order.
    headers = next(csv.reader(io.StringIO(original.decode("utf-8-sig"), newline=""),
                              strict=True))
    record = dict(zip(COLUMNS, values))
    output = io.StringIO(newline="")
    csv.writer(output, lineterminator="\n").writerow([record[field] for field in headers])
    delimiter = b"" if original.endswith((b"\n", b"\r")) else b"\n"
    candidate = original + delimiter + output.getvalue().encode("utf-8")
    if len(candidate) > MAX_CATALOG_BYTES:
        raise CatalogueAddError("appended catalogue would exceed the input size limit")

    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(mode="wb", dir=target.parent,
                                         prefix=".culturefit-add-", suffix=".csv",
                                         delete=False) as handle:
            temporary = Path(handle.name)
            os.fchmod(handle.fileno(), 0o600)
            handle.write(candidate)
            handle.flush()
            os.fsync(handle.fileno())
        updated = read_catalog(temporary)
        if len(updated) != len(prior) + 1 or updated[:-1] != prior:
            raise CatalogueAddError("new row failed full-catalogue validation")
        if target.is_symlink():
            raise CatalogueAddError("catalogue changed before append; nothing replaced")
        try:
            current = _read_bounded_bytes(target)
        except CatalogError as exc:
            raise CatalogueAddError("catalogue changed before append; nothing replaced") from exc
        if current != original:
            raise CatalogueAddError("catalogue changed before append; nothing replaced")
        os.replace(temporary, target)
        temporary = None
    except (OSError, CatalogError, UnicodeError) as exc:
        raise CatalogueAddError("candidate cannot be appended to valid catalogue") from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)

    return {
        "format": "culturefit_local_catalogue_append_v1",
        "operation": "append_one_operator_record",
        "source_status": "user_supplied_unverified",
        "provider_independently_verified": False,
        "provider_calls": 0,
        "previous_csv_sha256": original_sha,
        "new_csv_sha256": hashlib.sha256(candidate).hexdigest(),
        "record_count": len(updated),
        "added_lane": updated[-1].lane,
        "added_name": updated[-1].name,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Append one operator-authored record to a stopped local CultureFit catalogue")
    parser.add_argument("--catalog", required=True)
    parser.add_argument("--expect-current-sha256", required=True)
    parser.add_argument("--lane", required=True, choices=("place", "brand", "artist"))
    parser.add_argument("--name", required=True)
    parser.add_argument("--city", default="", help="Required for place, optional otherwise")
    parser.add_argument("--tags", required=True, help="Semicolon-separated source tags")
    parser.add_argument("--source-url", required=True, help="Operator-owned HTTPS attribution")
    parser.add_argument("--source-label", required=True)
    parser.add_argument("--confirm-add", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = append_candidate(
            args.catalog, expected_sha256=args.expect_current_sha256,
            lane=args.lane, name=args.name, city=args.city, tags=args.tags,
            source_url=args.source_url, source_label=args.source_label,
            confirm_add=args.confirm_add,
        )
    except CatalogueAddError as exc:
        print(f"catalogue add error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, allow_nan=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
