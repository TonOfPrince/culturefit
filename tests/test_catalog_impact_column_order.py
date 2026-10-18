"""Native regressions for accepted noncanonical CSV header column order."""
from __future__ import annotations

import csv
import itertools
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from catalog_impact import record_impact
from curated_catalog import COLUMNS, read_catalog

ROOT = Path(__file__).resolve().parents[1]
ORIGINAL = dict(zip(COLUMNS, ("brand", "Existing Brand", "", "coffee",
                              "https://operator.example/brand", "Operator A")))
PROPOSED = dict(zip(COLUMNS, ("place", "New Venue", "Minneapolis", "jazz;coffee",
                              "https://operator.example/new", "Operator B")))


class ImpactColumnOrderTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / "sources.csv"

    def catalogue(self, fields, *, bom=False, crlf=False, quoted=False):
        newline = "\r\n" if crlf else "\n"
        with self.path.open("w", encoding="utf-8-sig" if bom else "utf-8",
                            newline="") as stream:
            writer = csv.DictWriter(
                stream, fieldnames=fields, lineterminator=newline,
                quoting=csv.QUOTE_ALL if quoted else csv.QUOTE_MINIMAL)
            writer.writeheader()
            writer.writerow(ORIGINAL)
        before = self.path.read_bytes()
        self.assertEqual(len(read_catalog(self.path)), 1)
        return before

    def preview(self):
        return record_impact(self.path, row=PROPOSED, city="Minneapolis",
                             interests="jazz,coffee", per_lane=2)

    def assert_original_identity(self, result, original):
        self.assertEqual(result["proposed_candidate"]["name"], PROPOSED["name"])
        self.assertEqual(result["proposed_candidate"]["city"], PROPOSED["city"])
        self.assertEqual(result["proposed_candidate"]["source_url"], PROPOSED["source_url"])
        self.assertEqual(result["candidate_rank_within_top"], 1)
        self.assertEqual(result["provider_calls"], 0)
        self.assertFalse(result["provider_independently_verified"])
        self.assertEqual(self.path.read_bytes(), original)

    def test_permuted_header_preserves_operator_candidate_identity(self):
        before = self.catalogue(("lane", "city", "name", "tags",
                                 "source_url", "source_label"))
        self.assert_original_identity(self.preview(), before)

    def test_all_720_parser_supported_header_orders_preserve_identity(self):
        for fields in itertools.permutations(COLUMNS):
            with self.subTest(fields=fields):
                before = self.catalogue(fields)
                self.assert_original_identity(self.preview(), before)

    def test_bom_crlf_quoted_permuted_header(self):
        fields = ("source_label", "city", "lane", "source_url", "tags", "name")
        before = self.catalogue(fields, bom=True, crlf=True, quoted=True)
        self.assert_original_identity(self.preview(), before)

    def test_native_root_only_cli_reports_same_authored_identity(self):
        before = self.catalogue(("lane", "city", "name", "tags",
                                 "source_url", "source_label"))
        command = [sys.executable, "-S", str(ROOT / "catalog_impact.py"),
                   "--catalog", str(self.path), "--lane", "place",
                   "--name", PROPOSED["name"],
                   "--candidate-city", PROPOSED["city"],
                   "--tags", PROPOSED["tags"],
                   "--source-url", PROPOSED["source_url"],
                   "--source-label", PROPOSED["source_label"],
                   "--city", "Minneapolis", "--interests", "jazz,coffee"]
        env = {**os.environ, "PYTHONPATH": ""}
        result = subprocess.run(command, cwd=ROOT, env=env,
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_original_identity(json.loads(result.stdout), before)


if __name__ == "__main__":
    unittest.main()
