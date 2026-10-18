#!/usr/bin/env python3
"""Remove one operator-authored CSV record, preserving every other source line.

Local filesystem operation only; no provider, source URL or network access.
The operator must stop the local web app and every other CSV writer first.
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

from catalog_backup import BackupError, backup_catalogue
from catalog_diff import _read_bounded_bytes
from curated_catalog import CatalogError, _value, read_catalog

SHA256 = re.compile(r"[0-9a-f]{64}\Z")


class RemoveError(ValueError):
    """The requested one-record removal is not safe to apply."""


def remove_candidate(
    catalogue: str | Path, *, safety_backup: str | Path,
    expected_sha256: str, lane: str, name: str, city: str = "",
    confirm_remove: bool = False,
) -> dict:
    """Remove exactly one identified entry, with a new byte-exact safety backup.

    Preserves the original CSV bytes for all other physical lines, including
    header/BOM, line endings, spacing and blank lines. This function does not
    serialize or modify surviving operator records.
    """
    if confirm_remove is not True:
        raise RemoveError("explicit --confirm-remove is required")
    if not isinstance(expected_sha256, str) or SHA256.fullmatch(expected_sha256) is None:
        raise RemoveError("expected catalogue SHA-256 must be lowercase 64-hex")
    try:
        wanted_lane = _value(lane, "lane").casefold()
        wanted_name = _value(name, "name").casefold()
        wanted_city = _value(city, "city", required=(wanted_lane == "place")).casefold()
    except CatalogError as exc:
        raise RemoveError("invalid record identity") from exc
    if wanted_lane not in ("place", "brand", "artist"):
        raise RemoveError("invalid record lane")

    source = Path(catalogue).expanduser().absolute()
    backup = Path(safety_backup).expanduser().absolute()
    if source == backup or source.is_symlink() or not source.is_file():
        raise RemoveError("source must be an existing non-symlink catalogue")
    if not backup.parent.is_dir() or backup.exists() or backup.is_symlink():
        raise RemoveError("safety backup must have a new filename in an existing directory")
    try:
        if source.stat().st_ino == backup.stat().st_ino:
            raise RemoveError("safety backup aliases the source")
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise RemoveError("source or destination unavailable") from exc

    try:
        original_bytes = _read_bounded_bytes(source)
        records = read_catalog(source)
        if original_bytes != _read_bounded_bytes(source):
            raise RemoveError("catalogue changed during validation")
    except (CatalogError, OSError) as exc:
        raise RemoveError("catalogue is unavailable or invalid") from exc
    digest = hashlib.sha256(original_bytes).hexdigest()
    if digest != expected_sha256:
        raise RemoveError("catalogue digest differs from expected")
    matches = [i for i, record in enumerate(records)
               if (record.lane, record.name.casefold(), record.city.casefold()) ==
               (wanted_lane, wanted_name, wanted_city)]
    if len(matches) != 1:
        raise RemoveError("exactly one matching operator record is required")

    # The production validator rejects control characters, including literal
    # newlines inside fields. Map each validated CSV record to its physical line
    # using the standard CSV reader so blank lines and CRLF remain untouched.
    try:
        text = original_bytes.decode("utf-8-sig")
        reader = csv.reader(io.StringIO(text, newline=""), strict=True)
        next(reader)
        row_lines = []
        previous_line = reader.line_num
        for _row in reader:
            if not _row:
                # DictReader ignores empty physical lines; keep them intact,
                # but never count them as a catalogue record.
                previous_line = reader.line_num
                continue
            if reader.line_num != previous_line + 1:
                raise RemoveError("multiline catalogue records unsupported")
            row_lines.append(reader.line_num - 1)
            previous_line = reader.line_num
    except (UnicodeError, csv.Error, StopIteration) as exc:
        raise RemoveError("catalogue rows cannot be mapped to source lines") from exc
    if len(row_lines) != len(records):
        raise RemoveError("CSV row mapping differs from validated catalogue")
    lines = original_bytes.splitlines(keepends=True)
    line_index = row_lines[matches[0]]
    if line_index < 1 or line_index >= len(lines):
        raise RemoveError("invalid physical row mapping")
    proposed_bytes = b"".join(line for i, line in enumerate(lines) if i != line_index)
    if proposed_bytes == original_bytes:
        raise RemoveError("record removal did not change CSV")

    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(mode="wb", dir=source.parent,
                                         prefix=".catalogue-remove-", suffix=".csv",
                                         delete=False) as stream:
            temporary = Path(stream.name)
            os.fchmod(stream.fileno(), 0o600)
            stream.write(proposed_bytes)
            stream.flush()
            os.fsync(stream.fileno())
        survivors = read_catalog(temporary)
        expected = tuple(item for i, item in enumerate(records) if i != matches[0])
        if survivors != expected:
            raise RemoveError("removed CSV does not preserve surviving records")

        # An existing backup path fails without replacing it. The backup
        # captures the exact prior CSV bytes, not a regenerated representation.
        backup_receipt = backup_catalogue(source, backup)
        if backup_receipt["csv_sha256"] != digest:
            raise RemoveError("catalogue changed while preparing safety backup")
        if source.is_symlink() or _read_bounded_bytes(source) != original_bytes:
            raise RemoveError("catalogue changed before replacement")
        os.replace(temporary, source)
        temporary = None
    except (OSError, CatalogError, BackupError) as exc:
        raise RemoveError("record removal could not be completed") from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)

    removed = records[matches[0]]
    return {
        "format": "culturefit_operator_record_remove_v1",
        "source_status": "user_supplied_unverified",
        "provider_independently_verified": False,
        "provider_calls": 0,
        "previous_csv_sha256": digest,
        "current_csv_sha256": hashlib.sha256(proposed_bytes).hexdigest(),
        "previous_record_count": len(records),
        "current_record_count": len(survivors),
        "removed": {"lane": removed.lane, "name": removed.name, "city": removed.city or None},
        "safety_backup": str(backup),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Remove one local operator-supplied CSV record")
    parser.add_argument("--catalog", required=True)
    parser.add_argument("--safety-backup", required=True)
    parser.add_argument("--expect-current-sha256", required=True)
    parser.add_argument("--lane", required=True, choices=("place", "brand", "artist"))
    parser.add_argument("--name", required=True)
    parser.add_argument("--city", default="")
    parser.add_argument("--confirm-remove", action="store_true")
    args = parser.parse_args(argv)
    try:
        report = remove_candidate(
            args.catalog, safety_backup=args.safety_backup,
            expected_sha256=args.expect_current_sha256,
            lane=args.lane, name=args.name, city=args.city,
            confirm_remove=args.confirm_remove,
        )
    except RemoveError as exc:
        print(f"catalogue remove error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(report, ensure_ascii=False, allow_nan=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
