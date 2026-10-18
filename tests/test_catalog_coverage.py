"""Actual standalone-root coverage tests using operator-authored source CSV."""
from __future__ import annotations

import csv
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from curated_catalog import Candidate, CatalogError, read_catalog
from catalog_coverage import coverage_report


class CoverageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.csv_path = Path(self.tmp.name) / "sources.csv"
        with self.csv_path.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(("lane", "name", "city", "tags", "source_url", "source_label"))
            writer.writerows([
                ("place", "Local club", "MINNEAPOLIS", "jazz;coffee", "https://example.org/a", "Operator 1"),
                ("place", "Another club", "Minneapolis", "jazz", "https://example.org/b", "Operator 2"),
                ("place", "Other city", "Madison", "coffee", "https://example.org/c", "Operator 3"),
                ("brand", "Beans", "", "coffee", "https://example.org/d", "Operator 4"),
                ("artist", "Quartet", "", "jazz", "https://example.org/e", "Operator 5"),
            ])

    def report(self, *, city="Minneapolis", interests="Jazz,coffee"):
        return coverage_report(read_catalog(self.csv_path), city=city, interests=interests)

    def test_counts_by_lane_without_truncating_to_shortlist_limit(self):
        report = self.report()
        self.assertEqual(report["total_records"], 5)
        self.assertEqual(report["lanes"]["place"], {
            "eligible_records": 2, "matching_any_interest": 2,
            "matching_all_interests": 1, "per_interest": {"jazz": 2, "coffee": 1}})

    def test_only_places_are_city_restricted(self):
        report = self.report(city="Madison")
        self.assertEqual(report["lanes"]["place"]["eligible_records"], 1)
        self.assertEqual(report["lanes"]["brand"]["eligible_records"], 1)
        self.assertEqual(report["lanes"]["artist"]["eligible_records"], 1)

    def test_missing_interest_pairs_use_explicit_lane_attribution(self):
        report = self.report()
        self.assertEqual(report["uncovered_lane_interests"], [
            {"lane": "brand", "interest": "jazz"},
            {"lane": "artist", "interest": "coffee"}])

    def test_provenance_is_not_provider_evidence(self):
        report = self.report()
        self.assertEqual(report["basis"], "counts_of_operator_authored_tags_only")
        self.assertEqual(report["source_status"], "user_supplied_unverified")
        self.assertEqual(report["provider_calls"], 0)
        self.assertFalse(report["provider_independently_verified"])

    def test_empty_catalogue_has_explicit_absence_not_invented_matches(self):
        self.csv_path.write_text("lane,name,city,tags,source_url,source_label\n", encoding="utf-8")
        report = self.report()
        self.assertEqual(report["total_records"], 0)
        self.assertEqual(len(report["uncovered_lane_interests"]), 6)
        self.assertTrue(all(row["matching_any_interest"] == 0 for row in report["lanes"].values()))

    def test_noncanonical_repeated_request_terms_follow_existing_search_semantics(self):
        report = self.report(interests=" JAZZ, Jazz,coffee ")
        self.assertEqual(report["interests"], ["jazz", "coffee"])

    def test_empty_city_rejected(self):
        with self.assertRaises(CatalogError):
            self.report(city="  ")

    def test_empty_interest_rejected(self):
        with self.assertRaises(CatalogError):
            self.report(interests="")

    def test_cli_runs_from_root_only_with_empty_pythonpath(self):
        proc = subprocess.run([sys.executable, "catalog_coverage.py", "--catalog", str(self.csv_path),
                               "--city", "Minneapolis", "--interests", "jazz,coffee"],
                              cwd=ROOT, env={**os.environ, "PYTHONPATH": ""},
                              capture_output=True, text=True, timeout=6)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout)["lanes"]["place"]["matching_all_interests"], 1)

    def test_cli_rejects_invalid_operator_csv(self):
        self.csv_path.write_text("wrong,headers\na,b\n", encoding="utf-8")
        proc = subprocess.run([sys.executable, "catalog_coverage.py", "--catalog", str(self.csv_path),
                               "--city", "Minneapolis", "--interests", "jazz"],
                              cwd=ROOT, env={**os.environ, "PYTHONPATH": ""},
                              capture_output=True, text=True, timeout=6)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("coverage error:", proc.stderr)
        self.assertFalse(proc.stdout)


if __name__ == "__main__":
    unittest.main()
