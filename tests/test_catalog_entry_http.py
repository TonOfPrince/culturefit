"""Real loopback HTTP acceptance tests for the standalone catalogue entry API.

Requires the actual web_app.py and its app-local imports from this app root.
No browser JavaScript, third-party provider, or external URL is exercised.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import http.client
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest

from curated_catalog import read_catalog
from web_app import LocalCatalogueServer

ROOT = Path(__file__).resolve().parents[1]
CSV = (b"lane,name,city,tags,source_url,source_label\n"
       b"place,Hall,Minneapolis,jazz,https://source.example/hall,Operator source\n")


class CatalogueEntryHttpTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / "catalog.csv"
        self.server = LocalCatalogueServer(self.path, 0)
        self.port = self.server.server_port
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close)

    def close(self):
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()

    def request(self, method, path, data=None, extra=None):
        headers = {"Host": f"127.0.0.1:{self.port}",
                   "Origin": f"http://127.0.0.1:{self.port}"}
        if extra:
            headers.update(extra)
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            connection.request(method, path, body=data, headers=headers)
            response = connection.getresponse()
            body = json.loads(response.read())
            return response.status, body, dict(response.getheaders())
        finally:
            connection.close()

    def status(self):
        return self.request("GET", "/api/catalog")

    def sample_entry(self, **updates):
        payload = {
            "expected_catalog_sha256": hashlib.sha256(CSV).hexdigest(),
            "confirm_add": True, "lane": "artist", "name": "Music Act",
            "city": "", "tags": "jazz;art",
            "source_url": "https://source.example/music",
            "source_label": "Operator cited musician",
        }
        return {**payload, **updates}

    def entry(self, payload=None, extra=None):
        data = payload if isinstance(payload, bytes) else json.dumps(
            self.sample_entry() if payload is None else payload).encode("utf-8")
        return self.request("POST", "/api/entries", data,
                            {"Content-Type": "application/json", **(extra or {})})

    def test_empty_status_and_missing_catalogue_conflict(self):
        code, body, _ = self.status()
        self.assertEqual(code, 200)
        self.assertEqual(body["state"], "empty")
        self.assertNotIn("catalogue_sha256", body)
        self.assertEqual(self.entry()[:2], (409, {"error": "catalogue_missing"}))
        self.assertFalse(self.path.exists())

    def test_import_then_versioned_append_and_search_real_records(self):
        self.assertEqual(self.request("POST", "/api/catalog", CSV,
                                      {"Content-Type": "text/csv"})[0], 201)
        code, version, headers = self.status()
        self.assertEqual(code, 200)
        self.assertEqual(version["catalogue_sha256"], hashlib.sha256(CSV).hexdigest())
        self.assertEqual(version["lanes"], {"place": 1, "brand": 0, "artist": 0})
        self.assertEqual(headers["Cache-Control"], "no-store")
        code, added, _ = self.entry()
        self.assertEqual(code, 201)
        self.assertEqual(added["record_count"], 2)
        self.assertEqual(added["provider_calls"], 0)
        self.assertIs(added["provider_independently_verified"], False)
        self.assertEqual(added["source_status"], "user_supplied_unverified")
        self.assertEqual(self.path.read_bytes()[:len(CSV)], CSV)
        self.assertEqual(read_catalog(self.path)[-1].name, "Music Act")
        code, new_version, _ = self.status()
        self.assertEqual(code, 200)
        self.assertEqual(new_version["catalogue_sha256"], added["new_csv_sha256"])
        self.assertEqual(new_version["lanes"], {"place": 1, "brand": 0, "artist": 1})
        shortlist = json.dumps({"city": "Minneapolis", "interests": "jazz"}).encode()
        code, search, _ = self.request("POST", "/api/shortlist", shortlist,
                                       {"Content-Type": "application/json"})
        self.assertEqual(code, 200)
        self.assertEqual(search["lanes"]["artist"][0]["name"], "Music Act")

    def test_stale_hash_and_duplicate_are_rejected_without_overwriting(self):
        self.path.write_bytes(CSV)
        self.assertEqual(self.entry()[0], 201)
        after = self.path.read_bytes()
        self.assertEqual(self.entry()[:2], (409, {"error": "catalogue_changed"}))
        current_hash = hashlib.sha256(after).hexdigest()
        dup = self.sample_entry(expected_catalog_sha256=current_hash)
        self.assertEqual(self.entry(dup)[:2], (422, {"error": "invalid_entry"}))
        self.assertEqual(self.path.read_bytes(), after)

    def test_invalid_json_duplicate_fields_types_and_provenance_rejected(self):
        self.path.write_bytes(CSV)
        invalid = [
            b'{"lane":"artist","lane":"brand"}',
            b'{"expected_catalog_sha256":"a","confirm_add":true,"lane":"artist",'
            b'"name":"Musician","city":"","tags":"jazz","source_url":"https://example.org",'
            b'"source_label":"Operator","extra":{"x":1,"x":2}}',
            b'{"foo":NaN}', b'{"foo":"\\ud800"}', b'not json',
            {**self.sample_entry(), "confirm_add": "yes"},
            {**self.sample_entry(), "source_url": "http://source.example/music"},
            {**self.sample_entry(), "provider_mode": "live"},
        ]
        for payload in invalid:
            with self.subTest(payload=repr(payload)):
                self.assertEqual(self.entry(payload)[:2], (422, {"error": "invalid_entry"}))
                self.assertEqual(self.path.read_bytes(), CSV)

    def test_origin_host_and_media_type_fences(self):
        self.path.write_bytes(CSV)
        self.assertEqual(self.entry(extra={"Host": "other.example"})[0], 403)
        self.assertEqual(self.entry(extra={"Origin": "https://other.example"})[0], 403)
        self.assertEqual(self.entry(extra={"Content-Type": "text/plain"})[0], 415)
        self.assertEqual(self.path.read_bytes(), CSV)

    def test_invalid_existing_catalogue_reports_unavailable(self):
        self.path.write_bytes(b"bad,header\nfoo,bar\n")
        self.assertEqual(self.status()[:2], (503, {"error": "catalogue_unavailable"}))
        self.assertEqual(self.entry()[:2], (503, {"error": "catalogue_unavailable"}))

    def test_two_concurrent_requests_with_same_version_admit_one(self):
        self.path.write_bytes(CSV)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.entry()[0], range(2)))
        self.assertEqual(sorted(results), [201, 409])
        self.assertEqual(len(read_catalog(self.path)), 2)
        self.assertEqual(sum(1 for c in read_catalog(self.path) if c.name == "Music Act"), 1)

    def test_root_only_separate_process_actual_server(self):
        proc = subprocess.Popen(
            [sys.executable, "-S", str(ROOT / "web_app.py"),
             "--catalog", str(self.path), "--port", "0"],
            cwd=ROOT, env={"PYTHONPATH": "", "PATH": os.environ.get("PATH", "")},
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            address = proc.stdout.readline().strip()
            self.assertTrue(address.startswith("Local CultureFit catalogue: http://127.0.0.1:"), address)
            port = int(address.rsplit(":", 1)[-1])
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
            try:
                connection.request("GET", "/api/catalog", headers={"Host": f"127.0.0.1:{port}"})
                result = connection.getresponse()
                self.assertEqual(result.status, 200)
                self.assertEqual(json.loads(result.read())["state"], "empty")
            finally:
                connection.close()
        finally:
            proc.terminate()
            proc.communicate(timeout=5)


if __name__ == "__main__":
    unittest.main()
