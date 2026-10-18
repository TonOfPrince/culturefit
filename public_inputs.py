"""Validate public cultural anchors and a city before any CultureFit provider effect.

Pure standard-library code. This module performs no network I/O, reads no
credentials, and does not decide whether a request is authorized to run.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass


class PublicInputError(ValueError):
    """Sanitized code suitable for a user-visible validation response."""

    def __init__(self, code: str = "INVALID_PUBLIC_INPUT") -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class PublicRequest:
    seeds: tuple[str, ...]
    city: str


def _public_label(value: object, maximum: int) -> str:
    if not isinstance(value, str):
        raise PublicInputError()
    value = value.strip()
    if not 2 <= len(value) <= maximum:
        raise PublicInputError()
    if any(unicodedata.category(ch).startswith("C") for ch in value):
        raise PublicInputError()
    if "@" in value or re.search(r"(?:https?://|www\.)", value, re.IGNORECASE):
        raise PublicInputError("EMAIL_OR_URL_NOT_ALLOWED")
    return value


def parse_public_request(payload: object) -> PublicRequest:
    """Accept only explicit, bounded public anchors and a city.

    This filter does not establish that a name is public or that a future API
    call is permitted. The caller must still show a personal-data warning,
    apply a body-size limit, and use an authorized idempotent provider gate.
    """
    if not isinstance(payload, dict) or set(payload) != {"seeds", "city"}:
        raise PublicInputError()
    entries = payload["seeds"]
    if not isinstance(entries, list) or not 1 <= len(entries) <= 5:
        raise PublicInputError()
    seeds = tuple(_public_label(value, 100) for value in entries)
    if len({seed.casefold() for seed in seeds}) != len(seeds):
        raise PublicInputError("DUPLICATE_ANCHOR")
    city = _public_label(payload["city"], 80)
    return PublicRequest(seeds=seeds, city=city)
