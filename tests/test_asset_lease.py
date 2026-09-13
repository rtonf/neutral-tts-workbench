import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from neutral_tts.asset_lease import AssetLease
from neutral_tts.fs_safety import FilesystemBoundaryError


@unittest.skipUnless(os.name == "nt", "Windows mandatory file sharing semantics")
class AssetLeaseTests(unittest.TestCase):
    def test_another_process_cannot_replace_or_write_admitted_bytes(self):
        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / "asset.bin"
            path.write_bytes(b"fixed")
            code = "import pathlib,sys;p=pathlib.Path(sys.argv[1]);blocked=0\nfor op in (lambda:p.write_bytes(b'changed'),lambda:p.rename(p.with_suffix('.moved'))):\n try:op()\n except OSError:blocked+=1\nprint(blocked)"
            with AssetLease([path]):
                result = subprocess.run([sys.executable, "-I", "-c", code, str(path)], capture_output=True, text=True, check=True)
                self.assertEqual(result.stdout.strip(), "2")
                self.assertEqual(path.read_bytes(), b"fixed")
            path.write_bytes(b"released")
            self.assertEqual(path.read_bytes(), b"released")

    def test_preexisting_writer_prevents_admission_and_failed_lease_releases_handles(self):
        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / "asset.bin"
            path.write_bytes(b"fixed")
            with path.open("r+b"):
                with self.assertRaises(FilesystemBoundaryError):
                    with AssetLease([path]):
                        self.fail("A writable asset must not be admitted.")
            with AssetLease([path]):
                self.assertEqual(path.read_bytes(), b"fixed")
