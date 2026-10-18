"""Request actual locality restrictions for the place lane of CultureFit.

This narrow adapter deliberately does not change the original QlooClient or
CultureFitAgent. Qloo documents signal.location.query as relevance influence,
not as a geographic boundary. For venue recommendations, combine that signal
with filter.location.query and a zero filter.location.radius. Brand/artist
requests still use the original client unchanged. This is LOCAL_ONLY and
requires independent Qloo event-endpoint verification before acceptance.
"""
from __future__ import annotations

from collections.abc import Iterable
from qloo_client import QlooClient


class PlaceScopedTasteGraph:
    def __init__(self, upstream: QlooClient) -> None:
        self._upstream = upstream

    def search(self, query: str, *, types: Iterable[str] = (), take: int = 5):
        return self._upstream.search(query, types=types, take=take)

    def insights(self, *, filter_type: str, interest_entity_ids: Iterable[str],
                 location_query: str | None = None, take: int = 5):
        # Only place recommendations are intended to denote venues IN the city.
        # Artist and brand affinity need not be geographically constrained.
        if filter_type != "urn:entity:place":
            return self._upstream.insights(
                filter_type=filter_type,
                interest_entity_ids=interest_entity_ids,
                location_query=location_query,
                take=take,
            )
        if not isinstance(location_query, str) or not location_query.strip():
            raise ValueError("place recommendations require a nonempty locality query")
        locality = location_query.strip()
        ids = []
        for value in interest_entity_ids:
            if not isinstance(value, str):
                raise ValueError("interest IDs must be strings")
            value = value.strip()
            if value:
                ids.append(value)
        if not ids:
            raise ValueError("at least one interest entity is required")
        if not isinstance(take, int) or isinstance(take, bool) or not 1 <= take <= 20:
            raise ValueError("take must be between 1 and 20")
        # Retain the existing QlooClient._get protected transport, including its
        # configured event host, X-Api-Key, exact route, timeout and exception
        # behavior. Only the place-lane query gains two published filter fields.
        params = [
            ("filter.type", filter_type),
            ("signal.interests.entities", ",".join(ids)),
            ("take", str(take)),
            ("signal.location.query", locality),
            ("filter.location.query", locality),
            ("filter.location.radius", "0"),
        ]
        return self._upstream._entities(
            self._upstream._get("/v2/insights/", params)
        )
