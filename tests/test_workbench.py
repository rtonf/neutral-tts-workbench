from __future__ import annotations

import hashlib
import json
import os
import sys
import subprocess
import time
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from neutral_tts.application import StalePreviewError
from neutral_tts.backup_restore import _DataRootOwner, BackupRestoreBusy
from neutral_tts.desktop import WorkerProcess
from neutral_tts.filename_templates import DEFAULT_TEMPLATE
from neutral_tts.workbench import Workbench, WorkbenchError
from neutral_tts.tokenizer_runtime import load_tokenizer
from tests import test_local_runtime as runtime_tests
from tests.test_local_runtime import sha


class WorkbenchTests(unittest.TestCase):
    def configure(self, root):
        workbench = Workbench(root / "data")
        values = {"output_directory": str(root / "wav"), "tokenizer_config": "", "runtime_config": "",
                  "title": "audio", "template": DEFAULT_TEMPLATE, "pause_seconds": "0"}
        workbench.configure(values, [{"surface": "ABC", "reading": "alpha"}])
        return workbench, values

    def tokenizer_fixture(self, root):
        path, config, rights_path, rights = runtime_tests.LocalRuntimeTests().fixture(root)
        token_dir = root / "assets/tts/tokenizer"
        data = {"schema_version": "neutral-tokenizer-v1", "directory": str(token_dir),
                "files": [{"name": p.name, "size": p.stat().st_size, "sha256": sha(p.read_bytes())} for p in token_dir.iterdir()],
                "vocab_size": 1024, "bos_token_id": 1, "evidence": str(root / "evidence.txt"),
                "evidence_sha256": sha(b"synthetic fixture only"), "source": "https://example.invalid/tokenizer",
                "revision": "1" * 40, "use_scope": "local_tokenization"}
        token_path = root / "tokenizer.json"
        token_path.write_text(json.dumps(data), encoding="utf-8")
        return token_path, path

    def test_synthetic_desktop_path_backup_restore_and_external_wav_verification(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            workbench, _ = self.configure(root)
            preview = workbench.preview("ABC. synthetic", demo=True)
            result = workbench.generate("ABC. synthetic", demo=True)
            self.assertEqual(result["destination"], preview["destination"])
            self.assertTrue(result["outcome"]["demo"])
            self.assertEqual(len(Workbench(root / "data").startup()["history"]), 1)
            backup = root / "snapshot.zip"
            workbench.backup(backup)
            import zipfile
            with zipfile.ZipFile(backup) as archive:
                self.assertFalse(any(n.endswith(".wav") or "/staging/" in n for n in archive.namelist()))
            workbench.preview("second", demo=True)
            second = workbench.generate("second", demo=True)
            self.assertEqual(len(workbench.startup()["history"]), 2)
            restored = workbench.restore(backup)
            self.assertEqual(len(restored["history"]), 1)
            self.assertTrue(Path(second["destination"]).is_file())
            # A restore never upgrades missing or modified WAVs into playable history.
            Path(result["destination"]).write_bytes(b"changed synthetic data")
            restored = workbench.restore(backup)
            self.assertEqual(restored["history"], [])
            self.assertTrue(Path(second["destination"]).is_file())

    def test_real_tokenizer_preview_never_loads_model_and_generation_matches(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            token, runtime = self.tokenizer_fixture(root)
            workbench, values = self.configure(root)
            values.update(tokenizer_config=str(token), runtime_config=str(runtime))
            workbench.configure(values, [])
            events = []
            imports = runtime_tests.LocalRuntimeTests().fake_packages(events)
            with patch("importlib.import_module", side_effect=imports):
                preview = workbench.preview("synthetic source")
            self.assertEqual({kind for kind, _ in events}, {"tokenizer"})
            with patch("importlib.import_module", side_effect=imports):
                result = workbench.generate("synthetic source")
            self.assertEqual(result["destination"], preview["destination"])

    def test_preview_drift_rejected_without_generation(self):
        for drift in ("text", "dictionary", "destination", "config"):
            with self.subTest(drift=drift), tempfile.TemporaryDirectory() as name:
                root = Path(name)
                workbench, values = self.configure(root)
                preview = workbench.preview("ABC", demo=True)
                text = "ABC"
                if drift == "text":
                    text = "changed"
                elif drift == "dictionary":
                    workbench.configure(values, [{"surface": "ABC", "reading": "changed"}])
                elif drift == "destination":
                    Path(preview["destination"]).write_bytes(b"already owned")
                else:
                    values["pause_seconds"] = "0.5"
                    workbench.configure(values, [])
                with self.assertRaises(StalePreviewError):
                    workbench.generate(text, demo=True)

    def test_worker_process_roundtrip_environment_isolation_and_duplicate_rejection(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            workbench, _ = self.configure(root)
            before = dict(os.environ)
            worker = WorkerProcess(workbench.root)
            worker.start("preview", {"text": "synthetic", "demo": True})
            with self.assertRaises(RuntimeError):
                worker.start("startup", {})
            _, response = worker.events.get(timeout=15)
            self.assertEqual(response["status"], "ok", response)
            self.assertEqual(dict(os.environ), before)
            worker.start("generate", {"text": "synthetic", "demo": True})
            _, response = worker.events.get(timeout=15)
            self.assertEqual(response["status"], "ok", response)
            self.assertEqual(len(workbench.startup()["history"]), 1)

    def test_data_owner_blocks_all_product_operations(self):
        with tempfile.TemporaryDirectory() as name:
            workbench, values = self.configure(Path(name))
            with _DataRootOwner(workbench.root):
                worker = WorkerProcess(workbench.root)
                for operation, arguments in (("startup", {}), ("preview", {"text": "synthetic", "demo": True}),
                                             ("backup", {"archive": str(Path(name) / "backup.zip")}),
                                             ("configure", {"values": values, "rules": []})):
                    worker.start(operation, arguments)
                    _, response = worker.events.get(timeout=15)
                    self.assertEqual(response, {"status": "error", "code": "BUSY"})

    def test_tokenizer_file_swap_and_evidence_swap_are_rejected(self):
        for target in ("assets/tts/tokenizer/tokenizer.json", "evidence.txt"):
            with self.subTest(target=target), tempfile.TemporaryDirectory() as name:
                root = Path(name)
                token, _ = self.tokenizer_fixture(root)
                binding = load_tokenizer(token)
                (root / target).write_bytes(b"replaced")
                with self.assertRaises(ValueError):
                    binding.token_counter("synthetic")

    def test_process_death_during_product_generation_recovers_and_can_retry(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            workbench, _ = self.configure(root)
            workbench.preview("synthetic", demo=True)
            marker = root / "entered.txt"
            script = """
import sys,time
from pathlib import Path
from neutral_tts.demo_backend import DEMO_SILENCE_ONLY
from neutral_tts.workbench import Workbench
def interrupted(self, request):
    Path(sys.argv[2]).write_text('producing',encoding='utf-8')
    time.sleep(60)
DEMO_SILENCE_ONLY.generate=interrupted
Workbench(Path(sys.argv[1])).generate('synthetic',demo=True)
"""
            process = subprocess.Popen([sys.executable, "-B", "-c", script, str(workbench.root), str(marker)],
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                deadline = time.monotonic() + 10
                while not marker.exists() and process.poll() is None and time.monotonic() < deadline:
                    time.sleep(.02)
                self.assertTrue(marker.exists())
                process.terminate()
                process.wait(timeout=5)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)
            self.assertEqual(workbench.startup()["history"], [])
            self.assertFalse(list((root / "wav").glob("*.wav")))
            workbench.preview("retry", demo=True)
            workbench.generate("retry", demo=True)
            self.assertEqual(len(workbench.startup()["history"]), 1)

    def test_cancel_releases_worker_tree_and_data_owner(self):
        with tempfile.TemporaryDirectory() as name:
            workbench, _ = self.configure(Path(name))
            worker = WorkerProcess(workbench.root)
            worker.start("startup", {})
            worker.cancel()
            _, response = worker.events.get(timeout=15)
            self.assertEqual(response["status"], "cancelled")
            self.assertTrue(workbench.startup()["configured"])

    def test_expanded_dictionary_input_is_rejected_before_tokenization(self):
        with tempfile.TemporaryDirectory() as name:
            workbench, values = self.configure(Path(name))
            workbench.configure(values, [{"surface": "x", "reading": "expanded" * 30}])
            with self.assertRaises(ValueError):
                workbench.preview("x" * 4096, demo=True)

    def make_layouts(self, root):
        from neutral_tts.install_contract import create_test_layout
        layouts = []
        for index in (1, 2):
            source = root / ("source-" + str(index))
            source.mkdir()
            (source / "app.py").write_text("VERSION = " + str(index) + "\n", encoding="utf-8")
            layout = root / ("layout-" + str(index))
            create_test_layout(source, layout, {"app.py": "source"},
                candidate_id="candidate-" + str(index), source_commit=str(index) * 40,
                source_tree=str(index + 2) * 40, app_root_name="NeutralApp",
                verification_scope="LOCAL_WINDOWS_SOURCE_CANDIDATE")
            layouts.append(layout)
        return layouts

    def test_product_worker_install_update_rollback_uninstall_preserves_data(self):
        from neutral_tts.install_contract import verify_layout
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            host, _ = self.configure(root)
            host.preview("synthetic", demo=True)
            generated = host.generate("synthetic", demo=True)
            wav = Path(generated["destination"])
            before = sha(wav.read_bytes())
            settings = (host.root / "settings/app.json").read_bytes()
            layouts = self.make_layouts(root)
            app = root / "NeutralApp"
            worker = WorkerProcess(host.root)
            def run(operation, layout=None):
                worker.start("lifecycle", {"operation": operation, "app_root": str(app),
                    "layout_root": str(layout) if layout else None})
                _, response = worker.events.get(timeout=20)
                self.assertEqual(response["status"], "ok", response)
                self.assertEqual(len(host.startup()["history"]), 1)
                self.assertEqual(sha(wav.read_bytes()), before)
                self.assertEqual((host.root / "settings/app.json").read_bytes(), settings)
                return response["result"]
            run("install", layouts[0])
            upgraded = run("update", layouts[1])
            self.assertEqual(verify_layout(app)["candidate_id"], "candidate-2")
            run("rollback", Path(upgraded["retained_root"]))
            self.assertEqual(verify_layout(app)["candidate_id"], "candidate-1")
            self.assertFalse(run("recover")["reconciled"])
            unknown = app / "personal-note.txt"
            unknown.write_bytes(b"user owned")
            removed = run("uninstall")
            self.assertEqual(unknown.read_bytes(), b"user owned")
            self.assertFalse((app / "app.py").exists())
            self.assertEqual(verify_layout(Path(removed["retained_root"]))["candidate_id"], "candidate-1")
            for operation, layout in (("rollback", None), ("update", None), ("uninstall", layouts[0]),
                                      ("recover", layouts[0]), ("unknown", None)):
                with self.assertRaises(WorkbenchError):
                    host.lifecycle(operation, app, layout)

    def test_product_update_process_death_recovers_prior_application_then_retries(self):
        from neutral_tts.install_contract import verify_layout
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            host, _ = self.configure(root)
            layouts = self.make_layouts(root)
            app = root / "NeutralApp"
            host.lifecycle("install", app, layouts[0])
            script = """
import os, sys
from pathlib import Path
from neutral_tts import install_contract
from neutral_tts.workbench import Workbench
upgrade = install_contract.upgrade_layout
def interrupted(*args):
    def fault(phase):
        if phase == 'upgrade.after_current_relocated':
            os._exit(73)
    return upgrade(*args, fault_hook=fault)
install_contract.upgrade_layout = interrupted
Workbench(Path(sys.argv[1])).lifecycle('update', sys.argv[2], sys.argv[3])
"""
            process = subprocess.run([sys.executable, "-B", "-c", script, str(host.root), str(app), str(layouts[1])],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20)
            self.assertEqual(process.returncode, 73)
            self.assertFalse(app.exists())
            self.assertTrue(host.lifecycle("recover", app)["reconciled"])
            self.assertEqual(verify_layout(app)["candidate_id"], "candidate-1")
            host.lifecycle("update", app, layouts[1])
            self.assertEqual(verify_layout(app)["candidate_id"], "candidate-2")
            self.assertTrue(host.startup()["configured"])


if __name__ == "__main__":
    unittest.main()
