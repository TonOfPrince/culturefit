"""Native, app-root tests for the read-only, source-bound two-city comparison."""
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
from city_compare import compare_cities


def item(lane: str, name: str, tags: str, city: str = "") -> Candidate:
    return Candidate(lane, name, city, tuple(tags.split(";")),
                     f"https://sources.example/{lane}/{name.lower().replace(' ', '-')}",
                     f"Operator cited {name}")


class CityCompareTests(unittest.TestCase):
    def setUp(self):
        self.records = (
            item("place", "First Jazz", "jazz;coffee", "Minneapolis"),
            item("place", "First Cafe", "coffee", "Minneapolis"),
            item("place", "Other Gallery", "art", "St Paul"),
            item("place", "Second Jazz", "jazz", "St Paul"),
            item("brand", "Shared Brand", "coffee;art"),
            item("artist", "Shared Artist", "jazz"),
        )

    def compare(self, records=None, **kw):
        arguments = {"first_city": "Minneapolis", "second_city": "St Paul",
                     "interests": "jazz,coffee,art"}
        arguments.update(kw)
        return compare_cities(self.records if records is None else records, **arguments)

    def test_distinct_cities_have_actual_place_counts_and_tag_gaps(self):
        view = self.compare()["cities"]
        self.assertEqual([v["eligible_place_records"] for v in view], [2, 2])
        self.assertEqual([v["places_matching_any_interest"] for v in view], [2, 2])
        self.assertEqual([v["missing_place_interests"] for v in view], [["art"], ["coffee"]])
        self.assertEqual(view[0]["place_matches_by_interest"],
                         {"jazz": 1, "coffee": 2, "art": 0})

    def test_global_lanes_are_shared_once_not_misattributed_to_cities(self):
        result = self.compare()
        self.assertEqual(set(result["shared_lanes"]), {"artist", "brand"})
        self.assertEqual(result["shared_lanes"]["brand"][0]["name"], "Shared Brand")
        self.assertNotIn("brand", result["cities"][0])
        self.assertNotIn("artist", result["cities"][1])
        self.assertIn("not city-specific", result["shared_lanes_note"])

    def test_top_places_are_real_attributed_rows_with_explicit_unverified_status(self):
        result = self.compare(per_city=1)
        self.assertEqual(len(result["cities"][0]["top_places"]), 1)
        self.assertEqual(result["cities"][0]["top_places"][0]["name"], "First Jazz")
        self.assertEqual(result["cities"][0]["top_places"][0]["source_url"],
                         "https://sources.example/place/first-jazz")
        self.assertEqual(result["source_status"], "user_supplied_unverified")
        self.assertFalse(result["provider_independently_verified"])
        self.assertEqual(result["provider_calls"], 0)
        self.assertEqual(result["cities"][1]["top_places"][0]["city"], "St Paul")

    def test_no_place_candidate_does_not_generate_a_recommendation(self):
        result = self.compare(interests="blues")
        self.assertEqual(result["cities"][0]["top_places"], [])
        self.assertEqual(result["cities"][1]["top_places"], [])
        self.assertEqual(result["cities"][0]["missing_place_interests"], ["blues"])
        self.assertEqual(result["shared_lanes"], {"brand": [], "artist": []})

    def test_order_independent_and_city_casefold_for_place_fence(self):
        a = self.compare()
        b = self.compare(tuple(reversed(self.records)), first_city="MINNEAPOLIS", second_city="ST PAUL")
        for x, y in zip(a["cities"], b["cities"]):
            self.assertEqual(x["place_matches_by_interest"], y["place_matches_by_interest"])
            self.assertEqual(x["top_places"], y["top_places"])

    def test_comparison_delta_is_computed_not_a_fit_score(self):
        result = self.compare(interests="coffee")
        self.assertEqual(result["first_minus_second_place_matches"], 2)
        self.assertNotIn("fit_score", result)

    def test_rejects_identical_casefolded_cities_and_invalid_counts(self):
        with self.assertRaises(CatalogError):
            self.compare(second_city="minneapolis")
        for invalid in (0, 21, True, 2.5, "3"):
            with self.subTest(invalid=invalid), self.assertRaises(CatalogError):
                self.compare(per_city=invalid)

    def test_invalid_interests_or_city_fail_before_output(self):
        for kwargs in ({"interests": ""}, {"second_city": ""},
                       {"first_city": "\ud800"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(CatalogError):
                self.compare(**kwargs)

    def test_actual_cli_from_app_root_keeps_csv_unchanged(self):
        with tempfile.TemporaryDirectory() as folder:
            catalog = Path(folder) / "sources.csv"
            with catalog.open("w", encoding="utf-8", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=COLUMNS)
                writer.writeheader()
                for r in self.records:
                    writer.writerow({"lane": r.lane, "name": r.name, "city": r.city,
                                     "tags": ";".join(r.tags), "source_url": r.source_url,
                                     "source_label": r.source_label})
            original = catalog.read_bytes()
            root = Path(__file__).resolve().parents[1]
            env = os.environ.copy()
            env["PYTHONPATH"] = ""
            command = [sys.executable, "-S", str(root / "city_compare.py"),
                       "--catalog", str(catalog), "--first-city", "Minneapolis",
                       "--second-city", "St Paul", "--interests", "jazz,coffee,art"]
            process = subprocess.run(command, cwd=root, env=env,
                                     capture_output=True, text=True, check=False)
            self.assertEqual(process.returncode, 0, process.stderr)
            self.assertEqual(catalog.read_bytes(), original)
            output = json.loads(process.stdout)
            self.assertEqual(output["cities"][0]["top_places"][0]["name"], "First Jazz")
            self.assertEqual(len(read_catalog(catalog)), 6)

    def test_cli_rejects_missing_catalogue(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(__file__).resolve().parents[1]
            cmd = [sys.executable, "-S", str(root / "city_compare.py"),
                   "--catalog", str(Path(folder) / "not-there.csv"),
                   "--first-city", "Minneapolis", "--second-city", "Minneapolis",
                   "--interests", "jazz"]
            run = subprocess.run(cmd, cwd=root, capture_output=True, text=True, check=False)
            self.assertEqual(run.returncode, 2)
            self.assertEqual(run.stdout, "")
            self.assertIn("city comparison error:", run.stderr)


if __name__ == "__main__":
    unittest.main()
