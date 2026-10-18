"""Native tests for an actual one-record source-only shortlist impact preview."""
from __future__ import annotations

import csv
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from catalog_impact import record_impact
from curated_catalog import COLUMNS, CatalogError, read_catalog

ROOT = Path(__file__).resolve().parents[1]
RECORDS = (
    ("place", "First Venue", "Minneapolis", "jazz", "https://operator.example/venue", "Venue note"),
    ("brand", "First Brand", "", "coffee", "https://operator.example/brand", "Brand note"),
    ("artist", "First Artist", "", "art", "https://operator.example/artist", "Artist note"),
)


class ImpactTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / "sources.csv"
        with self.path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle, lineterminator="\n")
            writer.writerow(COLUMNS)
            writer.writerows(RECORDS)
        self.original = self.path.read_bytes()
        self.row = dict(zip(COLUMNS, ("place", "Top Venue", "Minneapolis", "jazz;coffee",
                                      "https://operator.example/top", "Operator new source")))

    def preview(self, **options):
        settings = {"row": self.row, "city": "Minneapolis", "interests": "jazz,coffee,art",
                    "per_lane": 2}
        settings.update(options)
        return record_impact(self.path, **settings)

    def unchanged(self):
        self.assertEqual(self.path.read_bytes(), self.original)
        self.assertEqual(len(read_catalog(self.path)), 3)

    def test_ranked_place_appears_with_authentic_source_and_changes_only_place_lane(self):
        result = self.preview()
        self.assertEqual(result["candidate_rank_within_top"], 1)
        self.assertEqual(list(result["changed_lanes"]), ["place"])
        self.assertEqual(result["changed_lanes"]["place"]["after"][0]["name"], "Top Venue")
        self.assertEqual(result["proposed_candidate"]["source_label"], "Operator new source")
        self.assertEqual(result["provider_calls"], 0)
        self.assertIs(result["provider_independently_verified"], False)
        self.assertEqual(result["source_status"], "user_supplied_unverified")
        self.assertEqual(result["original_catalogue_sha256"], hashlib.sha256(self.original).hexdigest())
        self.unchanged()

    def test_other_city_candidate_remains_outside_place_results(self):
        result = self.preview(row={**self.row, "city": "Chicago"})
        self.assertIsNone(result["candidate_rank_within_top"])
        self.assertTrue(result["no_change_to_top_ranked_results"])
        self.unchanged()

    def test_nonmatching_tags_do_not_invent_matches(self):
        result = self.preview(row={**self.row, "tags": "techno"})
        self.assertIsNone(result["candidate_rank_within_top"])
        self.assertTrue(result["no_change_to_top_ranked_results"])
        self.unchanged()

    def test_required_and_excluded_tag_filters_are_applied(self):
        result = self.preview(required_tags="coffee", excluded_tags="art")
        self.assertEqual(result["candidate_rank_within_top"], 1)
        self.assertEqual(result["required_tags"], ["coffee"])
        self.assertEqual(result["excluded_tags"], ["art"])
        self.unchanged()

    def test_candidate_can_rank_outside_top_cap(self):
        result = self.preview(row={**self.row, "name": "Z Venue", "tags": "jazz"}, per_lane=1)
        self.assertIsNone(result["candidate_rank_within_top"])
        self.assertTrue(result["no_change_to_top_ranked_results"])
        self.unchanged()

    def test_duplicate_candidate_identity_rejected_without_mutation(self):
        with self.assertRaises(CatalogError):
            self.preview(row={**self.row, "name": "FIRST VENUE", "tags": "coffee"})
        self.unchanged()

    def test_invalid_url_missing_city_and_duplicate_field_keys_rejected(self):
        for row in ({**self.row, "source_url": "http://operator.example/top"},
                    {**self.row, "city": ""}, {**self.row, "tags": ""},
                    {**self.row, "extra": "bad"},
                    {**self.row, "name": ""},
                    {**self.row, "name": None}):
            with self.subTest(row=row), self.assertRaises(CatalogError):
                self.preview(row=row)
            self.unchanged()

    def test_invalid_counts_filters_and_interests_do_not_mutate(self):
        for bad in (True, 0, 21, "2"):
            with self.subTest(count=bad), self.assertRaises(CatalogError):
                self.preview(per_lane=bad)
        with self.assertRaises(CatalogError):
            self.preview(interests="")
        with self.assertRaises(CatalogError):
            self.preview(required_tags="jazz", excluded_tags="JAZZ")
        self.unchanged()

    def test_missing_invalid_or_symlink_csv_rejected(self):
        alias = self.path.parent / "alias.csv"
        alias.symlink_to(self.path)
        with self.assertRaises(CatalogError):
            record_impact(alias, row=self.row, city="Minneapolis", interests="jazz")
        self.path.unlink()
        with self.assertRaises(CatalogError):
            self.preview()

    def test_native_cli_root_only_no_catalogue_mutation(self):
        env = os.environ.copy()
        env["PYTHONPATH"] = ""
        run = subprocess.run([sys.executable, "-S", str(ROOT / "catalog_impact.py"),
             "--catalog", str(self.path), "--lane", "place", "--name", "Top Venue",
             "--candidate-city", "Minneapolis", "--tags", "jazz;coffee",
             "--source-url", "https://operator.example/top", "--source-label", "Operator new source",
             "--city", "Minneapolis", "--interests", "jazz,coffee,art", "--per-lane", "2"],
             cwd=ROOT, env=env, capture_output=True, text=True, check=False)
        self.assertEqual(run.returncode, 0, run.stderr)
        report = json.loads(run.stdout)
        self.assertEqual(report["candidate_rank_within_top"], 1)
        self.assertEqual(report["proposed_record_count"], 4)
        self.unchanged()

    def test_native_cli_rejects_invalid_source_and_returns_no_json(self):
        run = subprocess.run([sys.executable, "-S", str(ROOT / "catalog_impact.py"),
             "--catalog", str(self.path), "--lane", "place", "--name", "Top Venue",
             "--candidate-city", "Minneapolis", "--tags", "jazz",
             "--source-url", "http://invalid.example/top", "--source-label", "Operator source",
             "--city", "Minneapolis", "--interests", "jazz"],
             cwd=ROOT, capture_output=True, text=True, check=False)
        self.assertEqual(run.returncode, 2)
        self.assertEqual(run.stdout, "")
        self.unchanged()


if __name__ == "__main__":
    unittest.main()
