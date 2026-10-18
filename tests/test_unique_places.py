"""Native app-root tests for distinct-place bundles over real local catalogue data."""
from __future__ import annotations

import http.client
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest

from curated_catalog import CatalogError, read_catalog
from fit_bundle import compose_bundles
from web_app import LocalCatalogueServer

CATALOGUE = (
    b"lane,name,city,tags,source_url,source_label\n"
    b"place,A Club,Minneapolis,jazz;coffee,https://sources.example/a,Venue A notes\n"
    b"place,B Club,MINNEAPOLIS,jazz;coffee,https://sources.example/b,Venue B notes\n"
    b"place,C Club,Minneapolis,jazz;coffee,https://sources.example/c,Venue C notes\n"
    b"place,D Club,Chicago,jazz;coffee,https://sources.example/d,Venue D notes\n"
    b"brand,A Brand,,jazz;coffee,https://sources.example/brand-a,Brand A notes\n"
    b"brand,B Brand,,jazz;coffee,https://sources.example/brand-b,Brand B notes\n"
    b"artist,A Artist,,jazz;coffee,https://sources.example/artist-a,Artist A notes\n"
    b"artist,B Artist,,jazz;coffee,https://sources.example/artist-b,Artist B notes\n"
)
QUERY = {"city": "Minneapolis", "interests": "jazz,coffee"}


class DistinctPlaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.catalog = Path(self.temp.name) / "operator.csv"
        self.catalog.write_bytes(CATALOGUE)
        self.original = self.catalog.read_bytes()
        self.server = LocalCatalogueServer(self.catalog, 0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close)

    def close(self):
        self.server.shutdown()
        self.thread.join(timeout=3)
        self.server.server_close()

    def result(self, **overrides):
        return compose_bundles(read_catalog(self.catalog), **{**QUERY, **overrides})

    def post(self, value, *, origin=None):
        raw = value if isinstance(value, bytes) else json.dumps(value).encode("utf-8")
        port = self.server.server_port
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
        try:
            connection.request(
                "POST", "/api/bundles", raw,
                headers={"Content-Type": "application/json",
                         "Host": f"127.0.0.1:{port}",
                         "Origin": origin or f"http://127.0.0.1:{port}"})
            response = connection.getresponse()
            return response.status, json.loads(response.read()), dict(response.getheaders())
        finally:
            connection.close()

    @staticmethod
    def place_names(result):
        return [bundle["members"]["place"]["name"] for bundle in result["bundles"]]

    def test_old_default_scoring_and_output_remains_available(self):
        result = self.result(max_bundles=3)
        self.assertIs(result["unique_places"], False)
        self.assertEqual(self.place_names(result), ["A Club", "A Club", "A Club"])
        self.assertEqual(result["provider_calls"], 0)

    def test_distinct_places_returns_real_different_venues(self):
        result = self.result(unique_places=True, max_bundles=3)
        self.assertIs(result["unique_places"], True)
        self.assertEqual(self.place_names(result), ["A Club", "B Club", "C Club"])
        self.assertEqual([item["covered_interest_count"] for item in result["bundles"]],
                         [2, 2, 2])
        self.assertEqual(result["bundles"][1]["members"]["place"]["source_label"],
                         "Venue B notes")
        self.assertEqual(result["source_status"], "user_supplied_unverified")
        self.assertFalse(result["provider_independently_verified"])
        self.assertEqual(result["provider_calls"], 0)
        self.assertEqual(self.catalog.read_bytes(), self.original)

    def test_distinct_pool_and_response_limits_are_honored(self):
        two = self.result(unique_places=True, per_lane=2, max_bundles=10)
        self.assertEqual(self.place_names(two), ["A Club", "B Club"])
        self.assertEqual(two["pool_limit_per_lane"], 2)
        self.assertEqual(two["candidate_pool_counts"]["place"], 2)
        one = self.result(unique_places=True, per_lane=1, max_bundles=10)
        self.assertEqual(self.place_names(one), ["A Club"])
        self.assertEqual(self.catalog.read_bytes(), self.original)

    def test_only_requested_city_places_are_returned(self):
        result = self.result(unique_places=True, city="MINNEAPOLIS", max_bundles=10)
        self.assertEqual(self.place_names(result), ["A Club", "B Club", "C Club"])
        self.assertNotIn("D Club", json.dumps(result))
        chicago = self.result(unique_places=True, city="Chicago", max_bundles=10)
        self.assertEqual(self.place_names(chicago), ["D Club"])

    def test_missing_lane_produces_no_invented_bundle(self):
        records = tuple(row for row in read_catalog(self.catalog) if row.lane != "artist")
        result = compose_bundles(records, **QUERY, unique_places=True)
        self.assertEqual(result["bundles"], [])
        self.assertEqual(result["missing_lanes"], ["artist"])
        self.assertTrue(result["unique_places"])

    def test_determinism_under_input_reordering(self):
        records = read_catalog(self.catalog)
        a = compose_bundles(records, **QUERY, unique_places=True, max_bundles=10)
        b = compose_bundles(tuple(reversed(records)), **QUERY,
                            unique_places=True, max_bundles=10)
        self.assertEqual(a, b)

    def test_invalid_option_types_fail_closed(self):
        for value in ("true", 1, 0, None, [], {}):
            with self.subTest(value=value), self.assertRaises(CatalogError):
                self.result(unique_places=value)
        self.assertEqual(self.catalog.read_bytes(), self.original)

    def test_real_http_and_cli_engine_have_identical_results(self):
        self.assertEqual(self.post({**QUERY, "unique_places": True})[:2],
                         (200, self.result(unique_places=True)))
        self.assertEqual(self.post({**QUERY, "unique_places": False})[:2],
                         (200, self.result(unique_places=False)))
        self.assertEqual(self.catalog.read_bytes(), self.original)

    def test_http_rejects_ambiguous_or_mistyped_option(self):
        for bad in [
            {**QUERY, "unique_places": "true"}, {**QUERY, "unique_places": 1},
            {**QUERY, "unique_places": None}, {**QUERY, "unique_places": []},
            b'{"city":"Minneapolis","interests":"jazz","unique_places":true,"unique_places":false}',
            {**QUERY, "unique_places": True, "provider_mode": "live"},
        ]:
            with self.subTest(bad=bad):
                self.assertEqual(self.post(bad)[:2],
                                 (422, {"error": "invalid_bundles"}))
        self.assertEqual(self.catalog.read_bytes(), self.original)
        self.assertEqual(self.post({**QUERY, "unique_places": True})[0], 200)

    def test_origin_restrictions_are_preserved(self):
        code, payload, _ = self.post({**QUERY, "unique_places": True},
                                      origin="https://untrusted.example")
        self.assertEqual((code, payload), (403, {"error": "invalid_origin"}))
        self.assertEqual(self.catalog.read_bytes(), self.original)

    def test_native_cli_launch_with_new_flag(self):
        root = Path(__file__).resolve().parents[1]
        env = os.environ.copy()
        env["PYTHONPATH"] = ""
        command = [sys.executable, "-S", str(root / "fit_bundle.py"),
                   "--catalog", str(self.catalog), "--city", "Minneapolis",
                   "--interests", "jazz,coffee", "--max-bundles", "3",
                   "--unique-places"]
        result = subprocess.run(command, cwd=str(root), env=env,
                                capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        parsed = json.loads(result.stdout)
        self.assertEqual(parsed, self.result(unique_places=True))
        self.assertEqual(self.catalog.read_bytes(), self.original)

    def test_native_cli_default_is_backward_compatible(self):
        root = Path(__file__).resolve().parents[1]
        env = os.environ.copy()
        env["PYTHONPATH"] = ""
        result = subprocess.run(
            [sys.executable, "-S", str(root / "fit_bundle.py"), "--catalog", str(self.catalog),
             "--city", "Minneapolis", "--interests", "jazz,coffee"],
            cwd=str(root), env=env, capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), self.result())
        self.assertEqual(self.catalog.read_bytes(), self.original)


if __name__ == "__main__":
    unittest.main()
