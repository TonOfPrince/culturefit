"""Native tests for sourced cross-lane bundle composition in app-only root."""
from __future__ import annotations

import csv
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from curated_catalog import COLUMNS, Candidate, CatalogError, read_catalog
from fit_bundle import compose_bundles


def candidate(lane: str, name: str, tags: str, city: str = "") -> Candidate:
    return Candidate(lane, name, city, tuple(tags.split(";")),
                     f"https://sources.example/{lane}/{name.lower().replace(' ', '-')}",
                     f"operator source for {name}")


class BundleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.records = (
            candidate("place", "North Hall", "jazz;coffee", "Minneapolis"),
            candidate("place", "South Hall", "jazz;art", "St Paul"),
            candidate("brand", "Coffee Collective", "coffee;art"),
            candidate("brand", "Jazz Co", "jazz"),
            candidate("artist", "Painter", "art"),
            candidate("artist", "Saxophone", "jazz"),
        )

    def query(self, records=None, **kwargs):
        opts = {"city": "Minneapolis", "interests": "jazz,coffee,art"}
        opts.update(kwargs)
        return compose_bundles(self.records if records is None else records, **opts)

    def test_all_three_typed_lanes_and_real_attribution(self):
        result = self.query()
        first = result["bundles"][0]
        self.assertEqual(list(first["members"]), ["place", "brand", "artist"])
        self.assertEqual(first["members"]["place"]["name"], "North Hall")
        self.assertEqual(first["members"]["brand"]["source_label"],
                         "operator source for Coffee Collective")
        self.assertEqual(first["members"]["artist"]["source_url"],
                         "https://sources.example/artist/painter")
        self.assertEqual(result["source_status"], "user_supplied_unverified")
        self.assertIs(result["provider_independently_verified"], False)
        self.assertEqual(result["provider_calls"], 0)

    def test_union_coverage_beats_repeated_tags(self):
        result = self.query()
        first = result["bundles"][0]
        self.assertEqual(first["covered_interests"], ["jazz", "coffee", "art"])
        self.assertEqual(first["covered_interest_count"], 3)
        self.assertEqual(first["members"]["artist"]["name"], "Painter")
        self.assertEqual(first["shared_interests"], ["coffee", "art"])

    def test_shared_interest_breaks_equal_coverage_tie(self):
        rows = (
            candidate("place", "Venue", "jazz;coffee", "Minneapolis"),
            candidate("brand", "Brand", "jazz;coffee"),
            candidate("artist", "Overlap", "jazz;coffee"),
            candidate("artist", "Sparse", "jazz"),
        )
        top = self.query(rows, interests="jazz,coffee")["bundles"][0]
        self.assertEqual(top["members"]["artist"]["name"], "Overlap")
        self.assertEqual(top["shared_interest_count"], 2)
        self.assertEqual(top["tag_observation_count"], 6)

    def test_city_casefold_filters_only_places(self):
        result = self.query(city="MINNEAPOLIS")
        self.assertEqual(result["candidate_pool_counts"]["place"], 1)
        self.assertEqual(result["candidate_pool_counts"]["brand"], 2)
        self.assertEqual(result["candidate_pool_counts"]["artist"], 2)
        self.assertNotIn("South Hall", json.dumps(result))

    def test_no_complete_bundle_when_lane_missing(self):
        rows = tuple(c for c in self.records if c.lane != "artist")
        result = self.query(rows)
        self.assertEqual(result["bundles"], [])
        self.assertEqual(result["missing_lanes"], ["artist"])
        self.assertEqual(result["provider_calls"], 0)

    def test_required_and_excluded_tags_apply_to_every_member(self):
        result = self.query(required_tags="jazz")
        self.assertTrue(result["bundles"])
        for bundle in result["bundles"]:
            for member in bundle["members"].values():
                self.assertIn("jazz", member["matched_interests"])
        missing = self.query(excluded_tags="jazz")
        self.assertEqual(missing["bundles"], [])
        self.assertIn("place", missing["missing_lanes"])

    def test_invalid_limits_and_conflicting_filters(self):
        for bad in (0, 21, True, 2.5, "4"):
            with self.subTest(per_lane=bad), self.assertRaises(CatalogError):
                self.query(per_lane=bad)
        for bad in (0, 11, False, 1.3, "3"):
            with self.subTest(max_bundles=bad), self.assertRaises(CatalogError):
                self.query(max_bundles=bad)
        with self.assertRaises(CatalogError):
            self.query(required_tags="jazz", excluded_tags="JAZZ")

    def test_top_of_each_lane_pool_is_explicitly_bounded(self):
        result = self.query(per_lane=1, max_bundles=10)
        self.assertEqual(result["candidate_pool_counts"],
                         {"place": 1, "brand": 1, "artist": 1})
        self.assertEqual(len(result["bundles"]), 1)
        self.assertEqual(result["pool_limit_per_lane"], 1)

    def test_reordering_input_is_deterministic(self):
        a = self.query(max_bundles=10)
        b = self.query(tuple(reversed(self.records)), max_bundles=10)
        self.assertEqual(a, b)

    def test_nonmatching_criteria_return_empty_no_fabricated_rows(self):
        result = self.query(interests="electronic")
        self.assertEqual(result["bundles"], [])
        self.assertEqual(result["missing_lanes"], ["place", "brand", "artist"])

    def test_native_file_cli_is_read_only_and_root_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp) / "operator.csv"
            with data.open("w", encoding="utf-8", newline="") as out:
                writer = csv.DictWriter(out, fieldnames=COLUMNS)
                writer.writeheader()
                for c in self.records:
                    writer.writerow({"lane": c.lane, "name": c.name, "city": c.city,
                                     "tags": ";".join(c.tags), "source_url": c.source_url,
                                     "source_label": c.source_label})
            original = data.read_bytes()
            env = os.environ.copy()
            env["PYTHONPATH"] = ""
            cmd = [sys.executable, "-S", str(Path(__file__).resolve().parents[1] / "fit_bundle.py"),
                   "--catalog", str(data), "--city", "Minneapolis",
                   "--interests", "jazz,coffee,art"]
            run = subprocess.run(cmd, capture_output=True, text=True,
                                 cwd=str(Path(__file__).resolve().parents[1]), env=env, check=False)
            self.assertEqual(run.returncode, 0, run.stderr)
            payload = json.loads(run.stdout)
            self.assertEqual(payload["bundles"][0]["covered_interest_count"], 3)
            self.assertEqual(data.read_bytes(), original)
            self.assertEqual(len(read_catalog(data)), 6)

    def test_native_cli_refuses_missing_csv_without_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "missing.csv"
            env = os.environ.copy()
            env["PYTHONPATH"] = ""
            run = subprocess.run(
                [sys.executable, "-S", str(Path(__file__).resolve().parents[1] / "fit_bundle.py"),
                 "--catalog", str(missing), "--city", "Minneapolis", "--interests", "jazz"],
                capture_output=True, text=True,
                cwd=str(Path(__file__).resolve().parents[1]), env=env, check=False,
            )
            self.assertEqual(run.returncode, 2)
            self.assertEqual(run.stdout, "")
            self.assertIn("catalogue error", run.stderr)


if __name__ == "__main__":
    unittest.main()
