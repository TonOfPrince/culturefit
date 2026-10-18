"""Focused offline checks of the existing CultureFit provider composition.

Run from the complete CultureFit distribution root.
This checks native Python execution, not live Qloo, browser, or release readiness.
"""
from __future__ import annotations

import unittest
from urllib.parse import urlencode, urlsplit, parse_qs
from urllib.request import HTTPSHandler
from unittest.mock import patch

from provider_gateway import (
    Session, LockedError, AlreadyUsed, backend_with_guarded_client)
from public_inputs import PublicRequest, parse_public_request, PublicInputError
from strict_culture_fit import IncompleteCultureFitFlow
from offline_fixture_transport import OfflineFixtureHTTPSHandler
from qloo_client import QlooError


def fixture(*, empty_lane=None, ambiguous=False, missing_route=False):
    def url(path, params):
        return "https://hackathon.api.qloo.com" + path + "?" + urlencode(params)
    def response(entities):
        return {"success": True, "results": {"entities": entities}}
    seed = {"entity_id": "synthetic-seed", "name": "Taylor Swift",
            "subtype": "urn:entity:artist"}
    seeds = [seed, {**seed, "entity_id": "synthetic-second"}] if ambiguous else [seed]
    responses = {url("/search", [("query", "Taylor Swift"), ("take", "5")]): response(seeds)}
    for lane in ("place", "brand", "artist"):
        params = [("filter.type", "urn:entity:" + lane),
                  ("signal.interests.entities", "synthetic-seed"),
                  ("take", "1"), ("signal.location.query", "Chicago")]
        if lane == "place":
            params += [("filter.location.query", "Chicago"), ("filter.location.radius", "0")]
        if missing_route and lane == "artist":
            continue
        item = {"entity_id": "synthetic-" + lane, "name": "Synthetic " + lane,
                "subtype": "urn:entity:" + lane}
        responses[url("/v2/insights/", params)] = response([] if lane == empty_lane else [item])
    return OfflineFixtureHTTPSHandler(responses)


class ProviderCloseoutTests(unittest.TestCase):
    def setUp(self):
        self.network = patch("http.client.HTTPSConnection.connect",
                             side_effect=AssertionError("External HTTPS is forbidden"))
        self.mock_connect = self.network.start()
        self.addCleanup(self.network.stop)
        self.addCleanup(self.mock_connect.assert_not_called)

    def session(self, **options):
        transport = fixture(**options)
        backend = backend_with_guarded_client(
            "SYNTHETIC-TEST-VALUE-NOT-A-CREDENTIAL", https_handler=transport, synthetic=True)
        return Session(backend), transport

    def test_exact_four_operation_three_lane_composition(self):
        session, transport = self.session()
        result = session.plan_once(PublicRequest(("Taylor Swift",), "Chicago"))
        self.assertEqual([card["lane"] for card in result["evidence_cards"]],
                         ["place", "brand_partner", "artist"])
        self.assertEqual(len(transport.seen_urls), 4)
        self.assertEqual([urlsplit(url).path for url in transport.seen_urls],
                         ["/search", "/v2/insights/", "/v2/insights/", "/v2/insights/"])
        self.assertTrue(result["complete_required_lanes"])
        self.assertFalse(result["provider_independently_verified"])
        self.assertFalse(result["submission_authorized"])
        self.assertTrue(all("NOT Qloo evidence" in card["source"]
                            for card in result["evidence_cards"]))

    def test_place_only_receives_geographic_filter(self):
        session, transport = self.session()
        session.plan_once()
        queries = [parse_qs(urlsplit(url).query) for url in transport.seen_urls[1:]]
        self.assertEqual(queries[0]["filter.location.query"], ["Chicago"])
        self.assertEqual(queries[0]["filter.location.radius"], ["0"])
        self.assertNotIn("filter.location.query", queries[1])
        self.assertNotIn("filter.location.query", queries[2])

    def test_same_session_cannot_repeat_provider_operations(self):
        session, transport = self.session()
        session.plan_once()
        with self.assertRaises(AlreadyUsed):
            session.plan_once()
        self.assertEqual(len(transport.seen_urls), 4)

    def test_ambiguous_seed_stops_before_recommendations(self):
        session, transport = self.session(ambiguous=True)
        with self.assertRaises(IncompleteCultureFitFlow) as found:
            session.plan_once()
        self.assertEqual(found.exception.code, "SEED_AMBIGUOUS")
        self.assertEqual(len(transport.seen_urls), 1)

    def test_missing_lane_is_not_complete(self):
        session, transport = self.session(empty_lane="brand")
        with self.assertRaises(IncompleteCultureFitFlow) as found:
            session.plan_once()
        self.assertEqual(found.exception.code, "REQUIRED_LANE_EMPTY")
        self.assertEqual(len(transport.seen_urls), 4)

    def test_missing_offline_fixture_never_falls_back_to_network(self):
        session, transport = self.session(missing_route=True)
        with self.assertRaises(QlooError):
            session.plan_once()
        self.assertEqual(len(transport.seen_urls), 4)
        with self.assertRaises(AlreadyUsed):
            session.plan_once()

    def test_network_capable_transport_cannot_be_called_synthetic(self):
        with self.assertRaises(LockedError):
            backend_with_guarded_client(
                "SYNTHETIC-TEST-VALUE-NOT-A-CREDENTIAL",
                https_handler=HTTPSHandler(), synthetic=True)

    def test_locked_default_does_not_consume_attempt(self):
        session = Session()
        with self.assertRaises(LockedError):
            session.plan_once()
        self.assertFalse(session.used)

    def test_invalid_public_input_is_rejected_before_composition(self):
        session, transport = self.session()
        with self.assertRaises(PublicInputError):
            parse_public_request({"seeds": ["name@example.com"], "city": "Chicago"})
        self.assertFalse(session.used)
        self.assertEqual(transport.seen_urls, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
