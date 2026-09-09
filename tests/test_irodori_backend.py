from __future__ import annotations

import hashlib
import math
import tempfile
import unittest
import wave
from dataclasses import dataclass, replace
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import neutral_tts.irodori_backend as irodori_backend_module
from neutral_tts.application import (
    ApplicationError,
    ApplicationService,
    GenerationRequest,
    PersistedSettings,
    UnsafeDirectoryError,
)
from neutral_tts.estimation import estimate_from_plan
from neutral_tts.filename_templates import FilenameContext
from neutral_tts.irodori_backend import (
    IrodoriBackend,
    IrodoriBackendError,
    IrodoriBackendHost,
    IrodoriTokenizerCounter,
)
from neutral_tts.planning import PausePolicy, PlanSettings, prepare_text_plan


class FakeTokenizer:
    bos_token_id = 1

    def __len__(self) -> int:
        return 1024

    def encode(self, text: str, *, add_special_tokens: bool) -> list[int]:
        if add_special_tokens:
            raise AssertionError("special tokens must be controlled by the counter")
        return [10 + (index % 10) for index, _character in enumerate(text)]


class FakeTensor:
    def __init__(self, values: list[list[float]], shape: tuple[int, int] | None = None):
        self._values = values
        self.shape = shape if shape is not None else (
            len(values),
            len(values[0]) if values else 0,
        )

    def detach(self) -> "FakeTensor":
        return self

    def cpu(self) -> "FakeTensor":
        return self

    def tolist(self) -> list[list[float]]:
        return self._values


@dataclass
class FakeSamplingResult:
    audio: object
    audios: list[object]
    sample_rate: int


class FakeCount:
    def __init__(self, value: int):
        self._value = value

    def numel(self) -> int:
        return self._value


class FakeRuntimeTokenizer:
    def encode(self, text: str) -> FakeCount:
        return FakeCount(1 + len(text))


class FakeRuntime:
    def __init__(self, *, watermark_ready: bool = True):
        self.tokenizer = FakeRuntimeTokenizer()
        self.watermarker = type("Watermarker", (), {"ready": watermark_ready})()


class FakeSynthesisRequest:
    def __init__(self, **values: object):
        self.__dict__.update(values)


class FakeReferenceLease:
    def __init__(self, adapter: "FakeAdapter", expected_paths: tuple[Path, ...]):
        self.adapter = adapter
        self.expected_paths = expected_paths
        self.active = False

    def __enter__(self) -> "FakeReferenceLease":
        self.adapter.events.append(("lease_enter", self.expected_paths))
        self.active = True
        return self

    def verify(self) -> None:
        if not self.active:
            raise AssertionError("reference lease must be active")
        self.adapter.events.append(("lease_verify", self.expected_paths))

    def __exit__(self, _type: object, _value: object, _traceback: object) -> None:
        self.adapter.events.append(("lease_exit", self.expected_paths))
        self.active = False


class FakeAdapter:
    def __init__(
        self,
        *,
        results: list[FakeSamplingResult] | None = None,
        watermark_ready: bool = True,
        count_override: dict[str, int] | None = None,
        synthesis_error: BaseException | None = None,
        close_error: BaseException | None = None,
    ) -> None:
        default_audio = FakeTensor([[0.25, -0.25]])
        self.results = results or [
            FakeSamplingResult(default_audio, [default_audio], 48_000)
        ]
        self.runtime = FakeRuntime(watermark_ready=watermark_ready)
        self.count_override = count_override or {}
        self.synthesis_error = synthesis_error
        self.close_error = close_error
        self.events: list[tuple[str, object]] = []
        self.closed = 0
        self.requests: list[FakeSynthesisRequest] = []
        self.lease: FakeReferenceLease | None = None

    def create_runtime(self, resources: object) -> FakeRuntime:
        self.events.append(("create", resources))
        return self.runtime

    def count_tokens(self, runtime: object, text: str) -> int:
        self.events.append(("count", text))
        return self.count_override.get(text, 1 + len(text))

    def watermark_ready(self, runtime: object) -> bool:
        self.events.append(("watermark", runtime))
        return bool(self.runtime.watermarker.ready)

    def reference_lease(
        self,
        reference_paths: tuple[Path, ...],
        *,
        reference_guard,
    ) -> FakeReferenceLease:
        observed = tuple(reference_guard() or ())
        if observed != tuple(reference_paths):
            raise AssertionError("reference guard differs from the request")
        self.events.append(("lease_create", tuple(reference_paths)))
        self.lease = FakeReferenceLease(self, tuple(reference_paths))
        return self.lease

    def synthesize(
        self,
        runtime: object,
        request: FakeSynthesisRequest,
        *,
        log_fn,
        reference_lease: FakeReferenceLease,
    ) -> FakeSamplingResult:
        if log_fn is not None or reference_lease is not self.lease:
            raise AssertionError("backend did not use the compatible adapter call shape")
        reference_lease.verify()
        self.events.append(("synthesize", request.text))
        self.requests.append(request)
        if self.synthesis_error is not None:
            raise self.synthesis_error
        return self.results[len(self.requests) - 1]

    def close_runtime(self, runtime: object) -> None:
        self.events.append(("close", runtime))
        self.closed += 1
        if self.close_error is not None:
            raise self.close_error


def make_counter() -> IrodoriTokenizerCounter:
    return IrodoriTokenizerCounter(
        FakeTokenizer,
        expected_vocab_size=1024,
        expected_bos_token_id=1,
    )


def make_request(root: Path, text: str = "abcd") -> GenerationRequest:
    plan = prepare_text_plan(
        text,
        make_counter(),
        settings=PlanSettings(max_tokens=256, max_characters=144),
        pause_policy=PausePolicy(Decimal("0.001"), 48_000),
        tokenizer_identity="tokenizer-contract-v1",
    )
    estimate = estimate_from_plan(plan, sample_rate_range=(48_000, 48_000))
    return GenerationRequest(plan, estimate, root / "output", "audio.wav")


def make_backend(root: Path, adapter: FakeAdapter, *, seed: int = 7) -> IrodoriBackend:
    reference = root / "reference-input"
    reference.write_bytes(b"synthetic-reference-placeholder")
    host = IrodoriBackendHost(
        adapter_factory=lambda: adapter,
        runtime_resources=object(),
        synthesis_request_factory=FakeSynthesisRequest,
        reference_paths=(reference,),
        reference_guard=lambda: (reference,),
    )
    return IrodoriBackend(host, seed=seed)


class IrodoriTokenizerCounterTests(unittest.TestCase):
    def test_counter_is_lazy_and_matches_runtime_bos_count(self) -> None:
        loads = 0

        def load() -> FakeTokenizer:
            nonlocal loads
            loads += 1
            return FakeTokenizer()

        counter = IrodoriTokenizerCounter(
            load,
            expected_vocab_size=1024,
            expected_bos_token_id=1,
        )
        self.assertEqual(loads, 0)
        runtime = FakeRuntime()
        for text in ("x", "abc", "z" * 255):
            self.assertEqual(counter(text), runtime.tokenizer.encode(text).numel())
        self.assertEqual(counter("z" * 255), 256)
        self.assertEqual(loads, 1)

    def test_local_counter_defers_optional_import_and_forces_offline_loading(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            directory = Path(name).absolute()
            calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

            class AutoTokenizer:
                @staticmethod
                def from_pretrained(*args: object, **kwargs: object) -> FakeTokenizer:
                    calls.append((args, kwargs))
                    return FakeTokenizer()

            module = type("FakeTransformers", (), {"AutoTokenizer": AutoTokenizer})
            with patch(
                "neutral_tts.irodori_backend.importlib.import_module",
                return_value=module,
            ) as imported:
                counter = IrodoriTokenizerCounter.from_local_directory(
                    directory,
                    expected_vocab_size=1024,
                    expected_bos_token_id=1,
                )
                imported.assert_not_called()
                self.assertEqual(counter("ab"), 3)
                imported.assert_called_once_with("transformers")
            self.assertEqual(
                calls,
                [
                    (
                        (str(directory),),
                        {
                            "local_files_only": True,
                            "use_fast": True,
                            "trust_remote_code": False,
                        },
                    )
                ],
            )

    def test_counter_rejects_changed_identity_and_invalid_ids(self) -> None:
        changed = FakeTokenizer()
        changed.bos_token_id = 2
        counter = IrodoriTokenizerCounter(
            lambda: changed,
            expected_vocab_size=1024,
            expected_bos_token_id=1,
        )
        with self.assertRaises(IrodoriBackendError):
            counter("text")

        class InvalidTokenizer(FakeTokenizer):
            def encode(self, text: str, *, add_special_tokens: bool) -> list[int]:
                return [len(self)]

        invalid = IrodoriTokenizerCounter(
            InvalidTokenizer,
            expected_vocab_size=1024,
            expected_bos_token_id=1,
        )
        with self.assertRaises(IrodoriBackendError):
            invalid("text")

        boolean_bos = FakeTokenizer()
        boolean_bos.bos_token_id = True
        invalid_bos = IrodoriTokenizerCounter(
            lambda: boolean_bos,
            expected_vocab_size=1024,
            expected_bos_token_id=1,
        )
        with self.assertRaises(IrodoriBackendError):
            invalid_bos("text")


class IrodoriBackendTests(unittest.TestCase):
    def test_all_chunks_are_prechecked_then_one_wav_gets_exact_boundary_pause(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name).absolute()
            first = FakeTensor([[0.25, -0.25]])
            second = FakeTensor([[0.5, -0.5, 0.0]])
            adapter = FakeAdapter(
                results=[
                    FakeSamplingResult(first, [first], 48_000),
                    FakeSamplingResult(second, [second], 48_000),
                ]
            )
            request = make_request(root, "a" * 145)
            backend = make_backend(root, adapter)
            result = backend.generate(request)

            event_names = [name for name, _value in adapter.events]
            first_synthesis = event_names.index("synthesize")
            self.assertEqual(event_names[:first_synthesis].count("count"), 2)
            self.assertEqual(result.staged_path, request.staging_path)
            self.assertEqual(
                sorted(path.name for path in root.iterdir()),
                ["output", "reference-input"],
            )
            with wave.open(str(result.staged_path), "rb") as wav:
                self.assertEqual((wav.getnchannels(), wav.getsampwidth()), (1, 2))
                self.assertEqual(wav.getframerate(), 48_000)
                self.assertEqual(wav.getnframes(), 2 + 48 + 3)
                pcm = wav.readframes(wav.getnframes())
            self.assertEqual(pcm[4 : 4 + 48 * 2], bytes(48 * 2))
            self.assertEqual(result.actual.tokens, request.plan.total_tokens)
            self.assertEqual(result.actual.chunks, 2)
            self.assertEqual(result.actual.boundaries, 1)
            self.assertEqual(result.actual.sample_rate, 48_000)
            self.assertTrue(
                math.isclose(result.actual.duration_seconds, 53 / 48_000)
            )
            self.assertEqual(result.actual.wav_bytes, result.staged_path.stat().st_size)
            self.assertEqual(
                result.staged_sha256,
                hashlib.sha256(result.staged_path.read_bytes()).hexdigest(),
            )
            self.assertEqual(adapter.closed, 1)
            self.assertFalse(adapter.lease.active)
            self.assertEqual([item.text for item in adapter.requests], ["a" * 144, "a"])
            self.assertEqual([item.seed for item in adapter.requests], [7, 1_000_010])
            self.assertTrue(all(item.trim_tail is False for item in adapter.requests))

    def test_late_token_drift_prevents_every_sampler_call_and_output(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name).absolute()
            request = make_request(root, "b" * 145)
            adapter = FakeAdapter(count_override={"b": 99})
            backend = make_backend(root, adapter)
            with self.assertRaises(IrodoriBackendError):
                backend.generate(request)
            self.assertEqual(
                [event for event in adapter.events if event[0] == "synthesize"], []
            )
            self.assertFalse(request.staging_path.exists())
            self.assertEqual(adapter.closed, 1)

    def test_watermark_is_required_before_token_count_or_sampling(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name).absolute()
            request = make_request(root)
            adapter = FakeAdapter(watermark_ready=False)
            with self.assertRaises(IrodoriBackendError):
                make_backend(root, adapter).generate(request)
            self.assertEqual([name for name, _value in adapter.events], [
                "create", "watermark", "close"
            ])
            self.assertFalse(request.staging_path.exists())

    def test_invalid_sample_rate_channel_extent_and_float_values_are_rejected(self) -> None:
        invalid_results = (
            FakeSamplingResult(FakeTensor([[0.0]]), [object()], 44_100),
            FakeSamplingResult(
                FakeTensor([[0.0], [0.0]], shape=(2, 1)), [object()], 48_000
            ),
            FakeSamplingResult(FakeTensor([[float("nan")]]), [object()], 48_000),
            FakeSamplingResult(FakeTensor([[1.01]]), [object()], 48_000),
            FakeSamplingResult(FakeTensor([[]], shape=(1, 0)), [object()], 48_000),
            FakeSamplingResult(
                FakeTensor([[0.0]], shape=(1, 48_000 * 30 + 1)),
                [object()],
                48_000,
            ),
        )
        for invalid in invalid_results:
            with self.subTest(invalid=invalid), tempfile.TemporaryDirectory() as name:
                root = Path(name).absolute()
                request = make_request(root)
                adapter = FakeAdapter(results=[invalid])
                with self.assertRaises(IrodoriBackendError):
                    make_backend(root, adapter).generate(request)
                self.assertFalse(request.staging_path.exists())
                self.assertEqual(adapter.closed, 1)

    def test_runtime_closes_on_sampler_error_and_interrupt(self) -> None:
        for error in (RuntimeError("synthetic failure"), KeyboardInterrupt()):
            with self.subTest(error=type(error).__name__), tempfile.TemporaryDirectory() as name:
                root = Path(name).absolute()
                request = make_request(root)
                adapter = FakeAdapter(synthesis_error=error)
                with self.assertRaises(type(error)):
                    make_backend(root, adapter).generate(request)
                self.assertEqual(adapter.closed, 1)
                self.assertFalse(request.staging_path.exists())
                self.assertFalse(adapter.lease.active)

    def test_cleanup_failure_is_fatal_but_does_not_mask_sampler_failure(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name).absolute()
            request = make_request(root)
            close_failure = RuntimeError("synthetic close failure")
            adapter = FakeAdapter(close_error=close_failure)
            with self.assertRaises(IrodoriBackendError) as raised:
                make_backend(root, adapter).generate(request)
            self.assertIs(raised.exception.__cause__, close_failure)
            self.assertEqual(adapter.closed, 1)

        for primary in (RuntimeError("synthetic sampler failure"), KeyboardInterrupt()):
            with (
                self.subTest(primary=type(primary).__name__),
                tempfile.TemporaryDirectory() as name,
            ):
                root = Path(name).absolute()
                request = make_request(root)
                adapter = FakeAdapter(
                    synthesis_error=primary,
                    close_error=RuntimeError("synthetic close failure"),
                )
                with self.assertRaises(type(primary)) as raised:
                    make_backend(root, adapter).generate(request)
                self.assertIs(raised.exception, primary)
                self.assertIn(
                    "Irodori backend cleanup also failed",
                    getattr(raised.exception, "__notes__", ()),
                )
                self.assertEqual(adapter.closed, 1)

    def test_unsupported_plan_controls_and_changed_references_fail_before_sampling(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name).absolute()
            request = make_request(root)
            adapter = FakeAdapter()
            backend = make_backend(root, adapter)
            changed = root / "changed-reference"
            changed.write_bytes(b"synthetic-reference-placeholder")
            object.__setattr__(backend._host, "reference_guard", lambda: (changed,))
            with self.assertRaises(IrodoriBackendError):
                backend.generate(request)
            self.assertEqual(adapter.events, [])

            long_pause = replace(
                request.plan,
                pause_policy=PausePolicy(Decimal("1.001"), 48_000),
            )
            unsupported = replace(
                request,
                plan=long_pause,
                estimate=estimate_from_plan(
                    long_pause,
                    sample_rate_range=(48_000, 48_000),
                ),
            )
            with self.assertRaises(IrodoriBackendError):
                make_backend(root, FakeAdapter()).generate(unsupported)

    def test_application_service_publishes_without_demo_label(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name).absolute()
            output = root / "published"
            recovery = root / "recovery"
            output.mkdir()
            recovery.mkdir()
            adapter = FakeAdapter()
            backend = make_backend(root, adapter)
            settings = PersistedSettings(
                filename_context=FilenameContext(
                    date="20260909",
                    time="120000",
                    title="audio",
                    voice="neutral",
                    quality="balanced",
                    reading="standard",
                    job_id_short="abc123",
                    sequence=1,
                ),
                plan_settings=PlanSettings(max_tokens=256, max_characters=144),
                pause_policy=PausePolicy(Decimal("0.001"), 48_000),
                tokenizer_identity="tokenizer-contract-v1",
                output_directory=output,
                staging_directory=recovery,
            )
            service = ApplicationService(make_counter(), settings, backend=backend)
            preview = service.preview("test")
            outcome = service.run("test", preview)
            payload = outcome.public_payload()
            self.assertNotIn("demo", payload)
            self.assertEqual(payload["backend"]["identity"], "irodori-compatible-v1")
            published = output / outcome.published_wav_name
            self.assertTrue(published.is_file())
            self.assertEqual(adapter.closed, 1)

    def test_writer_rejects_same_size_mutation_before_return(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name).absolute()
            request = make_request(root)
            adapter = FakeAdapter()
            backend = make_backend(root, adapter)
            real_open = irodori_backend_module.open_exclusive_binary

            class MutatingContext:
                def __init__(self, inner, path: Path) -> None:
                    self.inner = inner
                    self.path = path

                def __enter__(self):
                    return self.inner.__enter__()

                def __exit__(self, exc_type, exc, traceback):
                    self.inner.flush()
                    with self.path.open("r+b") as other:
                        other.seek(44)
                        other.write(b"\x00\x00\x00\x00")
                        other.flush()
                    return self.inner.__exit__(exc_type, exc, traceback)

            def mutating_open(guard, path: Path):
                return MutatingContext(real_open(guard, path), path)

            with patch(
                "neutral_tts.irodori_backend.open_exclusive_binary",
                side_effect=mutating_open,
            ):
                with self.assertRaises(UnsafeDirectoryError):
                    backend.generate(request)
            self.assertEqual(adapter.closed, 1)

    def test_application_rejects_same_size_change_after_backend_hash(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name).absolute()
            output = root / "published"
            recovery = root / "recovery"
            output.mkdir()
            recovery.mkdir()
            adapter = FakeAdapter()
            real_backend = make_backend(root, adapter)

            class MutatingBackend:
                backend_identity = real_backend.backend_identity

                def generate(self, request: GenerationRequest):
                    result = real_backend.generate(request)
                    with result.staged_path.open("r+b") as staged:
                        staged.seek(44)
                        staged.write(b"\x00\x00\x00\x00")
                    return result

            configured = PersistedSettings(
                filename_context=FilenameContext(
                    date="20260909",
                    time="120000",
                    title="audio",
                    voice="neutral",
                    quality="balanced",
                    reading="standard",
                    job_id_short="abc123",
                    sequence=1,
                ),
                plan_settings=PlanSettings(max_tokens=256, max_characters=144),
                pause_policy=PausePolicy(Decimal("0.001"), 48_000),
                tokenizer_identity="tokenizer-contract-v1",
                output_directory=output,
                staging_directory=recovery,
            )
            service = ApplicationService(
                make_counter(),
                configured,
                backend=MutatingBackend(),
            )
            preview = service.preview("test")
            with self.assertRaises(ApplicationError):
                service.run("test", preview)
            self.assertEqual(list(output.iterdir()), [])
            self.assertEqual(adapter.closed, 1)


if __name__ == "__main__":
    unittest.main()
