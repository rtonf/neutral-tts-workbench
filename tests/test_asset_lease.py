import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from neutral_tts.asset_lease import AssetLease
from neutral_tts.fs_safety import FilesystemBoundaryError


@unittest.skipUnless(os.name == "nt", "Windows mandatory file sharing semantics")
class AssetLeaseTests(unittest.TestCase):
    def test_short_name_lease_preserves_exclusive_write_and_target_checks(self):
        import ctypes
        from ctypes import wintypes
        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / "asset-long-filename.bin"
            path.write_bytes(b"fixed")
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            short_name = kernel.GetShortPathNameW
            short_name.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
            short_name.restype = wintypes.DWORD
            size = short_name(str(path), None, 0)
            self.assertGreater(size, 0)
            buffer = ctypes.create_unicode_buffer(size + 1)
            self.assertGreater(short_name(str(path), buffer, len(buffer)), 0)
            alias = Path(buffer.value)
            self.assertTrue(alias.samefile(path))
            with AssetLease([alias]):
                with self.assertRaises(OSError):
                    path.write_bytes(b"changed")
                with self.assertRaises(OSError):
                    path.rename(path.with_suffix(".moved"))
                self.assertEqual(alias.read_bytes(), b"fixed")
            with alias.open("r+b"):
                with self.assertRaises(FilesystemBoundaryError):
                    with AssetLease([alias]):
                        self.fail("A writable short-name asset must not be admitted.")
            with patch("neutral_tts.asset_lease._windows_final_path", return_value=str(path.with_name("different.bin"))):
                with self.assertRaises(FilesystemBoundaryError):
                    with AssetLease([alias]):
                        self.fail("A different target must not be admitted.")
            path.write_bytes(b"released")
            self.assertEqual(alias.read_bytes(), b"released")

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
