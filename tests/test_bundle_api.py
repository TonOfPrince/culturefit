"""Localhost integration tests of actual operator-sourced three-lane bundles."""
from __future__ import annotations

import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path

from web_app import LocalCatalogueServer
from fit_bundle import compose_bundles
from curated_catalog import read_catalog

DATA = (
    b"lane,name,city,tags,source_url,source_label\n"
    b"place,Jazz Club,MINNEAPOLIS,jazz;coffee,https://example.org/venue,Operator Venue\n"
    b"place,Gallery,Minneapolis,art,https://example.org/gallery,Operator Gallery\n"
    b"place,Other City,Madison,art,https://example.org/madison,Operator Madison\n"
    b"brand,Beans,,coffee;art,https://example.org/beans,Operator Brand\n"
    b"artist,Saxophonist,,jazz;art,https://example.org/artist,Operator Artist\n"
)


class NativeBundleApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.catalog = Path(self.temp.name) / "operator.csv"
        self.catalog.write_bytes(DATA)
        self.server = LocalCatalogueServer(self.catalog, 0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close)

    def close(self):
        self.server.shutdown()
        self.thread.join(timeout=3)
        self.server.server_close()

    def request(self, payload, *, host=None, origin=None):
        data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        port = self.server.server_port
        headers = {"Host": host or f"127.0.0.1:{port}",
                   "Origin": origin or f"http://127.0.0.1:{port}",
                   "Content-Type": "application/json"}
        client = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
        client.request("POST", "/api/bundles", body=data, headers=headers)
        response = client.getresponse()
        code, body = response.status, json.loads(response.read())
        client.close()
        return code, body

    def test_real_three_lane_output_matches_existing_cli_engine(self):
        request = {"city": "Minneapolis", "interests": "jazz,coffee,art"}
        code, result = self.request(request)
        self.assertEqual(code, 200)
        self.assertEqual(result, compose_bundles(read_catalog(self.catalog), **request))
        self.assertEqual(result["bundles"][0]["covered_interests"], ["jazz", "coffee", "art"])
        self.assertEqual(result["bundles"][0]["members"]["place"]["city"], "MINNEAPOLIS")
        self.assertEqual(result["bundles"][0]["members"]["artist"]["source_label"], "Operator Artist")
        self.assertEqual(result["source_status"], "user_supplied_unverified")
        self.assertEqual(result["provider_calls"], 0)
        self.assertIs(result["provider_independently_verified"], False)
        self.assertEqual(self.catalog.read_bytes(), DATA)

    def test_missing_lane_and_missing_matching_tags_produce_no_bundle(self):
        self.catalog.write_bytes(DATA.replace(
            b"artist,Saxophonist,,jazz;art,https://example.org/artist,Operator Artist\n", b""))
        code, result = self.request({"city": "Minneapolis", "interests": "jazz,coffee"})
        self.assertEqual((code, result["bundles"], result["missing_lanes"]),
                         (200, [], ["artist"]))
        self.catalog.write_bytes(DATA)
        code, result = self.request({"city": "Minneapolis", "interests": "electronic"})
        self.assertEqual((code, result["bundles"], result["missing_lanes"]),
                         (200, [], ["place", "brand", "artist"]))

    def test_city_and_bounded_result_pool_preserve_source(self):
        request = {"city": "Madison", "interests": "jazz,coffee,art",
                   "per_lane": 1, "max_bundles": 1}
        code, result = self.request(request)
        self.assertEqual(code, 200)
        self.assertEqual(result["candidate_pool_counts"], {"place": 1, "brand": 1, "artist": 1})
        self.assertEqual(result["bundles"][0]["members"]["place"]["name"], "Other City")
        self.assertEqual(len(result["bundles"]), 1)
        code, filtered = self.request({**request, "required_tags": "jazz"})
        self.assertEqual(code, 200)
        self.assertEqual(filtered["bundles"], [])
        self.assertIn("brand", filtered["missing_lanes"])

    def test_duplicate_invalid_json_and_limits_return_422_without_mutation(self):
        prior = self.catalog.read_bytes()
        cases = [
            {}, {"city": "Minneapolis"}, {"city": 9, "interests": "jazz"},
            {"city": "Minneapolis", "interests": "jazz", "per_lane": True},
            {"city": "Minneapolis", "interests": "jazz", "max_bundles": 11},
            {"city": "Minneapolis", "interests": "jazz", "provider_mode": "live"},
            {"city": "Minneapolis", "interests": "jazz", "required_tags": "jazz",
             "excluded_tags": "jazz"},
            {"city": "Minneapolis", "interests": "\ud800"},
            b'{"city":"Minneapolis","interests":"jazz","interests":"coffee"}',
            b'{"city":"Minneapolis","interests":NaN}',
        ]
        for case in cases:
            with self.subTest(case=repr(case)):
                self.assertEqual(self.request(case),
                                 (422, {"error": "invalid_bundles"}))
        self.assertEqual(self.catalog.read_bytes(), prior)
        self.assertEqual(self.request({"city": "Minneapolis", "interests": "jazz"})[0], 200)

    def test_missing_catalogue_and_host_origin_guard(self):
        payload = {"city": "Minneapolis", "interests": "jazz"}
        original = self.catalog.read_bytes()
        self.assertEqual(self.request(payload, host="untrusted.invalid")[0], 403)
        self.assertEqual(self.request(payload, origin="http://untrusted.invalid")[0], 403)
        self.assertEqual(self.catalog.read_bytes(), original)
        self.catalog.unlink()
        self.assertEqual(self.request(payload), (409, {"error": "catalogue_missing"}))


if __name__ == "__main__":
    unittest.main()
