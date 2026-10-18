"""Reject Qloo entities whose returned subtype contradicts the requested lane.

Original local adapter. No Qloo endpoint or credential is accessed here.
The verified base client and agent are imported unchanged.
"""
from __future__ import annotations
from typing import Iterable

from qloo_client import QlooEntity

VALID_LANE_TYPES = frozenset((
    "urn:entity:place", "urn:entity:brand", "urn:entity:artist"
))


class QlooCategoryMismatch(ValueError):
    """An input API response cannot safely substantiate the requested lane."""


class LaneIdentityTasteGraph:
    def __init__(self, upstream):
        self._upstream = upstream

    def search(self, query: str, *, types: Iterable[str] = (), take: int = 5):
        return self._upstream.search(query, types=types, take=take)

    def insights(self, *, filter_type: str, interest_entity_ids: Iterable[str],
                 location_query: str | None = None, take: int = 5):
        if filter_type not in VALID_LANE_TYPES:
            raise ValueError("unsupported recommendation lane")
        items = self._upstream.insights(
            filter_type=filter_type,
            interest_entity_ids=interest_entity_ids,
            location_query=location_query,
            take=take,
        )
        if not isinstance(items, (list, tuple)):
            raise QlooCategoryMismatch("Qloo recommendation lane has invalid result type")
        for entity in items:
            subtype = getattr(entity, "subtype", None)
            # Permit exact Qloo URN or a more specific subtype underneath
            # the same entity family, but never a different family.
            if not isinstance(entity, QlooEntity) or not isinstance(subtype, str) or not (
                subtype == filter_type or subtype.startswith(filter_type + ":")
            ):
                raise QlooCategoryMismatch("Qloo recommendation lane type mismatch")
        return items


def make_typed_agent(upstream):
    from culture_fit import CultureFitAgent
    return CultureFitAgent(LaneIdentityTasteGraph(upstream))
