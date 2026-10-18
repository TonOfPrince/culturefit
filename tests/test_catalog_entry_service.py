"""Native focused tests of actual app-local catalogue entry service and CLI."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from catalog_entry_service import append_entry, catalogue_status
from curated_catalog import read_catalog

ROOT = Path(__file__).resolve().parents[1]
CSV = (b"lane,name,city,tags,source_url,source_label\n"
       b"place,Hall,Minneapolis,jazz,https://source.example/hall,Owner note\n")


def digest(data):
    return hashlib.sha256(data).hexdigest()


class CatalogueEntryServiceTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "catalogue.csv"
        self.payload = {
            "expected_catalog_sha256": digest(CSV), "confirm_add": True,
            "lane": "artist", "name": "Example Musician", "city": "",
            "tags": "jazz;music", "source_url": "https://source.example/music",
            "source_label": "Operator artist source",
        }

    def write(self):
        self.path.write_bytes(CSV)

    def test_missing_catalogue_has_empty_status_without_credential_or_provider(self):
        self.assertEqual(catalogue_status(self.path),
                         (200, {"state": "empty", "record_count": 0,
                                "lanes": {"place": 0, "brand": 0, "artist": 0}}))
        self.assertFalse(self.path.exists())

    def test_real_catalogue_counts_and_hash_are_exact(self):
        self.write()
        code, result = catalogue_status(self.path)
        self.assertEqual(code, 200)
        self.assertEqual(result["record_count"], 1)
        self.assertEqual(result["lanes"], {"place": 1, "brand": 0, "artist": 0})
        self.assertEqual(result["catalogue_sha256"], digest(CSV))
        self.assertEqual(result["source_status"], "user_supplied_unverified")
        self.assertEqual(self.path.read_bytes(), CSV)

    def test_append_authentic_row_and_requery_catalogue(self):
        self.write()
        status, body = append_entry(self.path, self.payload)
        self.assertEqual(status, 201)
        self.assertEqual(body["previous_csv_sha256"], digest(CSV))
        self.assertEqual(body["provider_calls"], 0)
        self.assertFalse(body["provider_independently_verified"])
        self.assertTrue(self.path.read_bytes().startswith(CSV))
        records = read_catalog(self.path)
        self.assertEqual(len(records), 2)
        self.assertEqual(records[-1].name, "Example Musician")
        self.assertEqual(records[-1].source_label, "Operator artist source")
        _, new_state = catalogue_status(self.path)
        self.assertEqual(new_state["catalogue_sha256"], body["new_csv_sha256"])
        self.assertEqual(new_state["lanes"], {"place": 1, "brand": 0, "artist": 1})

    def test_stale_hash_prevents_double_append_and_preserves_bytes(self):
        self.write()
        self.assertEqual(append_entry(self.path, self.payload)[0], 201)
        after = self.path.read_bytes()
        code, result = append_entry(self.path, self.payload)
        self.assertEqual((code, result), (409, {"error": "catalogue_changed"}))
        self.assertEqual(self.path.read_bytes(), after)

    def test_duplicate_candidate_fails_without_mutation(self):
        self.write()
        entry = {**self.payload, "lane": "place", "name": "HALL", "city": "MINNEAPOLIS"}
        self.assertEqual(append_entry(self.path, entry), (422, {"error": "invalid_entry"}))
        self.assertEqual(self.path.read_bytes(), CSV)

    def test_invalid_entry_contract_never_changes_catalogue(self):
        self.write()
        invalid = [None, [], {**self.payload, "confirm_add": False},
                   {**self.payload, "confirm_add": "true"},
                   {**self.payload, "unused_provider": "live"},
                   {k: v for k, v in self.payload.items() if k != "source_url"},
                   {**self.payload, "name": 1},
                   {**self.payload, "expected_catalog_sha256": "bad"},
                   {**self.payload, "source_url": "http://source.example/music"},
                   {**self.payload, "tags": "\ud800"}]
        for candidate in invalid:
            with self.subTest(candidate=repr(candidate)):
                self.assertEqual(append_entry(self.path, candidate),
                                 (422, {"error": "invalid_entry"}))
                self.assertEqual(self.path.read_bytes(), CSV)

    def test_missing_catalogue_and_unreadable_data_are_distinct(self):
        self.assertEqual(append_entry(self.path, self.payload),
                         (409, {"error": "catalogue_missing"}))
        self.path.write_bytes(b"wrong,headers\na,b\n")
        self.assertEqual(catalogue_status(self.path),
                         (503, {"error": "catalogue_unavailable"}))
        self.assertEqual(append_entry(self.path, self.payload),
                         (503, {"error": "catalogue_unavailable"}))

    def test_symlink_never_mutated(self):
        self.write()
        alias = self.path.parent / "alias.csv"
        alias.symlink_to(self.path)
        self.assertEqual(catalogue_status(alias),
                         (503, {"error": "catalogue_unavailable"}))
        self.assertEqual(append_entry(alias, self.payload),
                         (409, {"error": "catalogue_missing"}))
        self.assertEqual(self.path.read_bytes(), CSV)

    def test_root_only_native_subprocess_can_import_and_execute(self):
        self.write()
        script = ("import json,sys; from catalog_entry_service import catalogue_status, append_entry; "
                  "from pathlib import Path; p=Path(sys.argv[1]); body=json.loads(sys.argv[2]); "
                  "a=catalogue_status(p); b=append_entry(p,body); c=catalogue_status(p); "
                  "print(json.dumps([a[0],a[1]['catalogue_sha256'],b[0],c[1]['record_count']]))")
        env = os.environ.copy()
        env["PYTHONPATH"] = ""
        process = subprocess.run([sys.executable, "-S", "-c", script, str(self.path),
                                  json.dumps(self.payload)], cwd=ROOT, env=env,
                                 capture_output=True, text=True, check=False)
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(json.loads(process.stdout), [200, digest(CSV), 201, 2])
        self.assertEqual(len(read_catalog(self.path)), 2)


if __name__ == "__main__":
    unittest.main()
