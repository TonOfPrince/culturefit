"""Adapt Qloo's declared entity types without changing the legacy client source.

Only provider-supplied entity URNs are used. Conflicting or ambiguous category
metadata fails closed; no family is inferred from a name, query or requested lane.
"""
from __future__ import annotations

import re
from qloo_client import QlooClient


class ProviderEntityTypeError(ValueError):
    pass


def _family(value):
    if not isinstance(value, str) or not re.fullmatch(
            r"urn:entity:[a-z][a-z0-9_]*(?::[A-Za-z0-9_.-]+)*", value):
        raise ProviderEntityTypeError("Invalid provider entity type")
    return ":".join(value.split(":")[:3])


def _typed_item(item):
    if not isinstance(item, dict) or "types" not in item:
        return item
    declared = item["types"]
    if not isinstance(declared, list) or not declared:
        raise ProviderEntityTypeError("Missing provider entity types")
    families = {_family(value) for value in declared}
    if len(families) != 1:
        raise ProviderEntityTypeError("Ambiguous provider entity types")
    family = next(iter(families))
    literal = item.get("subtype")
    if literal is not None:
        if _family(literal) != family:
            raise ProviderEntityTypeError("Conflicting provider entity types")
        return item
    # This compatibility field is derived from the provider's declared types,
    # not asserted to have existed as a literal subtype in the HTTP response.
    return {**item, "subtype": family}


class ProviderTypesQlooClient(QlooClient):
    @staticmethod
    def _entities(payload):
        results = payload.get("results", [])
        raw = results.get("entities", []) if isinstance(results, dict) else results
        if not isinstance(raw, list):
            return QlooClient._entities(payload)
        normalized = [_typed_item(item) for item in raw]
        if isinstance(results, dict):
            adapted = {**payload, "results": {**results, "entities": normalized}}
        else:
            adapted = {**payload, "results": normalized}
        return QlooClient._entities(adapted)


class ArtistSeedTasteGraph:
    """Explicit artist constraint for the separately authorized frozen example."""

    def __init__(self, upstream):
        self._upstream = upstream

    def search(self, query, *, types=(), take=5):
        if types and tuple(types) != ("urn:entity:artist",):
            raise ValueError("Frozen example requires an artist")
        entities = self._upstream.search(query, types=("urn:entity:artist",), take=take)
        if any(_family(entity.subtype) != "urn:entity:artist" for entity in entities):
            raise ProviderEntityTypeError("Frozen seed is not an artist")
        return entities

    def insights(self, **kwargs):
        return self._upstream.insights(**kwargs)
