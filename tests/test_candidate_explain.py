"""Root-only native tests for operator-source candidate explanation."""
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
from curated_catalog import CatalogError, read_catalog
from candidate_explain import explain_candidate


class CandidateExplanationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.catalog = Path(self.temp.name) / "operator.csv"
        with self.catalog.open("w", encoding="utf-8", newline="") as out:
            writer = csv.writer(out)
            writer.writerow(("lane", "name", "city", "tags", "source_url", "source_label"))
            writer.writerows([
                ("place", "Jasmine Room", "MINNEAPOLIS", "jazz;coffee", "https://example.org/a", "Operator A"),
                ("place", "Beat House", "Minneapolis", "jazz", "https://example.org/b", "Operator B"),
                ("place", "Far Away", "Madison", "jazz", "https://example.org/c", "Operator C"),
                ("place", "Quiet Room", "Minneapolis", "acoustic", "https://example.org/d", "Operator D"),
                ("place", "Loud Venue", "Minneapolis", "jazz;loud", "https://example.org/e", "Operator E"),
                ("brand", "Beans", "", "coffee", "https://example.org/f", "Operator F"),
                ("artist", "Jazz Quartet", "", "jazz", "https://example.org/g", "Operator G"),
                ("place", "Jasmine Room", "Madison", "coffee", "https://example.org/h", "Operator H"),
            ])

    def report(self, name="Jasmine Room", lane="place", *, candidate_city="Minneapolis", **overrides):
        fields = {"city": "Minneapolis", "interests": "jazz,coffee", "per_lane": 2,
                  "required_tags": "", "excluded_tags": ""}
        fields.update(overrides)
        return explain_candidate(read_catalog(self.catalog), name=name, lane=lane,
                                 candidate_city=candidate_city, **fields)

    def test_included_rank_matches_actual_shortlist_and_preserves_case(self):
        result = self.report()
        self.assertEqual(result["status"], "included")
        self.assertEqual(result["shortlist_rank"], 1)
        self.assertEqual(result["candidate"]["city"], "MINNEAPOLIS")
        self.assertEqual(result["matched_interests"], ["jazz", "coffee"])
        self.assertEqual(result["reasons"], [])

    def test_missing_candidate_city_disambiguation_fails(self):
        with self.assertRaisesRegex(CatalogError, "ambiguous"):
            self.report(candidate_city=None)

    def test_different_city_is_ineligible(self):
        result = self.report(name="Far Away", candidate_city="Madison")
        self.assertEqual(result["status"], "ineligible")
        self.assertEqual(result["reasons"], ["place_outside_requested_city"])
        self.assertIsNone(result["shortlist_rank"])

    def test_excluded_tag_is_explained_not_silently_dropped(self):
        result = self.report(name="Loud Venue", excluded_tags="loud")
        self.assertEqual(result["reasons"], ["excluded_tags_present"])
        self.assertEqual(result["excluded_tags_present"], ["loud"])

    def test_missing_required_tags_reported_with_exact_author_tag(self):
        result = self.report(name="Beat House", required_tags="coffee")
        self.assertEqual(result["missing_required_tags"], ["coffee"])
        self.assertEqual(result["reasons"], ["missing_required_tags"])

    def test_no_requested_interest_reports_reason(self):
        result = self.report(name="Quiet Room")
        self.assertEqual(result["reasons"], ["no_matching_interests"])
        self.assertEqual(result["matched_interests"], [])

    def test_top_n_cutoff_uses_existing_ranking(self):
        result = self.report(name="Beat House", per_lane=1)
        self.assertEqual(result["status"], "eligible_outside_result_limit")
        self.assertEqual(result["reasons"], ["outside_per_lane_limit"])

    def test_brand_not_city_restricted_and_has_source_label(self):
        result = self.report(name="Beans", lane="brand", candidate_city="")
        self.assertEqual(result["status"], "included")
        self.assertEqual(result["candidate"]["source_label"], "Operator F")
        self.assertNotIn("place_outside_requested_city", result["reasons"])

    def test_absent_candidate_is_error_not_fabricated(self):
        with self.assertRaisesRegex(CatalogError, "absent"):
            self.report(name="Unlisted Place")

    def test_contradictory_query_fails_using_real_search_rules(self):
        with self.assertRaisesRegex(CatalogError, "conflicting"):
            self.report(required_tags="jazz", excluded_tags="jazz")

    def test_provenance_is_explicitly_unverified(self):
        result = self.report()
        self.assertEqual(result["source_status"], "user_supplied_unverified")
        self.assertEqual(result["provider_calls"], 0)
        self.assertFalse(result["provider_independently_verified"])
        self.assertEqual(result["candidate"]["source_url"], "https://example.org/a")

    def test_real_root_only_cli_launch(self):
        proc = subprocess.run(
            [sys.executable, "-S", "candidate_explain.py", "--catalog", str(self.catalog),
             "--lane", "place", "--name", "Beat House", "--candidate-city", "Minneapolis",
             "--city", "Minneapolis", "--interests", "jazz,coffee", "--per-lane", "1"],
            cwd=ROOT, env={**os.environ, "PYTHONPATH": ""},
            capture_output=True, text=True, timeout=6,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        value = json.loads(proc.stdout)
        self.assertEqual(value["status"], "eligible_outside_result_limit")

    def test_invalid_csv_fails_cleanly_without_output(self):
        self.catalog.write_text("wrong,headers\n a,b\n", encoding="utf-8")
        proc = subprocess.run(
            [sys.executable, "-S", "candidate_explain.py", "--catalog", str(self.catalog),
             "--lane", "place", "--name", "Jasmine Room",
             "--city", "Minneapolis", "--interests", "jazz"],
            cwd=ROOT, env={**os.environ, "PYTHONPATH": ""},
            capture_output=True, text=True, timeout=6,
        )
        self.assertEqual(proc.returncode, 2)
        self.assertIn("explanation error:", proc.stderr)
        self.assertFalse(proc.stdout)


if __name__ == "__main__":
    unittest.main()
