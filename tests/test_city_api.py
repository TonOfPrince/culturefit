"""Native localhost integration tests for source-only operator city comparison."""
from __future__ import annotations

import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path

from curated_catalog import read_catalog
from city_compare import compare_cities
from web_app import LocalCatalogueServer

DATA = (
    b"lane,name,city,tags,source_url,source_label\n"
    b"place,First Jazz,Minneapolis,jazz;coffee,https://example.org/jazz,Operator A\n"
    b"place,First Art,Minneapolis,art,https://example.org/art,Operator B\n"
    b"place,Second Jazz,St Paul,jazz,https://example.org/second,Operator C\n"
    b"brand,Shared Brand,,coffee,https://example.org/brand,Operator D\n"
    b"artist,Shared Artist,,jazz,https://example.org/artist,Operator E\n"
)
REQUEST = {"first_city": "Minneapolis", "second_city": "St Paul",
           "interests": "jazz,coffee"}


class NativeCityApiTests(unittest.TestCase):
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

    def post(self, payload, *, host=None, origin=None, content_type="application/json"):
        raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
        port = self.server.server_port
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
        headers = {"Content-Type": content_type,
                   "Host": host if host is not None else f"127.0.0.1:{port}",
                   "Origin": origin if origin is not None else f"http://127.0.0.1:{port}"}
        conn.request("POST", "/api/cities", body=raw, headers=headers)
        response = conn.getresponse()
        code, result = response.status, json.loads(response.read())
        conn.close()
        return code, result

    def test_native_api_equals_exact_source_only_comparison_engine(self):
        code, result = self.post(REQUEST)
        self.assertEqual(code, 200)
        self.assertEqual(result, compare_cities(read_catalog(self.catalog), **REQUEST))
        self.assertEqual([entry["city"] for entry in result["cities"]],
                         ["Minneapolis", "St Paul"])
        self.assertEqual(result["cities"][0]["places_matching_any_interest"], 1)
        self.assertEqual(result["cities"][1]["missing_place_interests"], ["coffee"])
        self.assertEqual(result["source_status"], "user_supplied_unverified")
        self.assertIs(result["provider_independently_verified"], False)
        self.assertEqual(result["provider_calls"], 0)
        self.assertEqual(self.catalog.read_bytes(), DATA)

    def test_global_brand_and_artist_evidence_is_shared_not_city_local(self):
        code, result = self.post({**REQUEST, "per_city": 1})
        self.assertEqual(code, 200)
        self.assertEqual(len(result["cities"][0]["top_places"]), 1)
        self.assertEqual(result["shared_lanes"]["brand"][0]["source_label"], "Operator D")
        self.assertEqual(result["shared_lanes"]["artist"][0]["source_label"], "Operator E")
        self.assertNotIn("brand", result["cities"][0])
        self.assertNotIn("artist", result["cities"][1])
        self.assertEqual(self.catalog.read_bytes(), DATA)

    def test_invalid_fields_duplicate_keys_and_nonfinite_json_fail_closed(self):
        examples = (
            {}, {"first_city": "Minneapolis", "interests": "jazz"},
            {**REQUEST, "provider_mode": "live"}, {**REQUEST, "per_city": True},
            {**REQUEST, "per_city": 21}, {**REQUEST, "interests": "\ud800"},
            {**REQUEST, "second_city": "MINNEAPOLIS"},
            b'{"first_city":"Minneapolis","first_city":"Chicago","second_city":"St Paul","interests":"jazz"}',
            b'{"first_city":"Minneapolis","second_city":"St Paul","interests":NaN}',
            b'{"first_city":"Minneapolis","second_city":"St Paul","interests":"jazz","nested":{"x":1,"x":2}}',
        )
        for candidate in examples:
            with self.subTest(candidate=repr(candidate)):
                self.assertEqual(self.post(candidate),
                                 (422, {"error": "invalid_city_comparison"}))
        self.assertEqual(self.catalog.read_bytes(), DATA)
        self.assertEqual(self.post(REQUEST)[0], 200)

    def test_invalid_host_origin_and_media_type(self):
        self.assertEqual(self.post(REQUEST, host="other.invalid")[0], 403)
        self.assertEqual(self.post(REQUEST, origin="http://other.invalid")[0], 403)
        self.assertEqual(self.post(REQUEST, content_type="text/plain")[0], 415)
        self.assertEqual(self.catalog.read_bytes(), DATA)

    def test_missing_and_invalid_catalogue_have_explicit_statuses(self):
        self.catalog.unlink()
        self.assertEqual(self.post(REQUEST), (409, {"error": "catalogue_missing"}))
        self.catalog.write_bytes(b"wrong,headers\none,two\n")
        self.assertEqual(self.post(REQUEST), (503, {"error": "catalogue_unavailable"}))

    def test_repeated_read_only_requests_do_not_alter_csv(self):
        before = self.catalog.read_bytes()
        self.assertEqual(self.post(REQUEST)[0], 200)
        self.assertEqual(self.post({**REQUEST, "per_city": 1})[0], 200)
        self.assertEqual(self.post({**REQUEST, "per_city": 0})[0], 422)
        self.assertEqual(self.catalog.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
