"""Native app-root tests for appending one real operator-authored record."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest

from catalog_add import CatalogueAddError, append_candidate
from curated_catalog import read_catalog

HEADER = b'lane,name,city,tags,source_url,source_label\n'
EXISTING = HEADER + b'place,Hall,Minneapolis,jazz,https://source.example/hall,Original note\n'


def sha(b):
    return hashlib.sha256(b).hexdigest()


class AddTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.catalog = Path(self.folder.name) / 'catalog.csv'
        self.catalog.write_bytes(EXISTING)
        self.opts = dict(
            expected_sha256=sha(EXISTING),lane='brand',name='Ceramics Co',city='',
            tags='art;design',source_url='https://source.example/brand',
            source_label='Operator source',confirm_add=True)
    def add(self, **changes):
        return append_candidate(self.catalog, **{**self.opts,**changes})
    def unchanged(self):
        self.assertEqual(self.catalog.read_bytes(),EXISTING)
        self.assertFalse(list(self.catalog.parent.glob('.culturefit-add-*.csv')))

    def test_append_genuine_row_and_keep_old_bytes_unchanged(self):
        before=read_catalog(self.catalog)
        outcome=self.add()
        after=read_catalog(self.catalog)
        self.assertEqual(after[:-1],before)
        self.assertEqual(after[-1].name,'Ceramics Co')
        self.assertTrue(self.catalog.read_bytes().startswith(EXISTING))
        self.assertEqual(outcome['record_count'],2)
        self.assertEqual(outcome['previous_csv_sha256'],sha(EXISTING))
        self.assertEqual(outcome['new_csv_sha256'],sha(self.catalog.read_bytes()))
        self.assertEqual(outcome['provider_calls'],0)
        self.assertFalse(outcome['provider_independently_verified'])
        self.assertEqual(outcome['source_status'],'user_supplied_unverified')
        self.assertEqual(stat.S_IMODE(self.catalog.stat().st_mode),0o600)
        self.assertFalse(list(self.catalog.parent.glob('.culturefit-add-*.csv')))

    def test_source_url_is_literal_not_retrieved(self):
        self.add(source_url='https://source.example/private-proof')
        self.assertEqual(read_catalog(self.catalog)[-1].source_url,'https://source.example/private-proof')

    def test_place_city_and_quoted_field_are_valid(self):
        self.add(lane='place',name='"North Arts"',city='St Paul',tags='gallery',
                 source_url='https://source.example/gallery',source_label='Curator "Jane"')
        row=read_catalog(self.catalog)[-1]
        self.assertEqual((row.lane,row.name,row.city),('place','"North Arts"','St Paul'))
        self.assertEqual(row.source_label,'Curator "Jane"')

    def test_header_only_existing_catalogue_can_receive_first_row(self):
        self.catalog.write_bytes(HEADER)
        result=self.add(expected_sha256=sha(HEADER))
        self.assertEqual(result['record_count'],1)
        self.assertEqual(len(read_catalog(self.catalog)),1)

    def test_no_final_newline_is_repaired_only_at_append_boundary(self):
        old=EXISTING.rstrip(b'\n')
        self.catalog.write_bytes(old)
        self.add(expected_sha256=sha(old))
        self.assertTrue(self.catalog.read_bytes().startswith(old+b'\n'))
        self.assertEqual(len(read_catalog(self.catalog)),2)

    def test_bom_is_kept_byte_exact(self):
        before=b'\xef\xbb\xbf'+EXISTING
        self.catalog.write_bytes(before)
        self.add(expected_sha256=sha(before))
        self.assertTrue(self.catalog.read_bytes().startswith(before))
        self.assertEqual(len(read_catalog(self.catalog)),2)

    def test_refuse_duplicate_identity_and_leave_input_unchanged(self):
        with self.assertRaises(CatalogueAddError):
            self.add(lane='place',name='HALL',city='MINNEAPOLIS')
        self.unchanged()

    def test_bad_url_tags_and_missing_city_fail_without_writes(self):
        for changes in ({'source_url':'http://example.com'}, {'tags':''},
                        {'lane':'place','city':''},{'name':''},
                        {'source_url':'https://user:secret@example.com'}):
            with self.subTest(changes=changes),self.assertRaises(CatalogueAddError):
                self.add(**changes)
            self.unchanged()

    def test_required_confirmation_and_exact_hash(self):
        with self.assertRaisesRegex(CatalogueAddError,'confirm-add'):
            self.add(confirm_add=False)
        with self.assertRaisesRegex(CatalogueAddError,'SHA-256'):
            self.add(expected_sha256='0'*64)
        self.unchanged()

    def test_refuse_missing_catalogue_and_symlink(self):
        self.catalog.unlink()
        with self.assertRaises(CatalogueAddError):self.add()
        self.catalog.symlink_to(Path(self.folder.name)/'elsewhere.csv')
        with self.assertRaises(CatalogueAddError):self.add()
        self.assertTrue(self.catalog.is_symlink())

    def test_native_separate_process_cli_success_and_no_overwrite(self):
        root=Path(__file__).resolve().parents[1]
        command=[sys.executable,'-S',str(root/'catalog_add.py'),'--catalog',str(self.catalog),
                 '--expect-current-sha256',sha(EXISTING),'--lane','brand','--name','Ceramics Co',
                 '--tags','art;design','--source-url','https://source.example/brand',
                 '--source-label','Operator source','--confirm-add']
        env=os.environ.copy();env['PYTHONPATH']=''
        ok=subprocess.run(command,cwd=root,env=env,capture_output=True,text=True,check=False)
        self.assertEqual(ok.returncode,0,ok.stderr)
        self.assertEqual(json.loads(ok.stdout)['record_count'],2)
        # Reusing original expected hash must not overwrite changed catalogue.
        bad=subprocess.run(command,cwd=root,env=env,capture_output=True,text=True,check=False)
        self.assertEqual(bad.returncode,2)
        self.assertEqual(bad.stdout,'')
        self.assertEqual(len(read_catalog(self.catalog)),2)

    def test_invalid_existing_header_does_not_rewrite(self):
        broken=b'wrong,headers\nfoo,bar\n'
        self.catalog.write_bytes(broken)
        with self.assertRaises(CatalogueAddError):
            self.add(expected_sha256=sha(broken))
        self.assertEqual(self.catalog.read_bytes(),broken)

if __name__=='__main__': unittest.main()
