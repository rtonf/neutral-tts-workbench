"""Deterministic, integrity-checked backup and rollback-safe restore.

Backups are ordinary ZIP files containing only four explicitly supported
categories.  The archive format is deliberately boring: canonical JSON,
fixed ZIP timestamps/permissions, and SHA-256 hashes for every file.  This is
useful for local recovery and migration, not a general-purpose archive format.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
import unicodedata
import uuid
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Iterable

from .fs_safety import (
    DirectoryGuard,
    FilesystemBoundaryError,
    assert_safe_descendant,
    make_directories,
    open_exclusive_binary,
)


class BackupError(RuntimeError):
    """Base class for backup/restore errors."""


class ArchiveRejected(BackupError):
    """The archive is malformed, unsafe, or outside the supported schema."""


class RestoreError(BackupError):
    """A restore could not be completed and was rolled back."""


SCHEMA_VERSION = "neutral-tts-backup-v1"
VOICE_PROFILE_SCHEMA = "neutral-generic-voice-profile-v1"
MANIFEST_NAME = "manifest.json"
RESTORE_JOURNAL_SCHEMA = "neutral-tts-restore-journal-v1"
SUPPORTED_CATEGORIES = frozenset({"settings", "history", "dictionary", "voice_profiles"})
MAX_ENTRIES = 256
MAX_FILE_SIZE = 8 * 1024 * 1024
MAX_EXPANDED_SIZE = 32 * 1024 * 1024
MAX_COMPRESSION_RATIO = 100
_REPARSE_ATTRIBUTE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _path_exists(path: Path) -> bool:
    return os.path.lexists(path)


def _is_reparse(path: Path) -> bool:
    try:
        info = path.lstat()
    except OSError as exc:
        raise BackupError("filesystem metadata is unavailable") from exc
    return path.is_symlink() or bool(
        getattr(info, "st_file_attributes", 0) & _REPARSE_ATTRIBUTE
    )


def _read_stable_file(path: Path) -> bytes:
    try:
        before = path.stat()
        if _is_reparse(path) or not stat.S_ISREG(before.st_mode):
            raise BackupError("backup payload is not a regular non-link file")
        with path.open("rb") as handle:
            opened = os.fstat(handle.fileno())
            data = handle.read(MAX_FILE_SIZE + 1)
        after = path.stat()
    except BackupError:
        raise
    except OSError as exc:
        raise BackupError("backup payload is unreadable") from exc
    identities = {
        (item.st_dev, item.st_ino, item.st_size, item.st_mtime_ns)
        for item in (before, opened, after)
    }
    if len(data) > MAX_FILE_SIZE:
        raise BackupError("backup file is too large")
    if len(identities) != 1 or len(data) != before.st_size:
        raise BackupError("backup payload changed during inspection")
    return data


def _safe_directory(path: Path, label: str, *, create: bool = False) -> Path:
    if not isinstance(path, Path) or not path.is_absolute():
        raise BackupError(f"{label} must be an absolute path")
    path = path.absolute()
    exists = _path_exists(path)
    current = path if exists else path.parent
    if not current.is_dir():
        raise BackupError(f"{label} parent is unavailable")
    for component in (current, *current.parents):
        if _is_reparse(component):
            raise BackupError(f"{label} contains a reparse point")
    if not exists and create:
        try:
            with DirectoryGuard(path.parent) as parent_guard:
                path.mkdir()
                assert_safe_descendant(parent_guard, path)
        except (OSError, FilesystemBoundaryError) as exc:
            raise BackupError(f"{label} could not be created") from exc
        exists = True
    if not exists or not path.is_dir() or _is_reparse(path):
        raise BackupError(f"{label} is not a safe directory")
    return path


def _safe_new_file(path: Path, label: str) -> Path:
    if not isinstance(path, Path) or not path.is_absolute() or _path_exists(path):
        raise BackupError(f"{label} must be a fresh absolute path")
    _safe_directory(path.parent, f"{label} parent")
    return path.absolute()


def _safe_relative(value: str, *, category: str | None = None) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ArchiveRejected("archive path is invalid")
    if unicodedata.normalize("NFC", value) != value:
        raise ArchiveRejected("archive path is not NFC-normalized")
    if "\\" in value or value.startswith(("/", "\\")) or ":" in value:
        raise ArchiveRejected("archive path is not a Windows-safe relative path")
    raw_parts = value.split("/")
    if any(part in {"", ".", ".."} for part in raw_parts):
        raise ArchiveRejected("archive path spelling is not canonical")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ArchiveRejected("archive path traversal is not allowed")
    if len(path.parts) < 2:
        raise ArchiveRejected("archive path must contain a category")
    if category is not None and path.parts[0] != category:
        raise ArchiveRejected("entry category does not match path")
    if path.parts[0] not in SUPPORTED_CATEGORIES:
        raise ArchiveRejected("unsupported backup category")
    # Colon aliases and reserved device names are rejected even if they are
    # hidden in a later path component or would be normalized by Windows.
    reserved = {"CON", "PRN", "AUX", "NUL"} | {
        f"{prefix}{index}" for prefix in ("COM", "LPT") for index in range(1, 10)
    }
    for part in path.parts:
        if (
            part != part.strip()
            or part.endswith((".", " "))
            or part.upper().split(".", 1)[0] in reserved
            or any(ord(char) < 32 or ord(char) == 127 for char in part)
        ):
            raise ArchiveRejected("archive path is not Windows-safe")
    canonical = "/".join(path.parts)
    if canonical != value:
        raise ArchiveRejected("archive path spelling is not canonical")
    return canonical


def _validate_category_payload(category: str, relative: str, data: bytes) -> None:
    suffix = PurePosixPath(relative).suffix.casefold()
    allowed = {
        "settings": {".json"},
        "history": {".json", ".jsonl"},
        "dictionary": {".json"},
        "voice_profiles": {".json"},
    }
    if suffix not in allowed[category]:
        raise ArchiveRejected("backup payload type is unsupported for its category")
    try:
        text = data.decode("utf-8", "strict")
        if category != "voice_profiles":
            # Safety backups must preserve even a temporarily malformed
            # settings/history/dictionary file.  Type and UTF-8 bounds still
            # exclude binary, audio, model, and executable payloads.
            return
        value = json.loads(text)
    except ArchiveRejected:
        raise
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ArchiveRejected("backup payload is not valid UTF-8 JSON") from exc
    if category == "voice_profiles":
        if not isinstance(value, dict) or set(value) != {
            "channels", "language", "profile_id", "profile_kind", "sample_rate", "schema_version"
        }:
            raise ArchiveRejected("voice profile metadata shape is not approved")
        profile_id = value.get("profile_id")
        language = value.get("language")
        sample_rate = value.get("sample_rate")
        if (
            value.get("schema_version") != VOICE_PROFILE_SCHEMA
            or value.get("profile_kind") != "generic"
            or not isinstance(profile_id, str)
            or re.fullmatch(r"generic-[a-z0-9][a-z0-9_-]{0,55}", profile_id) is None
            or not isinstance(language, str)
            or re.fullmatch(r"(?:und|[a-z]{2,3}(?:-[A-Za-z0-9]{2,8})*)", language) is None
            or isinstance(sample_rate, bool)
            or not isinstance(sample_rate, int)
            or not 8_000 <= sample_rate <= 768_000
            or value.get("channels") != 1
        ):
            raise ArchiveRejected("voice profile metadata is not an approved generic record")


def _category_for(path: Path, root: Path) -> tuple[str, str]:
    try:
        relative = path.relative_to(root).as_posix()
    except ValueError as exc:
        raise BackupError("file escaped backup root") from exc
    if not relative:
        raise BackupError("backup path is empty")
    category = relative.split("/", 1)[0]
    if category not in SUPPORTED_CATEGORIES:
        raise BackupError(f"unsupported backup category: {category}")
    return category, _safe_relative(relative, category=category)


@dataclass(frozen=True, slots=True)
class ManifestEntry:
    path: str
    category: str
    size: int
    sha256: str

    def payload(self) -> dict[str, object]:
        return {"path": self.path, "category": self.category, "size": self.size, "sha256": self.sha256}


@dataclass(frozen=True, slots=True)
class BackupManifest:
    entries: tuple[ManifestEntry, ...]
    categories: tuple[str, ...]
    aggregate_sha256: str
    schema_version: str = SCHEMA_VERSION

    @classmethod
    def from_entries(cls, entries: Iterable[ManifestEntry]) -> "BackupManifest":
        ordered = tuple(sorted(entries, key=lambda item: (item.path.casefold(), item.path)))
        identities: set[str] = set()
        for entry in ordered:
            identity = entry.path.casefold()
            if identity in identities:
                raise BackupError("duplicate or case-colliding backup path")
            identities.add(identity)
        categories = tuple(sorted(SUPPORTED_CATEGORIES))
        aggregate = _sha256(
            _canonical({"categories": categories, "entries": [entry.payload() for entry in ordered]})
        )
        return cls(ordered, categories, aggregate)

    def payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "categories": list(self.categories),
            "entries": [entry.payload() for entry in self.entries],
            "aggregate_sha256": self.aggregate_sha256,
        }


def _validate_manifest(payload: object) -> BackupManifest:
    if (
        not isinstance(payload, dict)
        or set(payload) != {"aggregate_sha256", "categories", "entries", "schema_version"}
        or payload.get("schema_version") != SCHEMA_VERSION
    ):
        raise ArchiveRejected("backup schema mismatch")
    if payload.get("categories") != sorted(SUPPORTED_CATEGORIES):
        raise ArchiveRejected("backup category set is incomplete or unsupported")
    entries_raw = payload.get("entries")
    aggregate = payload.get("aggregate_sha256")
    if not isinstance(entries_raw, list) or len(entries_raw) > MAX_ENTRIES - 1 or not isinstance(aggregate, str) or len(aggregate) != 64 or any(c not in "0123456789abcdef" for c in aggregate):
        raise ArchiveRejected("backup manifest shape is invalid")
    entries: list[ManifestEntry] = []
    identities: set[str] = set()
    for raw in entries_raw:
        if not isinstance(raw, dict) or set(raw) != {"path", "category", "size", "sha256"}:
            raise ArchiveRejected("backup manifest entry shape is invalid")
        category, path = raw.get("category"), raw.get("path")
        if not isinstance(category, str) or category not in SUPPORTED_CATEGORIES:
            raise ArchiveRejected("unsupported backup category")
        path = _safe_relative(path, category=category)
        identity = path.casefold()
        if identity in identities:
            raise ArchiveRejected("duplicate or case-colliding archive path")
        identities.add(identity)
        size, digest = raw.get("size"), raw.get("sha256")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0 or size > MAX_FILE_SIZE:
            raise ArchiveRejected("backup entry size is invalid")
        if not isinstance(digest, str) or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise ArchiveRejected("backup entry hash is invalid")
        if category == "voice_profiles" and Path(path).suffix.casefold() != ".json":
            raise ArchiveRejected("voice profile backups must contain JSON metadata")
        entries.append(ManifestEntry(path, category, size, digest))
    manifest = BackupManifest.from_entries(entries)
    if entries != list(manifest.entries):
        raise ArchiveRejected("backup manifest entries are not in canonical order")
    if manifest.aggregate_sha256 != aggregate:
        raise ArchiveRejected("backup aggregate hash is invalid")
    return manifest


def _iter_source_files(root: Path) -> list[tuple[Path, str, str]]:
    files: list[tuple[Path, str, str]] = []
    for category in sorted(SUPPORTED_CATEGORIES):
        category_path = root / category
        if not _path_exists(category_path):
            continue
        if _is_reparse(category_path) or not category_path.is_dir():
            raise BackupError("backup category is not a regular directory")
        for current, dirs, names in os.walk(category_path, topdown=True, followlinks=False):
            current_path = Path(current)
            kept: list[str] = []
            for name in sorted(dirs, key=str.casefold):
                child = current_path / name
                if _is_reparse(child):
                    raise BackupError("symlink/reparse entries are not supported")
                kept.append(name)
            dirs[:] = kept
            for name in sorted(names, key=str.casefold):
                path = current_path / name
                if _is_reparse(path) or not path.is_file():
                    raise BackupError("backup payload is not a regular file")
                observed_category, relative = _category_for(path, root)
                files.append((path, observed_category, relative))
    if len(files) > MAX_ENTRIES - 1:
        raise BackupError("too many backup entries")
    return sorted(files, key=lambda item: (item[2].casefold(), item[2]))


def create_backup(source_root: Path, archive_path: Path) -> Path:
    """Write a deterministic ZIP backup and refuse to replace an archive."""
    root = _safe_directory(source_root, "backup root")
    archive_path = _safe_new_file(archive_path, "backup destination")
    files = _iter_source_files(root)
    entries: list[ManifestEntry] = []
    contents: dict[str, bytes] = {}
    total = 0
    for path, category, relative in files:
        data = _read_stable_file(path)
        try:
            _validate_category_payload(category, relative, data)
        except ArchiveRejected as exc:
            raise BackupError("backup source category data is invalid") from exc
        size, digest = len(data), _sha256(data)
        total += size
        if total > MAX_EXPANDED_SIZE:
            raise BackupError("backup expanded size is unsafe")
        entries.append(ManifestEntry(relative, category, size, digest))
        contents[relative] = data
    manifest = BackupManifest.from_entries(entries)
    temporary = archive_path.with_name(archive_path.name + ".tmp-" + uuid.uuid4().hex)
    if _path_exists(temporary):
        raise BackupError("backup staging destination was claimed")
    try:
        with zipfile.ZipFile(temporary, "x", compression=zipfile.ZIP_STORED, allowZip64=False) as archive:
            info = zipfile.ZipInfo(MANIFEST_NAME, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_STORED
            info.create_system = 3
            info.external_attr = (stat.S_IFREG | 0o600) << 16
            archive.writestr(info, _canonical(manifest.payload()))
            for relative in sorted(contents, key=lambda item: (item.casefold(), item)):
                info = zipfile.ZipInfo(relative, date_time=(1980, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_STORED
                info.create_system = 3
                info.external_attr = (stat.S_IFREG | 0o600) << 16
                archive.writestr(info, contents[relative])
        with temporary.open("rb+") as handle:
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, archive_path)
        except FileExistsError as exc:
            raise BackupError("backup destination was claimed") from exc
        except OSError as exc:
            raise BackupError("atomic no-replace backup publication is unavailable") from exc
        return archive_path
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _read_archive(archive_path: Path) -> tuple[BackupManifest, dict[str, bytes]]:
    if (
        not isinstance(archive_path, Path)
        or not archive_path.is_absolute()
        or not _path_exists(archive_path)
        or not archive_path.is_file()
        or _is_reparse(archive_path)
    ):
        raise ArchiveRejected("backup archive is unavailable")
    try:
        _safe_directory(archive_path.parent, "backup archive parent")
    except BackupError as exc:
        raise ArchiveRejected("backup archive path is unsafe") from exc
    try:
        before = archive_path.stat()
        with archive_path.open("rb") as handle:
            opened = os.fstat(handle.fileno())
            with zipfile.ZipFile(handle, "r") as archive:
                if archive.comment:
                    raise ArchiveRejected("archive comment is unsupported")
                infos = archive.infolist()
                if not infos or len(infos) > MAX_ENTRIES:
                    raise ArchiveRejected("archive entry count is invalid")
                names: set[str] = set()
                ordered_names: list[str] = []
                manifest_payload = None
                manifest_bytes = None
                raw_by_name: dict[str, bytes] = {}
                expanded = 0
                for info in infos:
                    if info.is_dir() or info.filename.endswith(("/", "\\")):
                        raise ArchiveRejected("directory entries are not supported")
                    if info.comment or info.extra or info.flag_bits & 0x1:
                        raise ArchiveRejected("archive member metadata is unsupported")
                    if info.date_time != (1980, 1, 1, 0, 0, 0) or info.compress_type != zipfile.ZIP_STORED:
                        raise ArchiveRejected("archive member encoding is not deterministic")
                    name = MANIFEST_NAME if info.filename == MANIFEST_NAME else _safe_relative(info.filename)
                    identity = name.casefold()
                    if identity in names:
                        raise ArchiveRejected("duplicate or case-colliding archive path")
                    names.add(identity)
                    ordered_names.append(name)
                    unix_mode = (info.external_attr >> 16) & 0xFFFF
                    dos_attributes = info.external_attr & 0xFFFF
                    if (
                        info.create_system != 3
                        or stat.S_IFMT(unix_mode) != stat.S_IFREG
                        or unix_mode & 0o777 != 0o600
                        or dos_attributes & _REPARSE_ATTRIBUTE
                    ):
                        raise ArchiveRejected("symlink/reparse-like or non-regular ZIP entry")
                    if info.file_size > MAX_FILE_SIZE:
                        raise ArchiveRejected("archive entry is too large")
                    if (
                        (info.compress_size == 0 and info.file_size > 0)
                        or info.file_size > max(info.compress_size, 1) * MAX_COMPRESSION_RATIO
                    ):
                        raise ArchiveRejected("archive compression ratio is unsafe")
                    expanded += info.file_size
                    if expanded > MAX_EXPANDED_SIZE:
                        raise ArchiveRejected("archive expanded size is unsafe")
                    data = archive.read(info)
                    if len(data) != info.file_size:
                        raise ArchiveRejected("archive member expanded size drifted")
                    if name == MANIFEST_NAME:
                        manifest_bytes = data
                        manifest_payload = json.loads(data.decode("utf-8", "strict"))
                    else:
                        raw_by_name[name] = data
                if manifest_payload is None or manifest_bytes != _canonical(manifest_payload):
                    raise ArchiveRejected("archive manifest is absent or non-canonical")
                manifest = _validate_manifest(manifest_payload)
                expected_order = [MANIFEST_NAME, *[entry.path for entry in manifest.entries]]
                if ordered_names != expected_order or set(raw_by_name) != {entry.path for entry in manifest.entries}:
                    raise ArchiveRejected("archive contents do not exactly match the manifest")
                for entry in manifest.entries:
                    data = raw_by_name[entry.path]
                    if len(data) != entry.size or _sha256(data) != entry.sha256:
                        raise ArchiveRejected("archive entry hash or size mismatch")
                    _validate_category_payload(entry.category, entry.path, data)
        after = archive_path.stat()
        identities = {
            (item.st_dev, item.st_ino, item.st_size, item.st_mtime_ns)
            for item in (before, opened, after)
        }
        if len(identities) != 1:
            raise ArchiveRejected("backup archive changed during validation")
        return manifest, raw_by_name
    except ArchiveRejected:
        raise
    except (OSError, zipfile.BadZipFile, UnicodeDecodeError, json.JSONDecodeError, zipfile.LargeZipFile) as exc:
        raise ArchiveRejected("backup archive is corrupt") from exc


def _is_within(root: Path, candidate: Path) -> bool:
    try:
        candidate.absolute().relative_to(root.absolute())
        return True
    except ValueError:
        return False


def _flush_directory(directory: Path) -> None:
    try:
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        descriptor = os.open(directory, flags)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError:
        # Windows commonly refuses directory fsync.  The lifecycle journal and
        # file bytes are still flushed; directory durability is best effort.
        pass


def _restore_journal_path(root: Path) -> Path:
    return root.parent / f".{root.name}.restore-journal.json"


def _atomic_runtime_json(path: Path, payload: object, guard: DirectoryGuard) -> None:
    temporary = path.with_name(path.name + ".tmp-" + uuid.uuid4().hex)
    try:
        with open_exclusive_binary(guard, temporary) as handle:
            handle.write(_canonical(payload) + b"\n")
            handle.flush()
            os.fsync(handle.fileno())
        guard.assert_unchanged()
        os.replace(temporary, path)
        _flush_directory(path.parent)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _write_restore_journal(
    journal: Path, payload: dict[str, object], parent_guard: DirectoryGuard
) -> None:
    _atomic_runtime_json(journal, payload, parent_guard)


def _create_restore_journal(
    journal: Path, payload: dict[str, object], parent_guard: DirectoryGuard
) -> None:
    try:
        with open_exclusive_binary(parent_guard, journal) as handle:
            handle.write(_canonical(payload) + b"\n")
            handle.flush()
            os.fsync(handle.fileno())
        _flush_directory(journal.parent)
    except FilesystemBoundaryError as exc:
        raise RestoreError("another restore operation claimed the journal") from exc


def _load_restore_journal(journal: Path, root: Path) -> dict[str, object]:
    try:
        raw = journal.read_bytes()
        payload = json.loads(raw.decode("utf-8", "strict"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RestoreError("restore lifecycle journal is unreadable") from exc
    if raw != _canonical(payload) + b"\n" or not isinstance(payload, dict):
        raise RestoreError("restore lifecycle journal is non-canonical")
    required = {
        "active_category", "categories", "installed", "journal_schema",
        "moved", "original_present", "phase", "rollback_name",
        "root_name", "safety_backup", "work_name",
    }
    if set(payload) != required or payload.get("journal_schema") != RESTORE_JOURNAL_SCHEMA:
        raise RestoreError("restore lifecycle journal schema is invalid")
    if payload.get("root_name") != root.name:
        raise RestoreError("restore lifecycle journal targets another data root")
    categories = payload.get("categories")
    original = payload.get("original_present")
    moved = payload.get("moved")
    installed = payload.get("installed")
    if (
        categories != sorted(SUPPORTED_CATEGORIES)
        or not isinstance(original, list)
        or not isinstance(moved, list)
        or not isinstance(installed, list)
        or any(
            not isinstance(values, list)
            or len(values) != len(set(values))
            or any(value not in SUPPORTED_CATEGORIES for value in values)
            for values in (original, moved, installed)
        )
        or not set(moved).issubset(set(original))
    ):
        raise RestoreError("restore lifecycle journal category state is invalid")
    work_name = payload.get("work_name")
    rollback_name = payload.get("rollback_name")
    safety = payload.get("safety_backup")
    if (
        not isinstance(work_name, str)
        or not work_name.startswith("neutral-restore-")
        or "/" in work_name
        or "\\" in work_name
        or not isinstance(rollback_name, str)
        or not rollback_name.startswith("neutral-rollback-")
        or "/" in rollback_name
        or "\\" in rollback_name
        or not isinstance(safety, str)
        or not Path(safety).is_absolute()
    ):
        raise RestoreError("restore lifecycle journal paths are invalid")
    active = payload.get("active_category")
    if active is not None and active not in SUPPORTED_CATEGORIES:
        raise RestoreError("restore lifecycle journal active category is invalid")
    if payload.get("phase") not in {
        "prepared", "relocating_old", "old_relocated", "promoting_new",
        "new_promoted", "verifying", "completed",
    }:
        raise RestoreError("restore lifecycle journal phase is invalid")
    return payload


def _verify_live_matches_backup(root: Path, safety_backup: Path) -> None:
    manifest, contents = _read_archive(safety_backup)
    expected = {entry.path for entry in manifest.entries}
    observed = {relative for _path, _category, relative in _iter_source_files(root)}
    if observed != expected:
        raise RestoreError("restored live file set does not match the safety backup")
    for entry in manifest.entries:
        restored = root.joinpath(*PurePosixPath(entry.path).parts)
        data = _read_stable_file(restored)
        if data != contents[entry.path]:
            raise RestoreError("restored live bytes do not match the safety backup")


def reconcile_restore(live_root: Path) -> bool:
    """Roll back one interrupted restore journal before normal data access.

    Returns ``True`` when an interrupted transaction was reconciled.  The
    automatic restore entry point calls this first; applications should also
    call it during startup before exposing the live data directory.
    """

    root = _safe_directory(live_root, "live data root", create=True)
    journal = _restore_journal_path(root)
    if not _path_exists(journal):
        return False
    with DirectoryGuard(root.parent) as parent_guard, DirectoryGuard(root) as root_guard:
        assert_safe_descendant(parent_guard, journal)
        payload = _load_restore_journal(journal, root)
        work = root.parent / str(payload["work_name"])
        rollback = root.parent / str(payload["rollback_name"])
        if not work.is_dir() or _is_reparse(work) or not rollback.is_dir() or _is_reparse(rollback):
            raise RestoreError("restore reconciliation workspace is unavailable")
        with DirectoryGuard(work) as work_guard, DirectoryGuard(rollback) as rollback_guard:
            installed = list(payload["installed"])
            moved = list(payload["moved"])
            active = payload["active_category"]
            phase = payload["phase"]
            # A process may die after a rename and before the following journal
            # replacement.  The pre-rename intent phase makes that state
            # inferable without trusting timestamps.
            if phase == "promoting_new" and isinstance(active, str):
                incoming = work / active
                current = root / active
                if _path_exists(current) and not _path_exists(incoming) and active not in installed:
                    installed.append(active)
            if phase == "relocating_old" and isinstance(active, str):
                old = rollback / active
                current = root / active
                if _path_exists(old) and not _path_exists(current) and active not in moved:
                    moved.append(active)
            for category in reversed(installed):
                current = root / category
                if _path_exists(current):
                    assert_safe_descendant(root_guard, current)
                    destination = work / f"reconcile-new-{category}-{uuid.uuid4().hex}"
                    assert_safe_descendant(work_guard, destination, allow_missing_leaf=True)
                    os.rename(current, destination)
                    _flush_directory(root)
            for category in reversed(moved):
                current = root / category
                old = rollback / category
                if _path_exists(old):
                    assert_safe_descendant(rollback_guard, old)
                    if _path_exists(current):
                        raise RestoreError("restore rollback destination is ambiguous")
                    os.rename(old, current)
                    _flush_directory(root)
                    assert_safe_descendant(root_guard, current)
            _verify_live_matches_backup(root, Path(str(payload["safety_backup"])))
            journal.unlink()
            _flush_directory(journal.parent)
            return True


def restore_backup(
    archive_path: Path,
    live_root: Path,
    *,
    safety_backup_path: Path | None = None,
    fault_hook: Callable[[str], None] | None = None,
) -> Path:
    """Validate then restore all categories; return the automatic safety archive."""
    root = _safe_directory(live_root, "live data root", create=True)
    # An interrupted earlier restore is always resolved before a new archive is
    # even inspected.  This is the startup recovery boundary for live data.
    reconcile_restore(root)
    manifest, contents = _read_archive(archive_path)
    if safety_backup_path is None:
        safety_backup_path = root.parent / f".neutral-pre-restore-{uuid.uuid4().hex}.zip"
    if not isinstance(safety_backup_path, Path) or not safety_backup_path.is_absolute():
        raise RestoreError("safety backup must be an absolute path")
    safety_backup_path = safety_backup_path.absolute()
    if _is_within(root, safety_backup_path) or safety_backup_path == archive_path:
        raise RestoreError("safety backup must be a fresh file outside live data")
    # This happens before any live mutation.  It remains available even when
    # extraction/restore fails, which makes recovery auditable.
    create_backup(root, safety_backup_path)
    validate_backup(safety_backup_path)
    categories = list(manifest.categories)
    # Reject every unsafe live category before moving the first one.
    for category in categories:
        current = root / category
        if _path_exists(current) and (_is_reparse(current) or not current.is_dir()):
            raise RestoreError("live category is not a regular directory")
    work = Path(tempfile.mkdtemp(prefix="neutral-restore-", dir=str(root.parent))).absolute()
    rollback = Path(tempfile.mkdtemp(prefix="neutral-rollback-", dir=str(root.parent))).absolute()
    journal = _restore_journal_path(root)
    if _path_exists(journal):
        raise RestoreError("another restore lifecycle journal is active")
    completed = False
    try:
        with (
            DirectoryGuard(root.parent) as parent_guard,
            DirectoryGuard(root) as root_guard,
            DirectoryGuard(work) as work_guard,
            DirectoryGuard(rollback) as rollback_guard,
        ):
            for category in categories:
                make_directories(work_guard, work / category)
            for entry in manifest.entries:
                target = work.joinpath(*PurePosixPath(entry.path).parts)
                make_directories(work_guard, target.parent)
                with open_exclusive_binary(work_guard, target) as handle:
                    handle.write(contents[entry.path])
                    handle.flush()
                    os.fsync(handle.fileno())
            original_present = [
                category for category in categories if _path_exists(root / category)
            ]
            lifecycle: dict[str, object] = {
                "journal_schema": RESTORE_JOURNAL_SCHEMA,
                "root_name": root.name,
                "work_name": work.name,
                "rollback_name": rollback.name,
                "safety_backup": str(safety_backup_path),
                "categories": categories,
                "original_present": original_present,
                "moved": [],
                "installed": [],
                "active_category": None,
                "phase": "prepared",
            }
            _create_restore_journal(journal, lifecycle, parent_guard)
            for category in categories:
                current = root / category
                if _path_exists(current):
                    assert_safe_descendant(root_guard, current)
                    lifecycle.update(active_category=category, phase="relocating_old")
                    _write_restore_journal(journal, lifecycle, parent_guard)
                    destination = rollback / category
                    assert_safe_descendant(
                        rollback_guard, destination, allow_missing_leaf=True
                    )
                    os.rename(current, destination)
                    _flush_directory(root)
                    _flush_directory(rollback)
                    assert_safe_descendant(rollback_guard, destination)
                    cast_moved = lifecycle["moved"]
                    assert isinstance(cast_moved, list)
                    cast_moved.append(category)
                    lifecycle["phase"] = "old_relocated"
                    _write_restore_journal(journal, lifecycle, parent_guard)
                    if fault_hook:
                        fault_hook(f"restore.after_live_relocated:{category}")
                incoming = work / category
                assert_safe_descendant(work_guard, incoming)
                if _path_exists(current):
                    raise RestoreError("live category was claimed during restore")
                lifecycle.update(active_category=category, phase="promoting_new")
                _write_restore_journal(journal, lifecycle, parent_guard)
                os.rename(incoming, current)
                _flush_directory(work)
                _flush_directory(root)
                assert_safe_descendant(root_guard, current)
                cast_installed = lifecycle["installed"]
                assert isinstance(cast_installed, list)
                cast_installed.append(category)
                lifecycle["phase"] = "new_promoted"
                _write_restore_journal(journal, lifecycle, parent_guard)
                if fault_hook:
                    fault_hook(f"restore.after_incoming_promoted:{category}")
            lifecycle.update(active_category=None, phase="verifying")
            _write_restore_journal(journal, lifecycle, parent_guard)
            for category in categories:
                _fsync_tree(root / category, root_guard)
            for entry in manifest.entries:
                restored = root.joinpath(*PurePosixPath(entry.path).parts)
                assert_safe_descendant(root_guard, restored)
                data = _read_stable_file(restored)
                if len(data) != entry.size or _sha256(data) != entry.sha256:
                    raise RestoreError("restored data failed final integrity verification")
            lifecycle["phase"] = "completed"
            _write_restore_journal(journal, lifecycle, parent_guard)
            journal.unlink()
            _flush_directory(journal.parent)
            completed = True
            return safety_backup_path
    except BaseException as exc:
        try:
            if _path_exists(journal):
                reconcile_restore(root)
            else:
                _verify_live_matches_backup(root, safety_backup_path)
        except BaseException as rollback_exc:
            raise RestoreError(
                "restore failed and automatic rollback needs recovery from the safety backup"
            ) from rollback_exc
        raise RestoreError("restore failed; live data was rolled back") from exc
    finally:
        if completed:
            shutil.rmtree(work)
            shutil.rmtree(rollback)


def validate_backup(archive_path: Path) -> BackupManifest:
    """Validate an archive without touching live data and return its manifest."""
    manifest, _ = _read_archive(archive_path)
    return manifest


def _fsync_tree(path: Path, root_guard: DirectoryGuard | None = None) -> None:
    if root_guard is not None:
        assert_safe_descendant(root_guard, path)
    for child in path.rglob("*"):
        if child.is_file():
            if root_guard is not None:
                assert_safe_descendant(root_guard, child)
            with child.open("rb+") as handle:
                os.fsync(handle.fileno())


class BackupRestore:
    """Object facade for callers that prefer dependency-injected roots."""

    def __init__(self, live_root: Path) -> None:
        self.live_root = live_root

    def create(self, archive_path: Path) -> Path:
        return create_backup(self.live_root, archive_path)

    def restore(
        self,
        archive_path: Path,
        *,
        safety_backup_path: Path | None = None,
        fault_hook: Callable[[str], None] | None = None,
    ) -> Path:
        return restore_backup(
            archive_path,
            self.live_root,
            safety_backup_path=safety_backup_path,
            fault_hook=fault_hook,
        )


# A manager name reads naturally in application wiring; retain one canonical
# implementation so both names have exactly the same validation semantics.
BackupManager = BackupRestore
make_backup = create_backup


__all__ = [
    "ArchiveRejected", "BackupError", "BackupManager", "BackupManifest", "BackupRestore", "ManifestEntry", "RestoreError",
    "SUPPORTED_CATEGORIES", "SCHEMA_VERSION", "VOICE_PROFILE_SCHEMA", "create_backup", "make_backup", "reconcile_restore", "restore_backup", "validate_backup",
]
