from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Callable, Iterable
from urllib.parse import urlencode
from urllib.request import Request, urlopen


class QlooError(RuntimeError):
    pass


@dataclass(frozen=True)
class QlooEntity:
    entity_id: str
    name: str
    subtype: str | None = None
    popularity: float | None = None


Opener = Callable[[Request, float], object]


class QlooClient:
    """Minimal Qloo search + Insights client with injectable I/O for safe testing."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout_seconds: float = 10.0,
        opener: Opener | None = None,
    ) -> None:
        self.api_key = api_key or os.environ.get("QLOO_API_KEY", "")
        self.base_url = (base_url or os.environ.get("QLOO_API_BASE", "https://api.qloo.com")).rstrip("/")
        self.timeout_seconds = timeout_seconds
        self._opener = opener or urlopen

    def _get(self, path: str, params: list[tuple[str, str]]) -> dict:
        if not self.api_key:
            raise QlooError("QLOO_API_KEY is required for live Qloo requests")
        url = f"{self.base_url}{path}?{urlencode(params, doseq=True)}"
        request = Request(
            url,
            method="GET",
            headers={"accept": "application/json", "X-Api-Key": self.api_key},
        )
        try:
            response = self._opener(request, self.timeout_seconds)
            body = response.read()
            status = getattr(response, "status", 200)
        except Exception as exc:
            raise QlooError(f"Qloo request failed: {exc}") from exc
        if status < 200 or status >= 300:
            raise QlooError(f"Qloo returned HTTP {status}")
        try:
            payload = json.loads(body.decode("utf-8"))
        except Exception as exc:
            raise QlooError("Qloo returned invalid JSON") from exc
        if payload.get("success") is False:
            raise QlooError("Qloo response reported success=false")
        return payload

    @staticmethod
    def _entities(payload: dict) -> list[QlooEntity]:
        results = payload.get("results", [])
        if isinstance(results, dict):
            raw_entities = results.get("entities", [])
        elif isinstance(results, list):
            raw_entities = results
        else:
            raw_entities = []
        entities: list[QlooEntity] = []
        for item in raw_entities:
            if not isinstance(item, dict):
                continue
            entity_id = item.get("entity_id") or item.get("id")
            name = item.get("name")
            if not entity_id or not name:
                continue
            popularity = item.get("popularity")
            entities.append(
                QlooEntity(
                    entity_id=str(entity_id),
                    name=str(name),
                    subtype=item.get("subtype"),
                    popularity=float(popularity) if isinstance(popularity, (int, float)) else None,
                )
            )
        return entities

    def search(self, query: str, *, types: Iterable[str] = (), take: int = 5) -> list[QlooEntity]:
        query = query.strip()
        if not query:
            raise ValueError("search query cannot be empty")
        if not 1 <= take <= 20:
            raise ValueError("take must be between 1 and 20")
        params: list[tuple[str, str]] = [("query", query), ("take", str(take))]
        for entity_type in types:
            params.append(("types", entity_type))
        return self._entities(self._get("/search", params))

    def insights(
        self,
        *,
        filter_type: str,
        interest_entity_ids: Iterable[str],
        location_query: str | None = None,
        take: int = 5,
    ) -> list[QlooEntity]:
        ids = [value.strip() for value in interest_entity_ids if value.strip()]
        if not filter_type.startswith("urn:entity:"):
            raise ValueError("filter_type must be a Qloo entity URN")
        if not ids:
            raise ValueError("at least one interest entity is required")
        if not 1 <= take <= 20:
            raise ValueError("take must be between 1 and 20")
        params: list[tuple[str, str]] = [
            ("filter.type", filter_type),
            ("signal.interests.entities", ",".join(ids)),
            ("take", str(take)),
        ]
        if location_query and location_query.strip():
            params.append(("signal.location.query", location_query.strip()))
        return self._entities(self._get("/v2/insights/", params))
