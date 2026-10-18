#!/usr/bin/env python3
"""Correct one operator-owned source record into a NEW private CSV.

No live catalogue is changed. The output can be reviewed with catalog_diff.py
and separately imported by its owner. URLs are retained as unverified operator
attribution and are never visited. Stop other writers while reading input.
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

from catalog_diff import _read_bounded_bytes
from curated_catalog import COLUMNS, LANES, MAX_CATALOG_BYTES, CatalogError, read_catalog, _value

DIGEST = re.compile(r"[0-9a-f]{64}\Z")


class CorrectionError(ValueError):
    """The requested local source-record correction cannot be produced."""


def correct_catalogue(
    catalogue: str | Path, *, output: str | Path, expected_sha256: str,
    lane: str, name: str, city: str = "", tags: str | None = None,
    source_url: str | None = None, source_label: str | None = None,
) -> dict:
    """Write a validated, exclusive candidate CSV; preserve the original bytes.

    The identity (lane, name, city) cannot be renamed. Only tags and source
    attribution of that one row can change. All other physical lines remain
    byte-for-byte untouched, including their spacing, BOM and line endings.
    This is deliberately not an in-place edit or cross-process file CAS.
    """
    if not isinstance(expected_sha256, str) or not DIGEST.fullmatch(expected_sha256):
        raise CorrectionError("expected SHA-256 must be lowercase 64-character hex")
    replacements = {"tags": tags, "source_url": source_url, "source_label": source_label}
    if all(value is None for value in replacements.values()):
        raise CorrectionError("at least one replacement field is required")
    if any(value is not None and not isinstance(value, str) for value in replacements.values()):
        raise CorrectionError("replacement fields must be text")
    try:
        identity = (_value(lane, "lane").casefold(), _value(name, "name").casefold(),
                    _value(city, "city", required=(str(lane).casefold() == "place")).casefold())
    except CatalogError as exc:
        raise CorrectionError("invalid record identity") from exc
    if identity[0] not in LANES:
        raise CorrectionError("invalid lane")

    source = Path(catalogue).expanduser().absolute()
    destination = Path(output).expanduser().absolute()
    if source == destination or source.is_symlink() or not source.is_file():
        raise CorrectionError("source must be an existing non-symlink CSV")
    if not destination.parent.is_dir() or destination.parent.is_symlink():
        raise CorrectionError("output parent must be an existing non-symlink directory")
    if destination.exists() or destination.is_symlink():
        raise CorrectionError("output must not exist")

    try:
        original = _read_bounded_bytes(source)
        previous = read_catalog(source)
        if original != _read_bounded_bytes(source):
            raise CorrectionError("source changed during validation")
    except (CatalogError, OSError) as exc:
        raise CorrectionError("source is missing, invalid or exceeds the bound") from exc
    original_digest = hashlib.sha256(original).hexdigest()
    if original_digest != expected_sha256:
        raise CorrectionError("source digest differs from expected")
    matches = [index for index, record in enumerate(previous)
               if (record.lane, record.name.casefold(), record.city.casefold()) == identity]
    if len(matches) != 1:
        raise CorrectionError("exactly one matching existing record required")
    selected = matches[0]

    try:
        reader = csv.reader(io.StringIO(original.decode("utf-8-sig"), newline=""), strict=True)
        header = next(reader)
        if len(header) != len(COLUMNS) or set(header) != set(COLUMNS):
            raise CorrectionError("unsupported catalogue header")
        locations = []
        for row in reader:
            if row:
                if len(row) != len(header):
                    raise CorrectionError("record cannot be safely mapped to one CSV line")
                locations.append((reader.line_num - 1, row))
        if len(locations) != len(previous):
            raise CorrectionError("physical row count differs from validated catalogue")
        line_number, raw_row = locations[selected]
        lines = original.splitlines(keepends=True)
        if not 1 <= line_number < len(lines):
            raise CorrectionError("selected physical CSV line not found")
        row_values = dict(zip(header, raw_row))
        for field, replacement in replacements.items():
            if replacement is not None:
                row_values[field] = replacement
        ending = ("\r\n" if lines[line_number].endswith(b"\r\n") else
                  "\n" if lines[line_number].endswith(b"\n") else
                  "\r" if lines[line_number].endswith(b"\r") else "")
        rendered = io.StringIO(newline="")
        csv.writer(rendered, lineterminator=ending or "\n").writerow(
            [row_values[field] for field in header])
        row_bytes = rendered.getvalue().encode("utf-8")
        if not ending:
            row_bytes = row_bytes[:-1]
        revised = b"".join(row_bytes if i == line_number else line
                           for i, line in enumerate(lines))
    except (StopIteration, UnicodeError, csv.Error) as exc:
        raise CorrectionError("cannot map the source record to a safe physical row") from exc
    if len(revised) > MAX_CATALOG_BYTES:
        raise CorrectionError("corrected catalogue would exceed the input size limit")

    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    created_identity = None
    fd = None
    try:
        fd = os.open(destination, flags, 0o600)
        metadata = os.fstat(fd)
        created_identity = (metadata.st_dev, metadata.st_ino)
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as stream:
            fd = None
            stream.write(revised)
            stream.flush()
            os.fsync(stream.fileno())
        current = read_catalog(destination)
        if (len(current) != len(previous) or current[:selected] != previous[:selected]
                or current[selected + 1:] != previous[selected + 1:]
                or current[selected] == previous[selected]):
            raise CorrectionError("corrected CSV did not change exactly one validated record")
        if _read_bounded_bytes(destination) != revised or _read_bounded_bytes(source) != original:
            raise CorrectionError("source or output changed during correction")
    except (OSError, CatalogError, CorrectionError) as exc:
        if fd is not None:
            os.close(fd)
        if created_identity is not None:
            try:
                stat_result = destination.lstat()
                if (stat_result.st_dev, stat_result.st_ino) == created_identity:
                    destination.unlink()
            except OSError:
                pass
        raise CorrectionError("correction output was not safely validated") from exc

    return {
        "format": "culturefit_operator_catalogue_correction_v1",
        "source_status": "user_supplied_unverified",
        "provider_calls": 0,
        "provider_independently_verified": False,
        "operation": "one_record_new_file_only",
        "original_csv_sha256": original_digest,
        "corrected_csv_sha256": hashlib.sha256(revised).hexdigest(),
        "record_count": len(current),
        "corrected_record": {"lane": current[selected].lane, "name": current[selected].name,
                             "city": current[selected].city or None},
        "output_path": str(destination),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Correct one real source record into a NEW local CSV")
    parser.add_argument("--catalog", required=True)
    parser.add_argument("--output", required=True, help="Nonexistent CSV path; original is untouched")
    parser.add_argument("--expect-current-sha256", required=True)
    parser.add_argument("--lane", required=True, choices=LANES)
    parser.add_argument("--name", required=True)
    parser.add_argument("--city", default="")
    parser.add_argument("--tags", default=None)
    parser.add_argument("--source-url", default=None)
    parser.add_argument("--source-label", default=None)
    args = parser.parse_args(argv)
    try:
        report = correct_catalogue(
            args.catalog, output=args.output,
            expected_sha256=args.expect_current_sha256, lane=args.lane,
            name=args.name, city=args.city, tags=args.tags,
            source_url=args.source_url, source_label=args.source_label,
        )
    except CorrectionError as exc:
        print(f"catalogue correction error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(report, ensure_ascii=False, allow_nan=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
