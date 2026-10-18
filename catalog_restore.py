#!/usr/bin/env python3
"""Restore a private operator CSV with two exact hashes and a safety backup.

Run only while the local catalogue server and other writers are stopped. This
CLI does not provide cross-process locking; a concurrent writer can race its
final comparison. No source URL or provider is contacted.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile

from catalog_backup import BackupError, backup_catalogue
from catalog_diff import _read_bounded_bytes
from curated_catalog import CatalogError, read_catalog

_DIGEST = re.compile(r"[a-f0-9]{64}\Z")


class RestoreError(ValueError):
    """The requested replacement cannot be shown to be safe."""


def _snapshot(path: Path) -> tuple[bytes, int]:
    """Validate a regular, non-symlink operator CSV bound to stable bytes."""
    if path.is_symlink():
        raise RestoreError("symlink catalogues are not allowed")
    try:
        before = _read_bounded_bytes(path)
        rows = read_catalog(path)
        after = _read_bounded_bytes(path)
    except (CatalogError, OSError) as exc:
        raise RestoreError("catalogue unavailable or invalid") from exc
    if before != after:
        raise RestoreError("catalogue changed while being checked")
    return before, len(rows)


def restore_catalogue(
    backup: str | Path,
    catalogue: str | Path,
    safety_backup: str | Path,
    *,
    expected_backup_sha256: str,
    expected_current_sha256: str,
    confirm_replace: bool = False,
) -> dict:
    """Safely replace one stopped local catalogue, retaining the prior bytes.

    Expected digests bind the operator's intent to specific old/new snapshots.
    Other writers must be stopped: the final compare/replace is NOT a native
    atomic conditional-write primitive across unrelated processes.
    """
    if not confirm_replace:
        raise RestoreError("explicit --confirm-replace is required")
    if any(not isinstance(d, str) or _DIGEST.fullmatch(d) is None
           for d in (expected_backup_sha256, expected_current_sha256)):
        raise RestoreError("expected digests must be 64 lowercase hex characters")

    source = Path(backup).expanduser().absolute()
    target = Path(catalogue).expanduser().absolute()
    fallback = Path(safety_backup).expanduser().absolute()
    if len({source, target, fallback}) != 3:
        raise RestoreError("backup, active catalogue and safety backup must be distinct")
    if source.is_symlink() or target.is_symlink() or fallback.is_symlink():
        raise RestoreError("symlink inputs or destinations are not supported")
    if not target.parent.is_dir() or not fallback.parent.is_dir():
        raise RestoreError("destination parent directories must exist")
    if fallback.exists():
        raise RestoreError("safety backup destination already exists")
    try:
        if source.samefile(target):
            raise RestoreError("backup and catalogue must not be hard-link aliases")
    except OSError as exc:
        raise RestoreError("required local files are unavailable") from exc

    replacement, replacement_count = _snapshot(source)
    prior, prior_count = _snapshot(target)
    replacement_sha = hashlib.sha256(replacement).hexdigest()
    prior_sha = hashlib.sha256(prior).hexdigest()
    if replacement_sha != expected_backup_sha256:
        raise RestoreError("backup SHA-256 does not match operator's expected version")
    if prior_sha != expected_current_sha256:
        raise RestoreError("current catalogue SHA-256 does not match operator's expected version")
    if replacement == prior:
        raise RestoreError("catalogue already contains the requested backup bytes")

    # Preserve the *actual* prior CSV before replacement. If this fails, do not
    # change the active catalogue. Never write a backup over an existing path.
    try:
        safe = backup_catalogue(target, fallback)
    except BackupError as exc:
        raise RestoreError("could not preserve prior catalogue") from exc
    if safe["csv_sha256"] != prior_sha:
        raise RestoreError("safety backup does not match expected current catalogue")

    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", dir=target.parent, prefix=".catalogue-restore-",
            suffix=".tmp", delete=False,
        ) as out:
            temporary = Path(out.name)
            os.fchmod(out.fileno(), 0o600)
            out.write(replacement)
            out.flush()
            os.fsync(out.fileno())
        if _read_bounded_bytes(temporary) != replacement:
            raise RestoreError("prepared restore file does not match requested backup")
        # Recheck source and target immediately before the atomic file replacement.
        if (source.is_symlink() or target.is_symlink() or
                _read_bounded_bytes(source) != replacement or
                _read_bounded_bytes(target) != prior):
            raise RestoreError("source or current catalogue changed before replacement")
        os.replace(temporary, target)
        temporary = None
        try:
            parent_fd = os.open(target.parent, os.O_RDONLY)
            try:
                os.fsync(parent_fd)
            finally:
                os.close(parent_fd)
        except OSError as exc:
            raise RestoreError("catalogue replaced, but directory sync failed") from exc
    except OSError as exc:
        raise RestoreError("restore could not be completed") from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)

    if _read_bounded_bytes(target) != replacement:
        raise RestoreError("restored catalogue bytes failed post-write verification")
    return {
        "format": "culturefit_private_catalog_restore_v1",
        "source_status": "user_supplied_unverified",
        "provider_independently_verified": False,
        "provider_calls": 0,
        "restored_csv_sha256": replacement_sha,
        "previous_csv_sha256": prior_sha,
        "restored_records": replacement_count,
        "previous_records": prior_count,
        "safety_backup_path": str(fallback),
        "safety_backup_sha256": safe["csv_sha256"],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Restore an operator CSV after stopping all local writers")
    parser.add_argument("--backup", required=True)
    parser.add_argument("--catalog", required=True)
    parser.add_argument("--safety-backup", required=True)
    parser.add_argument("--expect-backup-sha256", required=True)
    parser.add_argument("--expect-current-sha256", required=True)
    parser.add_argument("--confirm-replace", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = restore_catalogue(
            args.backup, args.catalog, args.safety_backup,
            expected_backup_sha256=args.expect_backup_sha256,
            expected_current_sha256=args.expect_current_sha256,
            confirm_replace=args.confirm_replace,
        )
    except RestoreError as exc:
        print(f"restore error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, allow_nan=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
