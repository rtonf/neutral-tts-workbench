"""Pure, Windows-safe WAV filename templates and no-replace publication."""

from __future__ import annotations

import os
import re
import stat
import string
import unicodedata
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Final


FILENAME_TEMPLATE_SCHEMA: Final = "neutral-tts-filename-template-v1"
ALLOWED_PLACEHOLDERS: Final[frozenset[str]] = frozenset(
    {"date", "time", "title", "voice", "quality", "reading", "job_id_short", "sequence"}
)
DEFAULT_TEMPLATE: Final = "audio-{date}-{time}-{job_id_short}"
MAX_TEMPLATE_CHARACTERS: Final = 256
MAX_COMPONENT_CHARACTERS: Final = 120
MAX_COMPONENT_UTF8_BYTES: Final = 240
MAX_PATH_UTF16_UNITS: Final = 240
_BACKSLASH = chr(92)
_INVALID_WINDOWS = frozenset('<>:/|?*' + _BACKSLASH)
_CONTROL = frozenset(chr(i) for i in range(32)) | {chr(127)}
_RESERVED = frozenset(
    {"con", "prn", "aux", "nul", "conin$", "conout$"}
    | {f"com{i}" for i in range(1, 10)}
    | {f"lpt{i}" for i in range(1, 10)}
)
_RESERVED_DEVICE_RE = re.compile(r"(?:com|lpt)(?:[1-9¹²³])\Z", re.IGNORECASE)


class FilenameTemplateError(ValueError):
    """A template or rendered component cannot safely name one WAV."""


class FilenameCollisionError(FileExistsError):
    """A no-replace destination was already claimed."""


def _reject_surrogates(value: str) -> None:
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise FilenameTemplateError("filename contains an unpaired surrogate") from exc


def _reject_static_hazards(value: str) -> None:
    _reject_surrogates(value)
    if any(char in _INVALID_WINDOWS or char in _CONTROL for char in value):
        raise FilenameTemplateError("template contains Windows-invalid characters")
    if "/" in value or _BACKSLASH in value or ":" in value:
        raise FilenameTemplateError("template must contain one filename component")
    if ".." in value:
        raise FilenameTemplateError("template contains traversal text")
    if "." in value:
        raise FilenameTemplateError("template must not provide an extension")


def validate_template(template: str) -> str:
    """Parse and validate a brace template, returning NFC-normalized text."""

    if not isinstance(template, str):
        raise FilenameTemplateError("template must be a string")
    template = unicodedata.normalize("NFC", template)
    if not template or len(template) > MAX_TEMPLATE_CHARACTERS:
        raise FilenameTemplateError("template is empty or oversized")
    _reject_static_hazards(template)
    formatter = string.Formatter()
    try:
        parts = tuple(formatter.parse(template))
    except ValueError as exc:
        raise FilenameTemplateError("template braces are malformed") from exc
    has_field = False
    for literal, field_name, format_spec, conversion in parts:
        _reject_static_hazards(literal)
        if field_name is None:
            continue
        has_field = True
        if field_name not in ALLOWED_PLACEHOLDERS:
            raise FilenameTemplateError("unknown, positional, nested, or indexed placeholder")
        if format_spec or conversion is not None:
            raise FilenameTemplateError("format specifications and conversions are not allowed")
    if not has_field and not template.strip():
        raise FilenameTemplateError("template is empty")
    return template


@dataclass(frozen=True, slots=True)
class FilenameContext:
    date: str
    time: str
    title: str = "audio"
    voice: str = "voice"
    quality: str = "standard"
    reading: str = "standard"
    job_id_short: str = "job"
    sequence: int = 1

    def __post_init__(self) -> None:
        if isinstance(self.sequence, bool) or not isinstance(self.sequence, int) or self.sequence < 1:
            raise FilenameTemplateError("sequence must be a positive integer")
        for name in ("date", "time", "title", "voice", "quality", "reading", "job_id_short"):
            if not isinstance(getattr(self, name), str):
                raise FilenameTemplateError(f"{name} must be a string")

    def values(self) -> dict[str, str]:
        return {
            "date": self.date,
            "time": self.time,
            "title": self.title,
            "voice": self.voice,
            "quality": self.quality,
            "reading": self.reading,
            "job_id_short": self.job_id_short,
            "sequence": f"{self.sequence:03d}",
        }


def _sanitize_value(value: str) -> str:
    value = unicodedata.normalize("NFC", value)
    _reject_surrogates(value)
    value = "".join("_" if char in _INVALID_WINDOWS or char in _CONTROL or char == "." else char for char in value)
    value = value.strip(" .")
    if not value:
        value = "_"
    stem = value.casefold()
    if _is_reserved_stem(stem):
        value = f"_{value}"
    return value


def _is_reserved_stem(stem: str) -> bool:
    return stem.casefold() in _RESERVED or _RESERVED_DEVICE_RE.fullmatch(stem) is not None


def _is_reparse_point(path: Path) -> bool:
    """Detect symlinks and Windows reparse points without following them."""

    try:
        metadata = path.lstat()
    except OSError as exc:
        raise FilenameTemplateError("filesystem path is unavailable") from exc
    attribute = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return path.is_symlink() or bool(getattr(metadata, "st_file_attributes", 0) & attribute)


def _has_reparse_component(path: Path) -> bool:
    """Reject a path whose existing chain is redirected by a reparse point."""

    try:
        current = path.absolute()
    except OSError as exc:
        raise FilenameTemplateError("filesystem path is unavailable") from exc
    for component in (current, *current.parents):
        if component.exists() and _is_reparse_point(component):
            return True
    return False


def canonical_collision_identity(name: str) -> str:
    """Return the Windows-like NFC/case-insensitive comparison identity."""

    return unicodedata.normalize("NFC", name).casefold()


def render_template(template: str, context: FilenameContext) -> str:
    template = validate_template(template)
    values = context.values()
    formatter = string.Formatter()
    rendered: list[str] = []
    try:
        for literal, field_name, _spec, _conversion in formatter.parse(template):
            rendered.append(literal)
            if field_name is not None:
                rendered.append(_sanitize_value(values[field_name]))
    except (KeyError, TypeError) as exc:
        raise FilenameTemplateError("template context is incomplete") from exc
    result = unicodedata.normalize("NFC", "".join(rendered))
    if any(char in _INVALID_WINDOWS or char in _CONTROL for char in result) or "/" in result or _BACKSLASH in result or ":" in result:
        raise FilenameTemplateError("rendered filename contains Windows-invalid characters")
    result = result.rstrip(" .")
    if not result or result in {".", ".."}:
        raise FilenameTemplateError("rendered filename is empty or traversal-like")
    # Literals and values are checked separately above, but their
    # concatenation can still spell a device name ("nu" + "l"). Publication
    # rejects such a basename, so neutralize it here before any generation.
    if _is_reserved_stem(result):
        result = f"_{result}"
    if len(result) + 4 > MAX_COMPONENT_CHARACTERS or len((result + ".wav").encode("utf-8")) > MAX_COMPONENT_UTF8_BYTES:
        raise FilenameTemplateError("rendered filename exceeds its length limit")
    _reject_surrogates(result)
    return result


@dataclass(frozen=True, slots=True)
class FilenamePreview:
    template: str
    base_name: str
    wav_name: str
    collision_identity: str
    context: FilenameContext | None = None


def preview_filename(template: str, context: FilenameContext) -> FilenamePreview:
    template = validate_template(template)
    base = render_template(template, context)
    return FilenamePreview(template, base, f"{base}.wav", canonical_collision_identity(f"{base}.wav"), context)


def _path_utf16_units(path: Path) -> int:
    return len(str(path.absolute()).encode("utf-16-le", errors="strict")) // 2


def choose_collision_target(directory: Path, preview: FilenamePreview, *, max_sequence: int = 999) -> Path:
    """Choose the first deterministic free name, without creating anything."""

    if not isinstance(directory, Path) or not directory.is_absolute() or not directory.is_dir() or _has_reparse_component(directory):
        raise FilenameTemplateError("output directory is unavailable")
    if isinstance(max_sequence, bool) or not isinstance(max_sequence, int) or max_sequence < 1:
        raise FilenameTemplateError("collision sequence bound is invalid")
    existing = {canonical_collision_identity(item.name) for item in directory.iterdir()}
    has_sequence = any(field_name == "sequence" for _, field_name, _, _ in string.Formatter().parse(preview.template))
    first_sequence = preview.context.sequence if has_sequence and preview.context is not None else 1
    if first_sequence > max_sequence:
        raise FilenameCollisionError("all deterministic filename suffixes are occupied")
    for sequence in range(first_sequence, max_sequence + 1):
        if has_sequence and preview.context is not None:
            context = FilenameContext(
                preview.context.date, preview.context.time, preview.context.title,
                preview.context.voice, preview.context.quality, preview.context.reading,
                preview.context.job_id_short, sequence,
            )
            name = preview_filename(preview.template, context).wav_name
        else:
            name = preview.wav_name if sequence == 1 else f"{preview.base_name}-{sequence:03d}.wav"
        _validate_wav_basename(name)
        if canonical_collision_identity(name) not in existing:
            target = directory / name
            if _path_utf16_units(target) > MAX_PATH_UTF16_UNITS:
                raise FilenameTemplateError("target path exceeds its UTF-16 length limit")
            return target
    raise FilenameCollisionError("all deterministic filename suffixes are occupied")


def _validate_wav_basename(name: str) -> str:
    if not isinstance(name, str):
        raise FilenameTemplateError("WAV basename must be a string")
    name = unicodedata.normalize("NFC", name)
    _reject_surrogates(name)
    if not name or name != name.strip(" .") or any(c in _INVALID_WINDOWS or c in _CONTROL for c in name):
        raise FilenameTemplateError("WAV basename contains invalid characters")
    if "/" in name or _BACKSLASH in name or ":" in name or (len(name) > 1 and name[1] == ":"):
        raise FilenameTemplateError("WAV basename must be one relative component")
    if not name.casefold().endswith(".wav") or "." in name[:-4] or not name[:-4]:
        raise FilenameTemplateError("product-owned WAV extension is required")
    stem = name[:-4].casefold()
    if _is_reserved_stem(stem):
        raise FilenameTemplateError("WAV basename uses a reserved device name")
    if len(name) > MAX_COMPONENT_CHARACTERS or len(name.encode("utf-8")) > MAX_COMPONENT_UTF8_BYTES:
        raise FilenameTemplateError("WAV basename exceeds its length limit")
    return name


def _validate_staged_pcm16_wav(path: Path) -> None:
    """Require a stable, non-empty mono PCM16 RIFF/WAVE staging file."""

    try:
        before = path.stat()
        with path.open("rb") as handle:
            opened = os.fstat(handle.fileno())
            with wave.open(handle, "rb") as wav:
                valid = (
                    wav.getnchannels() == 1
                    and wav.getsampwidth() == 2
                    and 0 < wav.getframerate() <= 768_000
                    and wav.getnframes() > 0
                    and wav.getcomptype() == "NONE"
                )
        after = path.stat()
    except (EOFError, OSError, wave.Error) as exc:
        raise FilenameTemplateError("staged file is not a readable PCM16 WAV") from exc
    identities = {
        (item.st_dev, item.st_ino, item.st_size, item.st_mtime_ns)
        for item in (before, opened, after)
    }
    if not valid or len(identities) != 1:
        raise FilenameTemplateError("staged file is not a stable mono PCM16 WAV")


def publish_staged_wav(staged: Path, output_directory: Path, wav_basename: str) -> Path:
    """Atomically hard-link a staged WAV into a verified directory, never replacing."""

    if not isinstance(staged, Path) or not staged.is_absolute() or not staged.is_file() or _has_reparse_component(staged):
        raise FilenameTemplateError("staged WAV or destination directory is unavailable")
    if not isinstance(output_directory, Path) or not output_directory.is_absolute() or not output_directory.is_dir() or _has_reparse_component(output_directory):
        raise FilenameTemplateError("staged WAV or destination directory is unavailable")
    _validate_staged_pcm16_wav(staged)
    wav_basename = _validate_wav_basename(wav_basename)
    destination = output_directory / wav_basename
    if _path_utf16_units(destination) > MAX_PATH_UTF16_UNITS:
        raise FilenameTemplateError("destination path exceeds its UTF-16 length limit")
    try:
        if os.stat(staged).st_dev != os.stat(output_directory).st_dev:
            raise FilenameTemplateError("staged WAV and output directory are on different filesystems")
    except OSError as exc:
        raise FilenameTemplateError("staged WAV or destination directory is unavailable") from exc
    destination_identity = canonical_collision_identity(destination.name)
    if any(canonical_collision_identity(item.name) == destination_identity for item in output_directory.iterdir()):
        raise FilenameCollisionError("destination already exists")
    try:
        os.link(staged, destination)
    except FileExistsError as exc:
        raise FilenameCollisionError("destination already exists") from exc
    except OSError as exc:
        # A copy would expose a partial destination and cleanup could race
        # with another owner; hard-link publication is the sole primitive.
        raise FilenameTemplateError("atomic same-filesystem publication is unavailable") from exc
    return destination


__all__ = [
    "ALLOWED_PLACEHOLDERS", "DEFAULT_TEMPLATE", "FILENAME_TEMPLATE_SCHEMA",
    "FilenameCollisionError", "FilenameContext", "FilenamePreview", "FilenameTemplateError",
    "MAX_COMPONENT_CHARACTERS", "MAX_COMPONENT_UTF8_BYTES", "MAX_PATH_UTF16_UNITS",
    "MAX_TEMPLATE_CHARACTERS", "canonical_collision_identity", "choose_collision_target",
    "preview_filename", "publish_staged_wav", "render_template", "validate_template",
]
