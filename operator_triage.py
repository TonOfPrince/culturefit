#!/usr/bin/env python3
"""Record local human triage of an existing source-backed CultureFit handoff.

The operator must provide both immutable input files. No source URL is visited,
no provider API is called, and a triage choice is never an external authorization.
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
SOURCE_STATUS = "user_supplied_unverified"
BASIS = "exact_matches_to_user_authored_tags"
STATUSES = {"retain": "retain_for_internal_review",
            "check": "needs_source_check", "exclude": "excluded_by_operator"}
MAX_BYTES = 250_000
MAX_NOTE = 500
DECISION = re.compile(r"^(place|brand|artist):([1-9][0-9]?):(retain|check|exclude)$")
SHA256 = re.compile(r"^[a-f0-9]{64}$")


class TriageError(ValueError):
    """One of the operator-supplied inputs cannot support a decision record."""


def _unique_pairs(items):
    data = {}
    for key, value in items:
        if key in data:
            raise TriageError("duplicate JSON field")
        data[key] = value
    return data


def _reject_constant(_value):
    raise TriageError("non-finite JSON value")


def _read(path):
    try:
        source = Path(path)
        if not source.is_file() or source.stat().st_size > MAX_BYTES:
            raise TriageError("input missing or exceeds size limit")
        raw = source.read_bytes()
    except OSError as exc:
        raise TriageError("input unavailable") from exc
    if not raw or len(raw) > MAX_BYTES:
        raise TriageError("input missing or exceeds size limit")
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_pairs,
                           parse_constant=_reject_constant)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise TriageError("input must be valid UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise TriageError("input must be a JSON object")
    return value, hashlib.sha256(raw).hexdigest()


def _text(value, field, *, allow_empty=False, limit=300):
    if not isinstance(value, str):
        raise TriageError("invalid " + field)
    normalized = value.strip()
    if (not normalized and not allow_empty) or len(normalized) > limit or any(
        ord(c) < 32 or 0xD800 <= ord(c) <= 0xDFFF for c in normalized
    ):
        raise TriageError("invalid " + field)
    return normalized


def _source_url(value):
    url = _text(value, "source URL")
    try:
        parts = urlsplit(url)
        if (parts.scheme != "https" or not parts.hostname or
                parts.username is not None or parts.password is not None or
                parts.port == 0 or any(c.isspace() for c in url)):
            raise TriageError("untrusted source URL")
    except ValueError as exc:
        raise TriageError("untrusted source URL") from exc
    return url


def read_review_inputs(shortlist_path, handoff_path):
    """Bind a selected handoff to exact shortlist bytes and ranked source cards."""
    shortlist, shortlist_digest = _read(shortlist_path)
    handoff, handoff_digest = _read(handoff_path)
    if (shortlist.get("basis") != BASIS or
            shortlist.get("source_status") != SOURCE_STATUS or
            type(shortlist.get("provider_calls")) is not int or
            shortlist["provider_calls"] != 0):
        raise TriageError("shortlist is not source-only")
    lanes = shortlist.get("lanes")
    if not isinstance(lanes, dict) or set(lanes) != set(LANES):
        raise TriageError("shortlist lacks three lanes")
    if any(not isinstance(lanes[lane], list) or len(lanes[lane]) > 20 for lane in LANES):
        raise TriageError("invalid shortlist lane")
    if (handoff.get("format") != "culturefit_operator_handoff_v1" or
            handoff.get("review_status") != "operator_selected_not_provider_verified" or
            handoff.get("originating_shortlist_sha256") != shortlist_digest or
            not SHA256.fullmatch(str(handoff.get("originating_shortlist_sha256", "")))):
        raise TriageError("handoff does not bind the exact source-only shortlist")
    query = handoff.get("query")
    if (not isinstance(query, dict) or set(query) !=
            {"city", "interests", "required_tags", "excluded_tags"}):
        raise TriageError("invalid handoff query")
    if query != {key: shortlist.get(key, []) for key in query}:
        raise TriageError("handoff query differs from shortlist")
    _text(query["city"], "city")
    if not isinstance(query["interests"], list) or not query["interests"]:
        raise TriageError("missing interests")
    for key in ("interests", "required_tags", "excluded_tags"):
        if not isinstance(query[key], list) or len(query[key]) > 12:
            raise TriageError("invalid query terms")
        for term in query[key]:
            _text(term, "query term")
    _text(handoff.get("operator_note"), "operator note", allow_empty=True, limit=MAX_NOTE)
    selections = handoff.get("selections")
    if (not isinstance(selections, list) or not 1 <= len(selections) <= 20 or
            type(handoff.get("selection_count")) is not int or
            handoff["selection_count"] != len(selections)):
        raise TriageError("invalid handoff selection count")
    if set(handoff) != {"format", "review_status", "originating_shortlist_sha256",
                        "query", "operator_note", "selection_count", "selections"}:
        raise TriageError("unexpected handoff fields")
    seen = set()
    for item in selections:
        if not isinstance(item, dict):
            raise TriageError("invalid selected item")
        pos = item.get("operator_selection")
        if not isinstance(pos, dict) or set(pos) != {"lane", "rank"}:
            raise TriageError("invalid selected position")
        lane, rank = pos["lane"], pos["rank"]
        if lane not in LANES or type(rank) is not int or rank < 1 or rank > len(lanes[lane]):
            raise TriageError("selected rank is unavailable")
        if (lane, rank) in seen:
            raise TriageError("duplicate selected position")
        seen.add((lane, rank))
        card = lanes[lane][rank - 1]
        if not isinstance(card, dict):
            raise TriageError("invalid shortlist item")
        expected = {"operator_selection": {"lane": lane, "rank": rank},
                    "name": card.get("name"), "city": card.get("city"),
                    "matched_interests": card.get("matched_interests"),
                    "source_url": card.get("source_url"),
                    "source_label": card.get("source_label"),
                    "source_status": SOURCE_STATUS}
        if item != expected or set(item) != set(expected) or card.get("lane") != lane:
            raise TriageError("selected item differs from the source shortlist")
        _text(item["name"], "candidate name")
        _text(item["source_label"], "source label")
        _source_url(item["source_url"])
        if item["city"] is not None:
            _text(item["city"], "candidate city")
        if lane == "place" and (not isinstance(item["city"], str) or
                                item["city"].casefold() != query["city"].casefold()):
            raise TriageError("selected place is outside requested city")
        tags = item["matched_interests"]
        if (not isinstance(tags, list) or not tags or
                any(not isinstance(tag, str) or tag not in query["interests"] for tag in tags)):
            raise TriageError("invalid matched interests")
        if card.get("source_status") != SOURCE_STATUS:
            raise TriageError("source verification was falsely asserted")
    return handoff, handoff_digest


def triage(handoff, handoff_digest, decisions, *, note=""):
    """Apply explicit, local-only human triage; other selected items stay pending."""
    if not isinstance(decisions, list) or not 1 <= len(decisions) <= 20:
        raise TriageError("one to twenty explicit triage decisions required")
    note = _text(note, "reviewer note", allow_empty=True, limit=MAX_NOTE)
    available = {(item["operator_selection"]["lane"], item["operator_selection"]["rank"])
                 for item in handoff["selections"]}
    changes = {}
    for value in decisions:
        match = DECISION.fullmatch(value) if isinstance(value, str) else None
        if not match:
            raise TriageError("decision must be lane:rank:retain|check|exclude")
        lane, rank, action = match.group(1), int(match.group(2)), match.group(3)
        if (lane, rank) not in available:
            raise TriageError("decision does not reference a selected candidate")
        if (lane, rank) in changes:
            raise TriageError("duplicate triage decision")
        changes[(lane, rank)] = STATUSES[action]
    items = []
    counts = {"pending": 0, **{status: 0 for status in STATUSES.values()}}
    for selected in handoff["selections"]:
        pos = selected["operator_selection"]
        disposition = changes.get((pos["lane"], pos["rank"]), "pending")
        counts[disposition] += 1
        items.append({**selected, "disposition": disposition})
    return {"format": "culturefit_local_triage_v1",
            "review_status": "internal_operator_triage_only",
            "provider_independently_verified": False,
            "external_contact_authorized": False,
            "source_status": SOURCE_STATUS,
            "originating_shortlist_sha256": handoff["originating_shortlist_sha256"],
            "originating_handoff_sha256": handoff_digest,
            "query": handoff["query"],
            "reviewer_note": note,
            "decided_count": len(changes),
            "counts": counts,
            "items": items}


def save_new_private(path, data):
    raw = (json.dumps(data, ensure_ascii=False, allow_nan=False, indent=2) + "\n").encode("utf-8")
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except OSError as exc:
        raise TriageError("private output unavailable or already exists") from exc
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
    except OSError as exc:
        Path(path).unlink(missing_ok=True)
        raise TriageError("private output write failed") from exc


def main(argv=None):
    parser = argparse.ArgumentParser(description="Triage exact-source CultureFit handoffs locally")
    parser.add_argument("--shortlist", required=True)
    parser.add_argument("--handoff", required=True)
    parser.add_argument("--decide", action="append", required=True,
                        help="Repeat e.g. place:1:check, brand:1:retain")
    parser.add_argument("--note", default="", help="Optional one-line internal reviewer note")
    parser.add_argument("--output", required=True, help="New private JSON path; no overwrite")
    args = parser.parse_args(argv)
    try:
        paths = [Path(x).absolute() for x in (args.shortlist, args.handoff, args.output)]
        if len(set(paths)) != 3:
            raise TriageError("inputs and output must be different files")
        handoff, digest = read_review_inputs(args.shortlist, args.handoff)
        result = triage(handoff, digest, args.decide, note=args.note)
        save_new_private(args.output, result)
    except TriageError as exc:
        print(f"triage error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
