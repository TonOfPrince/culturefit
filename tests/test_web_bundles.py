"""Native localhost HTTP integration tests for operator-sourced three-lane bundles."""
from __future__ import annotations

import http.client
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from curated_catalog import read_catalog
from fit_bundle import compose_bundles
from web_app import LocalCatalogueServer

CATALOGUE = (
    b"lane,name,city,tags,source_url,source_label\n"
    b"place,North Venue,Minneapolis,jazz;coffee,https://sources.example/venue,Operator venue note\n"
    b"place,South Venue,Chicago,art,https://sources.example/other,Operator venue note\n"
    b"brand,Brand A,,coffee;art,https://sources.example/brand,Operator brand note\n"
    b"brand,Brand B,,jazz,https://sources.example/brand2,Operator brand note\n"
    b"artist,Artist A,,art,https://sources.example/artist,Operator artist note\n"
    b"artist,Artist B,,jazz;coffee,https://sources.example/artist2,Operator artist note\n"
)
QUERY = {"city": "Minneapolis", "interests": "jazz,coffee,art"}


class BundleHttpTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.catalog = Path(self.folder.name) / "catalog.csv"
        self.server = LocalCatalogueServer(self.catalog, 0)
        self.port = self.server.server_port
        self.worker = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.worker.start()
        self.addCleanup(self.close)

    def close(self):
        self.server.shutdown()
        self.worker.join(timeout=3)
        self.server.server_close()

    def send(self, method, path, body=b"", overrides=None):
        headers = {"Host": f"127.0.0.1:{self.port}",
                   "Origin": f"http://127.0.0.1:{self.port}",
                   "Content-Type": "application/json"}
        if overrides:
            headers.update(overrides)
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        try:
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            raw = response.read()
            return response.status, json.loads(raw), dict(response.getheaders())
        finally:
            connection.close()

    def import_csv(self, data=CATALOGUE):
        return self.send("POST", "/api/catalog", data,
                         {"Content-Type": "text/csv"})

    def bundles(self, payload=QUERY, overrides=None):
        raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
        return self.send("POST", "/api/bundles", raw, overrides)

    def test_real_operator_catalogue_response_and_provenance(self):
        self.assertEqual(self.import_csv()[0], 201)
        code, result, headers = self.bundles()
        self.assertEqual(code, 200)
        self.assertEqual(result["basis"], "exact_user_authored_tag_coverage_not_provider_fit")
        self.assertEqual(result["source_status"], "user_supplied_unverified")
        self.assertFalse(result["provider_independently_verified"])
        self.assertEqual(result["provider_calls"], 0)
        self.assertEqual(list(result["bundles"][0]["members"]),
                         ["place", "brand", "artist"])
        self.assertEqual(result["bundles"][0]["members"]["brand"]["source_url"],
                         "https://sources.example/brand")
        self.assertEqual(result["bundles"][0]["covered_interests"],
                         ["jazz", "coffee", "art"])
        self.assertEqual(headers["Cache-Control"], "no-store")

    def test_http_matches_real_cli_composer_without_mutating_csv(self):
        self.assertEqual(self.import_csv()[0], 201)
        original = self.catalog.read_bytes()
        query = {**QUERY, "city": "MINNEAPOLIS", "per_lane": 2,
                 "max_bundles": 4, "required_tags": "", "excluded_tags": ""}
        code, result, _ = self.bundles(query)
        self.assertEqual(code, 200)
        expected = compose_bundles(read_catalog(self.catalog), **query)
        self.assertEqual(result, expected)
        self.assertEqual(self.catalog.read_bytes(), original)

    def test_missing_catalogue_is_conflict(self):
        self.assertEqual(self.bundles()[:2],
                         (409, {"error": "catalogue_missing"}))
        self.assertFalse(self.catalog.exists())

    def test_missing_lane_never_fabricates_bundle(self):
        without_artist = b"\n".join(
            line for line in CATALOGUE.splitlines()
            if not line.startswith(b"artist,")
        ) + b"\n"
        self.assertEqual(self.import_csv(without_artist)[0], 201)
        code, result, _ = self.bundles()
        self.assertEqual(code, 200)
        self.assertEqual(result["bundles"], [])
        self.assertEqual(result["missing_lanes"], ["artist"])

    def test_city_fence_applies_to_places_only(self):
        self.assertEqual(self.import_csv()[0], 201)
        code, result, _ = self.bundles({**QUERY, "city": "chicago"})
        self.assertEqual(code, 200)
        self.assertEqual(result["candidate_pool_counts"]["place"], 1)
        self.assertEqual(result["candidate_pool_counts"]["brand"], 2)
        self.assertEqual(result["candidate_pool_counts"]["artist"], 2)
        self.assertEqual(result["bundles"][0]["members"]["place"]["name"], "South Venue")

    def test_optional_tag_filters_and_limits(self):
        self.assertEqual(self.import_csv()[0], 201)
        code, result, _ = self.bundles({**QUERY, "required_tags": "coffee",
                                         "per_lane": 1, "max_bundles": 1})
        self.assertEqual(code, 200)
        self.assertEqual(len(result["bundles"]), 1)
        for member in result["bundles"][0]["members"].values():
            self.assertIn("coffee", member["matched_interests"])
        code, result, _ = self.bundles({**QUERY, "excluded_tags": "jazz"})
        self.assertEqual(code, 200)
        self.assertEqual(result["bundles"], [])

    def test_missing_or_extra_or_invalid_fields_fail_without_mutation(self):
        self.assertEqual(self.import_csv()[0], 201)
        original = self.catalog.read_bytes()
        bad = [
            {}, {"city": "Minneapolis"}, {**QUERY, "provider_mode": "live"},
            {**QUERY, "per_lane": True}, {**QUERY, "max_bundles": False},
            {**QUERY, "max_bundles": 11}, {**QUERY, "per_lane": 0},
            {**QUERY, "required_tags": ["jazz"]}, {**QUERY, "interests": ""},
            {**QUERY, "required_tags": "jazz", "excluded_tags": "JAZZ"},
            b'{"city":"Minneapolis","interests":"jazz","interests":"art"}',
            b'{"city":"Minneapolis","interests":NaN}',
            b'{"city":"\\ud800","interests":"art"}',
            b'not json',
        ]
        for query in bad:
            with self.subTest(query=query):
                code, result, _ = self.bundles(query)
                self.assertEqual((code, result), (422, {"error": "invalid_bundles"}))
        self.assertEqual(self.catalog.read_bytes(), original)
        self.assertEqual(self.bundles()[0], 200)

    def test_origin_and_host_fence_reject_external_clients(self):
        self.assertEqual(self.import_csv()[0], 201)
        original = self.catalog.read_bytes()
        self.assertEqual(self.bundles(overrides={"Origin": "http://example.test"})[:2],
                         (403, {"error": "invalid_origin"}))
        self.assertEqual(self.bundles(overrides={"Host": "example.test"})[:2],
                         (403, {"error": "invalid_host"}))
        self.assertEqual(self.catalog.read_bytes(), original)

    def test_correct_content_type_required(self):
        self.assertEqual(self.import_csv()[0], 201)
        code, body, _ = self.bundles(overrides={"Content-Type": "text/plain"})
        self.assertEqual((code, body), (415, {"error": "unsupported_media_type"}))


if __name__ == "__main__":
    unittest.main()
