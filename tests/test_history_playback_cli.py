from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
import wave
from contextlib import redirect_stdout
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import neutral_tts.resilience as resilience_module
from neutral_tts.application import (
    ApplicationError,
    ApplicationService,
    BackendResult,
    GenerationRequest,
    PersistedSettings,
)
from neutral_tts.cli import RealRuntimeBinding, main
from neutral_tts.filename_templates import FilenameContext
from neutral_tts.history import HistoryStore
from neutral_tts.playback import PlaybackError, WinsoundPlayback
from neutral_tts.receipts import ActualRun
from neutral_tts.resilience import RecoveryError


def counter(text: str) -> int:
    return 1 + len(text)


def settings(
    output: Path,
    recovery: Path,
    *,
    write_history: bool,
) -> PersistedSettings:
    return PersistedSettings(
        filename_context=FilenameContext(
            date="20260909",
            time="120000",
            title="audio",
            voice="neutral",
            quality="balanced",
            reading="standard",
            job_id_short="abcdef12",
            sequence=1,
        ),
        tokenizer_identity="tokenizer-contract-v1",
        output_directory=output,
        staging_directory=recovery,
        history_enabled=True,
        runtime_config_digest="a" * 64 if write_history else None,
        voice_rights_manifest_sha256="b" * 64 if write_history else None,
    )


class SyntheticBackend:
    backend_identity = "synthetic-real-v1"

    def __init__(self) -> None:
        self.calls = 0

    def generate(self, request: GenerationRequest) -> BackendResult:
        self.calls += 1
        started = time.monotonic()
        with wave.open(str(request.staging_path), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(48_000)
            wav.writeframes(b"\x01\x00\xff\xff" * 20)
        frames = 40
        size = request.staging_path.stat().st_size
        return BackendResult(
            request.staging_path,
            ActualRun(
                request.plan.total_tokens,
                request.plan.chunk_count,
                request.plan.boundary_count,
                48_000,
                frames / 48_000,
                size,
                generation_seconds=max(time.monotonic() - started, 0.000001),
                staging_bytes_peak=size,
            ),
        )


class RecordingPlayback:
    def __init__(self) -> None:
        self.records = []

    def play(self, record) -> None:
        self.records.append(record)
        WinsoundPlayback().play(record, player=lambda _data: None)


class HistoryPlaybackTests(unittest.TestCase):
    @staticmethod
    def _invoke(
        args: list[str],
        *,
        real_runtime_factory=None,
        playback=None,
    ) -> tuple[int, dict[str, object]]:
        output = io.StringIO()
        with redirect_stdout(output):
            code = main(
                args,
                real_runtime_factory=real_runtime_factory,
                playback=playback,
            )
        return code, json.loads(output.getvalue())

    def test_real_cli_generation_history_play_and_restart_do_not_reload_backend(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name).absolute()
            output = root / "output"
            recovery = root / "recovery"
            private_config = root / "runtime-config"
            private_config.write_text("{}", encoding="utf-8")
            backend = SyntheticBackend()
            factory_calls = []

            def factory(path: Path) -> RealRuntimeBinding:
                factory_calls.append(path)
                return RealRuntimeBinding(
                    counter,
                    backend,
                    "tokenizer-contract-v1",
                    "a" * 64,
                    "b" * 64,
                    "test-device",
                )

            common = [
                "--output-dir",
                str(output),
                "--staging-dir",
                str(recovery),
                "--date",
                "20260909",
                "--time",
                "120000",
            ]
            code, generated = self._invoke(
                [
                    "real-run",
                    "--runtime-config",
                    str(private_config),
                    "--text",
                    "synthetic input",
                    *common,
                ],
                real_runtime_factory=factory,
            )
            self.assertEqual(code, 0)
            self.assertEqual(generated["status"], "SUCCEEDED")
            run_id = generated["outcome"]["run_id"]
            wav_name = generated["outcome"]["publication"]["wav_name"]
            self.assertEqual(backend.calls, 1)
            self.assertEqual(factory_calls, [private_config])
            self.assertNotIn("demo", wav_name.casefold())
            self.assertNotIn("synthetic input", repr(generated))
            self.assertNotIn(str(private_config), repr(generated))
            self.assertEqual(
                generated["outcome"]["receipt"]["estimate"]["sample_rate"],
                {"minimum": 48_000.0, "maximum": 48_000.0},
            )

            # These are fresh CLI invocations.  No real-runtime factory is
            # supplied, so startup, listing, and playback cannot load a model.
            code, startup = self._invoke(
                ["startup", "--runtime-config", str(private_config), *common]
            )
            self.assertEqual(code, 0)
            self.assertEqual(startup["startup_recovery"]["history"]["status"], "VERIFIED")
            code, history = self._invoke(["history", "--run-id", run_id, *common])
            self.assertEqual(code, 0)
            self.assertEqual(len(history["records"]), 1)
            self.assertEqual(history["records"][0]["run_id"], run_id)
            playback = RecordingPlayback()
            code, played = self._invoke(
                ["play", "--run-id", run_id, *common], playback=playback
            )
            self.assertEqual(code, 0)
            self.assertTrue(played["played"])
            self.assertEqual(len(playback.records), 1)
            self.assertEqual(backend.calls, 1)

            raw_history = next((recovery / "history").glob("*.json")).read_text(
                encoding="utf-8"
            )
            self.assertNotIn("synthetic input", raw_history)
            self.assertNotIn(str(private_config), raw_history)
            journal = json.loads(
                (recovery / "journals" / f"{run_id}.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertRegex(journal["history_binding_sha256"], r"^[0-9a-f]{64}$")

    def test_real_cli_rejects_lifecycle_and_invalid_roots_before_host_factory(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name).absolute()
            private_config = root / "runtime-config"
            private_config.write_text("{}", encoding="utf-8")
            factory_calls: list[Path] = []

            def factory(path: Path) -> RealRuntimeBinding:
                factory_calls.append(path)
                return RealRuntimeBinding(
                    counter,
                    SyntheticBackend(),
                    "tokenizer-contract-v1",
                    "a" * 64,
                    "b" * 64,
                )

            output = root / "output"
            recovery = root / "recovery"
            lifecycle = root / "install-root"
            code, payload = self._invoke(
                [
                    "real-run",
                    "--runtime-config",
                    str(private_config),
                    "--text",
                    "synthetic input",
                    "--output-dir",
                    str(output),
                    "--staging-dir",
                    str(recovery),
                    "--install-app-root",
                    str(lifecycle),
                ],
                real_runtime_factory=factory,
            )
            self.assertEqual(code, 2)
            self.assertEqual(
                payload["error"]["code"],
                "UNSUPPORTED_LIFECYCLE_INTEGRATION",
            )
            self.assertEqual(factory_calls, [])
            self.assertFalse(output.exists())
            self.assertFalse(recovery.exists())
            self.assertFalse(lifecycle.exists())

            invalid_output = root / "not-a-directory"
            invalid_output.write_bytes(b"synthetic")
            code, payload = self._invoke(
                [
                    "real-run",
                    "--runtime-config",
                    str(private_config),
                    "--text",
                    "synthetic input",
                    "--output-dir",
                    str(invalid_output),
                    "--staging-dir",
                    str(recovery),
                ],
                real_runtime_factory=factory,
            )
            self.assertEqual(code, 2)
            self.assertEqual(payload["error"]["code"], "WORKFLOW_REJECTED")
            self.assertEqual(factory_calls, [])
            self.assertFalse(recovery.exists())

    def test_missing_history_binding_rejects_before_backend_generation(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name).absolute()
            output = root / "output"
            recovery = root / "recovery"
            output.mkdir()
            recovery.mkdir()
            backend = SyntheticBackend()
            service = ApplicationService(
                counter,
                settings(output, recovery, write_history=False),
                backend=backend,
            )
            preview = service.preview("synthetic binding input")
            with self.assertRaises(ApplicationError):
                service.run("synthetic binding input", preview)
            self.assertEqual(backend.calls, 0)

        binding = RealRuntimeBinding(
            counter,
            object(),
            " tokenizer-contract-v1 ",
            "a" * 64,
            "b" * 64,
            " test-device ",
        )
        self.assertEqual(binding.tokenizer_identity, "tokenizer-contract-v1")
        self.assertEqual(binding.device_class, "test-device")

    def test_validated_staging_fact_is_rechecked_before_ready(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name).absolute()
            output = root / "output"
            recovery = root / "recovery"
            output.mkdir()
            recovery.mkdir()
            service = ApplicationService(
                counter,
                settings(output, recovery, write_history=True),
                backend=SyntheticBackend(),
            )
            preview = service.preview("synthetic handoff input")
            real_digest = resilience_module._file_digest
            mutated = False

            def mutate_before_recovery_hash(path: Path):
                nonlocal mutated
                if not mutated and path.name == "output":
                    mutated = True
                    with path.open("r+b") as staged:
                        staged.seek(44)
                        staged.write(b"\x00\x00\x00\x00")
                return real_digest(path)

            with patch(
                "neutral_tts.resilience._file_digest",
                side_effect=mutate_before_recovery_hash,
            ):
                with self.assertRaises(RecoveryError):
                    service.run("synthetic handoff input", preview)
            self.assertTrue(mutated)
            self.assertEqual(list(output.iterdir()), [])

    def test_real_cli_uses_only_verified_key_matched_history_for_calibration(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name).absolute()
            private_config = root / "runtime-config"
            private_config.write_text("{}", encoding="utf-8")
            backend = SyntheticBackend()

            def factory(_path: Path) -> RealRuntimeBinding:
                return RealRuntimeBinding(
                    counter,
                    backend,
                    "tokenizer-contract-v1",
                    "a" * 64,
                    "b" * 64,
                    "test-device",
                )

            common = [
                "--runtime-config",
                str(private_config),
                "--text",
                "calibration input",
                "--output-dir",
                str(root / "output"),
                "--staging-dir",
                str(root / "recovery"),
                "--date",
                "20260909",
                "--time",
                "120000",
            ]
            mismatched_settings = replace(
                settings(
                    root / "output",
                    root / "recovery",
                    write_history=True,
                ),
                runtime_config_digest="c" * 64,
                voice_rights_manifest_sha256="d" * 64,
            )
            (root / "output").mkdir()
            (root / "recovery").mkdir()
            mismatched_service = ApplicationService(
                counter,
                mismatched_settings,
                backend=backend,
            )
            mismatched_preview = mismatched_service.preview("mismatched calibration")
            mismatched_service.run("mismatched calibration", mismatched_preview)
            payloads = []
            for _index in range(6):
                code, payload = self._invoke(
                    ["real-run", *common], real_runtime_factory=factory
                )
                self.assertEqual(code, 0)
                payloads.append(payload)
            first_confidence = payloads[0]["outcome"]["receipt"]["estimate"][
                "confidence"
            ]
            sixth_confidence = payloads[5]["outcome"]["receipt"]["estimate"][
                "confidence"
            ]
            self.assertEqual(first_confidence["state"], "unknown")
            self.assertEqual(first_confidence["sample_count"], 0)
            self.assertEqual(sixth_confidence["state"], "calibrated")
            self.assertEqual(sixth_confidence["sample_count"], 5)
            self.assertEqual(backend.calls, 7)

    def test_process_loss_after_publish_is_completed_by_a_fresh_process(self) -> None:
        child = r'''
import os
import sys
from pathlib import Path
from neutral_tts.application import ApplicationService, PersistedSettings
from neutral_tts.cli import demo_token_counter
from neutral_tts.demo_backend import DEMO_SILENCE_ONLY
from neutral_tts.filename_templates import FilenameContext
from neutral_tts.resilience import RecoveryManager

output = Path(sys.argv[1]).absolute()
recovery = Path(sys.argv[2]).absolute()
output.mkdir()
recovery.mkdir()
config = PersistedSettings(
    filename_context=FilenameContext(
        date="20260909", time="120000", title="audio", voice="neutral",
        quality="balanced", reading="standard", job_id_short="restart", sequence=1,
    ),
    tokenizer_identity="demo-tokenizer-v1",
    output_directory=output,
    staging_directory=recovery,
    history_enabled=True,
    runtime_config_digest="a" * 64,
    voice_rights_manifest_sha256="b" * 64,
)
original_fault = RecoveryManager._fault
def stop_after_publish(self, transition):
    if transition == "publication_linked":
        os._exit(73)
    return original_fault(self, transition)
RecoveryManager._fault = stop_after_publish
service = ApplicationService(demo_token_counter, config, backend=DEMO_SILENCE_ONLY())
preview = service.preview("synthetic restart input")
service.run("synthetic restart input", preview)
'''
        with tempfile.TemporaryDirectory() as name:
            root = Path(name).absolute()
            output = root / "output"
            recovery = root / "recovery"
            environment = os.environ.copy()
            source_root = Path(__file__).resolve().parents[1] / "src"
            environment["PYTHONPATH"] = str(source_root)
            result = subprocess.run(
                [sys.executable, "-c", child, str(output), str(recovery)],
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                timeout=30,
            )
            self.assertEqual(result.returncode, 73, result.stderr.decode(errors="replace"))
            published = next(output.glob("*.wav"))
            published_bytes = published.read_bytes()
            pending = next((recovery / "history").glob("*.json"))
            self.assertEqual(json.loads(pending.read_text())["state"], "pending")

            def forbidden_counter(_text: str) -> int:
                raise AssertionError("startup/history must not tokenize")

            class ForbiddenBackend:
                @property
                def backend_identity(self):
                    raise AssertionError("startup/history must not inspect backend")

                def generate(self, _request):
                    raise AssertionError("startup/history must not generate")

            service = ApplicationService(
                forbidden_counter,
                settings(output, recovery, write_history=False),
                backend=ForbiddenBackend(),
            )
            startup = service.startup()
            self.assertEqual(startup.history.recovered_pending_records, 1)
            records = service.history_records()
            self.assertEqual(len(records), 1)
            record = records[0]
            self.assertEqual(record.output_sha256, hashlib.sha256(published_bytes).hexdigest())
            captured = []
            WinsoundPlayback().play(record, player=captured.append)
            self.assertEqual(captured, [published_bytes])
            self.assertEqual(json.loads(pending.read_text())["state"], "completed")

    def test_canonical_timestamp_order_uses_instants_not_string_order(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name).absolute()
            output = root / "output"
            recovery = root / "recovery"
            output.mkdir()
            recovery.mkdir()
            service = ApplicationService(
                counter,
                settings(output, recovery, write_history=True),
                backend=SyntheticBackend(),
            )
            preview = service.preview("synthetic timestamp input")
            with patch(
                "neutral_tts.history._now_utc",
                side_effect=(
                    "2026-09-09T12:00:00Z",
                    "2026-09-09T12:00:00.100000Z",
                ),
            ):
                outcome = service.run("synthetic timestamp input", preview)
            sidecar = next((recovery / "history").glob("*.json"))
            payload = json.loads(sidecar.read_text(encoding="utf-8"))
            self.assertEqual(payload["created_at_utc"], "2026-09-09T12:00:00Z")
            self.assertEqual(
                payload["completed_at_utc"],
                "2026-09-09T12:00:00.100000Z",
            )
            records = service.history_records(outcome.run_id)
            self.assertEqual(len(records), 1)
            self.assertEqual(
                records[0].completed_at_utc,
                "2026-09-09T12:00:00.100000Z",
            )

    def test_semantic_sidecar_tampering_is_quarantined(self) -> None:
        def change_receipt_size(payload: dict[str, object]) -> None:
            payload["receipt"]["actual"]["audio"]["wav_bytes"] += 2

        mutations = (
            ("receipt-size", change_receipt_size),
            (
                "receipt-duration",
                lambda payload: payload["receipt"]["actual"]["audio"].__setitem__(
                    "duration_seconds", 1.0
                ),
            ),
            (
                "backend",
                lambda payload: payload.__setitem__(
                    "backend_identity", "forged-backend-v1"
                ),
            ),
            (
                "runtime",
                lambda payload: payload.__setitem__(
                    "runtime_config_digest", "c" * 64
                ),
            ),
            (
                "rights",
                lambda payload: payload.__setitem__(
                    "voice_rights_manifest_sha256", "d" * 64
                ),
            ),
        )
        for label, mutate in mutations:
            with self.subTest(field=label), tempfile.TemporaryDirectory() as name:
                root = Path(name).absolute()
                output = root / "output"
                recovery = root / "recovery"
                output.mkdir()
                recovery.mkdir()
                service = ApplicationService(
                    counter,
                    settings(output, recovery, write_history=True),
                    backend=SyntheticBackend(),
                )
                preview = service.preview("synthetic sidecar integrity input")
                outcome = service.run("synthetic sidecar integrity input", preview)
                published = output / outcome.published_wav_name
                expected_wav = published.read_bytes()
                sidecar = next((recovery / "history").glob("*.json"))
                payload = json.loads(sidecar.read_text(encoding="utf-8"))
                mutate(payload)
                sidecar.write_text(
                    json.dumps(
                        payload,
                        ensure_ascii=True,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    encoding="utf-8",
                )

                self.assertEqual(service.history_records(outcome.run_id), ())
                self.assertFalse(sidecar.exists())
                self.assertEqual(published.read_bytes(), expected_wav)
                self.assertEqual(
                    len(list((recovery / "quarantine").glob("history-*.json"))),
                    1,
                )

    def test_tampered_output_is_neither_listed_nor_played(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name).absolute()
            output = root / "output"
            recovery = root / "recovery"
            output.mkdir()
            recovery.mkdir()
            backend = SyntheticBackend()
            service = ApplicationService(
                counter,
                settings(output, recovery, write_history=True),
                backend=backend,
            )
            preview = service.preview("synthetic tamper input")
            outcome = service.run("synthetic tamper input", preview)
            (record,) = service.history_records(outcome.run_id)
            record.output_path.write_bytes(b"changed")
            with self.assertRaises(PlaybackError):
                WinsoundPlayback().play(record, player=lambda _data: None)

            fresh = ApplicationService(
                lambda _text: (_ for _ in ()).throw(AssertionError("no tokenization")),
                settings(output, recovery, write_history=False),
            )
            self.assertEqual(fresh.history_records(outcome.run_id), ())
            self.assertFalse(list((recovery / "history").glob("*.json")))
            self.assertTrue(list((recovery / "quarantine").glob("history-*.json")))

    def test_sidecar_filename_must_match_its_run_identity(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name).absolute()
            output = root / "output"
            recovery = root / "recovery"
            output.mkdir()
            recovery.mkdir()
            service = ApplicationService(
                counter,
                settings(output, recovery, write_history=True),
                backend=SyntheticBackend(),
            )
            preview = service.preview("synthetic sidecar input")
            outcome = service.run("synthetic sidecar input", preview)
            sidecar = next((recovery / "history").glob("*.json"))
            duplicate = sidecar.with_name("app-other.json")
            duplicate.write_bytes(sidecar.read_bytes())

            records = service.history_records()
            self.assertEqual(
                [record.run_id for record in records],
                [outcome.run_id],
            )
            self.assertFalse(duplicate.exists())
            self.assertEqual(
                len(list((recovery / "quarantine").glob("history-*.json"))),
                1,
            )

    def test_reparse_output_is_not_verified_when_supported(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name).absolute()
            output = root / "output"
            recovery = root / "recovery"
            output.mkdir()
            recovery.mkdir()
            service = ApplicationService(
                counter,
                settings(output, recovery, write_history=True),
                backend=SyntheticBackend(),
            )
            preview = service.preview("synthetic link input")
            outcome = service.run("synthetic link input", preview)
            (record,) = service.history_records(outcome.run_id)
            external = root / "external-copy"
            external.write_bytes(record.output_path.read_bytes())
            record.output_path.unlink()
            try:
                os.symlink(external, record.output_path)
            except (OSError, NotImplementedError):
                self.skipTest("symlink creation is unavailable on this host")
            with self.assertRaises(PlaybackError):
                WinsoundPlayback().play(record, player=lambda _data: None)
            fresh = ApplicationService(
                counter,
                settings(output, recovery, write_history=False),
            )
            self.assertEqual(fresh.history_records(outcome.run_id), ())
            self.assertEqual(external.read_bytes(), record.output_path.read_bytes())
            self.assertFalse(list((recovery / "history").glob("*.json")))

    def test_reparse_history_sidecar_is_moved_without_touching_target_when_supported(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name).absolute()
            output = root / "output"
            recovery = root / "recovery"
            output.mkdir()
            recovery.mkdir()
            service = ApplicationService(
                counter,
                settings(output, recovery, write_history=True),
                backend=SyntheticBackend(),
            )
            preview = service.preview("synthetic linked history input")
            outcome = service.run("synthetic linked history input", preview)
            sidecar = next((recovery / "history").glob("*.json"))
            target = root / "external-history-copy"
            target.write_bytes(sidecar.read_bytes())
            expected = target.read_bytes()
            sidecar.unlink()
            try:
                os.symlink(target, sidecar)
            except (OSError, NotImplementedError):
                self.skipTest("symlink creation is unavailable on this host")

            fresh = ApplicationService(
                counter,
                settings(output, recovery, write_history=False),
            )
            self.assertEqual(fresh.history_records(outcome.run_id), ())
            self.assertEqual(target.read_bytes(), expected)
            self.assertFalse(list((recovery / "history").glob("*.json")))
            quarantined = list((recovery / "quarantine").glob("history-*.json"))
            self.assertEqual(len(quarantined), 1)
            self.assertTrue(quarantined[0].is_symlink())


if __name__ == "__main__":
    unittest.main()
