import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import shared_run


class SharedRunTests(unittest.TestCase):
    def test_directory_alone_is_not_persistent_storage(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaisesRegex(ValueError, 'disk is missing'):
                shared_run.preflight(Path(d), '')

    def test_mount_must_match_exactly(self):
        info = '1 2 3:4 / /var/database rw - ext4 /dev/x rw\n'
        self.assertFalse(shared_run.mounted(Path('/var/data'), info))
        self.assertTrue(shared_run.mounted(Path('/var/database'), info))

    def test_valid_mount_writes_only_own_directory(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            info = f'1 2 3:4 / {d} rw - ext4 /dev/x rw\n'
            data = shared_run.preflight(root, info)
            self.assertEqual(data, root / 'fomo-lab')
            self.assertEqual(list(data.iterdir()), [])

    def test_full_disk_refuses_start(self):
        with tempfile.TemporaryDirectory() as d:
            info = f'1 2 3:4 / {d} rw - ext4 /dev/x rw\n'
            with patch('shared_run.shutil.disk_usage') as usage:
                usage.return_value.free = 1
                with self.assertRaisesRegex(ValueError, '128 MiB'):
                    shared_run.preflight(Path(d), info)
