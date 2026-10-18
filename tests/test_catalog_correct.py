"""Native full-file checks of actual one-record correction and CLI launch."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from catalog_correct import CorrectionError, correct_catalogue
from curated_catalog import read_catalog, shortlist

ROOT = Path(__file__).resolve().parents[1]
HEADER = b"lane,name,city,tags,source_url,source_label\r\n"
PLACE = b"place,Local venue,Minneapolis,jazz;coffee,https://example.org/place,Operator source\r\n"
BRAND = b"brand,Local brand,,coffee,https://example.org/brand,Brand citation\r\n"
ARTIST = b"artist,Local artist,,jazz,https://example.org/artist,Artist citation\r\n"


class NativeCatalogCorrectTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.source = self.directory / "original.csv"
        self.output = self.directory / "corrected.csv"
        self.original = HEADER + PLACE + BRAND + ARTIST
        self.source.write_bytes(self.original)
        self.sha = hashlib.sha256(self.original).hexdigest()

    def apply(self, **kwargs):
        return correct_catalogue(
            self.source, output=self.output, expected_sha256=self.sha,
            lane="place", name="Local venue", city="Minneapolis", **kwargs,
        )

    def test_correction_keeps_original_and_only_one_physical_line_changes(self):
        result = self.apply(tags="jazz;gallery", source_label="Corrected by operator")
        current = self.output.read_bytes()
        self.assertEqual(self.source.read_bytes(), self.original)
        self.assertTrue(current.startswith(HEADER))
        self.assertTrue(current.endswith(BRAND + ARTIST))
        self.assertIn(b"jazz;gallery", current)
        self.assertNotIn(b"jazz;coffee", current)
        self.assertEqual(3, result["record_count"])
        self.assertEqual("one_record_new_file_only", result["operation"])
        self.assertFalse(result["provider_independently_verified"])
        self.assertEqual(0, result["provider_calls"])
        self.assertEqual(self.sha, result["original_csv_sha256"])
        self.assertEqual(hashlib.sha256(current).hexdigest(), result["corrected_csv_sha256"])
        self.assertEqual(0o600, self.output.stat().st_mode & 0o777)

    def test_corrected_tags_change_real_source_backed_shortlist(self):
        before = shortlist(read_catalog(self.source), city="Minneapolis", interests="gallery")
        self.assertFalse(before["lanes"]["place"])
        self.apply(tags="gallery;coffee")
        after = shortlist(read_catalog(self.output), city="Minneapolis", interests="gallery")
        self.assertEqual("Local venue", after["lanes"]["place"][0]["name"])
        self.assertEqual("https://example.org/place", after["lanes"]["place"][0]["source_url"])
        self.assertEqual("user_supplied_unverified", after["source_status"])

    def test_source_url_corrected_without_url_fetch(self):
        self.apply(source_url="https://example.org/new-reference")
        self.assertEqual("https://example.org/new-reference", read_catalog(self.output)[0].source_url)
        self.assertEqual(self.original, self.source.read_bytes())

    def test_wrong_expected_digest_does_not_write(self):
        with self.assertRaises(CorrectionError):
            correct_catalogue(self.source, output=self.output, expected_sha256="0" * 64,
                              lane="place", name="Local venue", city="Minneapolis", tags="gallery")
        self.assertFalse(self.output.exists())

    def test_requires_actual_change_and_removes_failed_candidate(self):
        with self.assertRaises(CorrectionError):
            self.apply(tags="jazz;coffee")
        self.assertFalse(self.output.exists())
        self.assertEqual(self.original, self.source.read_bytes())

    def test_no_requested_fields_rejected(self):
        with self.assertRaises(CorrectionError):
            self.apply()
        self.assertFalse(self.output.exists())

    def test_bad_attribution_is_rejected_by_real_parser(self):
        with self.assertRaises(CorrectionError):
            self.apply(source_url="http://example.org/not-https")
        self.assertFalse(self.output.exists())

    def test_missing_identity_leaves_source_and_destination_unchanged(self):
        with self.assertRaises(CorrectionError):
            correct_catalogue(self.source, output=self.output, expected_sha256=self.sha,
                              lane="place", name="Not here", city="Minneapolis", tags="gallery")
        self.assertFalse(self.output.exists())
        self.assertEqual(self.original, self.source.read_bytes())

    def test_existing_output_is_never_overwritten(self):
        self.output.write_text("important existing data", encoding="utf-8")
        with self.assertRaises(CorrectionError):
            self.apply(tags="gallery")
        self.assertEqual("important existing data", self.output.read_text())

    def test_original_symlink_is_rejected(self):
        alias = self.directory / "alias.csv"
        alias.symlink_to(self.source)
        with self.assertRaises(CorrectionError):
            correct_catalogue(alias, output=self.output, expected_sha256=self.sha,
                              lane="place", name="Local venue", city="Minneapolis", tags="gallery")
        self.assertFalse(self.output.exists())

    def test_bom_and_blank_line_preserved_with_last_record_no_newline(self):
        unusual = b"\xef\xbb\xbf" + HEADER + b"\r\n" + BRAND + ARTIST.rstrip(b"\r\n")
        self.source.write_bytes(unusual)
        self.sha = hashlib.sha256(unusual).hexdigest()
        result = correct_catalogue(self.source, output=self.output, expected_sha256=self.sha,
                                   lane="artist", name="Local artist", tags="music")
        contents = self.output.read_bytes()
        self.assertTrue(contents.startswith(b"\xef\xbb\xbf" + HEADER + b"\r\n" + BRAND))
        self.assertTrue(contents.endswith(b"Artist citation"))
        self.assertNotIn(b"Artist citation\n", contents)
        self.assertEqual("music", read_catalog(self.output)[1].tags[0])
        self.assertEqual(self.sha, result["original_csv_sha256"])

    def test_root_only_separate_process_cli(self):
        completed = subprocess.run(
            [sys.executable, "-S", str(ROOT / "catalog_correct.py"),
             "--catalog", str(self.source), "--output", str(self.output),
             "--expect-current-sha256", self.sha,
             "--lane", "place", "--name", "Local venue", "--city", "Minneapolis",
             "--tags", "gallery;coffee"],
            cwd=ROOT, env={**os.environ, "PYTHONPATH": ""},
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=10,
        )
        self.assertEqual(0, completed.returncode, completed.stderr)
        report = json.loads(completed.stdout)
        self.assertEqual(3, report["record_count"])
        self.assertEqual("gallery", read_catalog(self.output)[0].tags[0])
        self.assertEqual(self.original, self.source.read_bytes())


if __name__ == "__main__":
    unittest.main()
