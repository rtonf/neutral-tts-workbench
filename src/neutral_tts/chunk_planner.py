"""Deterministic, token-budget-aware planning for normalized synthesis text.

The planner in this module deliberately does not perform text normalization,
tokenization, or synthesis.  A caller supplies a token-count callback for the
same tokenizer that the synthesizer will use.  The callback's returned count
must include the tokenizer's BOS token.  The planner only decides contiguous
source spans and records immutable evidence for each span.

The implementation is intentionally conservative around tokenizer behavior:
it stops the current candidate at the first over-budget prefix.  This is the
usual behavior for tokenizers used by the worker (adding text does not make a
previously valid prefix a valid *synthesis segment*), and it means an unusual
non-monotonic callback can never make the planner emit an over-budget chunk.
No characters are discarded: every returned chunk is an exact slice of the
input and the concatenation of all chunks is checked before returning.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final, TypeAlias


DEFAULT_MAX_TOKENS: Final[int] = 256
"""Default maximum token count for one synthesis segment, including BOS."""

_SENTENCE_BOUNDARY_CHARS: Final[frozenset[str]] = frozenset(
    "\r\n。！？!?"
)
_CLAUSE_BOUNDARY_CHARS: Final[frozenset[str]] = frozenset(
    "、，,；;：:"
)
NATURAL_BOUNDARY_CHARACTERS: Final[frozenset[str]] = (
    _SENTENCE_BOUNDARY_CHARS | _CLAUSE_BOUNDARY_CHARS
)
"""Characters the planner can prefer over an arbitrary character cut."""

TokenCounter: TypeAlias = Callable[[str], int]


class ChunkPlanningError(ValueError):
    """Raised when a text cannot be represented within the token budget."""


@dataclass(frozen=True, slots=True)
class ChunkEvidence:
    """Immutable evidence describing one contiguous planned text chunk.

    ``start`` and ``end`` are Python string offsets into the original,
    already-normalized source text, using the half-open interval
    ``[start, end)``.  ``sha256`` is the lower-case SHA-256 digest of
    ``text.encode("utf-8")``.  ``token_count`` is the value returned by the
    supplied callback and therefore includes BOS by contract.
    """

    index: int
    start: int
    end: int
    text: str
    token_count: int
    sha256: str

    def __post_init__(self) -> None:
        """Reject malformed evidence even when constructed directly."""

        if type(self.index) is not int or self.index < 0:
            raise ValueError("chunk index must be a non-negative integer")
        if type(self.start) is not int or self.start < 0:
            raise ValueError("chunk start must be a non-negative integer")
        if type(self.end) is not int or self.end <= self.start:
            raise ValueError("chunk end must be greater than chunk start")
        if type(self.text) is not str or not self.text:
            raise ValueError("chunk text must be a non-empty string")
        if self.end - self.start != len(self.text):
            raise ValueError("chunk offsets must span the exact chunk text")
        if type(self.token_count) is not int or self.token_count <= 0:
            raise ValueError("chunk token_count must be a positive integer")
        expected_sha256 = _sha256(self.text)
        if self.sha256 != expected_sha256:
            raise ValueError("chunk sha256 does not match its exact text")

    @property
    def text_sha256(self) -> str:
        """Compatibility/readability alias for the text digest."""

        return self.sha256

    @property
    def source_span(self) -> tuple[int, int]:
        """Immutable half-open source span ``(start, end)``."""

        return (self.start, self.end)

    def as_dict(self) -> dict[str, int | str]:
        """Return a JSON-ready copy of this chunk's evidence.

        The dataclass itself remains immutable; this method intentionally
        creates a fresh dictionary for receipt serialization.
        """

        return {
            "index": self.index,
            "start": self.start,
            "end": self.end,
            "text": self.text,
            "token_count": self.token_count,
            "sha256": self.sha256,
        }

    to_dict = as_dict


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _validate_max_tokens(max_tokens: int) -> int:
    if type(max_tokens) is not int:
        raise TypeError("max_tokens must be an integer")
    if max_tokens <= 0:
        raise ValueError("max_tokens must be greater than zero")
    return max_tokens


def _validate_max_characters(max_characters: int | None) -> int | None:
    if max_characters is None:
        return None
    if type(max_characters) is not int:
        raise TypeError("max_characters must be an integer or None")
    if max_characters <= 0:
        raise ValueError("max_characters must be greater than zero")
    return max_characters


def _validate_token_count(value: object, segment: str) -> int:
    """Validate one callback result without accepting bool as an integer."""

    if type(value) is not int:
        raise ChunkPlanningError(
            "token-count callback must return a plain integer "
            f"(got {type(value).__name__})"
        )
    # A non-empty segment always contains BOS under the callback contract.
    if value <= 0:
        raise ChunkPlanningError(
            "token-count callback must return a positive count for "
            f"non-empty text (got {value} for {segment!r})"
        )
    return value


def _count(counter: TokenCounter, segment: str) -> int:
    """Call and validate the supplied token counter for a non-empty segment."""

    try:
        result = counter(segment)
    except Exception as exc:
        raise ChunkPlanningError(
            "token-count callback failed while measuring a chunk"
        ) from exc
    return _validate_token_count(result, segment)


def _boundary_priority(character: str) -> int | None:
    if character in _SENTENCE_BOUNDARY_CHARS:
        return 0
    if character in _CLAUSE_BOUNDARY_CHARS:
        return 1
    return None


def _text_boundary_priority(text: str, end: int) -> int | None:
    """Return boundary priority for ``text[:end]`` without splitting CRLF."""

    character = text[end - 1]
    if character == "\r" and end < len(text) and text[end] == "\n":
        return None
    return _boundary_priority(character)


def is_natural_boundary_end(text: str, end: int) -> bool:
    """Return whether ``end`` is a boundary the planner can actually use.

    A CRLF pair is indivisible: the position after ``\r`` is not usable when
    ``\n`` follows, while the position after the complete pair is usable.
    """

    if not isinstance(text, str):
        raise TypeError("text must be a string")
    if type(end) is not int:
        raise TypeError("end must be an integer")
    if end <= 0 or end > len(text):
        raise ValueError("end must identify a non-empty prefix of text")
    return _text_boundary_priority(text, end) is not None


def plan_chunks(
    text: str,
    token_count: TokenCounter,
    *,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    max_characters: int | None = None,
) -> tuple[ChunkEvidence, ...]:
    """Plan exact contiguous chunks for already-normalized synthesis text.

    Args:
        text: Non-empty text that is ready for synthesis.  It is never
            normalized or rewritten by this function.
        token_count: Callable receiving a non-empty candidate string and
            returning its exact token count *including BOS*.
        max_tokens: Inclusive per-chunk budget.  Defaults to 256.
        max_characters: Optional inclusive Unicode-code-point budget for one
            chunk.  This conservative secondary ceiling is useful when the
            acoustic model has a duration limit that is tighter than its
            tokenizer limit.  ``None`` disables the character ceiling.

    Returns:
        A tuple of frozen :class:`ChunkEvidence` values.  For every returned
        item, ``text[start:end] == text`` for the original source string; the
        chunks are ordered and adjacent, and their concatenation exactly
        reconstructs the input.

    Raises:
        TypeError: If ``text``, ``token_count``, or ``max_tokens`` has an
            incompatible type.
        ValueError / ChunkPlanningError: If text is empty, callback output is
            invalid, the budget is invalid, or an indivisible first character
            already exceeds the budget.  No input is silently truncated.

    Boundary selection is deterministic.  When a prefix first exceeds the
    budget, the latest fitting sentence/newline boundary is preferred; if
    none exists, the latest fitting clause boundary is preferred; otherwise
    the latest fitting character is used.
    """

    if not isinstance(text, str):
        raise TypeError("text must be a string")
    if not text:
        raise ValueError("text must not be empty")
    if not callable(token_count):
        raise TypeError("token_count must be callable")
    budget = _validate_max_tokens(max_tokens)
    character_budget = _validate_max_characters(max_characters)

    chunks: list[ChunkEvidence] = []
    start = 0
    index = 0
    text_length = len(text)

    while start < text_length:
        # The first character is an indivisible source unit.  Check it
        # explicitly so an over-budget character cannot be hidden by a later
        # boundary or discarded during fallback.
        first_end = start + 1
        first_text = text[start:first_end]
        first_count = _count(token_count, first_text)
        if first_count > budget:
            raise ChunkPlanningError(
                "indivisible one-character segment exceeds token budget "
                f"at source offset {start}: {first_count} > {budget}"
            )

        # The normal tokenizer path is monotone enough for a linear scan, but
        # we only rely on the first over-budget prefix as a safe stop: a later
        # prefix is never emitted without being measured and no text can be
        # silently dropped.  If no prefix exceeds the budget, the remainder
        # fits as one chunk.
        latest_fitting_end = first_end
        latest_fitting_count = first_count
        fitting_counts: dict[int, int] = {first_end: first_count}
        latest_sentence_end: int | None = (
            first_end
            if _text_boundary_priority(text, first_end) == 0
            else None
        )
        latest_clause_end: int | None = (
            first_end
            if _text_boundary_priority(text, first_end) == 1
            else None
        )
        overflow_end: int | None = None

        for end in range(start + 2, text_length + 1):
            if (
                character_budget is not None
                and end - start > character_budget
            ):
                overflow_end = end
                break
            candidate = text[start:end]
            candidate_count = _count(token_count, candidate)
            if candidate_count > budget:
                overflow_end = end
                break
            latest_fitting_end = end
            latest_fitting_count = candidate_count
            fitting_counts[end] = candidate_count
            priority = _text_boundary_priority(text, end)
            if priority == 0:
                latest_sentence_end = end
            elif priority == 1:
                latest_clause_end = end

        if overflow_end is None:
            # We measured through the end of the source text and all of it
            # fits.  No boundary preference applies because another chunk is
            # unnecessary.
            end = text_length
            token_total = latest_fitting_count
        else:
            if latest_sentence_end is not None:
                end = latest_sentence_end
            elif latest_clause_end is not None:
                end = latest_clause_end
            else:
                end = latest_fitting_end
            # ``end`` is guaranteed to be a measured fitting prefix.  Reuse
            # its exact count rather than invoking a potentially stateful
            # callback a second time.
            token_total = fitting_counts[end]

        chunk_text = text[start:end]
        if not chunk_text or end <= start:
            # This is an internal invariant guard; all externally observable
            # validation errors happen above.
            raise RuntimeError("chunk planner produced an empty chunk")
        if token_total > budget:
            raise RuntimeError("chunk planner produced an over-budget chunk")

        chunks.append(
            ChunkEvidence(
                index=index,
                start=start,
                end=end,
                text=chunk_text,
                token_count=token_total,
                sha256=_sha256(chunk_text),
            )
        )
        index += 1
        start = end

    planned_text = "".join(chunk.text for chunk in chunks)
    if planned_text != text:
        raise RuntimeError("chunk planner failed exact source reconstruction")
    if chunks[-1].end != text_length:
        raise RuntimeError("chunk planner did not cover the full source span")
    return tuple(chunks)


def plan_text_chunks(
    text: str,
    token_count: TokenCounter,
    *,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    max_characters: int | None = None,
) -> tuple[ChunkEvidence, ...]:
    """Descriptive alias for :func:`plan_chunks`."""

    return plan_chunks(
        text,
        token_count,
        max_tokens=max_tokens,
        max_characters=max_characters,
    )


def chunk_text(
    text: str,
    token_count: TokenCounter,
    *,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    max_characters: int | None = None,
) -> tuple[ChunkEvidence, ...]:
    """Backward-friendly alias for :func:`plan_chunks`."""

    return plan_chunks(
        text,
        token_count,
        max_tokens=max_tokens,
        max_characters=max_characters,
    )


__all__ = [
    "ChunkEvidence",
    "ChunkPlanningError",
    "DEFAULT_MAX_TOKENS",
    "NATURAL_BOUNDARY_CHARACTERS",
    "TokenCounter",
    "chunk_text",
    "is_natural_boundary_end",
    "plan_chunks",
    "plan_text_chunks",
]
