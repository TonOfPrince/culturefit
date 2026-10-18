"""Real local-file bounded-read regressions for single-record catalogue append."""
from __future__ import annotations

import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import catalog_add
from catalog_add import CatalogueAddError, append_candidate
from curated_catalog import MAX_CATALOG_BYTES

DATA = (b"lane,name,city,tags,source_url,source_label\n"
        b"place,Hall,Minneapolis,jazz,https://source.example/hall,Original note\n")


class AppendReadBoundsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.catalog = Path(temporary.name) / "operator.csv"
        self.catalog.write_bytes(DATA)
        self.opts = dict(
            expected_sha256=hashlib.sha256(DATA).hexdigest(),
            lane="brand", name="Ceramics Co", city="", tags="art;design",
            source_url="https://source.example/brand",
            source_label="Operator source", confirm_add=True,
        )

    def append(self):
        return append_candidate(self.catalog, **self.opts)

    def test_normal_append_avoids_every_unbounded_path_read(self):
        with patch.object(Path, "read_bytes", side_effect=AssertionError("unbounded read")):
            result = self.append()
        with self.catalog.open("rb") as source:
            after = source.read()
        self.assertEqual(result["record_count"], 2)
        self.assertTrue(after.startswith(DATA))
        self.assertIn(b"Ceramics Co", after)

    def test_growth_between_stat_and_initial_read_is_bounded(self):
        initial = catalog_add._read_bounded_bytes
        calls = 0

        def grow_before_read(path):
            nonlocal calls
            calls += 1
            if calls == 1:
                with self.catalog.open("ab") as source:
                    source.truncate(MAX_CATALOG_BYTES + 4096)
            return initial(path)

        with patch.object(catalog_add, "_read_bounded_bytes", side_effect=grow_before_read):
            with patch.object(Path, "read_bytes", side_effect=AssertionError("unbounded read")):
                with self.assertRaises(CatalogueAddError):
                    self.append()
        self.assertGreater(self.catalog.stat().st_size, MAX_CATALOG_BYTES)
        self.assertFalse(list(self.catalog.parent.glob(".culturefit-add-*.csv")))

    def test_growth_during_validation_is_capped_and_rejected(self):
        parse = catalog_add.read_catalog

        def grow_after_parse(path):
            records = parse(path)
            if Path(path) == self.catalog:
                with self.catalog.open("ab") as source:
                    source.truncate(MAX_CATALOG_BYTES + 500)
            return records

        with patch.object(catalog_add, "read_catalog", side_effect=grow_after_parse):
            with patch.object(Path, "read_bytes", side_effect=AssertionError("unbounded read")):
                with self.assertRaisesRegex(CatalogueAddError, "changed during validation"):
                    self.append()
        self.assertGreater(self.catalog.stat().st_size, MAX_CATALOG_BYTES)
        self.assertFalse(list(self.catalog.parent.glob(".culturefit-add-*.csv")))

    def test_growth_before_replace_does_not_overwrite_source(self):
        parse = catalog_add.read_catalog

        def grow_after_temp_parse(path):
            records = parse(path)
            if Path(path) != self.catalog:
                with self.catalog.open("ab") as source:
                    source.truncate(MAX_CATALOG_BYTES + 500)
            return records

        with patch.object(catalog_add, "read_catalog", side_effect=grow_after_temp_parse):
            with patch.object(Path, "read_bytes", side_effect=AssertionError("unbounded read")):
                with self.assertRaisesRegex(CatalogueAddError, "changed before append"):
                    self.append()
        self.assertGreater(self.catalog.stat().st_size, MAX_CATALOG_BYTES)
        self.assertFalse(list(self.catalog.parent.glob(".culturefit-add-*.csv")))

    def test_stale_digest_keeps_source_unchanged(self):
        self.opts["expected_sha256"] = "0" * 64
        with self.assertRaisesRegex(CatalogueAddError, "SHA-256 differs"):
            self.append()
        self.assertEqual(self.catalog.read_bytes(), DATA)


if __name__ == "__main__":
    unittest.main()
