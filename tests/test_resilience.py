from __future__ import annotations

import json
import os
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from neutral_tts.resilience import FaultInjected, RecoveryError, RecoveryManager, RunCancelled


class ResilienceTests(unittest.TestCase):
    def test_double_run_same_requested_output_preserves_completed_output(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            manager = RecoveryManager(root / "recovery", root / "output")
            first = manager.execute("lesson.wav", lambda path: path.write_bytes(b"one"), run_id="run-one")
            second = manager.execute("lesson.wav", lambda path: path.write_bytes(b"two"), run_id="run-two")
            self.assertEqual(first.read_bytes(), b"one")
            self.assertEqual(second.read_bytes(), b"two")
            self.assertNotEqual(first.name.casefold(), second.name.casefold())

    def test_cancellation_does_not_publish_and_is_journaled(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            manager = RecoveryManager(root / "recovery", root / "output")
            manager.cancel("run-cancel")
            handle = manager.start("cancel.wav", run_id="run-cancel")
            with self.assertRaises(RunCancelled):
                handle.produce(lambda path: path.write_bytes(b"should-not-publish"))
            self.assertFalse(list((root / "output").iterdir()))
            journal = json.loads((root / "recovery" / "journals" / "run-cancel.json").read_text())
            self.assertEqual(journal["state"], "cancelled")

    def test_cancellation_while_producer_runs_cannot_be_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            manager = RecoveryManager(root / "recovery", root / "output")
            handle = manager.start("cancel.wav", run_id="run-active-cancel")
            started = threading.Event()
            release = threading.Event()
            errors: list[BaseException] = []

            def producer(path: Path) -> None:
                path.write_bytes(b"cancelled")
                started.set()
                release.wait(5)

            def run() -> None:
                try:
                    handle.produce(producer)
                except BaseException as exc:
                    errors.append(exc)

            worker = threading.Thread(target=run)
            worker.start()
            self.assertTrue(started.wait(5))
            manager.cancel(handle.run_id)
            release.set()
            worker.join(5)
            self.assertFalse(worker.is_alive())
            self.assertEqual(len(errors), 1)
            self.assertIsInstance(errors[0], RunCancelled)
            self.assertEqual(handle.record.state, "cancelled")
            with self.assertRaises(RecoveryError):
                handle.publish()
            journal = json.loads(handle.record.journal_path.read_text(encoding="utf-8"))
            self.assertEqual(journal["state"], "cancelled")

    def test_two_publishers_create_exactly_one_output(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            manager = RecoveryManager(root / "recovery", root / "output")
            handle = manager.start("same.wav", run_id="run-one-publisher")
            handle.produce(lambda path: path.write_bytes(b"one"))
            start = threading.Barrier(3)
            outputs: list[Path] = []
            errors: list[BaseException] = []

            def publish() -> None:
                start.wait()
                try:
                    outputs.append(handle.publish())
                except BaseException as exc:
                    errors.append(exc)

            threads = [threading.Thread(target=publish) for _ in range(2)]
            for thread in threads:
                thread.start()
            start.wait()
            for thread in threads:
                thread.join(5)
            self.assertEqual(len(outputs), 1)
            self.assertEqual(len(errors), 1)
            self.assertIsInstance(errors[0], RecoveryError)
            self.assertEqual([path.name for path in (root / "output").iterdir()], ["same.wav"])

    def test_publication_claim_serializes_late_cancellation(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            manager = RecoveryManager(root / "recovery", root / "output")
            handle = manager.start("claimed.wav", run_id="run-claimed")
            handle.produce(lambda path: path.write_bytes(b"claimed"))
            linked = threading.Event()
            release = threading.Event()
            real_link = os.link

            def slow_link(source: Path, target: Path) -> None:
                linked.set()
                release.wait(5)
                real_link(source, target)

            publish_thread = threading.Thread(target=handle.publish)
            cancel_thread = threading.Thread(target=lambda: manager.cancel(handle.run_id))
            with patch("neutral_tts.resilience.os.link", side_effect=slow_link):
                publish_thread.start()
                self.assertTrue(linked.wait(5))
                cancel_thread.start()
                self.assertTrue(cancel_thread.is_alive())
                release.set()
                publish_thread.join(5)
                cancel_thread.join(5)
            self.assertEqual(handle.record.state, "completed")
            self.assertFalse(manager.is_cancelled(handle.run_id))
            self.assertEqual((root / "output" / "claimed.wav").read_bytes(), b"claimed")

    def test_fault_injection_leaves_durable_transition_for_reconciliation(self) -> None:
        def inject(state: str) -> None:
            if state == "ready":
                raise FaultInjected("simulated power loss")

        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            manager = RecoveryManager(root / "recovery", root / "output", fault_injector=inject)
            handle = manager.start("power.wav", run_id="run-power")
            with self.assertRaises(FaultInjected):
                handle.produce(lambda path: path.write_bytes(b"partial"))
            journal = json.loads((root / "recovery" / "journals" / "run-power.json").read_text())
            self.assertEqual(journal["state"], "ready")
            recovered = RecoveryManager(root / "recovery", root / "output").reconcile()
            self.assertEqual(recovered, ())
            self.assertTrue(list((root / "recovery" / "quarantine").iterdir()))

    def test_corrupt_journal_is_quarantined(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            manager = RecoveryManager(root / "recovery", root / "output")
            (root / "recovery" / "journals" / "bad.json").write_text("not-json")
            self.assertEqual(manager.reconcile(), ())
            self.assertFalse((root / "recovery" / "journals" / "bad.json").exists())

    def test_terminal_reconciliation_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            manager = RecoveryManager(root / "recovery", root / "output")
            handle = manager.start("cancel.wav", run_id="run-terminal")
            handle.cancel()
            first = manager.reconcile()
            second = manager.reconcile()
            self.assertEqual([record.state for record in first], ["cancelled"])
            self.assertEqual([record.state for record in second], ["cancelled"])
            self.assertTrue(handle.record.journal_path.exists())

    def test_collision_identity_is_nfc_and_casefolded(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            manager = RecoveryManager(root / "recovery", root / "output")
            first = manager.execute("caf\u00e9.wav", lambda path: path.write_bytes(b"one"))
            second = manager.execute("cafe\u0301.wav", lambda path: path.write_bytes(b"two"))
            self.assertEqual(first.name, "caf\u00e9.wav")
            self.assertEqual(second.name, "caf\u00e9 (1).wav")

    def test_power_loss_after_link_is_reconciled_to_completed(self) -> None:
        def inject(state: str) -> None:
            if state == "publication_linked":
                raise FaultInjected("simulated power loss after durable link")

        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            manager = RecoveryManager(root / "recovery", root / "output", fault_injector=inject)
            handle = manager.start("linked.wav", run_id="run-linked")
            handle.produce(lambda path: path.write_bytes(b"durable"))
            with self.assertRaises(FaultInjected):
                handle.publish()
            journal = json.loads((root / "recovery" / "journals" / "run-linked.json").read_text())
            self.assertEqual(journal["state"], "publishing")
            recovered = RecoveryManager(root / "recovery", root / "output").reconcile()
            self.assertEqual(len(recovered), 1)
            self.assertEqual(recovered[0].state, "completed")
            self.assertEqual((root / "output" / "linked.wav").read_bytes(), b"durable")

    def test_recovery_and_output_roots_must_be_separate(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name).absolute()
            with self.assertRaises(RecoveryError):
                RecoveryManager(root / "state", root / "state" / "output")

    @unittest.skipUnless(os.name == "nt", "junction probe is Windows-specific")
    def test_recovery_child_junction_and_replaced_output_root_fail_closed(self) -> None:
        def junction(link: Path, target: Path) -> None:
            result = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(link), str(target)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
            if result.returncode:
                self.skipTest("junction creation is unavailable")

        with tempfile.TemporaryDirectory() as name:
            root = Path(name).absolute()
            recovery = root / "recovery"
            recovery.mkdir()
            outside_journals = root / "outside-journals"
            outside_journals.mkdir()
            junction(recovery / "journals", outside_journals)
            with self.assertRaises(RecoveryError):
                RecoveryManager(recovery, root / "output")
            self.assertFalse(list(outside_journals.iterdir()))

        with tempfile.TemporaryDirectory() as name:
            root = Path(name).absolute()
            manager = RecoveryManager(root / "recovery", root / "output")
            original = root / "original-output"
            os.rename(root / "output", original)
            outside = root / "outside-output"
            outside.mkdir()
            junction(root / "output", outside)
            with self.assertRaises(RecoveryError):
                manager.execute("redirect.wav", lambda path: path.write_bytes(b"blocked"))
            self.assertFalse(list(outside.iterdir()))
            manager.close()

    def test_linked_journal_is_quarantined_without_following_it_when_supported(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            manager = RecoveryManager(root / "recovery", root / "output")
            external = root / "external.json"
            external.write_text('{"untouched":true}', encoding="utf-8")
            linked = root / "recovery" / "journals" / "linked.json"
            try:
                os.symlink(external, linked)
            except (OSError, NotImplementedError):
                self.skipTest("symlink creation is unavailable on this host")
            manager.reconcile()
            self.assertEqual(external.read_text(encoding="utf-8"), '{"untouched":true}')
            self.assertFalse(linked.exists())


if __name__ == "__main__":
    unittest.main()
