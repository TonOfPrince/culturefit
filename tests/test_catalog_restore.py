"""Native independent app-root regression tests of exact private CSV restoration."""
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

import catalog_restore
from catalog_restore import RestoreError, restore_catalogue
from curated_catalog import COLUMNS


def make_csv(path: Path, name: str) -> bytes:
    with path.open("w", encoding="utf-8", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=COLUMNS)
        writer.writeheader()
        for lane, city in (("place", "Minneapolis"), ("brand", ""), ("artist", "")):
            writer.writerow({
                "lane": lane, "name": f"{name} {lane}", "city": city,
                "tags": "coffee;jazz", "source_url": f"https://example.org/{name}/{lane}",
                "source_label": "Operator supplied",
            })
    return path.read_bytes()


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class RestoreTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.backup = root / "snapshot.csv"
        self.active = root / "active.csv"
        self.safety = root / "safety.csv"
        self.saved = make_csv(self.backup, "old")
        self.current = make_csv(self.active, "new")

    def restore(self, **kwargs):
        parameters = {
            "expected_backup_sha256": digest(self.saved),
            "expected_current_sha256": digest(self.current),
            "confirm_replace": True,
        }
        parameters.update(kwargs)
        return restore_catalogue(self.backup, self.active, self.safety, **parameters)

    def unchanged(self):
        self.assertEqual(self.active.read_bytes(), self.current)
        self.assertEqual(self.backup.read_bytes(), self.saved)
        self.assertFalse(self.safety.exists())

    def test_restores_exact_bytes_with_private_safety_backup(self):
        result = self.restore()
        self.assertEqual(self.active.read_bytes(), self.saved)
        self.assertEqual(self.safety.read_bytes(), self.current)
        self.assertEqual(self.backup.read_bytes(), self.saved)
        self.assertEqual(stat.S_IMODE(self.active.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.safety.stat().st_mode), 0o600)
        self.assertEqual(result["restored_csv_sha256"], digest(self.saved))
        self.assertEqual(result["safety_backup_sha256"], digest(self.current))
        self.assertEqual((result["restored_records"], result["previous_records"]), (3, 3))
        self.assertEqual(result["provider_calls"], 0)
        self.assertIs(result["provider_independently_verified"], False)
        self.assertEqual(result["source_status"], "user_supplied_unverified")
        self.assertFalse(list(self.active.parent.glob(".catalogue-restore-*.tmp")))

    def test_requires_explicit_confirmation(self):
        with self.assertRaisesRegex(RestoreError, "confirm-replace"):
            self.restore(confirm_replace=False)
        self.unchanged()

    def test_requires_two_valid_64_character_hashes(self):
        for field in ("expected_backup_sha256", "expected_current_sha256"):
            for bad in ("", "a" * 63, "X" * 64, 20, None):
                with self.subTest(field=field, bad=bad), self.assertRaises(RestoreError):
                    self.restore(**{field: bad})
        self.unchanged()

    def test_refuses_wrong_backup_digest(self):
        with self.assertRaisesRegex(RestoreError, "backup SHA-256"):
            self.restore(expected_backup_sha256="f" * 64)
        self.unchanged()

    def test_refuses_wrong_current_digest(self):
        with self.assertRaisesRegex(RestoreError, "current catalogue SHA-256"):
            self.restore(expected_current_sha256="f" * 64)
        self.unchanged()

    def test_refuses_identical_backup_and_current(self):
        self.active.write_bytes(self.saved)
        with self.assertRaisesRegex(RestoreError, "already contains"):
            self.restore(expected_current_sha256=digest(self.saved))
        self.assertFalse(self.safety.exists())

    def test_refuses_missing_or_invalid_backup(self):
        self.backup.unlink()
        with self.assertRaises(RestoreError):
            self.restore()
        self.assertEqual(self.active.read_bytes(), self.current)
        self.assertFalse(self.safety.exists())
        self.backup.write_text("invalid csv", encoding="utf-8")
        with self.assertRaises(RestoreError):
            self.restore(expected_backup_sha256=digest(self.backup.read_bytes()))
        self.assertEqual(self.active.read_bytes(), self.current)

    def test_refuses_missing_or_invalid_current(self):
        self.active.unlink()
        with self.assertRaises(RestoreError):
            self.restore()
        self.assertFalse(self.safety.exists())
        self.active.write_text("invalid csv", encoding="utf-8")
        with self.assertRaises(RestoreError):
            self.restore(expected_current_sha256=digest(self.active.read_bytes()))
        self.assertFalse(self.safety.exists())

    def test_refuses_source_symlink(self):
        real = self.backup.with_name("real.csv")
        self.backup.rename(real)
        self.backup.symlink_to(real)
        with self.assertRaisesRegex(RestoreError, "symlink"):
            self.restore()
        self.assertEqual(self.active.read_bytes(), self.current)
        self.assertFalse(self.safety.exists())

    def test_refuses_target_symlink(self):
        real = self.active.with_name("real-current.csv")
        self.active.rename(real)
        self.active.symlink_to(real)
        with self.assertRaisesRegex(RestoreError, "symlink"):
            self.restore()
        self.assertEqual(real.read_bytes(), self.current)
        self.assertFalse(self.safety.exists())

    def test_refuses_safety_backup_symlink_or_existing_file(self):
        other = self.active.parent / "protected.txt"
        other.write_text("do not replace", encoding="utf-8")
        self.safety.symlink_to(other)
        with self.assertRaisesRegex(RestoreError, "symlink"):
            self.restore()
        self.assertEqual(other.read_text(encoding="utf-8"), "do not replace")
        self.safety.unlink()
        self.safety.write_text("old", encoding="utf-8")
        with self.assertRaisesRegex(RestoreError, "already exists"):
            self.restore()
        self.assertEqual(self.safety.read_text(encoding="utf-8"), "old")
        self.assertEqual(self.active.read_bytes(), self.current)

    def test_refuses_hardlink_alias_and_colliding_paths(self):
        self.active.unlink()
        os.link(self.backup, self.active)
        with self.assertRaises(RestoreError):
            self.restore(expected_current_sha256=digest(self.saved))
        self.assertFalse(self.safety.exists())
        with self.assertRaises(RestoreError):
            restore_catalogue(self.backup, self.backup, self.safety,
                              expected_backup_sha256=digest(self.saved),
                              expected_current_sha256=digest(self.saved), confirm_replace=True)

    def test_refuses_current_file_changed_after_safety_backup(self):
        original_backup = catalog_restore.backup_catalogue
        other = make_csv(self.active.parent / "other.csv", "later")
        def mutate_after_backup(*args, **kwargs):
            report = original_backup(*args, **kwargs)
            self.active.write_bytes(other)
            return report
        with patch.object(catalog_restore, "backup_catalogue", side_effect=mutate_after_backup):
            with self.assertRaisesRegex(RestoreError, "changed before replacement"):
                self.restore()
        self.assertEqual(self.active.read_bytes(), other)
        self.assertEqual(self.safety.read_bytes(), self.current)
        self.assertFalse(list(self.active.parent.glob(".catalogue-restore-*.tmp")))

    def test_refuses_backup_changed_after_safety_backup(self):
        original_backup = catalog_restore.backup_catalogue
        other = make_csv(self.active.parent / "other.csv", "later")
        def mutate_source(*args, **kwargs):
            report = original_backup(*args, **kwargs)
            self.backup.write_bytes(other)
            return report
        with patch.object(catalog_restore, "backup_catalogue", side_effect=mutate_source):
            with self.assertRaisesRegex(RestoreError, "changed before replacement"):
                self.restore()
        self.assertEqual(self.active.read_bytes(), self.current)
        self.assertEqual(self.safety.read_bytes(), self.current)

    def test_native_separate_process_cli_confirmation_and_hash_fence(self):
        root = Path(__file__).resolve().parents[1]
        command = [
            sys.executable, "-S", str(root / "catalog_restore.py"),
            "--backup", str(self.backup), "--catalog", str(self.active),
            "--safety-backup", str(self.safety),
            "--expect-backup-sha256", digest(self.saved),
            "--expect-current-sha256", digest(self.current),
        ]
        env = {**os.environ, "PYTHONPATH": ""}
        no = subprocess.run(command, cwd=root, env=env, capture_output=True, text=True)
        self.assertEqual(no.returncode, 2)
        self.unchanged()
        yes = subprocess.run(command + ["--confirm-replace"], cwd=root, env=env,
                             capture_output=True, text=True)
        self.assertEqual(yes.returncode, 0, yes.stderr)
        self.assertEqual(json.loads(yes.stdout)["restored_csv_sha256"], digest(self.saved))
        self.assertEqual(self.active.read_bytes(), self.saved)
        self.assertEqual(self.safety.read_bytes(), self.current)
        second = subprocess.run(command + ["--confirm-replace"], cwd=root, env=env,
                                capture_output=True, text=True)
        self.assertEqual(second.returncode, 2)
        self.assertEqual(self.active.read_bytes(), self.saved)


if __name__ == "__main__":
    unittest.main()
