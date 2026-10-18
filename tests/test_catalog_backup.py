"""Original native tests for a private non-overwriting operator CSV backup."""
from __future__ import annotations

import csv
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from catalog_backup import BackupError, backup_catalogue
from curated_catalog import COLUMNS


class PrivateCatalogueBackupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.source = Path(self.tmp.name) / "source.csv"
        self.target = Path(self.tmp.name) / "backed.csv"
        with self.source.open("w", encoding="utf-8", newline="") as output:
            writer = csv.DictWriter(output, fieldnames=COLUMNS)
            writer.writeheader()
            writer.writerow({"lane":"place","name":"Local Place", "city":"Minneapolis",
                             "tags":"jazz;coffee", "source_url":"https://example.org/place",
                             "source_label":"Operator-provided page"})
            writer.writerow({"lane":"brand","name":"Local Brand", "city":"",
                             "tags":"coffee", "source_url":"https://example.org/brand",
                             "source_label":"Operator note"})
            writer.writerow({"lane":"artist","name":"Local Artist", "city":"",
                             "tags":"jazz", "source_url":"https://example.org/artist",
                             "source_label":"Operator note"})
        self.original = self.source.read_bytes()

    def test_private_backup_exact_bytes_and_metadata(self):
        result = backup_catalogue(self.source, self.target)
        self.assertEqual(self.target.read_bytes(), self.original)
        self.assertEqual(result["records"], 3)
        self.assertEqual(result["bytes"], len(self.original))
        self.assertEqual(result["csv_sha256"], hashlib.sha256(self.original).hexdigest())
        self.assertEqual(result["provider_calls"], 0)
        self.assertIs(result["provider_independently_verified"], False)
        self.assertEqual(result["source_status"], "user_supplied_unverified")
        self.assertEqual(stat.S_IMODE(self.target.stat().st_mode), 0o600)

    def test_destination_must_not_be_overwritten(self):
        self.target.write_bytes(b"existing private backup")
        with self.assertRaises(BackupError):
            backup_catalogue(self.source, self.target)
        self.assertEqual(self.target.read_bytes(), b"existing private backup")
        self.assertEqual(self.source.read_bytes(), self.original)
        self.assertFalse(list(self.target.parent.glob(".catalogue-backup-*.tmp")))

    def test_symlink_destination_is_not_overwritten(self):
        other = Path(self.tmp.name) / "original.txt"
        other.write_text("protected", encoding="utf-8")
        self.target.symlink_to(other)
        with self.assertRaises(BackupError):
            backup_catalogue(self.source, self.target)
        self.assertEqual(other.read_text(encoding="utf-8"), "protected")

    def test_no_same_source_or_symlink_alias(self):
        with self.assertRaises(BackupError):
            backup_catalogue(self.source, self.source)
        alias = Path(self.tmp.name) / "symlink-source.csv"
        alias.symlink_to(self.source)
        with self.assertRaises(BackupError):
            backup_catalogue(alias, self.target)
        self.assertFalse(self.target.exists())

    def test_invalid_input_and_missing_parent_leave_no_backup(self):
        self.source.write_text("lane,name\ninvalid,a\n", encoding="utf-8")
        with self.assertRaises(BackupError):
            backup_catalogue(self.source, self.target)
        self.assertFalse(self.target.exists())
        with self.assertRaises(BackupError):
            backup_catalogue(self.source, Path(self.tmp.name)/"missing-parent"/"out.csv")

    def test_changed_source_rejected_before_output(self):
        with patch("catalog_backup._read_bounded_bytes", side_effect=[self.original, self.original + b"changed"]):
            with self.assertRaisesRegex(BackupError, "changed"):
                backup_catalogue(self.source, self.target)
        self.assertFalse(self.target.exists())

    def test_backup_rejects_oversized_sparse_source(self):
        with self.source.open("wb") as stream:
            stream.truncate(2_000_001)
        with self.assertRaises(BackupError):
            backup_catalogue(self.source, self.target)
        self.assertFalse(self.target.exists())

    def test_separate_interpreter_cli_and_no_overwrite(self):
        root = Path(__file__).resolve().parents[1]
        env = {**os.environ, "PYTHONPATH": ""}
        command = [sys.executable, "-S", str(root/"catalog_backup.py"),
                   "--catalog", str(self.source), "--backup", str(self.target)]
        success = subprocess.run(command, cwd=root, env=env, capture_output=True, text=True)
        self.assertEqual(success.returncode, 0, success.stderr)
        self.assertEqual(json.loads(success.stdout)["records"], 3)
        self.assertEqual(self.target.read_bytes(), self.original)
        retry = subprocess.run(command, cwd=root, env=env, capture_output=True, text=True)
        self.assertEqual(retry.returncode, 2)
        self.assertEqual(retry.stdout, "")
        self.assertEqual(self.target.read_bytes(), self.original)


if __name__ == "__main__":
    unittest.main()
