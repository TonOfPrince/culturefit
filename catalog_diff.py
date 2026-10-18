#!/usr/bin/env python3
"""Compare two operator-authored CultureFit catalogues without external requests.

A read-only maintenance tool. Both inputs use the existing standalone CSV
reader; source URLs and labels remain operator assertions, not verification.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import stat
import json
import sys
from pathlib import Path

from curated_catalog import CatalogError, LANES, MAX_CATALOG_BYTES, Candidate, read_catalog

FORMAT = "culturefit_operator_catalog_diff_v1"


def _key(candidate: Candidate) -> tuple[str, str, str]:
    return candidate.lane, candidate.name.casefold(), candidate.city.casefold()


def _card(candidate: Candidate) -> dict:
    return {
        "lane": candidate.lane,
        "name": candidate.name,
        "city": candidate.city or None,
        "tags": list(candidate.tags),
        "source_url": candidate.source_url,
        "source_label": candidate.source_label,
        "source_status": "user_supplied_unverified",
    }


def _identity_sort(key: tuple[str, str, str]) -> tuple[int, str, str]:
    return LANES.index(key[0]), key[1], key[2]


def compare_catalogues(previous: tuple[Candidate, ...],
                       current: tuple[Candidate, ...],
                       *, previous_digest: str, current_digest: str) -> dict:
    """Report genuine source/tag changes; never assert external truth."""
    before = {_key(candidate): candidate for candidate in previous}
    after = {_key(candidate): candidate for candidate in current}
    if len(before) != len(previous) or len(after) != len(current):
        raise CatalogError("duplicate candidate identity")
    added = []
    removed = []
    changed = []
    for key in sorted(set(before) | set(after), key=_identity_sort):
        old = before.get(key)
        new = after.get(key)
        if old is None:
            added.append(_card(new))
        elif new is None:
            removed.append(_card(old))
        else:
            fields = {}
            if set(old.tags) != set(new.tags):
                fields["added_tags"] = sorted(set(new.tags) - set(old.tags))
                fields["removed_tags"] = sorted(set(old.tags) - set(new.tags))
            for field in ("name", "city", "source_url", "source_label"):
                if getattr(old, field) != getattr(new, field):
                    fields[field] = {"before": getattr(old, field), "after": getattr(new, field)}
            if fields:
                changed.append({"before": _card(old), "after": _card(new),
                                "field_changes": fields})
    return {
        "format": FORMAT,
        "basis": "operator_authored_csv_snapshot_comparison",
        "source_status": "user_supplied_unverified",
        "provider_calls": 0,
        "provider_independently_verified": False,
        "previous_csv_sha256": previous_digest,
        "current_csv_sha256": current_digest,
        "counts": {
            "previous": len(previous),
            "current": len(current),
            "added": len(added),
            "removed": len(removed),
            "changed": len(changed),
            "unchanged": len(before.keys() & after.keys()) - len(changed),
        },
        "added": added,
        "removed": removed,
        "changed": changed,
    }


def _read_bounded_bytes(path: Path) -> bytes:
    """Read at most MAX+1 bytes from one regular file descriptor."""
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
        with os.fdopen(fd, "rb") as source:
            metadata = os.fstat(source.fileno())
            if not stat.S_ISREG(metadata.st_mode):
                raise CatalogError("catalogue snapshot must be a regular file")
            if metadata.st_size > MAX_CATALOG_BYTES:
                raise CatalogError("catalogue snapshot exceeds size limit")
            contents = source.read(MAX_CATALOG_BYTES + 1)
            if len(contents) > MAX_CATALOG_BYTES:
                raise CatalogError("catalogue snapshot exceeds size limit")
            return contents
    except OSError as exc:
        raise CatalogError("catalogue snapshot unavailable") from exc


def _read_stable_snapshot(path: Path) -> tuple[tuple[Candidate, ...], str]:
    """Bind parsed operator records to unchanged, bounded on-disk CSV bytes."""
    before = _read_bounded_bytes(path)
    records = read_catalog(path)
    after = _read_bounded_bytes(path)
    if before != after:
        raise CatalogError("catalogue changed during comparison")
    return records, hashlib.sha256(before).hexdigest()


def compare_files(previous_path: str | Path, current_path: str | Path) -> dict:
    """Compare source-bound file snapshots; refuse changed inputs."""
    previous = Path(previous_path)
    current = Path(current_path)
    old_candidates, old_digest = _read_stable_snapshot(previous)
    if previous.absolute() == current.absolute():
        new_candidates, new_digest = old_candidates, old_digest
    else:
        new_candidates, new_digest = _read_stable_snapshot(current)
    return compare_catalogues(old_candidates, new_candidates,
                              previous_digest=old_digest, current_digest=new_digest)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Compare operator-sourced CultureFit CSV catalogues")
    parser.add_argument("--previous", required=True, help="Existing older user-authored CSV")
    parser.add_argument("--current", required=True, help="Existing newer user-authored CSV")
    args = parser.parse_args(argv)
    try:
        result = compare_files(args.previous, args.current)
    except (CatalogError, OSError) as exc:
        print(f"catalogue comparison error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, allow_nan=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
