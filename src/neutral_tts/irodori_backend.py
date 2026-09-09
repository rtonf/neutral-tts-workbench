"""Optional Irodori-compatible backend with a standard-library public surface.

The optional tokenizer and synthesis runtime are supplied by the machine-local
host.  Importing this module never imports a model, sampler, tensor library, or
tokenizer package.  The backend consumes the narrow adapter methods already
used by the development runtime and writes one final WAV directly to the
application-owned staging path.
"""

from __future__ import annotations

import hashlib
import importlib
import math
import operator
import os
import stat
import struct
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Protocol

from .application import (
    ApplicationError,
    BackendResult,
    DigestMismatchError,
    GenerationRequest,
    UnsafeDirectoryError,
    _safe_existing_directory,
)
from .fs_safety import (
    DirectoryGuard,
    FilesystemBoundaryError,
    open_exclusive_binary,
)
from .receipts import ActualRun
from .resilience import RecoveryError, _file_digest


class IrodoriBackendError(ApplicationError):
    """The optional host runtime did not satisfy the backend contract."""


class _IrodoriReferenceLease(Protocol):
    def __enter__(self) -> "_IrodoriReferenceLease": ...

    def verify(self) -> None: ...

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None: ...


class _IrodoriAdapter(Protocol):
    """The exact adapter-method subset consumed by :class:`IrodoriBackend`."""

    def create_runtime(self, resources: object) -> object: ...

    def count_tokens(self, runtime: object, text: str) -> int: ...

    def watermark_ready(self, runtime: object) -> bool: ...

    def reference_lease(
        self,
        reference_paths: Sequence[Path],
        *,
        reference_guard: Callable[[], Sequence[Path] | None],
    ) -> _IrodoriReferenceLease: ...

    def synthesize(
        self,
        runtime: object,
        request: object,
        *,
        log_fn: Callable[[str], None] | None = None,
        reference_lease: _IrodoriReferenceLease | None = None,
    ) -> object: ...

    def close_runtime(self, runtime: object) -> None: ...


class IrodoriTokenizerCounter:
    """Lazy, model-free token counter matching the runtime's explicit BOS rule.

    ``tokenizer_loader`` must return the underlying Hugging Face-style
    tokenizer, not an inference runtime.  Its ``encode`` method is called with
    ``add_special_tokens=False`` and the configured BOS token is counted once,
    matching the compatible runtime wrapper.
    """

    def __init__(
        self,
        tokenizer_loader: Callable[[], object],
        *,
        expected_vocab_size: int,
        expected_bos_token_id: int,
    ) -> None:
        if not callable(tokenizer_loader):
            raise TypeError("tokenizer_loader must be callable")
        for value, label in (
            (expected_vocab_size, "expected_vocab_size"),
            (expected_bos_token_id, "expected_bos_token_id"),
        ):
            if type(value) is not int or value < 0:
                raise ValueError(f"{label} must be a non-negative integer")
        if expected_vocab_size <= 0 or expected_bos_token_id >= expected_vocab_size:
            raise ValueError("tokenizer identity is inconsistent")
        self._loader = tokenizer_loader
        self._expected_vocab_size = expected_vocab_size
        self._expected_bos_token_id = expected_bos_token_id
        self._tokenizer: object | None = None

    @classmethod
    def from_local_directory(
        cls,
        directory: Path,
        *,
        expected_vocab_size: int,
        expected_bos_token_id: int,
    ) -> "IrodoriTokenizerCounter":
        """Create a lazy local-only counter without importing optional packages.

        The directory and its asset-manifest verification remain host-private.
        This constructor performs no import or file access until the first
        count, and it never permits a network fallback or remote code.
        """

        if not isinstance(directory, Path) or not directory.is_absolute():
            raise ValueError("tokenizer directory must be an absolute path")
        fixed_directory = directory.absolute()

        def load() -> object:
            module = importlib.import_module("transformers")
            tokenizer_type = getattr(module, "AutoTokenizer", None)
            loader = getattr(tokenizer_type, "from_pretrained", None)
            if not callable(loader):
                raise IrodoriBackendError("tokenizer package contract is unavailable")
            return loader(
                str(fixed_directory),
                local_files_only=True,
                use_fast=True,
                trust_remote_code=False,
            )

        return cls(
            load,
            expected_vocab_size=expected_vocab_size,
            expected_bos_token_id=expected_bos_token_id,
        )

    def _loaded(self) -> object:
        if self._tokenizer is None:
            tokenizer = self._loader()
            try:
                vocab_size = len(tokenizer)  # type: ignore[arg-type]
            except (TypeError, AttributeError) as exc:
                raise IrodoriBackendError("tokenizer vocabulary is unavailable") from exc
            if type(vocab_size) is not int or vocab_size != self._expected_vocab_size:
                raise IrodoriBackendError("tokenizer vocabulary identity changed")
            bos_token_id = getattr(tokenizer, "bos_token_id", None)
            if (
                type(bos_token_id) is not int
                or bos_token_id != self._expected_bos_token_id
            ):
                raise IrodoriBackendError("tokenizer BOS identity changed")
            self._tokenizer = tokenizer
        return self._tokenizer

    def __call__(self, text: str) -> int:
        if type(text) is not str or not text:
            raise ValueError("token-count text must be a non-empty exact string")
        tokenizer = self._loaded()
        encode = getattr(tokenizer, "encode", None)
        if not callable(encode):
            raise IrodoriBackendError("tokenizer encode contract is unavailable")
        token_ids = encode(text, add_special_tokens=False)
        if type(token_ids) is not list or any(type(value) is not int for value in token_ids):
            raise IrodoriBackendError("tokenizer returned an invalid token sequence")
        if any(value < 0 or value >= self._expected_vocab_size for value in token_ids):
            raise IrodoriBackendError("tokenizer returned an out-of-vocabulary token")
        return 1 + len(token_ids)


@dataclass(frozen=True, slots=True)
class IrodoriBackendHost:
    """Machine-local objects needed to bind the compatible runtime.

    Paths and runtime resources are deliberately opaque and are never included
    in application payloads or receipts.  The adapter factory stays lazy so
    preview, startup, history, and playback do not import or load the runtime.
    """

    adapter_factory: Callable[[], _IrodoriAdapter]
    runtime_resources: object
    synthesis_request_factory: Callable[..., object]
    reference_paths: tuple[Path, ...]
    reference_guard: Callable[[], Sequence[Path] | None]

    def __post_init__(self) -> None:
        if not callable(self.adapter_factory):
            raise TypeError("adapter_factory must be callable")
        if not callable(self.synthesis_request_factory):
            raise TypeError("synthesis_request_factory must be callable")
        if not callable(self.reference_guard):
            raise TypeError("reference_guard must be callable")
        references = tuple(
            Path(os.path.abspath(path)) if isinstance(path, Path) else path
            for path in self.reference_paths
        )
        if not references or any(
            not isinstance(path, Path) or not path.is_absolute() for path in references
        ):
            raise ValueError("reference paths must be non-empty absolute paths")
        object.__setattr__(self, "reference_paths", references)

    def verified_reference_paths(self) -> tuple[Path, ...]:
        """Revalidate the host-private rights/integrity snapshot without disclosure."""

        try:
            guarded = self.reference_guard()
            if guarded is None:
                raise ValueError("reference guard returned no paths")
            observed = tuple(
                Path(os.path.abspath(path)) if isinstance(path, Path) else path
                for path in guarded
            )
        except Exception as exc:
            raise IrodoriBackendError("reference evidence is unavailable") from exc
        if observed != self.reference_paths:
            raise IrodoriBackendError("reference evidence changed")
        return self.reference_paths


def _plain_positive_int(value: object, *, label: str) -> int:
    if type(value) is not int or value <= 0:
        raise IrodoriBackendError(f"{label} is invalid")
    return value


def _mono_float_samples(audio: object, *, max_frames: int) -> list[float]:
    """Detach a compatible ``(1, frames)`` tensor without importing its library."""

    value = audio
    detach = getattr(value, "detach", None)
    if callable(detach):
        value = detach()
    cpu = getattr(value, "cpu", None)
    if callable(cpu):
        value = cpu()
    shape_value = getattr(value, "shape", None)
    try:
        shape = tuple(
            operator.index(item) if not isinstance(item, bool) else -1
            for item in shape_value
        )
    except (TypeError, ValueError) as exc:
        raise IrodoriBackendError("synthesis audio shape is unavailable") from exc
    if (
        len(shape) != 2
        or shape[0] != 1
        or shape[1] <= 0
        or shape[1] > max_frames
    ):
        raise IrodoriBackendError("synthesis audio must be non-empty mono data")
    tolist = getattr(value, "tolist", None)
    if not callable(tolist):
        raise IrodoriBackendError("synthesis audio values are unavailable")
    nested = tolist()
    if type(nested) is not list or len(nested) != 1 or type(nested[0]) is not list:
        raise IrodoriBackendError("synthesis audio layout is invalid")
    raw = nested[0]
    if len(raw) != shape[1]:
        raise IrodoriBackendError("synthesis audio extent is inconsistent")
    samples: list[float] = []
    for item in raw:
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            raise IrodoriBackendError("synthesis audio sample is invalid")
        sample = float(item)
        if not math.isfinite(sample) or sample < -1.0 or sample > 1.0:
            raise IrodoriBackendError("synthesis audio sample is out of range")
        samples.append(sample)
    return samples


def _pcm16(samples: Sequence[float]) -> bytes:
    encoded = bytearray()
    block_size = 32_768
    for offset in range(0, len(samples), block_size):
        values = []
        for sample in samples[offset : offset + block_size]:
            scaled = -32_768 if sample == -1.0 else int(round(sample * 32_768.0))
            values.append(max(-32_768, min(32_767, scaled)))
        encoded.extend(struct.pack(f"<{len(values)}h", *values))
    return bytes(encoded)


def _chunk_seed(seed: int, index: int) -> int:
    return (seed + index * 1_000_003) % (2**31)


class IrodoriBackend:
    """Bridge an admitted plan to one compatible, watermarked PCM16 WAV."""

    backend_identity = "irodori-compatible-v1"
    expected_sample_rate = 48_000
    max_chunk_seconds = 30
    max_boundary_pause_frames = 48_000

    def __init__(self, host: IrodoriBackendHost, *, seed: int = 0) -> None:
        if not isinstance(host, IrodoriBackendHost):
            raise TypeError("host must be IrodoriBackendHost")
        if type(seed) is not int:
            raise ValueError("seed must be an integer")
        self._host = host
        self._seed = seed

    @staticmethod
    def _validate_request(request: GenerationRequest) -> None:
        if not isinstance(request, GenerationRequest):
            raise TypeError("Irodori backend requires GenerationRequest")
        if request.plan.plan_digest != request.estimate.plan_digest:
            raise DigestMismatchError("plan and estimate digests do not match")
        if request.plan.settings.quality != "balanced":
            raise IrodoriBackendError("unsupported quality setting")
        if request.plan.settings.reading != "standard":
            raise IrodoriBackendError("unsupported reading setting")
        if request.plan.settings.max_tokens != 256:
            raise IrodoriBackendError("unsupported token budget")
        if request.plan.settings.max_characters != 144:
            raise IrodoriBackendError("unsupported character budget")
        if float(request.plan.settings.duration_scale) != 1.0:
            raise IrodoriBackendError("unsupported duration scale")
        if request.plan.pause_policy.sample_rate != IrodoriBackend.expected_sample_rate:
            raise IrodoriBackendError("pause sample rate differs from synthesis")
        if request.plan.pause_policy.samples > IrodoriBackend.max_boundary_pause_frames:
            raise IrodoriBackendError("boundary pause exceeds the compatible limit")
        path = request.staging_path
        if (
            not isinstance(path, Path)
            or not path.is_absolute()
            or os.path.lexists(path)
        ):
            raise UnsafeDirectoryError("staging path is not exclusively owned")
        parent = _safe_existing_directory(path.parent, label="staging directory")
        if parent != path.parent:
            raise UnsafeDirectoryError("staging path is not owned by staging directory")

    def _vendor_request(self, text: str, index: int, duration_scale: float) -> object:
        return self._host.synthesis_request_factory(
            text=text,
            reference_paths=self._host.reference_paths,
            caption=None,
            seconds=None,
            duration_scale=duration_scale,
            min_seconds=0.5,
            max_seconds=30.0,
            max_ref_seconds=30.0,
            num_steps=40,
            cfg_scale_text=3.0,
            cfg_scale_caption=3.0,
            cfg_scale_speaker=5.0,
            cfg_guidance_mode="independent",
            seed=_chunk_seed(self._seed, index),
            trim_tail=False,
            ref_normalize_db=-16.0,
            ref_ensure_max=True,
            num_candidates=1,
            decode_mode="sequential",
        )

    @staticmethod
    def _write_wave(
        path: Path,
        chunks: Sequence[bytes],
        pause_frames: int,
        guard: DirectoryGuard,
    ) -> tuple[int, str]:
        pause_bytes = pause_frames * 2 * max(len(chunks) - 1, 0)
        data_size = sum(len(chunk) for chunk in chunks) + pause_bytes
        if data_size <= 0 or data_size > 0xFFFFFFFF - 36:
            raise IrodoriBackendError("synthesis WAV extent is unsupported")
        header = struct.pack(
            "<4sI4s4sIHHIIHH4sI",
            b"RIFF",
            36 + data_size,
            b"WAVE",
            b"fmt ",
            16,
            1,
            1,
            IrodoriBackend.expected_sample_rate,
            IrodoriBackend.expected_sample_rate * 2,
            2,
            16,
            b"data",
            data_size,
        )
        expected_size = len(header) + data_size
        expected_digest = hashlib.sha256()

        def write_exact(raw: BinaryIO, block: bytes) -> None:
            written = raw.write(block)
            if written != len(block):
                raise OSError("short staging write")
            expected_digest.update(block)

        try:
            with open_exclusive_binary(guard, path) as raw:
                descriptor_before = os.fstat(raw.fileno())
                write_exact(raw, header)
                for index, pcm in enumerate(chunks):
                    write_exact(raw, pcm)
                    if index < len(chunks) - 1:
                        remaining = pause_frames
                        while remaining:
                            block_frames = min(remaining, 32_768)
                            write_exact(raw, bytes(block_frames * 2))
                            remaining -= block_frames
                raw.flush()
                os.fsync(raw.fileno())
                descriptor_after = os.fstat(raw.fileno())
                if (
                    raw.tell() != expected_size
                    or descriptor_after.st_size != expected_size
                    or (descriptor_before.st_dev, descriptor_before.st_ino)
                    != (descriptor_after.st_dev, descriptor_after.st_ino)
                ):
                    raise UnsafeDirectoryError(
                        "staging descriptor changed during generation"
                    )
            guard.assert_unchanged()
            observed_size, observed_sha256 = _file_digest(path)
        except (FilesystemBoundaryError, RecoveryError) as exc:
            raise UnsafeDirectoryError(
                "staging path changed during generation"
            ) from exc
        metadata = path.lstat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or path.is_symlink()
            or (observed_size, observed_sha256)
            != (expected_size, expected_digest.hexdigest())
        ):
            raise UnsafeDirectoryError("staging bytes changed during generation")
        return observed_size, observed_sha256

    @staticmethod
    def _cleanup(
        adapter: _IrodoriAdapter | None,
        runtime: object | None,
        staging_guard: DirectoryGuard,
        primary_error: BaseException | None,
    ) -> None:
        """Run every cleanup step without replacing an in-flight failure."""

        cleanup_error: BaseException | None = None
        try:
            if runtime is not None and adapter is not None:
                adapter.close_runtime(runtime)
        except BaseException as exc:
            cleanup_error = exc
        try:
            staging_guard.close()
        except BaseException as exc:
            if cleanup_error is None:
                cleanup_error = exc
        if cleanup_error is None:
            return
        if primary_error is not None:
            add_note = getattr(primary_error, "add_note", None)
            if callable(add_note):
                add_note("Irodori backend cleanup also failed")
            return
        raise IrodoriBackendError("Irodori backend cleanup failed") from cleanup_error

    def generate(self, request: GenerationRequest) -> BackendResult:
        self._validate_request(request)
        try:
            staging_guard = DirectoryGuard(request.staging_path.parent)
        except FilesystemBoundaryError as exc:
            raise UnsafeDirectoryError(
                "staging path is not exclusively owned"
            ) from exc
        started = time.monotonic()
        adapter: _IrodoriAdapter | None = None
        runtime: object | None = None
        primary_error: BaseException | None = None
        try:
            self._host.verified_reference_paths()
            adapter = self._host.adapter_factory()
            runtime = adapter.create_runtime(self._host.runtime_resources)
            if not adapter.watermark_ready(runtime):
                raise IrodoriBackendError("watermark support is unavailable")

            # Verify every chunk before the first sampler call.  A late mismatch
            # must not produce a partial generation or consume sampler work.
            for chunk in request.plan.chunks:
                count = adapter.count_tokens(runtime, chunk.text)
                if (
                    type(count) is not int
                    or count != chunk.token_count
                    or count > request.plan.settings.max_tokens
                ):
                    raise IrodoriBackendError("runtime token count differs from the plan")

            lease_context = adapter.reference_lease(
                self._host.reference_paths,
                reference_guard=self._host.reference_guard,
            )
            with lease_context as reference_lease:
                chunk_pcm: list[bytes] = []
                frame_count = 0
                for chunk in request.plan.chunks:
                    reference_lease.verify()
                    vendor_request = self._vendor_request(
                        chunk.text,
                        chunk.index,
                        float(request.plan.settings.duration_scale),
                    )
                    result = adapter.synthesize(
                        runtime,
                        vendor_request,
                        log_fn=None,
                        reference_lease=reference_lease,
                    )
                    reference_lease.verify()
                    sample_rate = _plain_positive_int(
                        getattr(result, "sample_rate", None),
                        label="synthesis sample rate",
                    )
                    if sample_rate != self.expected_sample_rate:
                        raise IrodoriBackendError(
                            "synthesis sample rate is unsupported"
                        )
                    samples = _mono_float_samples(
                        getattr(result, "audio", None),
                        max_frames=self.expected_sample_rate * self.max_chunk_seconds,
                    )
                    pcm = _pcm16(samples)
                    if not pcm or len(pcm) % 2:
                        raise IrodoriBackendError("synthesis PCM extent is invalid")
                    chunk_pcm.append(pcm)
                    frame_count += len(pcm) // 2

                pause_frames = request.plan.pause_policy.samples
                frame_count += pause_frames * request.plan.boundary_count
                wav_bytes, staged_sha256 = self._write_wave(
                    request.staging_path,
                    chunk_pcm,
                    pause_frames,
                    staging_guard,
                )
            elapsed = max(time.monotonic() - started, 0.000001)
            actual = ActualRun(
                request.plan.total_tokens,
                request.plan.chunk_count,
                request.plan.boundary_count,
                self.expected_sample_rate,
                frame_count / self.expected_sample_rate,
                wav_bytes,
                generation_seconds=elapsed,
                staging_bytes_peak=wav_bytes,
            )
            return BackendResult(
                request.staging_path,
                actual,
                staged_sha256=staged_sha256,
            )
        except BaseException as exc:
            primary_error = exc
            raise
        finally:
            self._cleanup(adapter, runtime, staging_guard, primary_error)


__all__ = [
    "IrodoriBackend",
    "IrodoriBackendError",
    "IrodoriBackendHost",
    "IrodoriTokenizerCounter",
]
