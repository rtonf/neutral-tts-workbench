from __future__ import annotations

import os
import struct
import tempfile
import unittest
import wave
from dataclasses import replace
from pathlib import Path
from unittest import mock

import neutral_tts.application as application_module
from neutral_tts.application import (
    ApplicationError,
    ApplicationService,
    DigestMismatchError,
    PersistedSettings,
    StalePreviewError,
    UnsupportedLifecycleIntegrationError,
    UnsafeDirectoryError,
)
from neutral_tts.demo_backend import DEMO_SILENCE_ONLY
from neutral_tts.filename_templates import FilenameContext
from neutral_tts.resilience import RecoveryError, RunCancelled


class ApplicationServiceTests(unittest.TestCase):
    @staticmethod
    def snapshot_tree(
        root: Path,
    ) -> tuple[tuple[str, ...], tuple[tuple[str, bytes], ...]]:
        paths = tuple(root.rglob("*"))
        directories = tuple(
            sorted(path.relative_to(root).as_posix() for path in paths if path.is_dir())
        )
        files = tuple(
            sorted(
                (path.relative_to(root).as_posix(), path.read_bytes())
                for path in paths
                if path.is_file()
            )
        )
        return directories, files

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

    def test_run_uses_one_snapshot_and_later_lifecycle_settings_apply_next_operation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output, staging = root / "output", root / "staging"
            output.mkdir()
            staging.mkdir()
            lifecycle = root / "lifecycle"
            lifecycle.mkdir()
            (lifecycle / "marker").write_bytes(b"unchanged")
            safe = PersistedSettings(
                template="lesson-{sequence}",
                filename_context=FilenameContext("20260901", "123000"),
                output_directory=output,
                staging_directory=staging,
            )
            unsupported = replace(safe, restore_live_data_root=lifecycle)

            class Store:
                value = safe
                calls = 0

                def get_settings(self):
                    self.calls += 1
                    current = self.value
                    if self.calls == 2:
                        self.value = unsupported
                    return current

            store = Store()
            service = ApplicationService(
                lambda text: max(1, len(text)),
                settings_store=store,
                backend=DEMO_SILENCE_ONLY(),
            )
            preview = service.preview("one")
            lifecycle_before = self.snapshot_tree(lifecycle)

            outcome = service.run("one", preview)

            self.assertEqual(store.calls, 2)
            self.assertTrue((output / outcome.published_wav_name).is_file())
            self.assertEqual(self.snapshot_tree(lifecycle), lifecycle_before)
            before_next_operation = self.snapshot_tree(root)
            with self.assertRaises(UnsupportedLifecycleIntegrationError):
                service.startup()
            self.assertEqual(store.calls, 3)
            self.assertEqual(self.snapshot_tree(root), before_next_operation)

    def test_startup_uses_returned_snapshot_and_rejects_lifecycle_on_next_operation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output, staging = root / "output", root / "staging"
            output.mkdir()
            staging.mkdir()
            lifecycle = root / "lifecycle"
            lifecycle.mkdir()
            (lifecycle / "marker").write_bytes(b"unchanged")
            safe = PersistedSettings(
                filename_context=FilenameContext("20260901", "123000"),
                output_directory=output,
                staging_directory=staging,
            )
            unsupported = replace(safe, install_app_root=lifecycle)

            class Store:
                value = safe
                calls = 0

                def get_settings(self):
                    self.calls += 1
                    current = self.value
                    self.value = unsupported
                    return current

            store = Store()
            service = ApplicationService(
                lambda text: max(1, len(text)), settings_store=store
            )
            lifecycle_before = self.snapshot_tree(lifecycle)

            self.assertEqual(service.startup().status, "CLEAN")
            self.assertEqual(store.calls, 1)
            self.assertEqual(self.snapshot_tree(lifecycle), lifecycle_before)
            before_next_operation = self.snapshot_tree(root)
            with self.assertRaises(UnsupportedLifecycleIntegrationError):
                service.startup()
            self.assertEqual(store.calls, 2)
            self.assertEqual(self.snapshot_tree(root), before_next_operation)

    def test_operation_snapshot_copies_nested_filename_context_content(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output, staging = root / "output", root / "staging"
            output.mkdir()
            staging.mkdir()
            context = FilenameContext(
                "20260901", "123000", title="original", job_id_short="test"
            )
            settings = PersistedSettings(
                template="lesson-{title}-{sequence}",
                filename_context=context,
                output_directory=output,
                staging_directory=staging,
            )
            expected_digest = settings.settings_digest

            def mutating_counter(text):
                object.__setattr__(context, "title", "changed")
                return max(1, len(text))

            service = ApplicationService(mutating_counter, settings)

            preview = service.preview("one")

            self.assertEqual(context.title, "changed")
            self.assertEqual(preview.filename.wav_name, "lesson-original-001.wav")
            self.assertEqual(preview.settings_digest, expected_digest)

    def test_admit_and_next_safe_store_snapshot_each_use_one_read(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            service = self.make_service(root)
            first = service._settings
            second = replace(first, tokenizer_identity="replacement-tokenizer")

            class Store:
                value = first
                calls = 0

                def get_settings(self):
                    self.calls += 1
                    return self.value

            store = Store()
            service._settings = None
            service._settings_store = store
            preview = service.preview("one")
            admission = service.admit("one", preview)
            self.assertEqual(admission.output_directory, root / "output")
            self.assertEqual(store.calls, 2)
            store.value = second

            with self.assertRaises(StalePreviewError):
                service.run("one", preview)

            self.assertEqual(store.calls, 3)

    def test_lifecycle_rejection_precedes_backend_identity_access(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            service = self.make_service(root)
            preview = service.preview("one")
            lifecycle = root / "lifecycle"
            lifecycle.mkdir()
            service._settings = replace(
                service._settings, install_user_data_root=lifecycle
            )

            class BackendProbe:
                identity_reads = 0
                generate_calls = 0

                @property
                def backend_identity(self):
                    self.identity_reads += 1
                    return "backend-probe-v1"

                def generate(self, request):
                    self.generate_calls += 1
                    raise AssertionError("backend must not run")

            backend = BackendProbe()
            service.backend = backend
            before = self.snapshot_tree(root)

            with self.assertRaises(UnsupportedLifecycleIntegrationError):
                service.run("one", preview)

            self.assertEqual(backend.identity_reads, 0)
            self.assertEqual(backend.generate_calls, 0)
            self.assertEqual(self.snapshot_tree(root), before)

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

    def test_declared_pcm_extent_must_fit_the_wav_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            service = self.make_service(root)

            class TruncatedBackend(DEMO_SILENCE_ONLY):
                def generate(self, request):
                    result = super().generate(request)
                    payload = bytearray(result.staged_path.read_bytes())
                    data_offset = payload.index(b"data")
                    declared_bytes = 1_600_000_000
                    payload[data_offset + 4 : data_offset + 8] = struct.pack(
                        "<I", declared_bytes
                    )
                    result.staged_path.write_bytes(payload)
                    return replace(
                        result,
                        actual=replace(
                            result.actual,
                            duration_seconds=(declared_bytes // 2)
                            / result.actual.sample_rate,
                        ),
                    )

            service.backend = TruncatedBackend()
            preview = service.preview("one")
            with self.assertRaises(ApplicationError):
                service.run("one", preview)
            self.assertFalse(list((root / "output").iterdir()))

    def test_sample_rate_must_stay_within_the_pcm_contract(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            service = self.make_service(root)

            class OversizedRateBackend(DEMO_SILENCE_ONLY):
                def generate(self, request):
                    result = super().generate(request)
                    payload = bytearray(result.staged_path.read_bytes())
                    sample_rate = 768_001
                    payload[24:28] = struct.pack("<I", sample_rate)
                    payload[28:32] = struct.pack("<I", sample_rate * 2)
                    result.staged_path.write_bytes(payload)
                    return replace(
                        result,
                        actual=replace(
                            result.actual,
                            sample_rate=sample_rate,
                            duration_seconds=1_600 / sample_rate,
                        ),
                    )

            service.backend = OversizedRateBackend()
            preview = service.preview("one")
            with self.assertRaises(ApplicationError):
                service.run("one", preview)
            self.assertFalse(list((root / "output").iterdir()))

    def test_backend_identity_alone_does_not_grant_demo_labeling(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            service = self.make_service(root)

            class IdentitySpoofBackend(DEMO_SILENCE_ONLY):
                def generate(self, request):
                    result = super().generate(request)
                    with result.staged_path.open("r+b") as handle:
                        handle.seek(44)
                        handle.write(b"\x01\x00")
                    return result

            service.backend = IdentitySpoofBackend()
            preview = service.preview("one")
            outcome = service.run("one", preview)
            self.assertNotIn("demo", outcome.public_payload())

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

    def test_optional_lifecycle_settings_reject_before_application_side_effects(self):
        for configuration in (
            "restore",
            "install-pair",
            "install-app-only",
            "install-user-only",
        ):
            for entry in ("preview", "startup", "run"):
                with (
                    self.subTest(configuration=configuration, entry=entry),
                    tempfile.TemporaryDirectory() as temporary,
                ):
                    root = Path(temporary)
                    service = self.make_service(root)
                    approved_preview = service.preview("one")
                    target = root / "lifecycle-target"
                    external = root / "external-data"
                    target.mkdir()
                    external.mkdir()
                    restore_journal = root / ".lifecycle-target.restore-journal.json"
                    install_journal = root / ".lifecycle-target.install-journal.json"
                    restore_journal.write_bytes(b"existing-restore-journal")
                    install_journal.write_bytes(b"existing-install-journal")
                    (external / "marker").write_bytes(b"external-unchanged")
                    configured: dict[str, Path] = {}
                    if configuration == "restore":
                        configured["restore_live_data_root"] = target
                    elif configuration == "install-pair":
                        configured["install_app_root"] = target
                        configured["install_user_data_root"] = external
                    elif configuration == "install-app-only":
                        configured["install_app_root"] = target
                    else:
                        configured["install_user_data_root"] = external
                    service._settings = replace(service._settings, **configured)
                    before = self.snapshot_tree(root)

                    with mock.patch.object(
                        service.backend,
                        "generate",
                        wraps=service.backend.generate,
                    ) as backend_call:
                        with self.assertRaises(
                            UnsupportedLifecycleIntegrationError
                        ) as rejected:
                            if entry == "preview":
                                service.preview("one")
                            elif entry == "startup":
                                service.startup()
                            else:
                                service.run("one", approved_preview)

                    self.assertEqual(
                        str(rejected.exception),
                        "Automatic restore/install lifecycle integration "
                        "is not supported by this application host.",
                    )
                    backend_call.assert_not_called()
                    self.assertEqual(self.snapshot_tree(root), before)
                    self.assertEqual(
                        restore_journal.read_bytes(), b"existing-restore-journal"
                    )
                    self.assertEqual(
                        install_journal.read_bytes(), b"existing-install-journal"
                    )
                    self.assertEqual(
                        (external / "marker").read_bytes(), b"external-unchanged"
                    )

    def test_startup_binds_output_and_recovery_before_owner_acquisition(self):
        for role in ("output", "staging"):
            with self.subTest(role=role), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                service = self.make_service(root)
                target = root / role
                retained = root / f"original-{role}"
                marker = target / "identity-marker"
                marker.write_bytes(b"original")
                real_owner = application_module._RecoveryOwner

                def replace_before_owner(recovery_directory, *, expected_identity=None):
                    target.rename(retained)
                    target.mkdir()
                    return real_owner(
                        recovery_directory,
                        expected_identity=expected_identity,
                    )

                with mock.patch.object(
                    application_module,
                    "_RecoveryOwner",
                    side_effect=replace_before_owner,
                ):
                    with self.assertRaises(StalePreviewError):
                        service.startup()

                self.assertEqual((retained / marker.name).read_bytes(), b"original")
                self.assertFalse(list(target.iterdir()))
                self.assertEqual(service.startup().status, "CLEAN")

    def test_preview_identity_rejects_output_directory_replacement(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            service = self.make_service(root)
            output = root / "output"
            original = root / "original-output"
            preview = service.preview("one")
            output.rename(original)
            output.mkdir()

            with self.assertRaises(StalePreviewError):
                service.run("one", preview)
            self.assertFalse(list(output.iterdir()))

    def test_unsupported_lifecycle_rejects_before_root_overlap_snapshot(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            service = self.make_service(root)
            nested_restore = root / "output" / "live-data"
            nested_restore.mkdir()
            service._settings = replace(
                service._settings,
                restore_live_data_root=nested_restore,
            )

            with mock.patch.object(
                service,
                "_operational_roots",
                wraps=service._operational_roots,
            ) as root_snapshot:
                with self.assertRaises(UnsupportedLifecycleIntegrationError):
                    service.preview("one")

            root_snapshot.assert_not_called()

    @unittest.skipUnless(os.name == "nt", "Windows filesystem alias check")
    def test_physical_alias_cannot_make_output_and_recovery_the_same_root(self):
        import ctypes

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).absolute()
            required = ctypes.windll.kernel32.GetShortPathNameW(str(root), None, 0)
            if required <= 0:
                self.skipTest("short path aliases unavailable")
            buffer = ctypes.create_unicode_buffer(required + 1)
            written = ctypes.windll.kernel32.GetShortPathNameW(
                str(root), buffer, len(buffer)
            )
            if written <= 0:
                self.skipTest("short path aliases unavailable")
            alias = Path(buffer.value)
            if os.path.normcase(str(alias)) == os.path.normcase(str(root)):
                self.skipTest("distinct short path alias unavailable")
            settings = PersistedSettings(
                filename_context=FilenameContext("20260906", "011700"),
                output_directory=root,
                staging_directory=alias,
            )
            service = ApplicationService(lambda text: 1, settings)

            with self.assertRaises(UnsafeDirectoryError):
                service.startup()

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
