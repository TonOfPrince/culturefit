from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Protocol

from qloo_client import QlooEntity


class TasteGraph(Protocol):
    def search(self, query: str, *, types=(), take: int = 5) -> list[QlooEntity]: ...
    def insights(self, *, filter_type: str, interest_entity_ids, location_query: str | None = None, take: int = 5) -> list[QlooEntity]: ...


@dataclass(frozen=True)
class EvidenceCard:
    lane: str
    entity_id: str
    name: str
    subtype: str | None
    source: str = "Qloo aggregate affinity"


class CultureFitAgent:
    """Bounded cultural-launch planner grounded only in Qloo affinity results."""

    LANES = (
        ("place", "urn:entity:place"),
        ("brand_partner", "urn:entity:brand"),
        ("artist", "urn:entity:artist"),
    )

    def __init__(self, qloo: TasteGraph) -> None:
        self.qloo = qloo

    def _resolve_seed(self, query: str) -> QlooEntity:
        candidates = self.qloo.search(query, take=5)
        if not candidates:
            raise ValueError(f"Qloo could not resolve seed: {query!r}")
        exact = [c for c in candidates if c.name.casefold() == query.strip().casefold()]
        if exact:
            return exact[0]
        names = ", ".join(candidate.name for candidate in candidates[:5])
        raise ValueError(
            f"Qloo seed requires clarification for {query!r}; candidates: {names}"
        )

    def plan(self, *, seeds: list[str], city: str, take_per_lane: int = 3) -> dict:
        normalized = [seed.strip() for seed in seeds if seed.strip()]
        if not 1 <= len(normalized) <= 5:
            raise ValueError("provide between 1 and 5 cultural seed entities")
        if not city.strip():
            raise ValueError("city is required")
        if not 1 <= take_per_lane <= 5:
            raise ValueError("take_per_lane must be between 1 and 5")

        resolved: list[QlooEntity] = []
        seen_ids: set[str] = set()
        for seed in normalized:
            entity = self._resolve_seed(seed)
            if entity.entity_id not in seen_ids:
                seen_ids.add(entity.entity_id)
                resolved.append(entity)
        if not resolved:
            raise ValueError("no distinct Qloo seed entities resolved")

        cards: list[EvidenceCard] = []
        ids = [entity.entity_id for entity in resolved]
        for lane, filter_type in self.LANES:
            for entity in self.qloo.insights(
                filter_type=filter_type,
                interest_entity_ids=ids,
                location_query=city,
                take=take_per_lane,
            ):
                cards.append(
                    EvidenceCard(
                        lane=lane,
                        entity_id=entity.entity_id,
                        name=entity.name,
                        subtype=entity.subtype,
                    )
                )

        return {
            "product": "CultureFit",
            "city": city.strip(),
            "resolved_seeds": [asdict(entity) for entity in resolved],
            "evidence_cards": [asdict(card) for card in cards],
            "interpretation_boundary": (
                "Qloo results are aggregate cultural affinities. CultureFit does not infer identity, "
                "sensitive traits, or individual future behavior, and it does not turn affinity into a causal claim."
            ),
        }
