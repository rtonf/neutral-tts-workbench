"""Sampler-free coarse estimates and receipt-derived calibration."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal, Mapping

from .planning import TextPlan, normalize_public_id


ConfidenceState = Literal["unknown", "low_sample", "calibrated"]
_MAX_SECONDS_PER_TOKEN = 3_600.0


def make_calibration_key(device_class: str, quality: str, reading: str, tokenizer_identity: str) -> str:
    values = [
        normalize_public_id(device_class, label="device class"),
        normalize_public_id(quality, label="quality"),
        normalize_public_id(reading, label="reading"),
        normalize_public_id(tokenizer_identity, label="tokenizer identity"),
    ]
    key = "|".join(values)
    if len(key) > 384:
        raise ValueError("calibration key is invalid")
    return key


def validate_calibration_key(key: str) -> str:
    """Validate the exact four-part generic calibration identity."""

    if not isinstance(key, str) or len(key) > 384:
        raise ValueError("calibration key is invalid")
    parts = key.split("|")
    if len(parts) != 4:
        raise ValueError("calibration key is invalid")
    rebuilt = make_calibration_key(*parts)
    if rebuilt != key:
        raise ValueError("calibration key is invalid")
    return rebuilt


@dataclass(frozen=True, slots=True)
class NumericRange:
    minimum: float
    maximum: float

    def __post_init__(self) -> None:
        values = (self.minimum, self.maximum)
        if any(isinstance(v, bool) or not isinstance(v, (int, float)) for v in values):
            raise ValueError("numeric range is invalid")
        if not all(math.isfinite(float(v)) for v in values) or self.minimum < 0 or self.maximum < self.minimum:
            raise ValueError("numeric range is invalid")
        object.__setattr__(self, "minimum", float(self.minimum))
        object.__setattr__(self, "maximum", float(self.maximum))

    def contains(self, value: float) -> bool:
        return self.minimum <= value <= self.maximum


@dataclass(frozen=True, slots=True)
class Calibration:
    key: str
    sample_count: int
    duration_per_token: NumericRange
    generation_seconds_per_token: NumericRange | None
    state: ConfidenceState

    def __post_init__(self) -> None:
        validate_calibration_key(self.key)
        if not isinstance(self.duration_per_token, NumericRange) or self.duration_per_token.minimum <= 0:
            raise ValueError("calibration duration range is invalid")
        if self.generation_seconds_per_token is not None and (not isinstance(self.generation_seconds_per_token, NumericRange) or self.generation_seconds_per_token.minimum <= 0):
            raise ValueError("calibration generation range is invalid")
        if self.state not in {"unknown", "low_sample", "calibrated"}:
            raise ValueError("calibration identity is invalid")
        if isinstance(self.sample_count, bool) or not isinstance(self.sample_count, int) or self.sample_count < 0:
            raise ValueError("calibration sample count is invalid")
        if self.state == "unknown" and (self.sample_count != 0 or self.generation_seconds_per_token is not None):
            raise ValueError("unknown calibration cannot contain successful history")
        if self.state != "unknown" and self.sample_count == 0:
            raise ValueError("known calibration requires successful history")


@dataclass(frozen=True, slots=True)
class GenerationEstimate:
    plan_digest: str
    confidence: Calibration
    counts: Mapping[str, int]
    duration_seconds: NumericRange
    wav_bytes: NumericRange
    staging_bytes: NumericRange
    generation_seconds: NumericRange | None
    sample_rate: NumericRange

    def __post_init__(self) -> None:
        if re.fullmatch(r"[0-9a-f]{64}", self.plan_digest) is None:
            raise ValueError("estimate plan digest is invalid")
        if not isinstance(self.confidence, Calibration):
            raise TypeError("estimate confidence is invalid")
        copied = dict(self.counts)
        if any(not isinstance(key, str) or isinstance(value, bool) or not isinstance(value, int) or value < 0 for key, value in copied.items()):
            raise ValueError("estimate counts are invalid")
        required = {"source_characters", "synthesis_characters", "tokens", "chunks", "sampler_calls", "boundaries", "pause_samples_per_boundary", "total_pause_samples"}
        if not required <= copied.keys() or copied["tokens"] <= 0 or copied["chunks"] <= 0:
            raise ValueError("estimate counts are incomplete")
        if copied["sampler_calls"] != copied["chunks"] or copied["boundaries"] != max(copied["chunks"] - 1, 0) or copied["total_pause_samples"] != copied["pause_samples_per_boundary"] * copied["boundaries"]:
            raise ValueError("estimate count relationships are invalid")
        object.__setattr__(self, "counts", MappingProxyType(copied))
        if any(not isinstance(value, NumericRange) for value in (self.duration_seconds, self.wav_bytes, self.staging_bytes, self.sample_rate)):
            raise TypeError("estimate range is invalid")
        if self.generation_seconds is not None and not isinstance(self.generation_seconds, NumericRange):
            raise TypeError("estimate generation range is invalid")

    def public_payload(self) -> dict[str, object]:
        def pair(value: NumericRange) -> dict[str, float]:
            return {"minimum": value.minimum, "maximum": value.maximum}
        return {
            "schema_version": "neutral-tts-estimate-v1",
            "plan_digest": self.plan_digest,
            "confidence": {"state": self.confidence.state, "sample_count": self.confidence.sample_count, "key": self.confidence.key},
            "counts": dict(self.counts),
            "duration_seconds": pair(self.duration_seconds),
            "wav_bytes": pair(self.wav_bytes),
            "staging_bytes": pair(self.staging_bytes),
            "generation_seconds": None if self.generation_seconds is None else pair(self.generation_seconds),
            "sample_rate": pair(self.sample_rate),
        }

    def coarse_display_payload(self) -> dict[str, object]:
        """Return coarse outward-rounded values for human display."""
        def seconds(value: NumericRange) -> dict[str, int]:
            return {"minimum": int(_outward(value.minimum, quantum=5)), "maximum": int(_outward(value.maximum, upper=True, quantum=5))}
        def mib(value: NumericRange) -> dict[str, int]:
            unit = 1024 * 1024
            return {"minimum": math.floor(value.minimum / unit), "maximum": math.ceil(value.maximum / unit)}
        return {
            "confidence": self.confidence.state,
            "duration_seconds": seconds(self.duration_seconds),
            "storage_mib": mib(self.wav_bytes),
            "staging_mib": mib(self.staging_bytes),
            "generation_seconds": None if self.generation_seconds is None else seconds(self.generation_seconds),
        }

    display_payload = coarse_display_payload


def _outward(value: float, *, upper: bool = False, quantum: float = 0.5) -> float:
    factor = value / quantum
    return (math.ceil(factor) if upper else math.floor(factor)) * quantum


def _range_from(values: list[float], *, widen: float = 1.0) -> NumericRange:
    if not values:
        raise ValueError("at least one calibration value is required")
    low = min(values) / widen
    high = max(values) * widen
    if low <= 0 or high <= 0:
        raise ValueError("calibration rates must be positive")
    lower = math.nextafter(low, 0.0)
    if lower <= 0:
        lower = low
    return NumericRange(lower, math.nextafter(high, math.inf))


def _calibration_from_samples(
    key: str,
    samples: list[object],
    *,
    threshold: int = 5,
) -> Calibration:
    """Build calibration while ignoring malformed or failed receipt samples."""

    if isinstance(threshold, bool) or not isinstance(threshold, int) or threshold < 1:
        raise ValueError("calibration threshold is invalid")
    if not isinstance(samples, (list, tuple)):
        raise TypeError("calibration samples must be a list or tuple")
    key = validate_calibration_key(key)
    valid: list[dict[str, float | None]] = []
    for sample in samples:
        try:
            if not isinstance(sample, dict) or sample.get("status") != "SUCCEEDED":
                continue
            tokens = sample["tokens"]
            if isinstance(tokens, bool) or not isinstance(tokens, int) or tokens <= 0:
                continue
            raw_duration = sample["duration_seconds"]
            if isinstance(raw_duration, bool) or not isinstance(raw_duration, (int, float)):
                continue
            duration = float(raw_duration)
            generation = sample.get("generation_seconds")
            if duration <= 0 or duration / tokens > _MAX_SECONDS_PER_TOKEN or not math.isfinite(duration):
                continue
            if generation is not None and (isinstance(generation, bool) or not isinstance(generation, (int, float)) or float(generation) <= 0 or float(generation) / tokens > _MAX_SECONDS_PER_TOKEN or not math.isfinite(float(generation))):
                continue
            valid.append({"tokens": float(tokens), "duration_seconds": duration, "generation_seconds": float(generation) if generation is not None else None})
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
    if not valid:
        return Calibration(key, 0, NumericRange(0.01, 30.0), None, "unknown")
    low_sample = len(valid) < threshold
    duration_rates = [float(item["duration_seconds"]) / float(item["tokens"]) for item in valid]
    gen_rates = [float(item["generation_seconds"]) / float(item["tokens"]) for item in valid if item["generation_seconds"] is not None]
    return Calibration(
        key,
        len(valid),
        _range_from(duration_rates, widen=2.0 if low_sample else 1.15),
        None if not gen_rates else _range_from(gen_rates, widen=3.0 if low_sample else 1.25),
        "low_sample" if low_sample else "calibrated",
    )


def estimate_from_plan(
    plan: TextPlan,
    *,
    calibration: Calibration | None = None,
    device_class: str = "unknown-device",
    calibration_key: str | None = None,
    sample_rate_range: tuple[int, int] = (16_000, 48_000),
) -> GenerationEstimate:
    """Compute bounded ranges from an exact plan without invoking a sampler."""

    low_rate, high_rate = sample_rate_range
    if any(isinstance(rate, bool) or not isinstance(rate, int) for rate in (low_rate, high_rate)) or low_rate <= 0 or high_rate < low_rate or high_rate > 768_000:
        raise ValueError("sample rate range is invalid")
    expected_key = make_calibration_key(device_class, plan.settings.quality, plan.settings.reading, plan.tokenizer_identity)
    if calibration_key is not None and validate_calibration_key(calibration_key) != expected_key:
        raise ValueError("calibration key does not match the plan")
    calibration = calibration or Calibration(expected_key, 0, NumericRange(0.01, 30.0), None, "unknown")
    if calibration.key != expected_key:
        raise ValueError("calibration key does not match the plan")
    tokens = max(1, plan.total_tokens)
    if calibration.state == "unknown":
        duration = NumericRange(max(1.0, _outward(tokens * 0.01)), max(30.0, _outward(tokens * 30.0, upper=True)))
    else:
        duration_low = tokens * calibration.duration_per_token.minimum * plan.settings.duration_scale
        rounded_low = _outward(duration_low)
        duration = NumericRange(rounded_low if rounded_low > 0 else duration_low, _outward(tokens * calibration.duration_per_token.maximum * plan.settings.duration_scale, upper=True))
    pause_seconds = (plan.pause_policy.samples * plan.boundary_count) / plan.pause_policy.sample_rate
    duration = NumericRange(duration.minimum + pause_seconds, duration.maximum + pause_seconds)
    sample_rate = NumericRange(float(low_rate), float(high_rate))
    wav_low = 44.0 + math.ceil(duration.minimum * low_rate) * 2
    wav_high = 44.0 + math.ceil(duration.maximum * high_rate) * 2
    wav = NumericRange(max(44.0, _outward(wav_low, quantum=1024)), _outward(wav_high, upper=True, quantum=1024))
    metadata = 16 * 1024 + plan.chunk_count * 4096
    staging = NumericRange(2 * wav.minimum + metadata, 3 * wav.maximum + metadata)
    generation = None
    if calibration.generation_seconds_per_token is not None:
        generation_low = tokens * calibration.generation_seconds_per_token.minimum
        rounded_generation_low = _outward(generation_low)
        generation = NumericRange(rounded_generation_low if rounded_generation_low > 0 else generation_low, _outward(tokens * calibration.generation_seconds_per_token.maximum, upper=True))
    return GenerationEstimate(
        plan_digest=plan.plan_digest,
        confidence=calibration,
        counts={"source_characters": plan.source_characters, "synthesis_characters": plan.synthesis_characters, "tokens": plan.total_tokens, "chunks": plan.chunk_count, "sampler_calls": plan.sampler_call_count, "boundaries": plan.boundary_count, "pause_samples_per_boundary": plan.pause_policy.samples, "total_pause_samples": plan.pause_policy.samples * plan.boundary_count},
        duration_seconds=duration,
        wav_bytes=wav,
        staging_bytes=staging,
        generation_seconds=generation,
        sample_rate=sample_rate,
    )


def calibration_from_receipts(key: str, receipts: list[object], *, threshold: int = 5) -> Calibration:
    if not isinstance(receipts, (list, tuple)):
        raise TypeError("receipts must be a list or tuple")
    from .receipts import parse_success_receipt
    samples = [sample for receipt in receipts if (sample := parse_success_receipt(receipt, key)) is not None]
    return _calibration_from_samples(key, samples, threshold=threshold)


__all__ = ["Calibration", "ConfidenceState", "GenerationEstimate", "NumericRange", "make_calibration_key", "validate_calibration_key", "calibration_from_receipts", "estimate_from_plan"]
