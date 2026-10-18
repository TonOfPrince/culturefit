#!/usr/bin/env python3
"""Create a private, operator-selected handoff from a CultureFit shortlist.

Consumes a single already-generated source-backed shortlist JSON. The operator
explicitly selects rank positions. No provider, URL fetch, or external service
is involved; the handoff preserves unverified source attribution.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit

LANES = ("place", "brand", "artist")
MAX_SHORTLIST_BYTES = 250_000
MAX_SELECTIONS = 20
MAX_NOTE_LENGTH = 500
SELECT_RE = re.compile(r"^(place|brand|artist):([1-9][0-9]*)$")
SOURCE_STATUS = "user_supplied_unverified"
BASIS = "exact_matches_to_user_authored_tags"


class HandoffError(ValueError):
    """A shortlist, selection, or destination cannot support a handoff."""


def _object_pairs_unique(items: list[tuple[str, object]]) -> dict:
    result: dict = {}
    for key, value in items:
        if key in result:
            raise HandoffError("duplicate JSON object field")
        result[key] = value
    return result


def _fail_constant(_value: str):
    raise HandoffError("non-finite JSON value")


def _safe_text(value: object, field: str, *, max_length: int = 300) -> str:
    if not isinstance(value, str):
        raise HandoffError("invalid " + field)
    stripped = value.strip()
    if not stripped or len(stripped) > max_length or any(
        ord(ch) < 32 or 0xD800 <= ord(ch) <= 0xDFFF for ch in stripped
    ):
        raise HandoffError("invalid " + field)
    return stripped


def _verify_source(value: object) -> str:
    value = _safe_text(value, "source_url")
    try:
        parsed = urlsplit(value)
        if (parsed.scheme != "https" or not parsed.hostname or
                parsed.username is not None or parsed.password is not None or
                parsed.port == 0 or any(ch.isspace() for ch in value)):
            raise HandoffError("invalid source_url")
    except ValueError as exc:
        raise HandoffError("invalid source_url") from exc
    return value


def read_shortlist(path: str | Path) -> tuple[dict, str]:
    path = Path(path)
    try:
        if not path.is_file() or path.stat().st_size > MAX_SHORTLIST_BYTES:
            raise HandoffError("shortlist missing or exceeds size limit")
        raw = path.read_bytes()
    except OSError as exc:
        raise HandoffError("shortlist unavailable") from exc
    if not raw or len(raw) > MAX_SHORTLIST_BYTES:
        raise HandoffError("shortlist missing or exceeds size limit")
    try:
        payload = json.loads(raw.decode("utf-8"), object_pairs_hook=_object_pairs_unique,
                             parse_constant=_fail_constant)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise HandoffError("shortlist is not UTF-8 JSON") from exc
    if not isinstance(payload, dict):
        raise HandoffError("shortlist must be a JSON object")
    if (payload.get("basis") != BASIS or payload.get("source_status") != SOURCE_STATUS or
            type(payload.get("provider_calls")) is not int or payload["provider_calls"] != 0):
        raise HandoffError("shortlist lacks expected source-only provenance")
    city = _safe_text(payload.get("city"), "city")
    terms = payload.get("interests")
    if not isinstance(terms, list) or not 1 <= len(terms) <= 12:
        raise HandoffError("shortlist interests missing")
    for term in terms:
        _safe_text(term, "interest")
    normalized_terms = [term.casefold() for term in terms]
    if terms != normalized_terms or len(set(normalized_terms)) != len(terms):
        raise HandoffError("interests must be canonical unique tags")
    normalized_filters: dict[str, list[str]] = {}
    for key in ("required_tags", "excluded_tags"):
        tags = payload.get(key, [])
        if not isinstance(tags, list) or len(tags) > 12:
            raise HandoffError("invalid " + key)
        for tag in tags:
            _safe_text(tag, key)
        folded = [tag.casefold() for tag in tags]
        if tags != folded or len(set(folded)) != len(tags):
            raise HandoffError("invalid " + key)
        normalized_filters[key] = folded
    if set(normalized_filters["required_tags"]) & set(normalized_filters["excluded_tags"]):
        raise HandoffError("required and excluded tags conflict")
    lanes = payload.get("lanes")
    if not isinstance(lanes, dict) or set(lanes) != set(LANES):
        raise HandoffError("expected three typed shortlist lanes")
    missing = payload.get("missing_lanes")
    if not isinstance(missing, list) or missing != [lane for lane in LANES if not lanes[lane]]:
        raise HandoffError("missing-lane provenance inconsistent")
    for lane in LANES:
        records = lanes[lane]
        if not isinstance(records, list) or len(records) > 20:
            raise HandoffError("invalid shortlist lane")
        for card in records:
            if not isinstance(card, dict) or card.get("lane") != lane:
                raise HandoffError("invalid card lane")
            _safe_text(card.get("name"), "card name")
            if "city" not in card:
                raise HandoffError("missing card city field")
            card_city = card["city"]
            if lane == "place" and (not isinstance(card_city, str) or
                                    card_city.casefold() != city.casefold()):
                raise HandoffError("place outside requested city")
            if card_city is not None:
                _safe_text(card_city, "card city")
            _safe_text(card.get("source_label"), "source_label")
            _verify_source(card.get("source_url"))
            if card.get("source_status") != SOURCE_STATUS:
                raise HandoffError("card provenance is not source-only")
            matched = card.get("matched_interests")
            if (not isinstance(matched, list) or not matched or
                    any(not isinstance(x, str) or x not in terms for x in matched) or
                    matched != [x.casefold() for x in matched] or
                    len(set(matched)) != len(matched) or
                    type(card.get("match_count")) is not int or
                    card["match_count"] != len(matched)):
                raise HandoffError("invalid card interest matches")
    return payload, hashlib.sha256(raw).hexdigest()


def select_handoff(payload: dict, digest: str, choices: list[str], *, note: str = "") -> dict:
    if not choices or len(choices) > MAX_SELECTIONS:
        raise HandoffError("select between 1 and 20 records")
    if not isinstance(note, str):
        raise HandoffError("invalid operator note")
    note = note.strip()
    if len(note) > MAX_NOTE_LENGTH or any(
        ord(ch) < 32 or 0xD800 <= ord(ch) <= 0xDFFF for ch in note
    ):
        raise HandoffError("invalid operator note")
    picked: list[dict] = []
    seen: set[tuple[str, int]] = set()
    for raw_choice in choices:
        if not isinstance(raw_choice, str):
            raise HandoffError("invalid selection")
        match = SELECT_RE.fullmatch(raw_choice)
        if not match:
            raise HandoffError("selection must be lane:1-based-rank")
        if len(match.group(2)) > 2:
            raise HandoffError("selection rank does not exist")
        lane, ordinal = match.group(1), int(match.group(2))
        key = (lane, ordinal)
        if key in seen:
            raise HandoffError("duplicate operator selection")
        seen.add(key)
        lane_records = payload["lanes"][lane]
        if ordinal > len(lane_records):
            raise HandoffError("selection rank does not exist")
        card = lane_records[ordinal - 1]
        picked.append({
            "operator_selection": {"lane": lane, "rank": ordinal},
            "name": card["name"],
            "city": card["city"],
            "matched_interests": card["matched_interests"],
            "source_url": card["source_url"],
            "source_label": card["source_label"],
            "source_status": SOURCE_STATUS,
        })
    return {
        "format": "culturefit_operator_handoff_v1",
        "review_status": "operator_selected_not_provider_verified",
        "originating_shortlist_sha256": digest,
        "query": {
            "city": payload["city"],
            "interests": payload["interests"],
            "required_tags": payload.get("required_tags", []),
            "excluded_tags": payload.get("excluded_tags", []),
        },
        "operator_note": note,
        "selection_count": len(picked),
        "selections": picked,
    }


def save_handoff(target: str | Path, record: dict) -> None:
    path = Path(target)
    data = (json.dumps(record, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise HandoffError("output already exists; no overwrites") from exc
    except OSError as exc:
        raise HandoffError("output cannot be created") from exc
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
    except OSError as exc:
        path.unlink(missing_ok=True)
        raise HandoffError("handoff write failed") from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Export operator-approved picks from one sourced shortlist")
    parser.add_argument("--shortlist", required=True, help="JSON generated by curated_catalog.py")
    parser.add_argument("--select", action="append", required=True,
                        help="Repeat lane:rank, e.g. --select place:1")
    parser.add_argument("--note", default="", help="Optional operator-authored note")
    parser.add_argument("--output", required=True, help="New private JSON handoff path; never overwritten")
    args = parser.parse_args(argv)
    try:
        if Path(args.shortlist).absolute() == Path(args.output).absolute():
            raise HandoffError("output must differ from source")
        payload, digest = read_shortlist(args.shortlist)
        handoff = select_handoff(payload, digest, args.select, note=args.note)
        save_handoff(args.output, handoff)
    except HandoffError as exc:
        print(f"handoff error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
