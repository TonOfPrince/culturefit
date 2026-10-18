"""Run the actual root-only source catalogue bootstrap CLI, not mock output."""
from __future__ import annotations

import csv
from concurrent.futures import ThreadPoolExecutor
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from catalog_bootstrap import BootstrapError, COLUMNS, HEADER, create_catalogue

ROOT = Path(__file__).resolve().parents[1]


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.target = Path(temp.name) / 'sources.csv'

    def test_creates_exact_valid_header_no_fictional_rows(self):
        result = create_catalogue(self.target)
        self.assertEqual(self.target.read_bytes(), HEADER)
        rows = list(csv.DictReader(io.StringIO(HEADER.decode('ascii')), strict=True))
        self.assertEqual(rows, [])
        self.assertEqual(tuple(HEADER.decode().strip().split(',')), COLUMNS)
        self.assertEqual(result['record_count'], 0)
        self.assertEqual(result['catalogue_sha256'], hashlib.sha256(HEADER).hexdigest())
        self.assertEqual(result['state'], 'ready')
        self.assertEqual(result['lanes'], {'place':0,'brand':0,'artist':0})

    def test_private_permissions_and_unverified_provenance(self):
        result = create_catalogue(self.target)
        self.assertEqual(stat.S_IMODE(self.target.stat().st_mode), 0o600)
        self.assertEqual(result['source_status'], 'user_supplied_unverified')
        self.assertEqual(result['provider_calls'], 0)
        self.assertIs(result['provider_independently_verified'], False)

    def test_existing_file_never_overwritten(self):
        previous = b'owned,private,data\nhello,world,here\n'
        self.target.write_bytes(previous)
        with self.assertRaises(BootstrapError):
            create_catalogue(self.target)
        self.assertEqual(self.target.read_bytes(), previous)

    def test_second_create_after_success_rejected_without_mutation(self):
        create_catalogue(self.target)
        with self.assertRaises(BootstrapError):
            create_catalogue(self.target)
        self.assertEqual(self.target.read_bytes(), HEADER)

    def test_dangling_symlink_destination_refused(self):
        missing = self.target.parent/'not-there.csv'
        self.target.symlink_to(missing)
        with self.assertRaises(BootstrapError):
            create_catalogue(self.target)
        self.assertTrue(self.target.is_symlink())
        self.assertFalse(missing.exists())

    def test_missing_parent_and_symlink_parent_refused(self):
        with self.assertRaises(BootstrapError):
            create_catalogue(self.target.parent/'missing'/'sources.csv')
        real = self.target.parent/'actual'
        real.mkdir()
        link = self.target.parent/'linked'
        link.symlink_to(real, target_is_directory=True)
        with self.assertRaises(BootstrapError):
            create_catalogue(link/'sources.csv')
        self.assertFalse((real/'sources.csv').exists())

    def test_invalid_empty_path_rejected(self):
        for bad in ('', '   ', None, 99):
            with self.subTest(bad=bad), self.assertRaises(BootstrapError):
                create_catalogue(bad)

    def test_concurrent_same_path_has_one_winner_no_clobber(self):
        def run(_):
            try:
                create_catalogue(self.target)
                return 'created'
            except BootstrapError:
                return 'conflict'
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(sorted(pool.map(run, range(2))), ['conflict','created'])
        self.assertEqual(self.target.read_bytes(), HEADER)

    def test_write_failure_cleans_only_new_file(self):
        with patch('catalog_bootstrap.os.fsync', side_effect=OSError('synthetic failed fsync')):
            with self.assertRaises(BootstrapError):
                create_catalogue(self.target)
        self.assertFalse(self.target.exists())

    def test_native_cli_success_from_standalone_root_without_pythonpath(self):
        env = dict(os.environ, PYTHONPATH='')
        run = subprocess.run([sys.executable,'-S',str(ROOT/'catalog_bootstrap.py'),
                              '--output',str(self.target)],cwd=ROOT,env=env,
                             capture_output=True,text=True,check=False)
        self.assertEqual(run.returncode,0,run.stderr)
        payload=json.loads(run.stdout)
        self.assertEqual(payload['catalogue_sha256'],hashlib.sha256(HEADER).hexdigest())
        self.assertEqual(self.target.read_bytes(),HEADER)
        self.assertEqual(stat.S_IMODE(self.target.stat().st_mode),0o600)

    def test_native_cli_refuses_retry_and_outputs_no_json(self):
        env = dict(os.environ, PYTHONPATH='')
        args=[sys.executable,'-S',str(ROOT/'catalog_bootstrap.py'),'--output',str(self.target)]
        self.assertEqual(subprocess.run(args,cwd=ROOT,env=env,capture_output=True).returncode,0)
        failed=subprocess.run(args,cwd=ROOT,env=env,capture_output=True,text=True)
        self.assertEqual(failed.returncode,2)
        self.assertEqual(failed.stdout,'')
        self.assertIn('catalogue bootstrap error:',failed.stderr)
        self.assertEqual(self.target.read_bytes(),HEADER)

    def test_native_cli_missing_required_argument_is_nonzero(self):
        run=subprocess.run([sys.executable,'-S',str(ROOT/'catalog_bootstrap.py')],
                           cwd=ROOT,capture_output=True,text=True)
        self.assertNotEqual(run.returncode,0)
        self.assertFalse(self.target.exists())


if __name__=='__main__':
    unittest.main()
