"""Explicit deterministic demo backend that emits silence, never speech.

There are intentionally no imports from a model, sampler, GPU, inference, or
audio-runtime package in this module.  It exists to exercise the complete
application workflow with a valid PCM16 WAV artifact.
"""

from __future__ import annotations

import os
import stat
import time
import wave
from pathlib import Path

from .application import (
    BackendResult,
    DigestMismatchError,
    GenerationRequest,
    UnsafeDirectoryError,
    _safe_existing_directory,
)
from .receipts import ActualRun


class DEMO_SILENCE_ONLY:
    """A clearly non-synthesis backend producing 100 ms mono PCM16 silence."""

    backend_identity = "DEMO_SILENCE_ONLY-v1"

    def generate(self, request: GenerationRequest) -> BackendResult:
        if not isinstance(request, GenerationRequest):
            raise TypeError("demo backend requires GenerationRequest")
        if request.plan.plan_digest != request.estimate.plan_digest:
            raise DigestMismatchError("plan and estimate digests do not match")
        path = request.staging_path
        if not isinstance(path, Path) or not path.is_absolute() or path.exists() or path.is_symlink():
            raise UnsafeDirectoryError("staging path is not exclusively owned")
        parent = _safe_existing_directory(path.parent, label="staging directory")
        if parent != path.parent:
            raise UnsafeDirectoryError("staging path is not owned by staging directory")
        sample_rate = 16_000
        frames = 1_600
        started = time.monotonic()
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            # fdopen keeps the exclusively-created descriptor rather than
            # reopening a path that another actor could replace.
            with os.fdopen(fd, "wb") as raw:
                with wave.open(raw, "wb") as wav:
                    wav.setnchannels(1)
                    wav.setsampwidth(2)
                    wav.setframerate(sample_rate)
                    wav.writeframes(b"\x00\x00" * frames)
            metadata = path.lstat()
            if not stat.S_ISREG(metadata.st_mode) or path.is_symlink():
                raise UnsafeDirectoryError("staging path changed during demo generation")
            wav_bytes = metadata.st_size
        except Exception:
            # Preserve the exclusively-created pathname on failure.  Deleting
            # it could race with a replacement/observer and turn an adapter
            # error into an unsafe cleanup operation.
            raise
        actual = ActualRun(
            request.plan.total_tokens,
            request.plan.chunk_count,
            request.plan.boundary_count,
            sample_rate,
            frames / sample_rate,
            wav_bytes,
            generation_seconds=max(time.monotonic() - started, 0.000001),
            staging_bytes_peak=wav_bytes,
        )
        return BackendResult(path, actual)


def demo_silence_only_backend() -> DEMO_SILENCE_ONLY:
    return DEMO_SILENCE_ONLY()


__all__ = ["DEMO_SILENCE_ONLY", "demo_silence_only_backend"]
