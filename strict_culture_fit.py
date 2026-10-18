"""Strict candidate-completeness adapter for the existing CultureFit agent.

LOCAL_ONLY / UNPUBLISHED: fails closed on ambiguous public seeds or missing
required recommendation lanes. It is not a provider credential, provider
attestation, endorsement, commercial license, or release authority.

The original qloo_client.py and culture_fit.py are imported unchanged.
"""
from __future__ import annotations

from culture_fit import CultureFitAgent
from qloo_client import QlooEntity


class IncompleteCultureFitFlow(ValueError):
    """A generated brief is not a complete three-lane candidate."""

    def __init__(self, code: str, lanes: tuple[str, ...] = ()) -> None:
        super().__init__(code + (f" ({', '.join(lanes)})" if lanes else ""))
        self.code = code
        self.lanes = lanes


class StrictCultureFitAgent(CultureFitAgent):
    """Drop-in subclass with deterministic acceptance checks, no extra I/O."""

    def _resolve_seed(self, query: str) -> QlooEntity:
        # Retain the incumbent's exact-match-only resolution rule, but never
        # silently choose between distinct Qloo identities sharing a name.
        candidates = self.qloo.search(query, take=5)
        exact = [c for c in candidates if c.name.casefold() == query.strip().casefold()]
        identities = {c.entity_id for c in exact if isinstance(c.entity_id, str) and c.entity_id.strip()}
        if len(identities) != 1:
            raise IncompleteCultureFitFlow(
                "SEED_AMBIGUOUS" if len(identities) > 1 else "SEED_NOT_RESOLVED"
            )
        # Multiplicity of the SAME identity is harmless, and the returned
        # object is one of the actual provider entities, not reconstructed.
        return next(c for c in exact if c.entity_id in identities)

    def plan(self, *, seeds: list[str], city: str, take_per_lane: int = 3) -> dict:
        # Delegate request construction, actual Qloo client calls and card
        # provenance to the exact existing agent. No local cards are invented.
        plan = super().plan(seeds=seeds, city=city, take_per_lane=take_per_lane)
        expected = tuple(lane for lane, _ in self.LANES)
        cards = plan.get("evidence_cards")
        if not isinstance(cards, list):
            raise IncompleteCultureFitFlow("CARD_LIST_INVALID")
        counts = {lane: 0 for lane in expected}
        for card in cards:
            if not isinstance(card, dict) or card.get("lane") not in counts:
                raise IncompleteCultureFitFlow("CARD_LANE_INVALID")
            if (not isinstance(card.get("entity_id"), str) or not card["entity_id"].strip()
                    or not isinstance(card.get("name"), str) or not card["name"].strip()):
                raise IncompleteCultureFitFlow("CARD_IDENTITY_INVALID")
            counts[card["lane"]] += 1
            if counts[card["lane"]] > take_per_lane:
                raise IncompleteCultureFitFlow("CARD_COUNT_EXCEEDED", (card["lane"],))
        missing = tuple(lane for lane in expected if counts[lane] == 0)
        if missing:
            raise IncompleteCultureFitFlow("REQUIRED_LANE_EMPTY", missing)
        # Identical original output shape and contents for a valid brief.
        # Deliberately does not claim LIVE_QLOO or HACK-002 acceptance.
        return plan
