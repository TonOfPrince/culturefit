"""Native filesystem and separate-process CLI regression tests for record removal."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import catalog_remove
from catalog_remove import RemoveError, remove_candidate
from curated_catalog import read_catalog

HEADER = b'lane,name,city,tags,source_url,source_label\n'
FIRST = b'place,Hall,Minneapolis,jazz;coffee,https://source.example/hall,Original note\n'
SECOND = b'brand,Ceramics Co,,art;design,https://source.example/brand,Operator source\n'
THIRD = b'artist,Singer,,jazz,https://source.example/artist,Artist website\n'
DATA = HEADER + FIRST + SECOND + THIRD


def sha(content):
    return hashlib.sha256(content).hexdigest()


class CatalogueRemoveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.source = Path(self.tmp.name) / 'catalog.csv'
        self.backup = Path(self.tmp.name) / 'safety.csv'
        self.source.write_bytes(DATA)
        self.args = {'safety_backup': self.backup, 'expected_sha256': sha(DATA),
                     'lane': 'place', 'name': 'Hall', 'city': 'Minneapolis',
                     'confirm_remove': True}

    def remove(self, **changes):
        return remove_candidate(self.source, **{**self.args, **changes})

    def test_removes_exact_record_preserving_all_other_bytes(self):
        result = self.remove()
        self.assertEqual(self.source.read_bytes(), HEADER + SECOND + THIRD)
        self.assertEqual(self.backup.read_bytes(), DATA)
        self.assertEqual((result['previous_record_count'], result['current_record_count']), (3, 2))
        self.assertEqual(result['removed'], {'lane':'place','name':'Hall','city':'Minneapolis'})
        self.assertEqual(result['previous_csv_sha256'], sha(DATA))
        self.assertEqual(result['current_csv_sha256'], sha(self.source.read_bytes()))
        self.assertEqual(result['provider_calls'], 0)
        self.assertIs(result['provider_independently_verified'], False)
        self.assertEqual(stat.S_IMODE(self.source.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.backup.stat().st_mode), 0o600)
        self.assertFalse(list(self.source.parent.glob('.catalogue-remove-*.csv')))

    def test_bom_blank_lines_and_crlf_untouched(self):
        data = b'\xef\xbb\xbf' + HEADER.replace(b'\n', b'\r\n') + b'\r\n' + FIRST.replace(b'\n',b'\r\n') + b'\r\n' + SECOND.replace(b'\n',b'\r\n') + THIRD.replace(b'\n',b'\r\n')
        self.source.write_bytes(data)
        self.remove(expected_sha256=sha(data))
        remaining = b'\xef\xbb\xbf' + HEADER.replace(b'\n', b'\r\n') + b'\r\n' + b'\r\n' + SECOND.replace(b'\n',b'\r\n') + THIRD.replace(b'\n',b'\r\n')
        self.assertEqual(self.source.read_bytes(), remaining)
        self.assertEqual(self.backup.read_bytes(), data)

    def test_quoted_commas_preserve_surviving_source_bytes(self):
        first = b'place,"Hall, North",Minneapolis,jazz,https://source.example/hall,"Curator, Jane"\n'
        data = HEADER + first + SECOND + THIRD
        self.source.write_bytes(data)
        self.remove(expected_sha256=sha(data), name='hall, north')
        self.assertEqual(self.source.read_bytes(), HEADER + SECOND + THIRD)
        self.assertEqual(self.backup.read_bytes(), data)

    def test_remove_brand_with_empty_city(self):
        result = self.remove(lane='brand', name='CERAMICS CO', city='')
        self.assertEqual(result['removed']['name'], 'Ceramics Co')
        self.assertEqual(self.source.read_bytes(), HEADER + FIRST + THIRD)
        self.assertEqual(len(read_catalog(self.source)), 2)

    def test_no_confirmation_or_stale_digest(self):
        for inputs in ({'confirm_remove':False}, {'confirm_remove': 1},
                       {'expected_sha256': '0'*64}, {'expected_sha256': 'ZZ'}):
            with self.subTest(inputs=inputs), self.assertRaises(RemoveError):
                self.remove(**inputs)
        self.assertEqual(self.source.read_bytes(), DATA)
        self.assertFalse(self.backup.exists())

    def test_missing_identity_is_rejected(self):
        for inputs in ({'name':'unlisted'}, {'lane':'museum'},
                       {'city':'Madison'}, {'city': ''}, {'name': ''}):
            with self.subTest(inputs=inputs), self.assertRaises(RemoveError):
                self.remove(**inputs)
        self.assertEqual(self.source.read_bytes(), DATA)
        self.assertFalse(self.backup.exists())

    def test_existing_backup_and_symlink_target_not_overwritten(self):
        self.backup.write_bytes(b'keep this backup')
        with self.assertRaises(RemoveError): self.remove()
        self.assertEqual(self.backup.read_bytes(), b'keep this backup')
        self.backup.unlink()
        original = Path(self.tmp.name) / 'original.txt'
        original.write_bytes(b'private')
        self.backup.symlink_to(original)
        with self.assertRaises(RemoveError): self.remove()
        self.assertEqual(original.read_bytes(), b'private')
        self.assertEqual(self.source.read_bytes(), DATA)

    def test_source_symlink_or_missing_rejected(self):
        alias = Path(self.tmp.name) / 'alias.csv'
        alias.symlink_to(self.source)
        with self.assertRaises(RemoveError): remove_candidate(alias, **self.args)
        self.source.unlink()
        with self.assertRaises(RemoveError): self.remove()
        self.assertFalse(self.backup.exists())

    def test_existing_malformed_csv_is_not_modified(self):
        data = b'wrong,columns\na,b\n'
        self.source.write_bytes(data)
        with self.assertRaises(RemoveError): self.remove(expected_sha256=sha(data))
        self.assertEqual(self.source.read_bytes(), data)
        self.assertFalse(self.backup.exists())

    def test_changed_during_initial_validation_fails_closed(self):
        original = catalog_remove._read_bounded_bytes
        count = 0
        def read_change(path):
            nonlocal count
            count += 1
            if count == 2:
                with self.source.open('ab') as fd: fd.write(b'changed')
            return original(path)
        with patch.object(catalog_remove, '_read_bounded_bytes', side_effect=read_change):
            with self.assertRaises(RemoveError): self.remove()
        self.assertFalse(self.backup.exists())

    def test_changed_during_backup_fails_without_replacing(self):
        original_backup = catalog_remove.backup_catalogue
        def changed_backup(source, backup):
            with self.source.open('ab') as fd: fd.write(b'changed')
            return original_backup(source, backup)
        with patch.object(catalog_remove, 'backup_catalogue', side_effect=changed_backup):
            with self.assertRaises(RemoveError): self.remove()
        self.assertTrue(self.source.read_bytes().startswith(DATA))

    def test_cli_actual_separate_process(self):
        root = Path(__file__).resolve().parents[1]
        command = [sys.executable, '-S', str(root/'catalog_remove.py'),
                   '--catalog',str(self.source),'--safety-backup',str(self.backup),
                   '--expect-current-sha256',sha(DATA), '--lane','place',
                   '--name','Hall','--city','Minneapolis','--confirm-remove']
        env={**os.environ, 'PYTHONPATH':''}
        run=subprocess.run(command,cwd=root,env=env,capture_output=True,text=True)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn('"current_record_count": 2', run.stdout)
        self.assertEqual(self.source.read_bytes(), HEADER+SECOND+THIRD)
        self.assertEqual(self.backup.read_bytes(), DATA)
        again=subprocess.run(command,cwd=root,env=env,capture_output=True,text=True)
        self.assertEqual(again.returncode,2)
        self.assertEqual(again.stdout,'')

    def test_absent_file_after_read_fails_closed(self):
        original = catalog_remove.read_catalog
        def vanish(path):
            records=original(path)
            self.source.unlink()
            return records
        with patch.object(catalog_remove,'read_catalog',side_effect=vanish):
            with self.assertRaises(RemoveError): self.remove()
        self.assertFalse(self.backup.exists())

if __name__=='__main__':
    unittest.main()
