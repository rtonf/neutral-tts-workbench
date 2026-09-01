"""Framework-neutral orchestration for preview, admission, and publication.

This module deliberately contains no web, GUI, model, sampler, or runtime
integration.  A caller supplies a tokenizer counter and a backend adapter.
The application service owns the important boundary between an immutable
preview and an admitted generation request.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import stat
import uuid
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol, runtime_checkable

from .estimation import Calibration, GenerationEstimate
from .filename_templates import (
    DEFAULT_TEMPLATE,
    FilenameContext,
    FilenamePreview,
    choose_collision_target,
    preview_filename,
    publish_staged_wav,
)
from .planning import DictionarySnapshot, PausePolicy, PlanSettings, TextPlan, normalize_public_id
from .preview import PreviewResult, pre_generation_preview
from .receipts import ActualRun, build_success_receipt


class ApplicationError(ValueError):
    """Base class for an invalid workflow or unsafe application boundary."""


class PreviewAdmissionError(ApplicationError):
    """The supplied preview cannot be admitted for generation."""


class StalePreviewError(PreviewAdmissionError):
    """The source/settings state no longer matches the preview."""


class DigestMismatchError(PreviewAdmissionError):
    """Plan, estimate, or filename evidence is internally inconsistent."""


class UnsafeDirectoryError(ApplicationError):
    """A configured output or staging directory is not safe to use."""


def _sha256_payload(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _safe_existing_directory(value: Path, *, label: str) -> Path:
    """Resolve a configured directory without following symlinks/reparse points."""

    if not isinstance(value, Path) or not value.is_absolute():
        raise UnsafeDirectoryError(f"{label} must be an absolute directory")
    try:
        current = value.absolute()
        # Check every existing component.  A missing leaf is intentionally not
        # accepted by the service; CLI setup creates it first and then passes
        # the verified directory here.
        if not current.is_dir():
            raise UnsafeDirectoryError(f"{label} is unavailable")
        for component in (current, *current.parents):
            metadata = component.lstat()
            if component.is_symlink() or bool(getattr(metadata, "st_file_attributes", 0) & 0x400):
                raise UnsafeDirectoryError(f"{label} contains a reparse point")
    except UnsafeDirectoryError:
        raise
    except OSError as exc:
        raise UnsafeDirectoryError(f"{label} is unavailable") from exc
    return current


def _safe_new_staging_path(directory: Path) -> Path:
    """Return a private, not-yet-created path in an owned staging directory."""

    # The random component is intentionally private and is never serialized.
    for _ in range(20):
        candidate = directory / (".neutral-stage-" + uuid.uuid4().hex + ".wav")
        if not candidate.exists() and not candidate.is_symlink():
            return candidate
    raise ApplicationError("unable to reserve a private staging name")


def _validate_staged_actual(path: Path, actual: ActualRun) -> None:
    """Verify adapter facts against the stable staged PCM16 WAV before publish."""

    if not isinstance(actual, ActualRun):
        raise ApplicationError("backend actual evidence is invalid")
    if not isinstance(path, Path) or not path.is_absolute() or not path.is_file() or path.is_symlink():
        raise ApplicationError("backend staged evidence is invalid")
    try:
        before = path.stat()
        with path.open("rb") as handle:
            opened = os.fstat(handle.fileno())
            with wave.open(handle, "rb") as wav:
                channels = wav.getnchannels()
                width = wav.getsampwidth()
                rate = wav.getframerate()
                frames = wav.getnframes()
                compression = wav.getcomptype()
        after = path.stat()
    except (EOFError, OSError, wave.Error) as exc:
        raise ApplicationError("backend staged evidence is invalid") from exc
    identities = {(item.st_dev, item.st_ino, item.st_size, item.st_mtime_ns) for item in (before, opened, after)}
    if len(identities) != 1 or not stat.S_ISREG(before.st_mode):
        raise ApplicationError("backend staged evidence is unstable")
    if channels != 1 or width != 2 or rate <= 0 or frames <= 0 or compression != "NONE":
        raise ApplicationError("backend staged evidence is not mono PCM16")
    expected_duration = frames / rate
    if (
        actual.sample_rate != rate
        or actual.wav_bytes != before.st_size
        or not math.isclose(float(actual.duration_seconds), expected_duration, rel_tol=1e-9, abs_tol=1e-9)
    ):
        raise ApplicationError("backend actual evidence does not match staged audio")


@dataclass(frozen=True, slots=True)
class PersistedSettings:
    """The serializable, framework-neutral settings snapshot used by a run.

    Paths are operational configuration and are never included in public
    payloads.  ``filename_context`` is already sanitized by the filename
    contract when rendered.
    """

    template: str = DEFAULT_TEMPLATE
    filename_context: FilenameContext | None = None
    plan_settings: PlanSettings = PlanSettings()
    pause_policy: PausePolicy = PausePolicy()
    dictionary: DictionarySnapshot | None = None
    tokenizer_identity: str = "unspecified"
    device_class: str = "unknown-device"
    calibration: Calibration | None = None
    output_directory: Path | None = None
    staging_directory: Path | None = None

    @property
    def settings_digest(self) -> str:
        context = None
        if self.filename_context is not None:
            context = self.filename_context.values()
        def operational_identity(path: Path | None) -> str | None:
            if path is None:
                return None
            # This is a private state binding only; the path itself never
            # crosses into an application payload or receipt.
            return hashlib.sha256(str(path.absolute()).encode("utf-8", "strict")).hexdigest()

        return _sha256_payload(
            {
                "template": self.template,
                "filename_context": context,
                "plan_settings": self.plan_settings.identity,
                "pause_policy": {"seconds": str(self.pause_policy.seconds), "sample_rate": self.pause_policy.sample_rate},
                "dictionary_identity": None if self.dictionary is None else self.dictionary.identity,
                "tokenizer_identity": self.tokenizer_identity,
                "device_class": self.device_class,
                "calibration_key": None if self.calibration is None else self.calibration.key,
                "output_directory": operational_identity(self.output_directory),
                "staging_directory": operational_identity(self.staging_directory),
            }
        )


# A shorter name is convenient for callers that call this a settings record.
ApplicationSettings = PersistedSettings


class SettingsStore(Protocol):
    def get_settings(self) -> PersistedSettings: ...


@dataclass(frozen=True, slots=True)
class ApplicationPreview:
    """Internal preview bundle plus privacy-bounded display information."""

    result: PreviewResult
    filename_digest: str
    settings_digest: str
    source_digest: str
    candidate_wav_name: str | None = None

    @property
    def filename(self) -> FilenamePreview:
        return self.result.filename

    @property
    def plan(self) -> TextPlan:
        return self.result.plan

    @property
    def estimate(self) -> GenerationEstimate:
        return self.result.estimate

    @property
    def plan_digest(self) -> str:
        return self.plan.plan_digest

    @property
    def estimate_digest(self) -> str:
        return self.estimate.plan_digest

    def public_payload(self) -> dict[str, object]:
        estimate = self.estimate
        return {
            "filename": {
                "template": self.filename.template,
                "wav_name": self.candidate_wav_name or self.filename.wav_name,
            },
            "filename_digest": self.filename_digest,
            "plan_digest": self.plan_digest,
            "plan": self.plan.public_payload(),
            "estimate": estimate.public_payload(),
            "display_ranges": {
                "chunks": self.plan.chunk_count,
                "duration_seconds": estimate.coarse_display_payload()["duration_seconds"],
                "storage_mib": estimate.coarse_display_payload()["storage_mib"],
                "staging_mib": estimate.coarse_display_payload()["staging_mib"],
            },
        }


@dataclass(frozen=True, slots=True)
class GenerationRequest:
    """What an admitted backend receives.

    The plan necessarily carries source/synthesis text for an approved real
    adapter.  It is internal-only and is never serialized by this module,
    returned in a public view-model, or included in a receipt.
    """

    plan: TextPlan
    estimate: GenerationEstimate
    staging_path: Path
    output_basename: str


@dataclass(frozen=True, slots=True)
class BackendResult:
    staged_path: Path
    actual: ActualRun


@runtime_checkable
class Backend(Protocol):
    """Minimal backend port.  Implementations must write only staging_path."""

    backend_identity: str

    def generate(self, request: GenerationRequest) -> BackendResult: ...


@dataclass(frozen=True, slots=True)
class GenerationAdmission:
    preview: ApplicationPreview
    request: GenerationRequest
    # Verified at admission and intentionally not placed on the backend port
    # or in public payloads.  Publication must remain bound to this directory.
    output_directory: Path


@dataclass(frozen=True, slots=True)
class ApplicationOutcome:
    """Receipt plus non-schema application evidence (for example demo mode)."""

    receipt: dict[str, Any]
    published_wav_name: str
    backend_identity: str
    demo: bool = False
    demo_label: str | None = None

    def public_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "status": "SUCCEEDED",
            "publication": {"wav_name": self.published_wav_name},
            "receipt": self.receipt,
            "receipt_feedback": self.feedback_payload(),
            "backend": {"identity": self.backend_identity},
        }
        if self.demo:
            payload["demo"] = {"mode": self.demo_label or "demo", "speech": False, "audio_kind": "silence"}
        return payload

    def feedback_payload(self) -> dict[str, object]:
        """Expose actual receipt feedback without paths or source text."""
        actual = self.receipt.get("actual", {})
        return {
            "status": self.receipt.get("status"),
            "estimate_relation": self.receipt.get("estimate_relation"),
            "actual": actual,
        }


class ApplicationService:
    """Connect settings, pure preview, admission, backend, and publication."""

    def __init__(
        self,
        token_counter: Callable[[str], int],
        settings: PersistedSettings | SettingsStore | None = None,
        backend: Backend | None = None,
        *,
        settings_store: SettingsStore | None = None,
    ) -> None:
        if not callable(token_counter):
            raise TypeError("token_counter must be callable")
        if settings is not None and settings_store is not None:
            raise TypeError("provide settings or settings_store, not both")
        self.token_counter = token_counter
        self._settings = settings
        self._settings_store = settings_store
        self.backend = backend

    def _current_settings(self) -> PersistedSettings:
        source = self._settings_store if self._settings_store is not None else self._settings
        if source is None:
            raise ApplicationError("persisted settings are required")
        if hasattr(source, "get_settings"):
            value = source.get_settings()
        elif callable(source):
            value = source()
        else:
            value = source
        if not isinstance(value, PersistedSettings):
            raise TypeError("settings store must return PersistedSettings")
        if value.filename_context is None:
            raise ApplicationError("filename context is required")
        return value

    @staticmethod
    def _filename_digest(filename: FilenamePreview, candidate_wav_name: str | None = None) -> str:
        return _sha256_payload(
            {
                "template": filename.template,
                "wav_name": filename.wav_name,
                "collision_identity": filename.collision_identity,
                "candidate_wav_name": candidate_wav_name,
            }
        )

    def preview(self, source_text: str) -> ApplicationPreview:
        settings = self._current_settings()
        result = pre_generation_preview(
            source_text,
            settings.template,
            settings.filename_context,
            self.token_counter,
            dictionary=settings.dictionary,
            settings=settings.plan_settings,
            pause_policy=settings.pause_policy,
            tokenizer_identity=settings.tokenizer_identity,
            calibration=settings.calibration,
            device_class=settings.device_class,
        )
        candidate = None
        if settings.output_directory is not None:
            directory = _safe_existing_directory(settings.output_directory, label="output directory")
            candidate = choose_collision_target(directory, result.filename).name
        source_digest = hashlib.sha256(source_text.encode("utf-8")).hexdigest()
        return ApplicationPreview(result, self._filename_digest(result.filename, candidate), settings.settings_digest, source_digest, candidate)

    def admit(self, source_text: str, preview: ApplicationPreview) -> GenerationAdmission:
        if not isinstance(preview, ApplicationPreview):
            raise TypeError("admission requires an ApplicationPreview")
        settings = self._current_settings()
        # Recompute all pure evidence.  This catches source, tokenizer,
        # dictionary, plan-settings, pause-policy, calibration, and template
        # changes instead of trusting a caller-provided digest alone.
        fresh = self.preview(source_text)
        if preview.plan_digest != preview.estimate_digest:
            raise DigestMismatchError("plan and estimate digests do not match")
        if preview.filename_digest != self._filename_digest(preview.filename, preview.candidate_wav_name):
            raise DigestMismatchError("filename digest does not match preview")
        if preview.settings_digest != settings.settings_digest or preview.source_digest != fresh.source_digest:
            raise StalePreviewError("preview is stale")
        # Collision resolution is part of the admission.  If another process
        # claims the originally previewed name, force a fresh preview instead
        # of silently switching to a suffix.
        if (
            preview.plan_digest != fresh.plan_digest
            or preview.estimate.public_payload() != fresh.estimate.public_payload()
            or preview.filename_digest != fresh.filename_digest
            or preview.candidate_wav_name != fresh.candidate_wav_name
        ):
            raise StalePreviewError("preview is stale")
        if settings.output_directory is None or settings.staging_directory is None:
            raise ApplicationError("output and staging directories are required")
        output = _safe_existing_directory(settings.output_directory, label="output directory")
        staging = _safe_existing_directory(settings.staging_directory, label="staging directory")
        target = choose_collision_target(output, fresh.filename)
        if fresh.candidate_wav_name != target.name:
            raise StalePreviewError("preview is stale")
        request = GenerationRequest(fresh.plan, fresh.estimate, _safe_new_staging_path(staging), target.name)
        return GenerationAdmission(fresh, request, output)

    def run(self, source_text: str, preview: ApplicationPreview) -> ApplicationOutcome:
        if self.backend is None:
            raise ApplicationError("a generation backend is required")
        try:
            backend_identity = normalize_public_id(getattr(self.backend, "backend_identity"), label="backend identity")
        except (TypeError, ValueError) as exc:
            raise ApplicationError("backend identity is invalid") from exc
        admission = self.admit(source_text, preview)
        result = self.backend.generate(admission.request)
        if not isinstance(result, BackendResult) or result.staged_path != admission.request.staging_path:
            raise ApplicationError("backend returned an unexpected staging result")
        actual = result.actual
        _validate_staged_actual(result.staged_path, actual)
        # Backends may omit timing, but the receipt requires strict aggregate
        # counts and audio facts; demo and real adapters both use this path.
        receipt = build_success_receipt(admission.preview.plan, admission.preview.estimate, actual)
        # Publication is the final side effect.  The output directory was
        # verified and bound in GenerationAdmission; never consult mutable
        # settings again after the backend has run.
        published = publish_staged_wav(result.staged_path, admission.output_directory, admission.request.output_basename)
        demo = backend_identity == "DEMO_SILENCE_ONLY-v1"
        return ApplicationOutcome(receipt, published.name, backend_identity, demo, "DEMO_SILENCE_ONLY" if demo else None)

    @staticmethod
    def receipt_feedback(outcome: ApplicationOutcome) -> dict[str, object]:
        """Return the actual-vs-estimate feedback view from a completed run."""
        if not isinstance(outcome, ApplicationOutcome):
            raise TypeError("receipt feedback requires an ApplicationOutcome")
        return outcome.feedback_payload()

    # Semantic aliases keep the port pleasant for callers that name the
    # operation "generate" or "admit_generation" in their application layer.
    admit_generation = admit
    generate = run


__all__ = [
    "ApplicationError", "ApplicationOutcome", "ApplicationPreview", "ApplicationService", "ApplicationSettings",
    "Backend", "BackendResult", "DigestMismatchError", "GenerationAdmission",
    "GenerationRequest", "PersistedSettings", "PreviewAdmissionError", "SettingsStore", "StalePreviewError",
    "UnsafeDirectoryError", "DEMO_SILENCE_ONLY", "demo_silence_only_backend",
]


def __getattr__(name: str) -> object:
    # Lazy aliases avoid a circular import when demo_backend imports the port
    # types above, while keeping the explicit demo backend easy to discover.
    if name in {"DEMO_SILENCE_ONLY", "demo_silence_only_backend"}:
        from .demo_backend import DEMO_SILENCE_ONLY, demo_silence_only_backend
        return DEMO_SILENCE_ONLY if name == "DEMO_SILENCE_ONLY" else demo_silence_only_backend
    raise AttributeError(name)
