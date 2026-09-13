import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from neutral_tts.inference_setup import _bytes_once, _download, provision_inference, _write_progress, read_progress
from neutral_tts.resilience import _atomic_json
from neutral_tts.workbench import WorkbenchError


class InferenceSetupTests(unittest.TestCase):
    def test_progress_observer_cannot_break_setup_and_final_snapshot_is_readable(self):
        import threading
        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / "setup-progress.json"
            _write_progress(path, "assets", 0, 100)
            errors, values = [], []
            done = threading.Event()
            def reader():
                while not done.is_set():
                    try:
                        value = read_progress(path)
                        if value is not None:
                            values.append(value)
                    except PermissionError:
                        pass  # An advisory observation may miss a snapshot.
                    except Exception as error:
                        errors.append(error)
            thread = threading.Thread(target=reader)
            thread.start()
            try:
                for index in range(1, 101):
                    _write_progress(path, "assets", index, 100)
            finally:
                done.set()
                thread.join(timeout=10)
            self.assertFalse(thread.is_alive())
            self.assertEqual(errors, [])
            self.assertTrue(values)
            _write_progress(path, "complete", 1, 1)
            self.assertEqual(read_progress(path), {"phase": "complete", "completed": 1, "total": 1})
            failure = PermissionError("sharing race")
            failure.winerror = 32
            with patch("neutral_tts.inference_setup.write_private", side_effect=failure):
                _write_progress(path, "assets", 1, 6)
            with patch("neutral_tts.inference_setup.write_private", side_effect=OSError("disk failure")):
                with self.assertRaises(OSError):
                    _write_progress(path, "assets", 1, 6)

    def test_download_failure_retry_and_existing_bytes(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            target = root / "asset.bin"
            data = b"reviewed fixture"
            record = {"url": "https://example.org/fixed", "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
            with self.assertRaises(WorkbenchError):
                _download(target, record, opener=lambda *a, **k: io.BytesIO(b"wrong"))
            self.assertFalse(target.exists())
            self.assertEqual(list(root.iterdir()), [])
            _download(target, record, opener=lambda *a, **k: io.BytesIO(data))
            _download(target, record, opener=lambda *a, **k: self.fail("Verified bytes must be reused."))
            self.assertEqual(target.read_bytes(), data)
            target.write_bytes(b"changed")
            with self.assertRaises(ValueError):
                _download(target, record, opener=lambda *a, **k: self.fail("Changed existing bytes must not be replaced."))

    def test_generated_file_failure_never_admits_partial_final_name(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            target = root / "derived.bin"
            with patch("neutral_tts.inference_setup.os.link", side_effect=OSError("disk failure")):
                with self.assertRaises(OSError):
                    _bytes_once(target, b"fixture")
            self.assertFalse(target.exists())
            self.assertEqual(list(root.iterdir()), [])
            _bytes_once(target, b"fixture")
            self.assertEqual(target.read_bytes(), b"fixture")
            with self.assertRaises(ValueError):
                _bytes_once(target, b"different")
            self.assertEqual(target.read_bytes(), b"fixture")

    def test_atomic_recovery_record_supports_long_destination_basename(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            parent = root / ("p" * max(1, 155 - len(str(root))))
            parent.mkdir()
            destination = parent / ("r" * 66 + ".reason.json")
            _atomic_json(destination, {"reason": "synthetic failure"})
            self.assertEqual(json.loads(destination.read_text()), {"reason": "synthetic failure"})
            self.assertEqual(list(parent.iterdir()), [destination])

    def test_conditions_rejected_before_folder_or_network_side_effects(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            with self.assertRaises(WorkbenchError):
                provision_inference(root / "data", setup_directory=root / "setup")
            self.assertEqual(list(root.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
