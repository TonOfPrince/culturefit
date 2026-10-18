"""Local catalogue status and single-record append for the standalone HTTP API.

Operates on exactly the configured operator-owned CSV path, never source URLs.
The web server owns its in-process catalogue lock around calls. External
writers must still coordinate separately; the digest is not an OS-level CAS.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from catalog_add import CatalogueAddError, append_candidate
from curated_catalog import CatalogError, LANES, read_catalog
from catalog_diff import _read_bounded_bytes

ENTRY_FIELDS = frozenset({"expected_catalog_sha256", "confirm_add", "lane", "name",
                          "city", "tags", "source_url", "source_label"})


def catalogue_status(catalogue: str | Path) -> tuple[int, dict]:
    """Return real lane counts and current CSV identity, or an explicit failure."""
    path = Path(catalogue)
    if not path.exists() and not path.is_symlink():
        return 200, {"state": "empty", "record_count": 0,
                     "lanes": {lane: 0 for lane in LANES}}
    if path.is_symlink() or not path.is_file():
        return 503, {"error": "catalogue_unavailable"}
    try:
        before = _read_bounded_bytes(path)
        records = read_catalog(path)
        after = _read_bounded_bytes(path)
    except (OSError, CatalogError, ValueError, UnicodeError):
        return 503, {"error": "catalogue_unavailable"}
    if before != after:
        return 503, {"error": "catalogue_unavailable"}
    return 200, {
        "state": "ready", "record_count": len(records),
        "lanes": {lane: sum(item.lane == lane for item in records) for lane in LANES},
        "source_status": "user_supplied_unverified",
        "catalogue_sha256": hashlib.sha256(before).hexdigest(),
    }


def append_entry(catalogue: str | Path, payload: object) -> tuple[int, dict]:
    """Validate one authored record and append only against its expected hash."""
    if (not isinstance(payload, dict) or set(payload) != ENTRY_FIELDS
            or payload.get("confirm_add") is not True
            or any(not isinstance(payload[field], str) for field in ENTRY_FIELDS - {"confirm_add"})):
        return 422, {"error": "invalid_entry"}
    try:
        result = append_candidate(
            catalogue, expected_sha256=payload["expected_catalog_sha256"],
            lane=payload["lane"], name=payload["name"], city=payload["city"],
            tags=payload["tags"], source_url=payload["source_url"],
            source_label=payload["source_label"], confirm_add=True,
        )
    except CatalogueAddError as exc:
        reason = str(exc)
        if reason == "catalogue must be an existing regular non-symlink file":
            return 409, {"error": "catalogue_missing"}
        if reason in ("current catalogue SHA-256 differs from expected",
                      "catalogue changed during validation",
                      "catalogue changed before append; nothing replaced"):
            return 409, {"error": "catalogue_changed"}
        if reason == "existing catalogue is unavailable or invalid":
            return 503, {"error": "catalogue_unavailable"}
        return 422, {"error": "invalid_entry"}
    except (OSError, UnicodeError, ValueError):
        return 422, {"error": "invalid_entry"}
    return 201, result
