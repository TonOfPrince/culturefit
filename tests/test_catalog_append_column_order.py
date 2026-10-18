"""Exercise real single-record appends to already-supported CSV column orders."""
from __future__ import annotations

import csv
import hashlib
import io
import itertools
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from catalog_add import CatalogueAddError, append_candidate
from catalog_entry_service import append_entry
from curated_catalog import COLUMNS, Candidate, read_catalog, shortlist

ROOT = Path(__file__).resolve().parents[1]
SWAPPED = ("lane", "city", "name", "tags", "source_url", "source_label")
ORIGINAL = dict(zip(COLUMNS, ("place", "Original Hall", "Minneapolis", "jazz",
                             "https://source.example/original", "Operator original")))
PROPOSED = dict(zip(COLUMNS, ("place", "New Venue", "Minneapolis", "jazz;coffee",
                             "https://source.example/new", "Operator new source")))


class AppendColumnOrderTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "sources.csv"

    def create(self, header=COLUMNS, *, ending="\n", bom=False, final_newline=True):
        stream = io.StringIO(newline="")
        writer = csv.DictWriter(stream, fieldnames=header, lineterminator=ending)
        writer.writeheader()
        writer.writerow(ORIGINAL)
        raw = stream.getvalue().encode("utf-8")
        if not final_newline:
            raw = raw[:-len(ending.encode())]
        if bom:
            raw = b"\xef\xbb\xbf" + raw
        self.path.write_bytes(raw)
        self.original = raw
        return hashlib.sha256(raw).hexdigest()

    def append(self, row=None, **overrides):
        return append_candidate(
            self.path, expected_sha256=hashlib.sha256(self.original).hexdigest(),
            confirm_add=True, **{**(PROPOSED if row is None else row), **overrides})

    def assert_appended(self, row=PROPOSED):
        expected = Candidate(row["lane"], row["name"], row["city"],
                             tuple(row["tags"].split(";")),
                             row["source_url"], row["source_label"])
        records = read_catalog(self.path)
        self.assertEqual(len(records), 2)
        self.assertEqual(records[-1], expected)
        self.assertEqual(records[0].name, ORIGINAL["name"])
        self.assertTrue(self.path.read_bytes().startswith(self.original))

    def test_reordered_name_city_service_preserves_requested_identity(self):
        digest = self.create(SWAPPED)
        status, result = append_entry(self.path, {
            "expected_catalog_sha256": digest, "confirm_add": True, **PROPOSED})
        self.assertEqual(status, 201)
        self.assert_appended()
        ranked = shortlist(read_catalog(self.path), city="Minneapolis", interests="coffee")
        self.assertEqual(ranked["lanes"]["place"][0]["name"], PROPOSED["name"])
        self.assertEqual(result["added_name"], PROPOSED["name"])
        self.assertEqual(result["new_csv_sha256"], hashlib.sha256(self.path.read_bytes()).hexdigest())

    def test_all_720_supported_header_permutations_keep_every_field(self):
        for header in itertools.permutations(COLUMNS):
            with self.subTest(header=header):
                self.create(header)
                self.append()
                self.assert_appended()

    def test_attribution_columns_cannot_silently_exchange_valid_urls(self):
        self.create(("lane", "name", "city", "tags", "source_label", "source_url"))
        row = {**PROPOSED, "source_label": "https://source.example/operator-label"}
        self.append(row)
        self.assert_appended(row)

    def test_bom_crlf_blank_line_and_unterminated_old_row_are_preserved(self):
        for final_newline in (True, False):
            with self.subTest(final_newline=final_newline):
                self.create(SWAPPED, ending="\r\n", bom=True, final_newline=final_newline)
                self.original = self.original.replace(b"\r\n", b"\r\n\r\n", 1)
                self.path.write_bytes(self.original)
                self.append()
                self.assert_appended()

    def test_shared_lanes_unicode_and_csv_quoting_keep_values(self):
        for lane in ("brand", "artist"):
            with self.subTest(lane=lane):
                self.create(tuple(reversed(COLUMNS)))
                row = {**PROPOSED, "lane": lane, "city": "",
                       "name": 'Cafe\u0301, "North"', "source_label": 'Operator, "citation"'}
                self.append(row)
                self.assert_appended(row)

    def test_stale_hash_and_duplicate_identity_still_refuse_without_changes(self):
        old_digest = self.create(SWAPPED)
        self.append()
        after = self.path.read_bytes()
        code, body = append_entry(self.path, {
            "expected_catalog_sha256": old_digest, "confirm_add": True, **PROPOSED})
        self.assertEqual((code, body), (409, {"error": "catalogue_changed"}))
        code, body = append_entry(self.path, {
            "expected_catalog_sha256": hashlib.sha256(after).hexdigest(),
            "confirm_add": True, **PROPOSED})
        self.assertEqual((code, body), (422, {"error": "invalid_entry"}))
        self.assertEqual(self.path.read_bytes(), after)

    def test_default_header_and_invalid_record_behaviors_are_unchanged(self):
        self.create()
        with self.assertRaises(CatalogueAddError):
            self.append(source_url="http://source.example/not-https")
        self.assertEqual(self.path.read_bytes(), self.original)
        self.assertFalse(list(self.path.parent.glob(".culturefit-add-*.csv")))
        result = self.append()
        self.assert_appended()
        self.assertEqual(result["provider_calls"], 0)
        self.assertFalse(result["provider_independently_verified"])

    def test_root_only_native_cli_uses_existing_header_order(self):
        digest = self.create(SWAPPED)
        command = [sys.executable, "-S", str(ROOT / "catalog_add.py"),
                   "--catalog", str(self.path), "--expect-current-sha256", digest,
                   "--confirm-add", "--lane", PROPOSED["lane"], "--name", PROPOSED["name"],
                   "--city", PROPOSED["city"], "--tags", PROPOSED["tags"],
                   "--source-url", PROPOSED["source_url"],
                   "--source-label", PROPOSED["source_label"]]
        result = subprocess.run(command, cwd=ROOT, env={**os.environ, "PYTHONPATH": ""},
                                capture_output=True, text=True, timeout=10, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["added_name"], PROPOSED["name"])
        self.assert_appended()


if __name__ == "__main__":
    unittest.main()
