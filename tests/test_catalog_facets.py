"""Actual native tests of the standalone source-tag facet engine."""
from __future__ import annotations

from collections import namedtuple
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from catalog_facets import FacetError, MAX_RETURNED_TAGS, discover_facets

Candidate = namedtuple("Candidate", "lane name city tags source_url source_label")


def candidate(lane, name, city, tags):
    return Candidate(lane, name, city, tuple(tags),
                     "https://operator.example/" + name.casefold().replace(" ", "-"),
                     "Operator notes")


DATA = (
    candidate("place", "Jazz Hall", "Minneapolis", ["jazz", "coffee"]),
    candidate("place", "Art Hall", "Minneapolis", ["art", "jazz"]),
    candidate("place", "Chicago Room", "Chicago", ["jazz", "blues"]),
    candidate("brand", "Shared Brand", "", ["coffee"]),
    candidate("artist", "Shared Artist", "", ["jazz", "art"]),
)


class NativeFacetTests(unittest.TestCase):
    def test_real_eligible_lane_tag_counts_and_sort_order(self):
        result = discover_facets(DATA, city="Minneapolis")
        self.assertEqual(result["eligible_record_count"], 4)
        self.assertEqual(result["lanes"]["place"]["record_count"], 2)
        self.assertEqual(result["lanes"]["place"]["tags"], [
            {"tag": "jazz", "record_count": 2},
            {"tag": "art", "record_count": 1},
            {"tag": "coffee", "record_count": 1},
        ])
        self.assertEqual(result["lanes"]["brand"]["tags"],
                         [{"tag": "coffee", "record_count": 1}])
        self.assertEqual(result["lanes"]["artist"]["tags"], [
            {"tag": "art", "record_count": 1},
            {"tag": "jazz", "record_count": 1}])

    def test_city_scope_changes_places_without_changing_global_lanes(self):
        first = discover_facets(DATA, city="Minneapolis")
        second = discover_facets(DATA, city="Chicago")
        self.assertEqual(second["lanes"]["place"]["record_count"], 1)
        self.assertEqual(second["lanes"]["place"]["tags"], [
            {"tag": "blues", "record_count": 1},
            {"tag": "jazz", "record_count": 1}])
        for lane in ("brand", "artist"):
            self.assertEqual(first["lanes"][lane], second["lanes"][lane])

    def test_case_insensitive_location_and_normalized_city(self):
        report = discover_facets(DATA, city="  MINNEAPOLIS ")
        self.assertEqual(report["city"], "MINNEAPOLIS")
        self.assertEqual(report["lanes"]["place"]["record_count"], 2)

    def test_absent_city_stays_empty_but_shared_lanes_stay_real(self):
        report = discover_facets(DATA, city="No Such City")
        self.assertEqual(report["lanes"]["place"], {
            "record_count": 0, "distinct_tag_count": 0,
            "tags_truncated": False, "tags": []})
        self.assertEqual(report["eligible_record_count"], 2)
        self.assertEqual(report["lanes"]["brand"]["record_count"], 1)

    def test_never_fabricates_provider_evidence(self):
        report = discover_facets(DATA, city="Minneapolis")
        self.assertEqual(report["source_status"], "user_supplied_unverified")
        self.assertEqual(report["provider_calls"], 0)
        self.assertIs(report["provider_independently_verified"], False)
        self.assertEqual(report["basis"], "frequencies_of_user_authored_tags_not_provider_rankings")
        self.assertNotIn("source_url", json.dumps(report))
        self.assertNotIn("source_label", json.dumps(report))

    def test_duplicate_tag_per_item_counts_once(self):
        report = discover_facets((candidate("place", "A", "X", ["jazz", "jazz"]),), city="X")
        self.assertEqual(report["lanes"]["place"]["tags"],
                         [{"tag": "jazz", "record_count": 1}])

    def test_bounded_top_tags_and_explicit_truncation(self):
        report = discover_facets(
            (candidate("artist", "A", "", ["t%03d" % x for x in range(MAX_RETURNED_TAGS + 4)]),),
            city="Minneapolis")
        artist = report["lanes"]["artist"]
        self.assertEqual(artist["distinct_tag_count"], MAX_RETURNED_TAGS + 4)
        self.assertTrue(artist["tags_truncated"])
        self.assertEqual(len(artist["tags"]), MAX_RETURNED_TAGS)
        self.assertEqual(artist["tags"][0]["tag"], "t000")

    def test_invalid_cities_fail_with_no_mutation(self):
        invalid = (None, 1, "", "  ", "ab\x00cd", "ab\nd", "\ud800", "x" * 301)
        for item in invalid:
            with self.subTest(item=repr(item)), self.assertRaises(FacetError):
                discover_facets(DATA, city=item)
        self.assertEqual(len(DATA), 5)

    def test_root_only_python_subprocess_imports_and_executes_real_module(self):
        script = ("from catalog_facets import discover_facets;"
                  "from collections import namedtuple;"
                  "C=namedtuple('C','lane city tags');"
                  "x=discover_facets((C('place','Minneapolis',('jazz',)),),city='Minneapolis');"
                  "print(x['lanes']['place']['tags'][0]['tag'])")
        env = os.environ.copy()
        env["PYTHONPATH"] = ""
        result = subprocess.run([sys.executable, "-S", "-c", script],
                                cwd=ROOT, env=env, capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "jazz")


if __name__ == "__main__":
    unittest.main()
