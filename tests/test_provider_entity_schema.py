"""Offline regressions for provider types arrays and the frozen artist case."""
from __future__ import annotations
import copy
import unittest
from urllib.parse import urlencode
from unittest.mock import patch

from provider_entity_schema import ProviderTypesQlooClient, ProviderEntityTypeError
from provider_gateway import Session, AlreadyUsed, backend_with_guarded_client
from strict_culture_fit import IncompleteCultureFitFlow
from offline_fixture_transport import OfflineFixtureHTTPSHandler


def entity(family, name="Taylor Swift", identity="synthetic-seed"):
    return {"entity_id": identity, "name": name, "types": ["urn:entity:" + family]}


def typed_fixture(*, ambiguous=False, wrong_seed=False):
    def url(path, params):
        return "https://hackathon.api.qloo.com" + path + "?" + urlencode(params)
    seeds = [entity("person" if wrong_seed else "artist")]
    if ambiguous:
        seeds.append(entity("artist", identity="synthetic-second"))
    responses = {url("/search", [("query", "Taylor Swift"), ("take", "5"),
                                  ("types", "urn:entity:artist")]): {"results": seeds}}
    for family in ("place", "brand", "artist"):
        params = [("filter.type", "urn:entity:" + family),
                  ("signal.interests.entities", "synthetic-seed"), ("take", "1"),
                  ("signal.location.query", "Chicago")]
        if family == "place":
            params += [("filter.location.query", "Chicago"), ("filter.location.radius", "0")]
        responses[url("/v2/insights/", params)] = {
            "results": {"entities": [entity(family, "Synthetic " + family,
                                            "synthetic-" + family)]}}
    return OfflineFixtureHTTPSHandler(responses)


class ProviderEntitySchemaTests(unittest.TestCase):
    def test_list_results_types_bind_identity_without_mutating_raw_source(self):
        payload = {"results": [entity("artist")]}
        before = copy.deepcopy(payload)
        result = ProviderTypesQlooClient._entities(payload)
        self.assertEqual(result[0].entity_id, "synthetic-seed")
        self.assertEqual(result[0].name, "Taylor Swift")
        self.assertEqual(result[0].subtype, "urn:entity:artist")
        self.assertEqual(payload, before)
        self.assertNotIn("subtype", payload["results"][0])

    def test_nested_results_types_supported(self):
        result = ProviderTypesQlooClient._entities({"results": {"entities": [entity("place")]}})
        self.assertEqual(result[0].subtype, "urn:entity:place")

    def test_existing_literal_subtype_remains_unchanged(self):
        result = ProviderTypesQlooClient._entities({"results": [
            {"entity_id": "synthetic-place", "name": "Synthetic Place",
             "subtype": "urn:entity:place:venue"}]})
        self.assertEqual(result[0].subtype, "urn:entity:place:venue")

    def test_consistent_literal_and_declared_family_remain_literal(self):
        item = {**entity("place"), "subtype": "urn:entity:place:venue"}
        self.assertEqual(ProviderTypesQlooClient._entities({"results": [item]})[0].subtype,
                         "urn:entity:place:venue")

    def test_conflicting_provider_type_fields_fail_closed(self):
        with self.assertRaises(ProviderEntityTypeError):
            ProviderTypesQlooClient._entities({"results": [
                {**entity("place"), "subtype": "urn:entity:brand"}]})

    def test_multiple_distinct_families_are_not_inferred_from_requested_lane(self):
        item = {**entity("artist"), "types": ["urn:entity:artist", "urn:entity:person"]}
        with self.assertRaises(ProviderEntityTypeError):
            ProviderTypesQlooClient._entities({"results": [item]})

    def test_malformed_type_metadata_fails_closed(self):
        for declared in ([], None, "urn:entity:artist", [3], ["artist"],
                         ["urn:entity:artist", "not-a-type"]):
            with self.subTest(declared=declared), self.assertRaises(ProviderEntityTypeError):
                ProviderTypesQlooClient._entities({"results": [
                    {**entity("artist"), "types": declared}]})

    def test_same_family_declared_subtypes_do_not_create_conflict(self):
        item = {**entity("place"), "types": ["urn:entity:place", "urn:entity:place:venue"]}
        self.assertEqual(ProviderTypesQlooClient._entities({"results": [item]})[0].subtype,
                         "urn:entity:place")

    def test_empty_provider_results_stay_empty(self):
        self.assertEqual(ProviderTypesQlooClient._entities({"results": []}), [])

    def test_missing_provider_types_are_not_fabricated(self):
        item = {"entity_id": "synthetic-no-type", "name": "Synthetic"}
        self.assertIsNone(ProviderTypesQlooClient._entities({"results": [item]})[0].subtype)


class FrozenArtistCompositionTests(unittest.TestCase):
    def setUp(self):
        guard = patch("http.client.HTTPSConnection.connect",
                      side_effect=AssertionError("External HTTPS forbidden in offline tests"))
        self.connection = guard.start()
        self.addCleanup(guard.stop)
        self.addCleanup(self.connection.assert_not_called)

    def session(self, **options):
        transport = typed_fixture(**options)
        # This callback authorizes only this offline injected fixture in a test.
        # It is not live execution evidence or production authority.
        backend = backend_with_guarded_client(
            "SYNTHETIC-TEST-VALUE-NOT-A-CREDENTIAL", https_handler=transport,
            allowed_by_external_controller=lambda: True, synthetic=False)
        return Session(backend), transport

    def test_frozen_artist_query_and_all_provider_types_compose_without_network(self):
        session, transport = self.session()
        result = session.plan_once()
        self.assertIn("types=urn%3Aentity%3Aartist", transport.seen_urls[0])
        self.assertEqual([v["lane"] for v in result["evidence_cards"]],
                         ["place", "brand_partner", "artist"])
        self.assertEqual([v["subtype"] for v in result["evidence_cards"]],
                         ["urn:entity:place", "urn:entity:brand", "urn:entity:artist"])
        self.assertEqual(len(transport.seen_urls), 4)
        self.assertFalse(result["provider_independently_verified"])
        self.assertFalse(result["public_release_authorized"])
        self.assertFalse(result["submission_authorized"])
        with self.assertRaises(AlreadyUsed):
            session.plan_once()
        self.assertEqual(len(transport.seen_urls), 4)

    def test_provider_ignoring_artist_constraint_is_rejected_before_insights(self):
        session, transport = self.session(wrong_seed=True)
        with self.assertRaises(ProviderEntityTypeError):
            session.plan_once()
        self.assertEqual(len(transport.seen_urls), 1)

    def test_two_distinct_artist_identities_still_require_clarification(self):
        session, transport = self.session(ambiguous=True)
        with self.assertRaises(IncompleteCultureFitFlow) as found:
            session.plan_once()
        self.assertEqual(found.exception.code, "SEED_AMBIGUOUS")
        self.assertEqual(len(transport.seen_urls), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
