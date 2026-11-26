import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import cloud_archive_v1 as C
from daily_review_v1 import _atomic_write

class CloudArchiveTests(unittest.TestCase):
    def tearDown(self): C.install(None)
    def test_python_replace_retains_prior_evidence_in_native_compatible_outbox(self):
        with tempfile.TemporaryDirectory() as root:
            C.install(root)
            file=Path(root,'single_leg_v1','positions.json');file.parent.mkdir()
            _atomic_write(str(file),b'{"position":"OPEN"}')
            _atomic_write(str(file),b'{"position":"CLOSED"}')
            queue=Path(root,C.DIRECTORY)
            self.assertEqual(next(queue.glob('*.bin')).read_bytes(),b'{"position":"OPEN"}')
            metadata=json.loads(next(queue.glob('*.json')).read_text())
            self.assertEqual(metadata['source_path'],'single_leg_v1/positions.json')
            self.assertEqual(file.read_bytes(),b'{"position":"CLOSED"}')
    def test_failed_history_archive_cannot_replace_the_original(self):
        with tempfile.TemporaryDirectory() as root:
            C.install(root); file=Path(root,'state.json');file.write_bytes(b'original')
            with patch.object(C.shutil,'disk_usage',return_value=type('Disk',(),{'free':0})()):
                with self.assertRaisesRegex(OSError,'PENDING_ARCHIVE_DISK_FULL'):
                    _atomic_write(str(file),b'changed')
            self.assertEqual(file.read_bytes(),b'original')
