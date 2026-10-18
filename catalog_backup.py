#!/usr/bin/env python3
"""Privately preserve one exact operator-authored catalogue CSV snapshot.

Never fetch sources or contact providers. This intentionally copies the user's
original CSV bytes, not a regenerated result or provider recommendation.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path

from catalog_diff import _read_bounded_bytes
from curated_catalog import CatalogError, read_catalog


class BackupError(ValueError):
    """The requested private backup cannot be created safely."""


def backup_catalogue(catalog: str | Path, destination: str | Path) -> dict:
    """Create one private, complete, no-overwrite copy of a validated CSV."""
    source = Path(catalog).expanduser().absolute()
    target = Path(destination).expanduser().absolute()
    if source == target or source.is_symlink():
        raise BackupError("source and backup must be different regular files")
    if not target.parent.is_dir():
        raise BackupError("backup parent directory must already exist")

    try:
        before = _read_bounded_bytes(source)
        records = read_catalog(source)
        after = _read_bounded_bytes(source)
    except (CatalogError, OSError) as exc:
        raise BackupError("operator catalogue is unavailable or invalid") from exc
    if before != after:
        raise BackupError("operator catalogue changed while preparing backup")

    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", dir=target.parent, prefix=".catalogue-backup-",
            suffix=".tmp", delete=False,
        ) as stream:
            temporary = Path(stream.name)
            os.fchmod(stream.fileno(), 0o600)
            stream.write(before)
            stream.flush()
            os.fsync(stream.fileno())
        # Hard linking the completed private temporary file creates the named
        # backup atomically without ever replacing a preexisting path.
        os.link(temporary, target)
    except OSError as exc:
        raise BackupError("backup could not be created without overwriting") from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)

    if _read_bounded_bytes(target) != before:
        raise BackupError("backup bytes do not match the validated catalogue")
    digest = hashlib.sha256(before).hexdigest()
    return {
        "format": "culturefit_private_catalog_backup_v1",
        "source_status": "user_supplied_unverified",
        "provider_independently_verified": False,
        "provider_calls": 0,
        "backup_path": str(target),
        "csv_sha256": digest,
        "bytes": len(before),
        "records": len(records),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Create a private, non-overwriting CSV backup")
    parser.add_argument("--catalog", required=True, help="Operator-authored source CSV")
    parser.add_argument("--backup", required=True, help="New local CSV path; never overwritten")
    args = parser.parse_args(argv)
    try:
        report = backup_catalogue(args.catalog, args.backup)
    except BackupError as exc:
        print(f"backup error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
