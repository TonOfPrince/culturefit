"""Native localhost API tests for real operator-authored catalogue tag discovery."""
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
from web_app import LocalCatalogueServer

DATA = (
    b"lane,name,city,tags,source_url,source_label\n"
    b"place,North Hall,Minneapolis,jazz;coffee,https://sources.example/north,Operator north notes\n"
    b"place,Chicago Room,Chicago,jazz;art,https://sources.example/chicago,Operator south notes\n"
    b"brand,Shared Brand,,coffee;art,https://sources.example/brand,Operator brand notes\n"
    b"artist,Shared Artist,,jazz;art,https://sources.example/artist,Operator artist notes\n"
)


class FacetsHttpTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.catalogue = Path(folder.name) / "operator.csv"
        self.server = LocalCatalogueServer(self.catalogue, 0)
        self.port = self.server.server_port
        self.worker = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.worker.start()
        self.addCleanup(self.close)

    def close(self):
        self.server.shutdown()
        self.worker.join(timeout=3)
        self.server.server_close()

    def request(self, path, data, extra=None):
        headers = {"Host": f"127.0.0.1:{self.port}",
                   "Origin": f"http://127.0.0.1:{self.port}",
                   "Content-Type": "application/json"}
        if extra:
            headers.update(extra)
        client = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        try:
            client.request("POST", path, body=data, headers=headers)
            response = client.getresponse()
            return response.status, json.loads(response.read()), dict(response.getheaders())
        finally:
            client.close()

    def upload(self):
        return self.request("/api/catalog", DATA, {"Content-Type": "text/csv"})

    def facets(self, city="Minneapolis", extra=None):
        raw = city if isinstance(city, bytes) else json.dumps({"city": city}).encode("utf-8")
        return self.request("/api/facets", raw, extra)

    def test_missing_catalogue_and_real_imported_tag_counts(self):
        self.assertEqual(self.facets()[0:2], (409, {"error": "catalogue_missing"}))
        self.assertEqual(self.upload()[0], 201)
        code, result, headers = self.facets()
        self.assertEqual(code, 200)
        self.assertEqual(result["format"], "culturefit_operator_facets_v1")
        self.assertEqual(result["source_status"], "user_supplied_unverified")
        self.assertEqual(result["provider_calls"], 0)
        self.assertIs(result["provider_independently_verified"], False)
        self.assertEqual(result["eligible_record_count"], 3)
        self.assertEqual(result["lanes"]["place"]["record_count"], 1)
        self.assertEqual(result["lanes"]["place"]["tags"], [
            {"tag": "coffee", "record_count": 1},
            {"tag": "jazz", "record_count": 1},
        ])
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(self.catalogue.read_bytes(), DATA)

    def test_city_changes_only_place_lane(self):
        self.assertEqual(self.upload()[0], 201)
        a = self.facets("Minneapolis")[1]
        b = self.facets("CHICAGO")[1]
        self.assertEqual(b["city"], "CHICAGO")
        self.assertEqual(b["lanes"]["place"]["tags"], [
            {"tag": "art", "record_count": 1},
            {"tag": "jazz", "record_count": 1},
        ])
        self.assertEqual(b["lanes"]["place"]["record_count"], 1)
        for lane in ("brand", "artist"):
            self.assertEqual(a["lanes"][lane], b["lanes"][lane])

    def test_absent_place_city_does_not_fabricate_local_records(self):
        self.assertEqual(self.upload()[0], 201)
        code, report, _ = self.facets("No Such City")
        self.assertEqual(code, 200)
        self.assertEqual(report["lanes"]["place"]["record_count"], 0)
        self.assertEqual(report["lanes"]["place"]["tags"], [])
        self.assertEqual(report["lanes"]["brand"]["record_count"], 1)
        self.assertEqual(report["lanes"]["artist"]["record_count"], 1)
        self.assertEqual(report["eligible_record_count"], 2)

    def test_invalid_and_ambiguous_json_rejected_before_catalogue_read(self):
        self.assertEqual(self.upload()[0], 201)
        original = self.catalogue.read_bytes()
        invalid = [
            b'{}', b'[]', b'{"city":1}', b'{"city":null}',
            b'{"city":"Minneapolis","live":true}',
            b'{"city":"Minneapolis","city":"Chicago"}',
            b'{"city":NaN}', b'{"city":"\\ud800"}',
            b'{"city":"   "}', b'{"city":""}',
            json.dumps({"city": "M" * 301}).encode("utf-8"),
            b'{"city":"Minneapolis","meta":{"a":1,"a":2}}',
            b'bad JSON',
        ]
        for body in invalid:
            with self.subTest(raw=body):
                code, report, _ = self.facets(body)
                self.assertEqual((code, report), (422, {"error": "invalid_facets"}))
        self.assertEqual(self.catalogue.read_bytes(), original)
        self.assertEqual(self.facets()[0], 200)

    def test_existing_host_origin_and_content_type_controls(self):
        self.assertEqual(self.upload()[0], 201)
        original = self.catalogue.read_bytes()
        self.assertEqual(self.facets(extra={"Host": "untrusted.example"})[0:2],
                         (403, {"error": "invalid_host"}))
        self.assertEqual(self.facets(extra={"Origin": "https://untrusted.example"})[0:2],
                         (403, {"error": "invalid_origin"}))
        self.assertEqual(self.facets(extra={"Content-Type": "text/plain"})[0:2],
                         (415, {"error": "unsupported_media_type"}))
        self.assertEqual(self.catalogue.read_bytes(), original)

    def test_corrupt_csv_is_unavailable_not_a_fake_empty_report(self):
        self.catalogue.write_bytes(b"wrong,headers\na,b\n")
        original = self.catalogue.read_bytes()
        self.assertEqual(self.facets()[0:2], (503, {"error": "catalogue_unavailable"}))
        self.assertEqual(self.catalogue.read_bytes(), original)

    def test_tag_result_contains_counts_not_private_source_urls(self):
        self.assertEqual(self.upload()[0], 201)
        _, report, _ = self.facets()
        raw = json.dumps(report)
        self.assertNotIn("sources.example", raw)
        self.assertNotIn("source_url", raw)
        self.assertNotIn("source_label", raw)
        self.assertEqual(report["basis"], "frequencies_of_user_authored_tags_not_provider_rankings")

    def test_operator_can_discover_real_tag_then_search_for_it(self):
        self.assertEqual(self.upload()[0], 201)
        code, available, _ = self.facets("  MINNEAPOLIS  ")
        self.assertEqual(code, 200)
        self.assertEqual(available["city"], "MINNEAPOLIS")
        tag = available["lanes"]["place"]["tags"][0]["tag"]
        request = json.dumps({"city": "Minneapolis", "interests": tag}).encode("utf-8")
        status, matches, _ = self.request("/api/shortlist", request)
        self.assertEqual(status, 200)
        self.assertEqual(matches["lanes"]["place"][0]["name"], "North Hall")
        self.assertEqual(matches["lanes"]["place"][0]["source_url"],
                         "https://sources.example/north")
        self.assertEqual(matches["provider_calls"], 0)
        self.assertEqual(self.catalogue.read_bytes(), DATA)


if __name__ == "__main__":
    unittest.main()
