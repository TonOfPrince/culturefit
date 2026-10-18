"""Native standalone localhost regression for the real operator-source impact preview."""
from __future__ import annotations

import hashlib
import http.client
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from catalog_impact import record_impact
from curated_catalog import COLUMNS, read_catalog
from web_app import LocalCatalogueServer

ROOT = Path(__file__).resolve().parents[1]
DATA = (
    b"lane,name,city,tags,source_url,source_label\n"
    b"place,Hall,Minneapolis,jazz,https://example.org/hall,Operator A\n"
    b"brand,Beans,,coffee,https://example.org/beans,Operator B\n"
    b"artist,Performer,,jazz,https://example.org/artist,Operator C\n"
)
ROW = {"lane": "place", "name": "A Club", "city": "Minneapolis",
       "tags": "jazz;coffee", "source_url": "https://example.org/new",
       "source_label": "Operator-authored venue source"}
QUERY = {"row": ROW, "city": "Minneapolis", "interests": "jazz,coffee"}


class NativeImpactApiTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.catalog = Path(temporary.name) / "operator.csv"
        self.catalog.write_bytes(DATA)
        self.server = LocalCatalogueServer(self.catalog, 0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close)

    def close(self):
        self.server.shutdown()
        self.thread.join(timeout=3)
        self.server.server_close()

    def post(self, value, *, host=None, origin=None, kind="application/json"):
        data = value if isinstance(value, bytes) else json.dumps(value).encode("utf-8")
        port = self.server.server_port
        client = http.client.HTTPConnection("127.0.0.1", port, timeout=4)
        client.request("POST", "/api/impact", body=data, headers={
            "Host": host if host is not None else f"127.0.0.1:{port}",
            "Origin": origin if origin is not None else f"http://127.0.0.1:{port}",
            "Content-Type": kind,
        })
        response = client.getresponse()
        result = response.status, json.loads(response.read())
        client.close()
        return result

    def test_actual_http_preview_matches_real_impact_engine(self):
        status, report = self.post(QUERY)
        self.assertEqual(status, 200)
        self.assertEqual(report, record_impact(self.catalog, **QUERY))
        self.assertEqual(report["changed_lanes"]["place"]["after"][0]["name"], "A Club")
        self.assertEqual(report["proposed_candidate"]["source_label"], ROW["source_label"])
        self.assertEqual(report["original_catalogue_sha256"], hashlib.sha256(DATA).hexdigest())
        self.assertEqual(report["provider_calls"], 0)
        self.assertFalse(report["provider_independently_verified"])
        self.assertEqual(report["source_status"], "user_supplied_unverified")
        self.assertEqual(self.catalog.read_bytes(), DATA)

    def test_nonmatching_city_and_hard_filters_never_fabricate_results(self):
        code, absent = self.post({**QUERY, "city": "Chicago"})
        self.assertEqual(code, 200)
        self.assertIsNone(absent["candidate_rank_within_top"])
        self.assertTrue(absent["no_change_to_top_ranked_results"])
        code, filtered = self.post({**QUERY, "excluded_tags": "coffee"})
        self.assertEqual(code, 200)
        self.assertIsNone(filtered["candidate_rank_within_top"])
        self.assertTrue(filtered["no_change_to_top_ranked_results"])
        self.assertEqual(self.catalog.read_bytes(), DATA)

    def test_duplicate_source_candidate_is_not_admitted(self):
        collision = {**ROW, "name": "HALL", "tags": "jazz;coffee"}
        self.assertEqual(self.post({**QUERY, "row": collision}),
                         (422, {"error": "invalid_impact"}))
        self.assertEqual(self.catalog.read_bytes(), DATA)

    def test_invalid_types_unknown_fields_and_nonfinite_json_rejected(self):
        invalid = (
            {}, {"row": ROW}, {**QUERY, "provider_mode": "live"},
            {**QUERY, "row": []}, {**QUERY, "row": {**ROW, "extra": "forbidden"}},
            {**QUERY, "row": {k: v for k, v in ROW.items() if k != "source_url"}},
            {**QUERY, "row": {**ROW, "name": None}},
            {**QUERY, "city": None}, {**QUERY, "interests": 9},
            {**QUERY, "per_lane": True}, {**QUERY, "per_lane": 21},
            {**QUERY, "required_tags": []}, {**QUERY, "row": {**ROW, "name": "\ud800"}},
            b'{"row":{},"city":"Minneapolis","interests":NaN}',
            b'{"row":{},"city":"Minneapolis","interests":',
        )
        for case in invalid:
            with self.subTest(value=repr(case)[:100]):
                self.assertEqual(self.post(case), (422, {"error": "invalid_impact"}))
        self.assertEqual(self.catalog.read_bytes(), DATA)
        self.assertEqual(self.post(QUERY)[0], 200)

    def test_nested_and_top_level_duplicate_json_fields_rejected(self):
        for raw in (
            b'{"row":{},"row":{},"city":"Minneapolis","interests":"jazz"}',
            b'{"row":{"lane":"place","lane":"artist"},"city":"Minneapolis","interests":"jazz"}',
            b'{"row":{},"city":"Minneapolis","city":"Chicago","interests":"jazz"}',
        ):
            with self.subTest(raw=raw):
                self.assertEqual(self.post(raw), (422, {"error": "invalid_impact"}))
        self.assertEqual(self.catalog.read_bytes(), DATA)

    def test_missing_invalid_catalogue_and_request_bounds(self):
        self.assertEqual(self.post(b"x" * 4100), (413, {"error": "body_too_large"}))
        self.catalog.unlink()
        self.assertEqual(self.post(QUERY), (409, {"error": "catalogue_missing"}))
        self.catalog.write_bytes(b"wrong,headers\nvalue,here\n")
        self.assertEqual(self.post(QUERY), (503, {"error": "catalogue_unavailable"}))

    def test_origin_host_and_media_type_fences(self):
        self.assertEqual(self.post(QUERY, host="evil.example")[0], 403)
        self.assertEqual(self.post(QUERY, origin="https://evil.example")[0], 403)
        self.assertEqual(self.post(QUERY, kind="text/plain")[0], 415)
        self.assertEqual(self.catalog.read_bytes(), DATA)

    def test_native_root_only_cli_and_http_parity(self):
        status, actual = self.post(QUERY)
        self.assertEqual(status, 200)
        args = [sys.executable, "-S", str(ROOT / "catalog_impact.py"),
                "--catalog", str(self.catalog), "--lane", ROW["lane"],
                "--name", ROW["name"], "--candidate-city", ROW["city"],
                "--tags", ROW["tags"], "--source-url", ROW["source_url"],
                "--source-label", ROW["source_label"], "--city", QUERY["city"],
                "--interests", QUERY["interests"]]
        run = subprocess.run(args, cwd=ROOT, env={**os.environ, "PYTHONPATH": ""},
                             capture_output=True, text=True, timeout=6, check=False)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(json.loads(run.stdout), actual)
        self.assertEqual(len(read_catalog(self.catalog)), 3)
        self.assertEqual(self.catalog.read_bytes(), DATA)


if __name__ == "__main__":
    unittest.main()
