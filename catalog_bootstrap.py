#!/usr/bin/env python3
"""Create a private, empty CultureFit operator catalogue with no sample data.

This independent, dependency-free CLI only prepares a user-owned local file.
It never fetches attributed sources or contacts a provider. Existing paths
are never replaced; all further edits belong to the separately gated importer.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys

COLUMNS = ("lane", "name", "city", "tags", "source_url", "source_label")
HEADER = (",".join(COLUMNS) + "\n").encode("ascii")


class BootstrapError(ValueError):
    """A new private catalogue cannot be created at the requested path."""


def create_catalogue(output: str | Path) -> dict:
    """Exclusively create a mode-0600, zero-record operator source CSV."""
    if not isinstance(output, (str, Path)) or not str(output).strip():
        raise BootstrapError("nonempty output path required")
    target = Path(output).expanduser().absolute()
    if not target.parent.is_dir() or target.parent.is_symlink():
        raise BootstrapError("destination parent must exist and not be a symlink")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = None
    created_identity = None
    try:
        fd = os.open(target, flags, 0o600)
        created_identity = os.fstat(fd).st_ino
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            fd = None
            handle.write(HEADER)
            handle.flush()
            os.fsync(handle.fileno())
    except OSError as exc:
        if fd is not None:
            os.close(fd)
        if created_identity is not None:
            try:
                if target.lstat().st_ino == created_identity:
                    target.unlink()
            except OSError:
                pass
        raise BootstrapError("new catalogue unavailable or destination already exists") from exc
    return {
        "format": "culturefit_empty_operator_catalogue_v1",
        "state": "ready",
        "record_count": 0,
        "lanes": {lane: 0 for lane in ("place", "brand", "artist")},
        "source_status": "user_supplied_unverified",
        "provider_independently_verified": False,
        "provider_calls": 0,
        "catalogue_sha256": hashlib.sha256(HEADER).hexdigest(),
        "output_path": str(target),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Create a private blank source CSV; never overwrite an existing file")
    parser.add_argument("--output", required=True, help="Nonexistent CSV path in an existing directory")
    args = parser.parse_args(argv)
    try:
        result = create_catalogue(args.output)
    except BootstrapError as exc:
        print(f"catalogue bootstrap error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, allow_nan=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
