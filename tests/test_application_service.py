from __future__ import annotations

import tempfile
import unittest
import wave
from dataclasses import replace
from pathlib import Path
from unittest import mock

from neutral_tts.application import (
    ApplicationError,
    ApplicationService,
    DigestMismatchError,
    PersistedSettings,
    StalePreviewError,
)
from neutral_tts.demo_backend import DEMO_SILENCE_ONLY
from neutral_tts.filename_templates import FilenameContext
from neutral_tts.resilience import RecoveryError, RunCancelled


class ApplicationServiceTests(unittest.TestCase):
    def make_service(self, root: Path) -> ApplicationService:
        output, staging = root / "output", root / "staging"
        output.mkdir()
        staging.mkdir()
        settings = PersistedSettings(
            template="lesson-{sequence}",
            filename_context=FilenameContext("20260901", "123000", job_id_short="test"),
            tokenizer_identity="demo-tokenizer-v1",
            output_directory=output,
            staging_directory=staging,
        )
        return ApplicationService(lambda text: max(1, len(text)), settings, DEMO_SILENCE_ONLY())

    def test_full_workflow_has_private_source_and_actual_feedback(self):
        with tempfile.TemporaryDirectory() as temporary:
            service = self.make_service(Path(temporary))
            preview = service.preview("private source text")
            public_preview = preview.public_payload()
            self.assertNotIn("private source text", repr(public_preview))
            self.assertEqual(preview.plan_digest, preview.estimate_digest)
            outcome = service.run("private source text", preview)
            self.assertTrue((Path(temporary) / "output" / outcome.published_wav_name).is_file())
            self.assertEqual(outcome.public_payload()["operation"], "NEW_GENERATION")
            self.assertEqual(
                outcome.public_payload()["startup_recovery"]["operation"],
                "STARTUP_RECOVERY",
            )
            self.assertFalse(outcome.public_payload()["startup_recovery"]["backend_invoked"])
            self.assertEqual(outcome.receipt["status"], "SUCCEEDED")
            self.assertFalse(outcome.public_payload()["demo"]["speech"])
            feedback = service.receipt_feedback(outcome)
            self.assertEqual(feedback["actual"]["audio"]["sample_rate"], 16000)
            with wave.open(str(Path(temporary) / "output" / outcome.published_wav_name), "rb") as wav:
                self.assertEqual((wav.getnchannels(), wav.getsampwidth()), (1, 2))

    def test_stale_source_settings_and_collision_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            service = self.make_service(root)
            preview = service.preview("one")
            with self.assertRaises(StalePreviewError):
                service.admit("two", preview)
            settings = service._settings
            service._settings = replace(settings, tokenizer_identity="different-tokenizer")
            with self.assertRaises(StalePreviewError):
                service.admit("one", preview)
            service._settings = settings
            (root / "output" / preview.candidate_wav_name).write_bytes(b"occupied")
            with self.assertRaises(StalePreviewError):
                service.admit("one", preview)

    def test_digest_mismatch_is_rejected_before_backend(self):
        with tempfile.TemporaryDirectory() as temporary:
            service = self.make_service(Path(temporary))
            preview = service.preview("one")
            forged = replace(preview, filename_digest="0" * 64)
            with self.assertRaises(DigestMismatchError):
                service.admit("one", forged)

    def test_no_overwrite_uses_suffix_and_keeps_existing_bytes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            service = self.make_service(root)
            preview = service.preview("one")
            occupied = root / "output" / preview.candidate_wav_name
            occupied.write_bytes(b"keep")
            fresh = service.preview("one")
            outcome = service.run("one", fresh)
            self.assertNotEqual(outcome.published_wav_name, occupied.name)
            self.assertEqual(occupied.read_bytes(), b"keep")

    def test_late_collision_rejects_exact_admitted_name_without_a_suffix(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            service = self.make_service(root)
            output = root / "output"

            class RacingBackend(DEMO_SILENCE_ONLY):
                def generate(self, request):
                    result = super().generate(request)
                    (output / request.output_basename).write_bytes(b"keep")
                    return result

            service.backend = RacingBackend()
            preview = service.preview("one")
            with self.assertRaises(RecoveryError):
                service.run("one", preview)
            self.assertEqual((output / preview.candidate_wav_name).read_bytes(), b"keep")
            self.assertEqual([path.name for path in output.iterdir()], [preview.candidate_wav_name])

    def test_admission_rejects_a_target_that_changes_during_final_selection(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            service = self.make_service(root)
            base = root / "output" / "lesson-001.wav"
            suffix = root / "output" / "lesson-002.wav"
            # preview(), admit()->preview(), and admit()->final selection.
            with mock.patch(
                "neutral_tts.application.choose_collision_target",
                side_effect=[base, base, suffix],
            ):
                preview = service.preview("one")
                with self.assertRaises(StalePreviewError):
                    service.admit("one", preview)

    def test_admission_binds_output_when_store_mutates_during_backend(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output_one, output_two = root / "output-one", root / "output-two"
            stage = root / "staging"
            output_one.mkdir()
            output_two.mkdir()
            stage.mkdir()
            settings_one = PersistedSettings(
                template="lesson-{sequence}", filename_context=FilenameContext("20260901", "123000"),
                output_directory=output_one, staging_directory=stage,
            )
            settings_two = replace(settings_one, output_directory=output_two)

            class Store:
                value = settings_one

                def get_settings(self):
                    return self.value

            store = Store()

            class MutatingBackend(DEMO_SILENCE_ONLY):
                def generate(self, request):
                    result = super().generate(request)
                    store.value = settings_two
                    return result

            service = ApplicationService(lambda text: max(1, len(text)), settings_store=store, backend=MutatingBackend())
            preview = service.preview("one")
            outcome = service.run("one", preview)
            self.assertTrue((output_one / outcome.published_wav_name).exists())
            self.assertFalse(list(output_two.iterdir()))

    def test_inconsistent_backend_audio_never_publishes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            service = self.make_service(root)

            class BadBackend(DEMO_SILENCE_ONLY):
                def generate(self, request):
                    result = super().generate(request)
                    return replace(result, actual=replace(result.actual, wav_bytes=result.actual.wav_bytes + 1))

            service.backend = BadBackend()
            preview = service.preview("one")
            with self.assertRaises(ApplicationError):
                service.run("one", preview)
            self.assertFalse(list((root / "output").iterdir()))

    def test_count_drift_fails_before_publication_after_audio_validation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            service = self.make_service(root)

            class BadBackend(DEMO_SILENCE_ONLY):
                def generate(self, request):
                    result = super().generate(request)
                    return replace(result, actual=replace(result.actual, tokens=result.actual.tokens + 1))

            service.backend = BadBackend()
            preview = service.preview("one")
            with self.assertRaises(ValueError):
                service.run("one", preview)
            self.assertFalse(list((root / "output").iterdir()))
            # The failed durable run releases its process owner and is safely
            # reconciled by the next ordinary startup.
            startup = service.startup()
            self.assertEqual(startup.status, "RECOVERED")
            self.assertEqual(startup.quarantined_runs, 1)

    def test_cancellation_releases_owner_and_next_startup_reconciles(self):
        with tempfile.TemporaryDirectory() as temporary:
            service = self.make_service(Path(temporary))

            class CancellingBackend(DEMO_SILENCE_ONLY):
                def generate(self, request):
                    raise RunCancelled("cancelled by test adapter")

            service.backend = CancellingBackend()
            preview = service.preview("one")
            with self.assertRaises(RunCancelled):
                service.run("one", preview)
            startup = service.startup()
            self.assertEqual(startup.status, "RECOVERED")
            self.assertEqual(startup.quarantined_runs, 1)

    def test_preview_is_non_mutating_and_startup_uses_only_configured_roots(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            service = self.make_service(root)
            restore = root / "live-data"
            install_app = root / "installed-app"
            install_user = root / "installed-user-data"
            service._settings = replace(
                service._settings,
                restore_live_data_root=restore,
                install_app_root=install_app,
                install_user_data_root=install_user,
            )
            before = sorted(path.relative_to(root) for path in root.rglob("*"))
            with (
                mock.patch.object(service.backend, "generate") as backend_call,
                mock.patch("neutral_tts.application.reconcile_restore", return_value=True) as restore_call,
                mock.patch(
                    "neutral_tts.application.reconcile_install_lifecycle",
                    return_value=True,
                ) as install_call,
            ):
                service.preview("one")
                self.assertEqual(before, sorted(path.relative_to(root) for path in root.rglob("*")))
                restore_call.assert_not_called()
                install_call.assert_not_called()
                backend_call.assert_not_called()

                startup = service.startup()

            restore_call.assert_called_once_with(restore)
            install_call.assert_called_once_with(install_app, install_user)
            backend_call.assert_not_called()
            self.assertTrue(startup.restore_reconciled)
            self.assertTrue(startup.install_reconciled)
            self.assertFalse(startup.public_payload()["backend_invoked"])

    def test_startup_accepts_explicit_restore_and_install_roots(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            service = self.make_service(root)
            restore = root / "live-data"
            install_app = root / "installed-app"
            install_user = root / "installed-user-data"
            untouched = root / "unconfigured"
            for directory in (restore, install_app, install_user, untouched):
                directory.mkdir()
            marker = untouched / "marker"
            marker.write_bytes(b"keep")
            service._settings = replace(
                service._settings,
                restore_live_data_root=restore,
                install_app_root=install_app,
                install_user_data_root=install_user,
            )

            startup = service.startup()

            self.assertTrue(startup.restore_configured)
            self.assertTrue(startup.install_configured)
            self.assertFalse(startup.restore_reconciled)
            self.assertFalse(startup.install_reconciled)
            self.assertEqual(marker.read_bytes(), b"keep")

    def test_reparse_directory_is_rejected_when_supported(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "output"
            staging = root / "staging"
            output.mkdir()
            staging.mkdir()
            linked = root / "linked"
            try:
                linked.symlink_to(output, target_is_directory=True)
            except OSError:
                self.skipTest("symlinks unavailable")
            settings = PersistedSettings(filename_context=FilenameContext("20260901", "123000"), output_directory=linked, staging_directory=staging)
            with self.assertRaises(Exception):
                ApplicationService(lambda text: 1, settings).preview("text")


if __name__ == "__main__":
    unittest.main()
