"""Neutral source planning shared by preview, estimation, and generation."""

from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Protocol

from .chunk_planner import ChunkEvidence, plan_chunks


class TokenCounter(Protocol):
    def __call__(self, text: str) -> int: ...


def _hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


MAX_PUBLIC_ID_CHARACTERS = 96
MAX_PUBLIC_ID_UTF8_BYTES = 192
MAX_PLAN_BUDGET = 1_000_000
MAX_PAUSE_SECONDS = Decimal("86400")
MAX_SAMPLE_RATE = 768_000
_ID_CONTROL = frozenset(chr(i) for i in range(32)) | {chr(127)}
_PUBLIC_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,95}\Z", re.ASCII)


def normalize_public_id(value: str, *, label: str = "identifier") -> str:
    """Validate a bounded, path-free generic identity used in public metadata."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty identifier")
    value = unicodedata.normalize("NFC", value).strip()
    if any(c in _ID_CONTROL for c in value) or ".." in value or _PUBLIC_ID_RE.fullmatch(value) is None:
        raise ValueError(f"{label} must use the public ASCII identifier grammar")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError(f"{label} contains an unpaired surrogate") from exc
    if len(value) > MAX_PUBLIC_ID_CHARACTERS or len(value.encode("utf-8")) > MAX_PUBLIC_ID_UTF8_BYTES:
        raise ValueError(f"{label} is oversized")
    return value


@dataclass(frozen=True, slots=True)
class DictionaryRule:
    surface: str
    reading: str
    rule_id: str = ""
    enabled: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.surface, str) or not self.surface.strip() or any(ord(c) < 32 for c in self.surface):
            raise ValueError("dictionary surface is invalid")
        if not isinstance(self.reading, str) or not self.reading.strip() or any(ord(c) < 32 for c in self.reading):
            raise ValueError("dictionary reading is invalid")
        if not isinstance(self.rule_id, str) or len(self.rule_id) > MAX_PUBLIC_ID_CHARACTERS:
            raise ValueError("dictionary rule_id is invalid")
        if not isinstance(self.enabled, bool):
            raise TypeError("dictionary enabled must be bool")
        try:
            self.surface.encode("utf-8")
            self.reading.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ValueError("dictionary text contains an unpaired surrogate") from exc
        object.__setattr__(self, "surface", unicodedata.normalize("NFC", self.surface))
        object.__setattr__(self, "reading", unicodedata.normalize("NFC", self.reading))
        if not self.rule_id:
            object.__setattr__(self, "rule_id", "rule-" + _hash_text(self.surface)[:16])
        else:
            object.__setattr__(self, "rule_id", normalize_public_id(self.rule_id, label="dictionary rule_id"))

    def payload(self) -> dict[str, object]:
        return {"rule_id": self.rule_id, "surface_sha256": _hash_text(self.surface), "reading_sha256": _hash_text(self.reading), "enabled": self.enabled}


@dataclass(frozen=True, slots=True)
class DictionarySnapshot:
    revision: int
    rules: tuple[DictionaryRule, ...] = ()

    def __post_init__(self) -> None:
        if isinstance(self.revision, bool) or not isinstance(self.revision, int) or self.revision < 0:
            raise ValueError("dictionary revision is invalid")
        rules = tuple(self.rules)
        if any(not isinstance(rule, DictionaryRule) for rule in rules):
            raise TypeError("dictionary rules must be DictionaryRule values")
        if len({rule.surface for rule in rules}) != len(rules):
            raise ValueError("dictionary surfaces must be unique")
        object.__setattr__(self, "rules", rules)

    @property
    def identity(self) -> str:
        payload = {"revision": self.revision, "rules": [rule.payload() for rule in self.rules]}
        return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class PlanSettings:
    quality: str = "balanced"
    reading: str = "standard"
    max_tokens: int = 256
    max_characters: int = 144
    duration_scale: float = 1.0

    def __post_init__(self) -> None:
        object.__setattr__(self, "quality", normalize_public_id(self.quality, label="quality"))
        object.__setattr__(self, "reading", normalize_public_id(self.reading, label="reading"))
        if any(isinstance(v, bool) or not isinstance(v, int) or v <= 0 or v > MAX_PLAN_BUDGET for v in (self.max_tokens, self.max_characters)):
            raise ValueError("plan budgets are invalid")
        if isinstance(self.duration_scale, bool) or not isinstance(self.duration_scale, (int, float)) or not math.isfinite(float(self.duration_scale)) or self.duration_scale <= 0 or self.duration_scale > 100:
            raise ValueError("duration scale is invalid")

    @property
    def identity(self) -> str:
        return f"quality={self.quality};reading={self.reading};tokens={self.max_tokens};chars={self.max_characters};scale={self.duration_scale:g}"


@dataclass(frozen=True, slots=True)
class PausePolicy:
    seconds: Decimal = Decimal("0")
    sample_rate: int = 48000

    def __post_init__(self) -> None:
        if isinstance(self.seconds, bool) or not isinstance(self.seconds, (Decimal, int, float, str)):
            raise ValueError("pause seconds are invalid")
        try:
            value = self.seconds if isinstance(self.seconds, Decimal) else Decimal(str(self.seconds))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise ValueError("pause seconds are invalid") from exc
        if not value.is_finite() or value < 0 or value > MAX_PAUSE_SECONDS:
            raise ValueError("pause seconds are invalid")
        if isinstance(self.sample_rate, bool) or not isinstance(self.sample_rate, int) or self.sample_rate <= 0 or self.sample_rate > MAX_SAMPLE_RATE:
            raise ValueError("pause sample rate is invalid")
        object.__setattr__(self, "seconds", value)

    @property
    def samples(self) -> int:
        return int((self.seconds * self.sample_rate).to_integral_value(rounding=ROUND_HALF_UP))


def _normalize_text(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("source text must be non-blank")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError("source text contains an unpaired surrogate") from exc
    # Trim transport/UI padding after newline and NFC normalization so every
    # caller (preview, estimate, and generation) receives the same plan.
    return unicodedata.normalize("NFC", value).replace("\r\n", "\n").replace("\r", "\n").strip()


def _apply_dictionary(text: str, snapshot: DictionarySnapshot | None) -> str:
    if snapshot is None:
        return text
    enabled = tuple(sorted((rule for rule in snapshot.rules if rule.enabled), key=lambda rule: (-len(rule.surface), rule.rule_id)))
    out: list[str] = []
    cursor = 0
    while cursor < len(text):
        match = next((rule for rule in enabled if text.startswith(rule.surface, cursor)), None)
        if match is None:
            out.append(text[cursor])
            cursor += 1
        else:
            out.append(match.reading)
            cursor += len(match.surface)
    return "".join(out)


@dataclass(frozen=True, slots=True)
class TextPlan:
    source_text: str
    synthesis_text: str
    chunks: tuple[ChunkEvidence, ...]
    source_characters: int
    synthesis_characters: int
    total_tokens: int
    boundary_count: int
    pause_policy: PausePolicy
    settings: PlanSettings
    tokenizer_identity: str
    dictionary_identity: str | None
    plan_digest: str

    @property
    def chunk_count(self) -> int:
        return len(self.chunks)

    @property
    def sampler_call_count(self) -> int:
        return self.chunk_count

    def public_payload(self) -> dict[str, object]:
        return {
            "schema_version": "neutral-tts-plan-v1",
            "plan_digest": self.plan_digest,
            "source_text_sha256": _hash_text(self.source_text),
            "synthesis_text_sha256": _hash_text(self.synthesis_text),
            "source_characters": self.source_characters,
            "synthesis_characters": self.synthesis_characters,
            "total_tokens": self.total_tokens,
            "chunk_count": self.chunk_count,
            "sampler_call_count": self.sampler_call_count,
            "boundary_count": self.boundary_count,
            "pause_seconds": str(self.pause_policy.seconds),
            "pause_samples_per_boundary": self.pause_policy.samples,
            "total_pause_samples": self.pause_policy.samples * self.boundary_count,
            "sample_rate": self.pause_policy.sample_rate,
            "quality": self.settings.quality,
            "reading": self.settings.reading,
            "tokenizer_identity": self.tokenizer_identity,
            "dictionary_identity": self.dictionary_identity,
            "chunks": [{"index": c.index, "start": c.start, "end": c.end, "token_count": c.token_count, "sha256": c.sha256} for c in self.chunks],
        }


def prepare_text_plan(
    source_text: str,
    token_counter: TokenCounter,
    *,
    dictionary: DictionarySnapshot | None = None,
    settings: PlanSettings | None = None,
    pause_policy: PausePolicy | None = None,
    tokenizer_identity: str = "unspecified",
) -> TextPlan:
    """Build the one deterministic plan used by estimate/preview/generation."""

    tokenizer_identity = normalize_public_id(tokenizer_identity, label="tokenizer identity")
    if not callable(token_counter):
        raise TypeError("token_counter must be callable")
    source = _normalize_text(source_text)
    settings = settings or PlanSettings()
    pause_policy = pause_policy or PausePolicy()
    synthesis = _apply_dictionary(source, dictionary)
    chunks = plan_chunks(synthesis, token_counter, max_tokens=settings.max_tokens, max_characters=settings.max_characters)
    public = {
        "source_text_sha256": _hash_text(source),
        "synthesis_text_sha256": _hash_text(synthesis),
        "chunks": [{"index": c.index, "start": c.start, "end": c.end, "token_count": c.token_count, "sha256": c.sha256} for c in chunks],
        "settings": settings.identity,
        "tokenizer_identity": tokenizer_identity,
        "dictionary_identity": None if dictionary is None else dictionary.identity,
        "pause_seconds": str(pause_policy.seconds),
        "pause_samples_per_boundary": pause_policy.samples,
        "total_pause_samples": pause_policy.samples * max(0, len(chunks) - 1),
    }
    digest = hashlib.sha256(json.dumps(public, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return TextPlan(source, synthesis, chunks, len(source), len(synthesis), sum(c.token_count for c in chunks), max(0, len(chunks) - 1), pause_policy, settings, tokenizer_identity, None if dictionary is None else dictionary.identity, digest)


__all__ = ["DictionaryRule", "DictionarySnapshot", "PausePolicy", "PlanSettings", "TextPlan", "TokenCounter", "MAX_PUBLIC_ID_CHARACTERS", "MAX_PUBLIC_ID_UTF8_BYTES", "MAX_PLAN_BUDGET", "MAX_PAUSE_SECONDS", "MAX_SAMPLE_RATE", "normalize_public_id", "prepare_text_plan"]
