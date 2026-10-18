"""Root-only, no-network tests of the authored-source catalogue flow."""
from __future__ import annotations

import csv
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from curated_catalog import CatalogError, read_catalog, shortlist  # noqa: E402


class CuratedCatalogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "sources.csv"
        self.rows = [
            ("place", "Jasmine Room", "Minneapolis", "jazz; coffee", "https://example.org/place/1", "Curator notes"),
            ("place", "Lakeside Venue", "Chicago", "jazz", "https://example.org/place/2", "Curator notes"),
            ("brand", "Coffee Works", "", "coffee; design", "https://example.org/brands/a", "Partner catalogue"),
            ("artist", "The Blue Trio", "", "jazz; live", "https://example.org/artists/b", "Artist website"),
            ("artist", "Another Act", "", "classical", "https://example.org/artists/c", "Artist website"),
        ]
        self.write(self.rows)

    def write(self, rows):
        with self.path.open("w", encoding="utf-8", newline="") as fp:
            writer = csv.writer(fp)
            writer.writerow(("lane", "name", "city", "tags", "source_url", "source_label"))
            writer.writerows(rows)

    def result(self, interests="jazz,coffee", city="Minneapolis", per_lane=3):
        return shortlist(read_catalog(self.path), interests=interests, city=city, per_lane=per_lane)

    def test_three_real_input_lanes_without_provider_activity(self):
        result = self.result()
        self.assertEqual([len(result["lanes"][lane]) for lane in ("place", "brand", "artist")], [1, 1, 1])
        self.assertEqual(result["lanes"]["place"][0]["match_count"], 2)
        self.assertEqual(result["provider_calls"], 0)
        self.assertEqual(result["missing_lanes"], [])
        self.assertEqual(result["lanes"]["artist"][0]["source_status"], "user_supplied_unverified")

    def test_city_filters_only_venues(self):
        result = self.result(city="Chicago")
        self.assertEqual(result["lanes"]["place"][0]["name"], "Lakeside Venue")
        self.assertEqual(result["lanes"]["brand"][0]["name"], "Coffee Works")

    def test_missing_lanes_are_visible_not_fabricated(self):
        result = self.result(interests="classical")
        self.assertEqual(result["missing_lanes"], ["place", "brand"])
        self.assertEqual(result["lanes"]["artist"][0]["name"], "Another Act")

    def test_exact_terms_do_not_guess_tags(self):
        self.assertEqual(self.result(interests="jaz")["missing_lanes"], ["place", "brand", "artist"])

    def test_match_is_casefolded_and_deduplicated(self):
        result = self.result(interests="JAZZ,jazz,COFFEE")
        self.assertEqual(result["interests"], ["jazz", "coffee"])
        self.assertEqual(result["lanes"]["place"][0]["match_count"], 2)

    def test_reject_duplicate_candidate_identity(self):
        self.write(self.rows + [self.rows[0]])
        with self.assertRaisesRegex(CatalogError, "duplicate"):
            read_catalog(self.path)

    def test_reject_unsourced_and_insecure_rows(self):
        for url in ("", "http://example.org/1", "https://user:pw@example.org/1"):
            with self.subTest(url=url):
                row = list(self.rows[0]); row[4] = url
                self.write([row])
                with self.assertRaises(CatalogError):
                    read_catalog(self.path)

    def test_reject_place_without_city(self):
        row = list(self.rows[0]); row[2] = ""
        self.write([row])
        with self.assertRaises(CatalogError):
            read_catalog(self.path)

    def test_reject_unknown_lane(self):
        row = list(self.rows[0]); row[0] = "fictional"
        self.write([row])
        with self.assertRaises(CatalogError):
            read_catalog(self.path)

    def test_reject_too_many_interests_and_invalid_limit(self):
        for kwargs in ({"interests": ",".join(str(n) for n in range(13))}, {"per_lane": 0}):
            with self.assertRaises(CatalogError):
                self.result(**kwargs)

    def test_real_cli_launches_from_independent_root(self):
        proc = subprocess.run([sys.executable, str(ROOT / "curated_catalog.py"),
                               "--catalog", str(self.path), "--city", "Minneapolis",
                               "--interests", "jazz,coffee"], cwd=ROOT,
                              env={"PYTHONPATH": ""}, capture_output=True, text=True, timeout=5)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        result = json.loads(proc.stdout)
        self.assertEqual(result["lanes"]["place"][0]["name"], "Jasmine Room")

    def test_cli_reports_validation_error_without_fake_results(self):
        row = list(self.rows[0]); row[4] = "http://not-https.invalid"
        self.write([row])
        proc = subprocess.run([sys.executable, str(ROOT / "curated_catalog.py"),
                               "--catalog", str(self.path), "--city", "Minneapolis",
                               "--interests", "jazz"], cwd=ROOT,
                              env={"PYTHONPATH": ""}, capture_output=True, text=True, timeout=5)
        self.assertEqual(proc.returncode, 2)
        self.assertFalse(proc.stdout.strip())

    def test_must_have_tags_restrict_candidates_without_inventing_matches(self):
        result = shortlist(read_catalog(self.path), city="Minneapolis",
                           interests="jazz,coffee", required_tags="JAZZ,coffee")
        self.assertEqual([x["name"] for x in result["lanes"]["place"]], ["Jasmine Room"])
        self.assertEqual(result["missing_lanes"], ["brand", "artist"])
        self.assertEqual(result["required_tags"], ["jazz", "coffee"])
        self.assertEqual(result["lanes"]["place"][0]["source_label"], "Curator notes")

    def test_excluded_tags_remove_ineligible_candidates(self):
        result = shortlist(read_catalog(self.path), city="Minneapolis",
                           interests="jazz,coffee", excluded_tags="coffee")
        self.assertEqual(result["missing_lanes"], ["place", "brand"])
        self.assertEqual([x["name"] for x in result["lanes"]["artist"]], ["The Blue Trio"])
        self.assertEqual(result["provider_calls"], 0)

    def test_combined_filters_preserve_city_and_source_provenance(self):
        result = shortlist(read_catalog(self.path), city="Minneapolis",
                           interests="jazz,coffee", required_tags="jazz",
                           excluded_tags="coffee")
        self.assertFalse(result["lanes"]["place"])
        self.assertEqual(result["lanes"]["artist"][0]["source_status"],
                         "user_supplied_unverified")
        self.assertEqual(result["excluded_tags"], ["coffee"])

    def test_invalid_and_conflicting_filters_fail_closed(self):
        cases = (
            {"required_tags": "jazz", "excluded_tags": "JAZZ"},
            {"required_tags": "jazz,"},
            {"excluded_tags": None},
            {"required_tags": "\ud800"},
            {"excluded_tags": ",".join(str(n) for n in range(13))},
        )
        for kwargs in cases:
            with self.subTest(kwargs=repr(kwargs)), self.assertRaises(CatalogError):
                shortlist(read_catalog(self.path), city="Minneapolis",
                          interests="jazz,coffee", **kwargs)

    def test_cli_supports_both_constraints_at_independent_root(self):
        proc = subprocess.run(
            [sys.executable, str(ROOT / "curated_catalog.py"), "--catalog", str(self.path),
             "--city", "Minneapolis", "--interests", "jazz,coffee",
             "--required-tags", "jazz", "--excluded-tags", "coffee"],
            cwd=ROOT, env={"PYTHONPATH": ""},
            capture_output=True, text=True, timeout=5)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        parsed = json.loads(proc.stdout)
        self.assertEqual(parsed["lanes"]["artist"][0]["name"], "The Blue Trio")
        self.assertEqual(parsed["missing_lanes"], ["place", "brand"])



if __name__ == "__main__":
    unittest.main()
