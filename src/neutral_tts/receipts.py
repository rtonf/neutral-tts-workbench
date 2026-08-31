"""Privacy-bounded success receipts and estimate-versus-actual relation."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from .estimation import GenerationEstimate, validate_calibration_key
from .planning import TextPlan, normalize_public_id


RECEIPT_SCHEMA = "neutral-tts-receipt-v1"


@dataclass(frozen=True, slots=True)
class ActualRun:
    tokens: int
    chunks: int
    boundaries: int
    sample_rate: int
    duration_seconds: float
    wav_bytes: int
    generation_seconds: float | None = None
    staging_bytes_peak: int | None = None

    def __post_init__(self) -> None:
        if any(isinstance(v, bool) or not isinstance(v, int) or v <= 0 for v in (self.tokens, self.chunks)) or isinstance(self.boundaries, bool) or not isinstance(self.boundaries, int) or self.boundaries != max(self.chunks - 1, 0) or isinstance(self.wav_bytes, bool) or not isinstance(self.wav_bytes, int) or self.wav_bytes < 44:
            raise ValueError("actual counts are invalid")
        if isinstance(self.sample_rate, bool) or not isinstance(self.sample_rate, int) or self.sample_rate <= 0:
            raise ValueError("actual sample rate is invalid")
        if isinstance(self.duration_seconds, bool) or not isinstance(self.duration_seconds, (int, float)) or self.duration_seconds <= 0 or not math.isfinite(float(self.duration_seconds)):
            raise ValueError("actual duration is invalid")
        if self.generation_seconds is not None and (
            isinstance(self.generation_seconds, bool)
            or not isinstance(self.generation_seconds, (int, float))
            or self.generation_seconds <= 0
            or not math.isfinite(float(self.generation_seconds))
        ):
            raise ValueError("actual generation measurement is invalid")
        if self.staging_bytes_peak is not None and (
            isinstance(self.staging_bytes_peak, bool)
            or not isinstance(self.staging_bytes_peak, int)
            or self.staging_bytes_peak <= 0
        ):
            raise ValueError("actual staging measurement is invalid")


def _relation(estimate: GenerationEstimate, actual: ActualRun) -> str:
    if estimate.confidence.state == "unknown":
        return "unknown"
    values = (
        estimate.duration_seconds.contains(actual.duration_seconds),
        estimate.wav_bytes.contains(float(actual.wav_bytes)),
        estimate.staging_bytes.contains(float(actual.staging_bytes_peak)) if actual.staging_bytes_peak is not None else True,
    )
    if actual.generation_seconds is not None and estimate.generation_seconds is not None:
        values += (estimate.generation_seconds.contains(actual.generation_seconds),)
    return "in_range" if all(values) else "outside_range"


def build_success_receipt(plan: TextPlan, estimate: GenerationEstimate, actual: ActualRun, *, calibration_key: str | None = None) -> dict[str, Any]:
    """Build a source-free success receipt, retaining the estimate unchanged."""

    if plan.plan_digest != estimate.plan_digest:
        raise ValueError("plan and estimate digests do not match")
    key = estimate.confidence.key if calibration_key is None else calibration_key
    if key != estimate.confidence.key:
        raise ValueError("receipt calibration key does not match estimate")
    if (actual.tokens, actual.chunks, actual.boundaries) != (plan.total_tokens, plan.chunk_count, plan.boundary_count):
        raise ValueError("actual plan counts drifted")
    return {
        "schema_version": RECEIPT_SCHEMA,
        "status": "SUCCEEDED",
        "plan": plan.public_payload(),
        "estimate": estimate.public_payload(),
        "calibration_key": key,
        "actual": {
            "counts": {"tokens": actual.tokens, "chunks": actual.chunks, "boundaries": actual.boundaries},
            "audio": {"sample_rate": actual.sample_rate, "duration_seconds": actual.duration_seconds, "wav_bytes": actual.wav_bytes},
            "generation_seconds": actual.generation_seconds,
            "staging_bytes_peak": actual.staging_bytes_peak,
        },
        "estimate_relation": _relation(estimate, actual),
    }


def calibration_sample_from_receipt(receipt: object, expected_key: str) -> dict[str, object] | None:
    """Extract one successful, key-matched aggregate sample; otherwise None."""

    try:
        expected_key = validate_calibration_key(expected_key)
    except (TypeError, ValueError):
        return None
    expected_receipt_keys = {"schema_version", "status", "plan", "estimate", "calibration_key", "actual", "estimate_relation"}
    if not isinstance(receipt, dict) or set(receipt) != expected_receipt_keys or receipt.get("schema_version") != RECEIPT_SCHEMA or receipt.get("status") != "SUCCEEDED" or receipt.get("calibration_key") != expected_key or receipt.get("estimate_relation") not in {"unknown", "in_range", "outside_range"}:
        return None
    plan_payload = receipt.get("plan")
    estimate_payload = receipt.get("estimate")
    if not _valid_public_plan(plan_payload) or not _valid_public_estimate(estimate_payload):
        return None
    if estimate_payload.get("plan_digest") != plan_payload.get("plan_digest"):
        return None
    confidence = estimate_payload.get("confidence")
    if confidence.get("key") != expected_key:
        return None
    actual = receipt.get("actual")
    if not isinstance(actual, dict) or set(actual) != {"counts", "audio", "generation_seconds", "staging_bytes_peak"} or not isinstance(actual.get("counts"), dict) or set(actual["counts"]) != {"tokens", "chunks", "boundaries"} or not isinstance(actual.get("audio"), dict) or set(actual["audio"]) != {"sample_rate", "duration_seconds", "wav_bytes"}:
        return None
    try:
        tokens = actual["counts"]["tokens"]
        if isinstance(tokens, bool) or not isinstance(tokens, int) or tokens <= 0:
            return None
        raw_duration = actual["audio"]["duration_seconds"]
        if isinstance(raw_duration, bool) or not isinstance(raw_duration, (int, float)):
            return None
        duration = float(raw_duration)
        chunks = actual["counts"]["chunks"]
        boundaries = actual["counts"]["boundaries"]
        sample_rate = actual["audio"]["sample_rate"]
        wav_bytes = actual["audio"]["wav_bytes"]
        if any(isinstance(v, bool) or not isinstance(v, int) or v <= 0 for v in (chunks, sample_rate)) or isinstance(boundaries, bool) or not isinstance(boundaries, int) or boundaries != max(chunks - 1, 0) or isinstance(wav_bytes, bool) or not isinstance(wav_bytes, int) or wav_bytes < 44:
            return None
        staging = actual.get("staging_bytes_peak")
        if staging is not None and (isinstance(staging, bool) or not isinstance(staging, int) or staging <= 0):
            return None
        generation = actual.get("generation_seconds")
        if tokens <= 0 or duration <= 0 or not math.isfinite(tokens + duration):
            return None
        if generation is not None and (isinstance(generation, bool) or not isinstance(generation, (int, float)) or float(generation) <= 0 or not math.isfinite(float(generation))):
            return None
        expected_counts = (plan_payload["total_tokens"], plan_payload["chunk_count"], plan_payload["boundary_count"])
        estimate_counts = estimate_payload["counts"]
        if (estimate_counts["tokens"], estimate_counts["chunks"], estimate_counts["boundaries"]) != expected_counts:
            return None
        if (tokens, chunks, boundaries) != expected_counts:
            return None
        if estimate_counts["source_characters"] != plan_payload["source_characters"] or estimate_counts["synthesis_characters"] != plan_payload["synthesis_characters"] or estimate_counts["sampler_calls"] != plan_payload["sampler_call_count"] or estimate_counts["pause_samples_per_boundary"] != plan_payload["pause_samples_per_boundary"] or estimate_counts["total_pause_samples"] != plan_payload["total_pause_samples"]:
            return None
        return {"status": "SUCCEEDED", "tokens": tokens, "duration_seconds": duration, "generation_seconds": None if generation is None else float(generation)}
    except (KeyError, TypeError, ValueError, OverflowError):
        return None


def _valid_public_plan(value: object) -> bool:
    expected_keys = {
        "schema_version", "plan_digest", "source_text_sha256", "synthesis_text_sha256",
        "source_characters", "synthesis_characters", "total_tokens", "chunk_count",
        "sampler_call_count", "boundary_count", "pause_seconds",
        "pause_samples_per_boundary", "total_pause_samples", "sample_rate", "quality",
        "reading", "tokenizer_identity", "dictionary_identity", "chunks",
    }
    if not isinstance(value, dict) or set(value) != expected_keys or value.get("schema_version") != "neutral-tts-plan-v1" or not _is_sha256(value.get("plan_digest")) or not _is_sha256(value.get("source_text_sha256")) or not _is_sha256(value.get("synthesis_text_sha256")):
        return False
    positive_fields = ("source_characters", "synthesis_characters", "total_tokens", "chunk_count", "sampler_call_count", "sample_rate")
    nonnegative_fields = ("boundary_count", "pause_samples_per_boundary", "total_pause_samples")
    if not all(_is_exact_int(value.get(name), minimum=1) for name in positive_fields) or not all(_is_exact_int(value.get(name), minimum=0) for name in nonnegative_fields):
        return False
    if value["sampler_call_count"] != value["chunk_count"] or value["boundary_count"] != max(value["chunk_count"] - 1, 0) or value["total_pause_samples"] != value["pause_samples_per_boundary"] * value["boundary_count"]:
        return False
    try:
        pause_seconds = Decimal(value["pause_seconds"])
        if not pause_seconds.is_finite() or pause_seconds < 0:
            return False
        for label in ("quality", "reading", "tokenizer_identity"):
            normalize_public_id(value[label], label=label)
    except (InvalidOperation, TypeError, ValueError):
        return False
    dictionary_identity = value.get("dictionary_identity")
    if dictionary_identity is not None and not _is_sha256(dictionary_identity):
        return False
    chunks = value.get("chunks")
    if not isinstance(chunks, list) or len(chunks) != value["chunk_count"]:
        return False
    token_sum = 0
    previous_end = 0
    for expected_index, chunk in enumerate(chunks):
        if not isinstance(chunk, dict) or set(chunk) != {"index", "start", "end", "token_count", "sha256"}:
            return False
        if chunk.get("index") != expected_index or not _is_exact_int(chunk.get("start"), minimum=0) or not _is_exact_int(chunk.get("end"), minimum=1) or chunk["start"] != previous_end or chunk["end"] <= chunk["start"] or chunk["end"] > value["synthesis_characters"] or not _is_exact_int(chunk.get("token_count"), minimum=1) or not _is_sha256(chunk.get("sha256")):
            return False
        token_sum += chunk["token_count"]
        previous_end = chunk["end"]
    return token_sum == value["total_tokens"] and previous_end == value["synthesis_characters"]


def _valid_public_estimate(value: object) -> bool:
    expected_keys = {"schema_version", "plan_digest", "confidence", "counts", "duration_seconds", "wav_bytes", "staging_bytes", "generation_seconds", "sample_rate"}
    if not isinstance(value, dict) or set(value) != expected_keys or value.get("schema_version") != "neutral-tts-estimate-v1" or not _is_sha256(value.get("plan_digest")):
        return False
    confidence = value.get("confidence")
    counts = value.get("counts")
    if not isinstance(confidence, dict) or set(confidence) != {"state", "sample_count", "key"} or confidence.get("state") not in {"unknown", "low_sample", "calibrated"} or not _is_exact_int(confidence.get("sample_count"), minimum=0):
        return False
    try:
        validate_calibration_key(confidence.get("key"))
    except (TypeError, ValueError):
        return False
    if (confidence["state"] == "unknown") != (confidence["sample_count"] == 0):
        return False
    expected_count_keys = {"source_characters", "synthesis_characters", "tokens", "chunks", "sampler_calls", "boundaries", "pause_samples_per_boundary", "total_pause_samples"}
    if not isinstance(counts, dict) or set(counts) != expected_count_keys:
        return False
    positive_counts = ("source_characters", "synthesis_characters", "tokens", "chunks", "sampler_calls")
    nonnegative_counts = ("boundaries", "pause_samples_per_boundary", "total_pause_samples")
    if not all(_is_exact_int(counts.get(name), minimum=1) for name in positive_counts) or not all(_is_exact_int(counts.get(name), minimum=0) for name in nonnegative_counts):
        return False
    if counts["sampler_calls"] != counts["chunks"] or counts["boundaries"] != max(counts["chunks"] - 1, 0) or counts["total_pause_samples"] != counts["pause_samples_per_boundary"] * counts["boundaries"]:
        return False
    if not all(_valid_range(value.get(name)) for name in ("duration_seconds", "wav_bytes", "staging_bytes", "sample_rate")):
        return False
    if value["wav_bytes"]["minimum"] < 44 or value["sample_rate"]["minimum"] <= 0:
        return False
    generation = value.get("generation_seconds")
    return generation is None or _valid_range(generation)


def _is_sha256(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _is_exact_int(value: object, *, minimum: int) -> bool:
    return not isinstance(value, bool) and isinstance(value, int) and value >= minimum


def _valid_range(value: object) -> bool:
    if not isinstance(value, dict) or set(value) != {"minimum", "maximum"}:
        return False
    minimum = value.get("minimum")
    maximum = value.get("maximum")
    if any(isinstance(item, bool) or not isinstance(item, (int, float)) for item in (minimum, maximum)):
        return False
    return math.isfinite(float(minimum)) and math.isfinite(float(maximum)) and minimum >= 0 and maximum >= minimum


def parse_success_receipt(receipt: object, expected_key: str) -> dict[str, object] | None:
    """Strict parser shared by calibration and receipt-facing callers."""
    return calibration_sample_from_receipt(receipt, expected_key)


__all__ = ["ActualRun", "RECEIPT_SCHEMA", "build_success_receipt", "calibration_sample_from_receipt", "parse_success_receipt"]
