"""Native offline tests for source-attributed CultureFit catalogue comparisons."""
from __future__ import annotations

import csv
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from catalog_diff import compare_files
from curated_catalog import CatalogError


def _write(path, rows):
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(("lane", "name", "city", "tags", "source_url", "source_label"))
        writer.writerows(rows)


class CatalogueDiffTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.previous = self.folder / "previous.csv"
        self.current = self.folder / "current.csv"
        self.original_rows = [
            ("place", "Jazz Club", "Minneapolis", "jazz;coffee", "https://example.org/old", "old source"),
            ("brand", "Coffee Co", "", "coffee", "https://example.org/b", "brand source"),
            ("artist", "Artist Gone", "", "jazz", "https://example.org/a", "artist source"),
        ]
        self.updated_rows = [
            ("place", "Jazz Club", "MINNEAPOLIS", "jazz;live", "https://example.org/new", "new source"),
            ("brand", "Coffee Co", "", "coffee", "https://example.org/b", "brand source"),
            ("artist", "Artist Added", "", "jazz", "https://example.org/newartist", "other source"),
        ]
        _write(self.previous, self.original_rows)
        _write(self.current, self.updated_rows)

    def report(self):
        return compare_files(self.previous, self.current)

    def test_exact_change_and_unchanged_counts(self):
        self.assertEqual(self.report()["counts"], {
            "previous": 3, "current": 3, "added": 1,
            "removed": 1, "changed": 1, "unchanged": 1,
        })

    def test_tags_and_attribution_changes_are_specific(self):
        record = self.report()["changed"][0]
        self.assertEqual(record["field_changes"]["added_tags"], ["live"])
        self.assertEqual(record["field_changes"]["removed_tags"], ["coffee"])
        self.assertEqual(record["field_changes"]["source_label"], {
            "before": "old source", "after": "new source",
        })
        self.assertEqual(record["before"]["source_url"], "https://example.org/old")
        self.assertEqual(record["after"]["source_url"], "https://example.org/new")

    def test_casefold_equivalent_city_retains_candidate_identity(self):
        record = self.report()["changed"][0]
        self.assertEqual(record["field_changes"]["city"], {
            "before": "Minneapolis", "after": "MINNEAPOLIS",
        })
        self.assertEqual(self.report()["counts"]["changed"], 1)

    def test_added_and_removed_keep_original_operator_sources(self):
        report = self.report()
        self.assertEqual(report["removed"][0]["source_url"], "https://example.org/a")
        self.assertEqual(report["added"][0]["source_label"], "other source")

    def test_source_status_does_not_claim_provider_verification(self):
        report = self.report()
        self.assertEqual(report["provider_calls"], 0)
        self.assertFalse(report["provider_independently_verified"])
        self.assertEqual(report["source_status"], "user_supplied_unverified")
        self.assertEqual(report["added"][0]["source_status"], "user_supplied_unverified")

    def test_report_fingerprints_input_file_bytes(self):
        report = self.report()
        self.assertEqual(report["previous_csv_sha256"],
                         hashlib.sha256(self.previous.read_bytes()).hexdigest())
        self.assertEqual(report["current_csv_sha256"],
                         hashlib.sha256(self.current.read_bytes()).hexdigest())

    def test_identical_snapshots_have_no_changes(self):
        report = compare_files(self.previous, self.previous)
        self.assertEqual(report["counts"]["changed"], 0)
        self.assertEqual(report["counts"]["unchanged"], 3)

    def test_row_and_tag_order_alone_are_not_changes(self):
        reordered = self.folder / "reordered.csv"
        _write(reordered, [
            self.original_rows[2], self.original_rows[1],
            ("place", "Jazz Club", "Minneapolis", "coffee;jazz",
             "https://example.org/old", "old source"),
        ])
        self.assertEqual(compare_files(self.previous, reordered)["counts"]["unchanged"], 3)

    def test_city_change_to_different_place_changes_identity(self):
        moved = self.folder / "moved.csv"
        _write(moved, [
            ("place", "Jazz Club", "Madison", "jazz;coffee",
             "https://example.org/old", "old source"),
            *self.original_rows[1:],
        ])
        result = compare_files(self.previous, moved)
        self.assertEqual((result["counts"]["added"], result["counts"]["removed"]), (1, 1))

    def test_native_cli_from_independent_root(self):
        result = subprocess.run([
            sys.executable, "-S", "catalog_diff.py",
            "--previous", str(self.previous), "--current", str(self.current),
        ], cwd=ROOT, env={**os.environ, "PYTHONPATH": ""},
            capture_output=True, text=True, timeout=6)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["counts"]["changed"], 1)

    def test_cli_invalid_input_returns_error_not_report(self):
        bad = self.folder / "bad.csv"
        bad.write_text("bad,headers\n1,2\n", encoding="utf-8")
        result = subprocess.run([
            sys.executable, "catalog_diff.py",
            "--previous", str(bad), "--current", str(self.current),
        ], cwd=ROOT, env={**os.environ, "PYTHONPATH": ""},
            capture_output=True, text=True, timeout=6)
        self.assertEqual(result.returncode, 2)
        self.assertIn("comparison error", result.stderr)
        self.assertFalse(result.stdout)

    def test_casefold_duplicate_identity_is_rejected(self):
        duplicate = self.folder / "duplicate.csv"
        _write(duplicate, [
            ("brand", "Beans", "", "coffee", "https://example.org/one", "operator"),
            ("brand", "BEANS", "", "coffee", "https://example.org/two", "operator"),
        ])
        with self.assertRaises(CatalogError):
            compare_files(self.previous, duplicate)

    def test_comparison_never_changes_input_csv(self):
        old = self.previous.read_bytes()
        new = self.current.read_bytes()
        self.report()
        self.assertEqual(self.previous.read_bytes(), old)
        self.assertEqual(self.current.read_bytes(), new)

    def test_two_empty_catalogues_produce_empty_diff(self):
        _write(self.previous, [])
        _write(self.current, [])
        result = self.report()
        self.assertEqual(result["counts"]["previous"], 0)
        self.assertEqual(result["counts"]["added"], 0)
        self.assertEqual(result["counts"]["changed"], 0)

    def test_missing_input_fails_closed(self):
        with self.assertRaises(CatalogError):
            compare_files(self.previous, self.folder / "nope.csv")

    def test_previous_snapshot_change_mid_parse_is_rejected(self):
        import catalog_diff
        original = catalog_diff.read_catalog

        def changed_while_parsing(path):
            rows = original(path)
            if Path(path) == self.previous:
                self.previous.write_text(
                    self.previous.read_text(encoding="utf-8").replace(
                        "Jazz Club", "Replaced Venue"), encoding="utf-8")
            return rows

        with patch.object(catalog_diff, "read_catalog", side_effect=changed_while_parsing):
            with self.assertRaisesRegex(CatalogError, "changed during comparison"):
                compare_files(self.previous, self.current)

    def test_current_snapshot_change_mid_parse_is_rejected(self):
        import catalog_diff
        original = catalog_diff.read_catalog

        def changed_while_parsing(path):
            rows = original(path)
            if Path(path) == self.current:
                self.current.write_text(
                    self.current.read_text(encoding="utf-8").replace(
                        "Artist Added", "Another Artist"), encoding="utf-8")
            return rows

        with patch.object(catalog_diff, "read_catalog", side_effect=changed_while_parsing):
            with self.assertRaisesRegex(CatalogError, "changed during comparison"):
                compare_files(self.previous, self.current)

    def test_same_path_is_one_consistent_snapshot(self):
        import catalog_diff
        original = catalog_diff.read_catalog
        observed = []

        def counted(path):
            observed.append(Path(path))
            return original(path)

        with patch.object(catalog_diff, "read_catalog", side_effect=counted):
            report = compare_files(self.previous, self.previous)
        self.assertEqual(observed, [self.previous])
        self.assertEqual(report["previous_csv_sha256"], report["current_csv_sha256"])
        self.assertEqual(report["counts"]["unchanged"], 3)

    def test_oversized_snapshot_is_rejected_before_parse(self):
        import curated_catalog
        self.previous.write_bytes(b"x" * (curated_catalog.MAX_CATALOG_BYTES + 1))
        with self.assertRaisesRegex(CatalogError, "size limit"):
            compare_files(self.previous, self.current)

    def test_oversized_sparse_snapshot_never_reads_unbounded_or_parses(self):
        import catalog_diff
        import curated_catalog
        with self.previous.open("wb") as stream:
            stream.truncate(curated_catalog.MAX_CATALOG_BYTES + 4096)
        with patch.object(Path, "read_bytes", side_effect=AssertionError("unbounded read")):
            with patch.object(catalog_diff, "read_catalog", side_effect=AssertionError("parsed oversized file")):
                with self.assertRaisesRegex(CatalogError, "size limit"):
                    compare_files(self.previous, self.current)

    def test_growth_after_parse_is_rejected_by_second_size_guard(self):
        import catalog_diff
        import curated_catalog
        parser = catalog_diff.read_catalog

        def grow_after_parse(path):
            records = parser(path)
            if Path(path) == self.previous:
                with self.previous.open("ab") as stream:
                    stream.truncate(curated_catalog.MAX_CATALOG_BYTES + 500)
            return records

        with patch.object(Path, "read_bytes", side_effect=AssertionError("unbounded read")):
            with patch.object(catalog_diff, "read_catalog", side_effect=grow_after_parse):
                with self.assertRaisesRegex(CatalogError, "size limit"):
                    compare_files(self.previous, self.current)

    def test_nonregular_fifo_is_rejected_without_blocking(self):
        if not hasattr(os, "mkfifo"):
            self.skipTest("FIFO not supported")
        pipe = self.folder / "source.pipe"
        os.mkfifo(pipe)
        with self.assertRaisesRegex(CatalogError, "regular file"):
            compare_files(pipe, self.current)


if __name__ == "__main__":
    unittest.main()
