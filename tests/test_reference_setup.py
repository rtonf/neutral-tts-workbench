from __future__ import annotations

import json
import tempfile
import unittest
import wave
import zipfile
from pathlib import Path
from unittest.mock import patch

from neutral_tts.backup_restore import _DataRootOwner, BackupRestoreBusy
from neutral_tts.desktop_worker import dispatch
from neutral_tts.filename_templates import DEFAULT_TEMPLATE
from neutral_tts.local_runtime import LocalRuntimeError
from neutral_tts.reference_setup import prepare_references, current_references, SCOPES
from neutral_tts.workbench import Workbench, WorkbenchError


class ReferenceSetupTests(unittest.TestCase):
    def fixture(self, root):
        source = root / "synthetic.wav"
        with wave.open(str(source), "wb") as out:
            out.setparams((1, 2, 48000, 0, "NONE", "not compressed"))
            out.writeframes(b"\0\0" * 4800)
        return {"reference_paths": [str(source)],
                "permissions": {k: "Synthetic fixture, private functional test only." for k in SCOPES},
                "acknowledged": {k: True for k in SCOPES}}

    def test_worker_registration_restart_copy_and_backup_exclusion(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            args = self.fixture(root)
            source = Path(args["reference_paths"][0])
            original = source.read_bytes()
            reply = dispatch({"operation": "prepare_references", "data_root": str(root / "data"), "arguments": args})
            self.assertEqual(source.read_bytes(), original)
            copied = Path(reply["references"][0]["path"])
            self.assertNotEqual(copied, source)
            self.assertEqual(copied.read_bytes(), original)
            fresh = current_references(root / "data")
            self.assertEqual(fresh["permissions"], args["permissions"])
            self.assertFalse(fresh["inference_ready"])
            record = json.loads(Path(fresh["selection_file"]).read_text())
            self.assertNotIn("reviewed", record)
            self.assertIs(record["public_distribution_granted"], False)
            workbench = Workbench(root / "data")
            workbench.configure({"output_directory": str(root / "output"), "tokenizer_config": "",
                                 "runtime_config": "", "title": "audio", "template": DEFAULT_TEMPLATE,
                                 "pause_seconds": "0"}, [])
            workbench.backup(root / "backup.zip")
            with zipfile.ZipFile(root / "backup.zip") as archive:
                self.assertFalse(any("local-voice" in n or n.endswith(".wav") for n in archive.namelist()))
            copied.write_bytes(b"changed")
            with self.assertRaises(LocalRuntimeError):
                current_references(root / "data")

    def test_missing_scope_or_confirmation_is_not_inferred(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for scope in SCOPES:
                args = self.fixture(root)
                args["acknowledged"][scope] = False
                with self.assertRaises(WorkbenchError):
                    prepare_references(root / "data", **args)
                args["acknowledged"][scope] = True
                args["permissions"][scope] = ""
                with self.assertRaises(WorkbenchError):
                    prepare_references(root / "data", **args)
            self.assertFalse((root / "data/local-voice/active.json").exists())

    def test_failed_replacement_keeps_previous_voice_selection(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            args = self.fixture(root)
            prepare_references(root / "data", **args)
            pointer = root / "data/local-voice/active.json"
            previous = pointer.read_bytes()
            with patch("neutral_tts.reference_setup.open_exclusive_binary", side_effect=OSError("synthetic disk full")):
                with self.assertRaises(OSError):
                    prepare_references(root / "data", **args)
            self.assertEqual(pointer.read_bytes(), previous)
            self.assertTrue(current_references(root / "data")["configured"])

    def test_generation_restore_and_reference_setup_share_owner(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            args = self.fixture(root)
            data = Workbench(root / "data").root
            with _DataRootOwner(data):
                with self.assertRaises(BackupRestoreBusy):
                    prepare_references(data, **args)
            self.assertFalse((data / "local-voice/active.json").exists())

    def test_permission_evidence_is_copied_and_revalidated(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            args = self.fixture(root)
            evidence = root / "fixture-permission.txt"
            evidence.write_text("Synthetic test evidence only.")
            args["evidence_path"] = str(evidence)
            reply = prepare_references(root / "data", **args)
            private = Path(reply["selection_file"]).parent / "permission-evidence.bin"
            self.assertEqual(private.read_bytes(), evidence.read_bytes())
            private.write_bytes(b"changed")
            with self.assertRaises(LocalRuntimeError):
                current_references(root / "data")


if __name__ == "__main__":
    unittest.main()
