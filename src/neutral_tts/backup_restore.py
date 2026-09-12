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
from typing import BinaryIO, Iterable

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


class BackupRestoreBusy(BackupError):
    """Another process owns the same bounded data-root lifecycle."""


SCHEMA_VERSION = "neutral-tts-backup-v1"
VOICE_PROFILE_SCHEMA = "neutral-generic-voice-profile-v1"
MANIFEST_NAME = "manifest.json"
RESTORE_JOURNAL_SCHEMA = "neutral-tts-restore-journal-v2"
SUPPORTED_CATEGORIES = frozenset({"settings", "history", "dictionary", "voice_profiles"})
MAX_ENTRIES = 256
MAX_FILE_SIZE = 8 * 1024 * 1024
MAX_EXPANDED_SIZE = 32 * 1024 * 1024
MAX_COMPRESSION_RATIO = 100
RESTORE_CAPACITY_RESERVE_BYTES = 1024 * 1024
_REPARSE_ATTRIBUTE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


def _owner_lock_path(root: Path) -> Path:
    return root.parent / f".{root.name}.backup-restore-owner.lock"


class _DataRootOwner:
    """Hold one OS-released lock before inspecting a root lifecycle journal."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.path = _owner_lock_path(root)
        self._handle: BinaryIO | None = None
        self._locked = False
        self._parent_guard: DirectoryGuard | None = None

    def _lock(self) -> None:
        assert self._handle is not None
        self._handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self._handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(
                    self._handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB
                )
        except (OSError, BlockingIOError) as exc:
            raise BackupRestoreBusy(
                "another backup/restore operation is active"
            ) from exc
        self._locked = True

    def _unlock(self) -> None:
        if not self._locked or self._handle is None:
            return
        try:
            self._handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        finally:
            self._locked = False

    def __enter__(self) -> "_DataRootOwner":
        if self.root.parent == self.root:
            raise BackupError("data root cannot be a filesystem root")
        try:
            self._parent_guard = DirectoryGuard(self.root.parent)
            assert_safe_descendant(
                self._parent_guard, self.path, allow_missing_leaf=True
            )
            if _path_exists(self.path) and (
                _is_reparse(self.path) or not self.path.is_file()
            ):
                raise BackupError("backup/restore owner path is unsafe")
            flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0)
            flags |= getattr(os, "O_NOINHERIT", 0)
            descriptor = os.open(self.path, flags, 0o600)
            self._handle = os.fdopen(descriptor, "r+b", buffering=0)
            opened = os.fstat(self._handle.fileno())
            linked = self.path.stat(follow_symlinks=False)
            if (
                _is_reparse(self.path)
                or not stat.S_ISREG(opened.st_mode)
                or (opened.st_dev, opened.st_ino)
                != (linked.st_dev, linked.st_ino)
            ):
                raise BackupError("backup/restore owner path changed")
            self._lock()
            self._parent_guard.assert_unchanged()
            linked_after = self.path.stat(follow_symlinks=False)
            if (
                _is_reparse(self.path)
                or (opened.st_dev, opened.st_ino)
                != (linked_after.st_dev, linked_after.st_ino)
            ):
                raise BackupError("backup/restore owner path changed")
            return self
        except BaseException as exc:
            try:
                try:
                    self._unlock()
                except OSError:
                    pass
            finally:
                if self._handle is not None:
                    self._handle.close()
                    self._handle = None
                if self._parent_guard is not None:
                    self._parent_guard.close()
                    self._parent_guard = None
            if isinstance(exc, FilesystemBoundaryError):
                raise BackupError("backup/restore owner boundary changed") from exc
            if isinstance(exc, OSError):
                raise BackupError("backup/restore owner claim is unavailable") from exc
            raise

    def __exit__(self, _type: object, _value: object, _traceback: object) -> None:
        try:
            self._unlock()
        finally:
            if self._handle is not None:
                self._handle.close()
                self._handle = None
            if self._parent_guard is not None:
                self._parent_guard.close()
                self._parent_guard = None


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


@dataclass(frozen=True, slots=True)
class BackupArtifact:
    """The exact archive inode and bytes committed by one backup operation."""

    path: Path
    manifest: BackupManifest
    archive_size: int
    archive_sha256: str
    device: int
    inode: int


@dataclass(frozen=True, slots=True)
class ReconcileResult:
    """A truthful terminal state for an explicit recovery attempt."""

    reconciled: bool
    terminal_state: str


@dataclass(frozen=True, slots=True)
class RestoreResult:
    """The committed restore manifest, bound safety artifact, and terminal state."""

    restored_manifest: BackupManifest
    safety_backup: BackupArtifact
    terminal_state: str


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


def _published_artifact(
    archive_path: Path, manifest: BackupManifest
) -> BackupArtifact:
    try:
        before = archive_path.stat(follow_symlinks=False)
        if _is_reparse(archive_path) or not stat.S_ISREG(before.st_mode):
            raise BackupError("published backup is not a regular file")
        digest = hashlib.sha256()
        size = 0
        with archive_path.open("rb") as handle:
            opened = os.fstat(handle.fileno())
            while block := handle.read(1024 * 1024):
                size += len(block)
                digest.update(block)
            opened_after = os.fstat(handle.fileno())
        after = archive_path.stat(follow_symlinks=False)
    except BackupError:
        raise
    except OSError as exc:
        raise BackupError("published backup identity is unavailable") from exc
    identities = {
        (item.st_dev, item.st_ino, item.st_size, item.st_mtime_ns)
        for item in (before, opened, opened_after, after)
    }
    if len(identities) != 1 or size != before.st_size:
        raise BackupError("published backup changed during verification")
    return BackupArtifact(
        archive_path,
        manifest,
        size,
        digest.hexdigest(),
        before.st_dev,
        before.st_ino,
    )


def _remove_own_failed_publication(
    archive_path: Path, identity: tuple[int, int] | None
) -> None:
    if identity is None:
        return
    try:
        current = archive_path.stat(follow_symlinks=False)
        if (
            not _is_reparse(archive_path)
            and stat.S_ISREG(current.st_mode)
            and (current.st_dev, current.st_ino) == identity
        ):
            archive_path.unlink()
    except OSError:
        pass


def _create_backup_artifact_owned(
    source_root: Path, archive_path: Path
) -> BackupArtifact:
    """Create one archive while the caller owns the source-root lifecycle."""

    root = _safe_directory(source_root, "backup root")
    archive_path = _safe_new_file(archive_path, "backup destination")
    with DirectoryGuard(root) as root_guard, DirectoryGuard(
        archive_path.parent
    ) as archive_parent_guard:
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
        temporary = archive_path.with_name(
            archive_path.name + ".tmp-" + uuid.uuid4().hex
        )
        if _path_exists(temporary):
            raise BackupError("backup staging destination was claimed")
        published_identity: tuple[int, int] | None = None
        try:
            with zipfile.ZipFile(
                temporary,
                "x",
                compression=zipfile.ZIP_STORED,
                allowZip64=False,
            ) as archive:
                info = zipfile.ZipInfo(
                    MANIFEST_NAME, date_time=(1980, 1, 1, 0, 0, 0)
                )
                info.compress_type = zipfile.ZIP_STORED
                info.create_system = 3
                info.external_attr = (stat.S_IFREG | 0o600) << 16
                archive.writestr(info, _canonical(manifest.payload()))
                for relative in sorted(
                    contents, key=lambda item: (item.casefold(), item)
                ):
                    info = zipfile.ZipInfo(
                        relative, date_time=(1980, 1, 1, 0, 0, 0)
                    )
                    info.compress_type = zipfile.ZIP_STORED
                    info.create_system = 3
                    info.external_attr = (stat.S_IFREG | 0o600) << 16
                    archive.writestr(info, contents[relative])
            with temporary.open("rb+") as handle:
                handle.flush()
                os.fsync(handle.fileno())
            root_guard.assert_unchanged()
            archive_parent_guard.assert_unchanged()
            try:
                os.link(temporary, archive_path)
            except FileExistsError as exc:
                raise BackupError("backup destination was claimed") from exc
            except OSError as exc:
                raise BackupError(
                    "atomic no-replace backup publication is unavailable"
                ) from exc
            linked = archive_path.stat(follow_symlinks=False)
            published_identity = linked.st_dev, linked.st_ino
            artifact = _published_artifact(archive_path, manifest)
            if (artifact.device, artifact.inode) != published_identity:
                raise BackupError("published backup identity changed")
            root_guard.assert_unchanged()
            archive_parent_guard.assert_unchanged()
            return artifact
        except BaseException:
            _remove_own_failed_publication(archive_path, published_identity)
            raise
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


def create_backup_artifact(source_root: Path, archive_path: Path) -> BackupArtifact:
    """Write, verify, and identify a deterministic fresh-file ZIP backup."""

    root = _safe_directory(source_root, "backup root")
    if (
        isinstance(archive_path, Path)
        and archive_path.is_absolute()
        and _is_within(root, archive_path)
    ):
        raise BackupError("backup destination must be outside the data root")
    with _DataRootOwner(root):
        return _create_backup_artifact_owned(root, archive_path)


def create_backup(source_root: Path, archive_path: Path) -> Path:
    """Write a deterministic ZIP backup and refuse to replace an archive."""

    return create_backup_artifact(source_root, archive_path).path


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


def _load_restore_journal(
    journal: Path, root: Path
) -> tuple[dict[str, object], BackupManifest, BackupManifest]:
    try:
        raw = journal.read_bytes()
        payload = json.loads(raw.decode("utf-8", "strict"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RestoreError("restore lifecycle journal is unreadable") from exc
    if raw != _canonical(payload) + b"\n" or not isinstance(payload, dict):
        raise RestoreError("restore lifecycle journal is non-canonical")
    required = {
        "active_category", "categories", "installed", "journal_schema",
        "incoming_manifest", "moved", "original_present", "phase",
        "rollback_name", "root_name", "safety_archive_sha256",
        "safety_archive_size", "safety_backup", "safety_device",
        "safety_inode", "safety_manifest", "work_name",
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
    phase = payload.get("phase")
    if phase not in {
        "prepared", "relocating_old", "old_relocated", "promoting_new",
        "new_promoted", "verifying", "reverting_new", "new_reverted",
        "restoring_old", "old_restored", "rollback_verifying",
        "rolled_back", "completed",
    }:
        raise RestoreError("restore lifecycle journal phase is invalid")
    archive_digest = payload.get("safety_archive_sha256")
    archive_size = payload.get("safety_archive_size")
    safety_device = payload.get("safety_device")
    safety_inode = payload.get("safety_inode")
    if (
        not isinstance(archive_digest, str)
        or len(archive_digest) != 64
        or any(character not in "0123456789abcdef" for character in archive_digest)
        or isinstance(archive_size, bool)
        or not isinstance(archive_size, int)
        or archive_size <= 0
        or isinstance(safety_device, bool)
        or not isinstance(safety_device, int)
        or safety_device < 0
        or isinstance(safety_inode, bool)
        or not isinstance(safety_inode, int)
        or safety_inode < 0
    ):
        raise RestoreError("restore lifecycle journal safety identity is invalid")
    try:
        incoming_manifest = _validate_manifest(payload.get("incoming_manifest"))
        safety_manifest = _validate_manifest(payload.get("safety_manifest"))
    except BackupError as exc:
        raise RestoreError("restore lifecycle journal manifest is invalid") from exc
    if phase == "completed" and (
        active is not None
        or set(installed) != set(categories)
        or set(moved) != set(original)
    ):
        raise RestoreError("completed restore journal state is incomplete")
    if phase == "rolled_back" and (active is not None or installed or moved):
        raise RestoreError("rolled-back restore journal state is incomplete")
    return payload, incoming_manifest, safety_manifest


def _verify_live_matches_manifest(
    root: Path, manifest: BackupManifest, *, label: str
) -> None:
    expected = {entry.path for entry in manifest.entries}
    observed = {relative for _path, _category, relative in _iter_source_files(root)}
    if observed != expected:
        raise RestoreError(f"{label} file set does not match its manifest")
    for entry in manifest.entries:
        restored = root.joinpath(*PurePosixPath(entry.path).parts)
        data = _read_stable_file(restored)
        if len(data) != entry.size or _sha256(data) != entry.sha256:
            raise RestoreError(f"{label} bytes do not match their manifest")


def _remove_restore_workspace(
    workspace: Path, parent_guard: DirectoryGuard
) -> bool:
    if not _path_exists(workspace):
        return True
    try:
        assert_safe_descendant(parent_guard, workspace)
        if _is_reparse(workspace) or not workspace.is_dir():
            return False
        for current, directories, names in os.walk(
            workspace, topdown=True, followlinks=False
        ):
            current_path = Path(current)
            for name in (*directories, *names):
                if _is_reparse(current_path / name):
                    return False
        shutil.rmtree(workspace)
        parent_guard.assert_unchanged()
        return not _path_exists(workspace)
    except (OSError, BackupError, FilesystemBoundaryError):
        return False


def _finish_terminal_restore(
    root: Path,
    journal: Path,
    payload: dict[str, object],
    expected_manifest: BackupManifest,
    parent_guard: DirectoryGuard,
) -> ReconcileResult:
    phase = str(payload["phase"])
    prefix = "COMPLETED" if phase == "completed" else "ROLLED_BACK"
    try:
        _verify_live_matches_manifest(
            root,
            expected_manifest,
            label="committed restore" if phase == "completed" else "rolled-back restore",
        )
    except BackupError:
        return ReconcileResult(False, f"{prefix}_INTEGRITY_UNVERIFIED")
    work = root.parent / str(payload["work_name"])
    rollback = root.parent / str(payload["rollback_name"])
    if not _remove_restore_workspace(work, parent_guard):
        return ReconcileResult(False, f"{prefix}_CLEANUP_PENDING")
    if not _remove_restore_workspace(rollback, parent_guard):
        return ReconcileResult(False, f"{prefix}_CLEANUP_PENDING")
    try:
        assert_safe_descendant(parent_guard, journal)
        journal.unlink()
        _flush_directory(journal.parent)
    except (OSError, FilesystemBoundaryError):
        return ReconcileResult(False, f"{prefix}_CLEANUP_PENDING")
    return ReconcileResult(True, prefix)


def _reconcile_restore_owned(
    root: Path, *, fault_hook: Callable[[str], None] | None = None
) -> ReconcileResult:
    """Resolve one journal while the caller holds the root operation lock."""

    journal = _restore_journal_path(root)
    if not _path_exists(journal):
        return ReconcileResult(False, "NO_PENDING_RESTORE")
    with DirectoryGuard(root.parent) as parent_guard, DirectoryGuard(root) as root_guard:
        assert_safe_descendant(parent_guard, journal)
        payload, incoming_manifest, safety_manifest = _load_restore_journal(
            journal, root
        )
        work = root.parent / str(payload["work_name"])
        rollback = root.parent / str(payload["rollback_name"])
        phase = str(payload["phase"])
        if phase in {"completed", "rolled_back"}:
            expected = incoming_manifest if phase == "completed" else safety_manifest
            return _finish_terminal_restore(
                root, journal, payload, expected, parent_guard
            )
        if (
            not work.is_dir()
            or _is_reparse(work)
            or not rollback.is_dir()
            or _is_reparse(rollback)
        ):
            raise RestoreError("restore reconciliation workspace is unavailable")
        with DirectoryGuard(work) as work_guard, DirectoryGuard(rollback) as rollback_guard:
            installed = list(payload["installed"])
            moved = list(payload["moved"])
            active = payload["active_category"]
            # A process may die after a rename and before the following journal
            # replacement.  The pre-rename intent phase makes that state
            # inferable without trusting timestamps.
            if phase == "promoting_new" and isinstance(active, str):
                incoming = work / active
                current = root / active
                if (
                    _path_exists(current)
                    and not _path_exists(incoming)
                    and active not in installed
                ):
                    installed.append(active)
                    payload.update(
                        active_category=None,
                        installed=installed,
                        phase="new_promoted",
                    )
                    _write_restore_journal(journal, payload, parent_guard)
                elif _path_exists(current) == _path_exists(incoming):
                    raise RestoreError("restore promotion state is ambiguous")
            if phase == "relocating_old" and isinstance(active, str):
                old = rollback / active
                current = root / active
                if (
                    _path_exists(old)
                    and not _path_exists(current)
                    and active not in moved
                ):
                    moved.append(active)
                    payload.update(
                        active_category=None,
                        moved=moved,
                        phase="old_relocated",
                    )
                    _write_restore_journal(journal, payload, parent_guard)
                elif _path_exists(old) == _path_exists(current):
                    raise RestoreError("restore relocation state is ambiguous")
            if phase == "reverting_new" and isinstance(active, str):
                incoming = work / active
                current = root / active
                if not _path_exists(current) and _path_exists(incoming):
                    if active not in installed:
                        raise RestoreError("restore rollback state is inconsistent")
                    installed.remove(active)
                    payload.update(
                        active_category=None,
                        installed=installed,
                        phase="new_reverted",
                    )
                    _write_restore_journal(journal, payload, parent_guard)
                elif _path_exists(current) == _path_exists(incoming):
                    raise RestoreError("restore rollback state is ambiguous")
            if phase == "restoring_old" and isinstance(active, str):
                old = rollback / active
                current = root / active
                if not _path_exists(old) and _path_exists(current):
                    if active not in moved:
                        raise RestoreError("restore rollback state is inconsistent")
                    moved.remove(active)
                    payload.update(
                        active_category=None,
                        moved=moved,
                        phase="old_restored",
                    )
                    _write_restore_journal(journal, payload, parent_guard)
                elif _path_exists(old) == _path_exists(current):
                    raise RestoreError("restore rollback state is ambiguous")
            for category in reversed(tuple(installed)):
                current = root / category
                incoming = work / category
                if not _path_exists(current) or _path_exists(incoming):
                    raise RestoreError("restore rollback destination is ambiguous")
                assert_safe_descendant(root_guard, current)
                assert_safe_descendant(
                    work_guard, incoming, allow_missing_leaf=True
                )
                payload.update(active_category=category, phase="reverting_new")
                _write_restore_journal(journal, payload, parent_guard)
                os.rename(current, incoming)
                _flush_directory(root)
                _flush_directory(work)
                if fault_hook:
                    fault_hook(f"reconcile.after_new_reverted:{category}")
                installed.remove(category)
                payload.update(
                    active_category=None,
                    installed=installed,
                    phase="new_reverted",
                )
                _write_restore_journal(journal, payload, parent_guard)
            for category in reversed(tuple(moved)):
                current = root / category
                old = rollback / category
                if not _path_exists(old) or _path_exists(current):
                    raise RestoreError("restore rollback destination is ambiguous")
                assert_safe_descendant(rollback_guard, old)
                assert_safe_descendant(root_guard, current, allow_missing_leaf=True)
                payload.update(active_category=category, phase="restoring_old")
                _write_restore_journal(journal, payload, parent_guard)
                os.rename(old, current)
                _flush_directory(root)
                _flush_directory(rollback)
                if fault_hook:
                    fault_hook(f"reconcile.after_old_restored:{category}")
                moved.remove(category)
                payload.update(
                    active_category=None,
                    moved=moved,
                    phase="old_restored",
                )
                _write_restore_journal(journal, payload, parent_guard)
            payload.update(active_category=None, phase="rollback_verifying")
            _write_restore_journal(journal, payload, parent_guard)
            _verify_live_matches_manifest(
                root, safety_manifest, label="rolled-back restore"
            )
            payload["phase"] = "rolled_back"
            _write_restore_journal(journal, payload, parent_guard)
        return _finish_terminal_restore(
            root, journal, payload, safety_manifest, parent_guard
        )


def reconcile_restore_result(
    live_root: Path, *, fault_hook: Callable[[str], None] | None = None
) -> ReconcileResult:
    """Reconcile one root under an OS-released process owner lock."""

    root = _safe_directory(live_root, "live data root", create=True)
    with _DataRootOwner(root):
        return _reconcile_restore_owned(root, fault_hook=fault_hook)


def reconcile_restore(
    live_root: Path, *, fault_hook: Callable[[str], None] | None = None
) -> bool:
    """Return whether an interrupted restore reached a clean terminal state."""

    return reconcile_restore_result(live_root, fault_hook=fault_hook).reconciled


def _supported_source_bytes(root: Path) -> int:
    total = 0
    for path, category, relative in _iter_source_files(root):
        data = _read_stable_file(path)
        try:
            _validate_category_payload(category, relative, data)
        except ArchiveRejected as exc:
            raise RestoreError("live data cannot be captured safely") from exc
        total += len(data)
    return total


def _capacity_device(directory: Path) -> int:
    """Return the filesystem identity used to combine capacity requirements."""

    try:
        return directory.stat(follow_symlinks=False).st_dev
    except OSError as exc:
        raise RestoreError("restore capacity endpoint is unavailable") from exc


def _assert_restore_capacity(
    root: Path,
    safety_backup_path: Path,
    incoming_manifest: BackupManifest,
    capacity_probe: Callable[[Path], int] | None,
) -> None:
    if capacity_probe is None:
        return
    incoming_bytes = sum(entry.size for entry in incoming_manifest.entries)
    current_bytes = _supported_source_bytes(root)
    requirements: dict[int, tuple[Path, int]] = {}
    for directory, required in (
        (
            root.parent,
            incoming_bytes + RESTORE_CAPACITY_RESERVE_BYTES,
        ),
        (
            safety_backup_path.parent,
            current_bytes + RESTORE_CAPACITY_RESERVE_BYTES,
        ),
    ):
        device = _capacity_device(directory)
        if device in requirements:
            existing_directory, existing_required = requirements[device]
            requirements[device] = (
                existing_directory,
                existing_required + required,
            )
        else:
            requirements[device] = directory, required
    for directory, required in requirements.values():
        try:
            available = capacity_probe(directory)
        except Exception as exc:
            raise RestoreError("restore capacity is unavailable") from exc
        if (
            isinstance(available, bool)
            or not isinstance(available, int)
            or available < required
        ):
            raise RestoreError("restore capacity is unavailable")


def _restore_backup_result_owned(
    archive_path: Path,
    root: Path,
    *,
    safety_backup_path: Path | None = None,
    expected_manifest: BackupManifest | None = None,
    fault_hook: Callable[[str], None] | None = None,
    capacity_probe: Callable[[Path], int] | None = None,
) -> RestoreResult:
    previous = _reconcile_restore_owned(root)
    if previous.terminal_state.endswith(("_PENDING", "_UNVERIFIED")):
        raise RestoreError("an earlier restore requires explicit recovery")
    manifest, contents = _read_archive(archive_path)
    if expected_manifest is not None and manifest != expected_manifest:
        raise ArchiveRejected("backup archive differs from its admitted manifest")
    if safety_backup_path is None:
        safety_backup_path = (
            root.parent / f".neutral-pre-restore-{uuid.uuid4().hex}.zip"
        )
    if not isinstance(safety_backup_path, Path) or not safety_backup_path.is_absolute():
        raise RestoreError("safety backup must be an absolute path")
    safety_backup_path = safety_backup_path.absolute()
    if _is_within(root, safety_backup_path) or safety_backup_path == archive_path:
        raise RestoreError("safety backup must be a fresh file outside live data")
    if _path_exists(safety_backup_path):
        raise RestoreError("safety backup destination was claimed")
    categories = list(manifest.categories)
    for category in categories:
        current = root / category
        if _path_exists(current) and (
            _is_reparse(current) or not current.is_dir()
        ):
            raise RestoreError("live category is not a regular directory")
    with (
        DirectoryGuard(archive_path.parent) as archive_parent_guard,
        DirectoryGuard(root.parent) as root_parent_guard,
        DirectoryGuard(root) as root_guard,
        DirectoryGuard(safety_backup_path.parent) as safety_parent_guard,
    ):
        _assert_restore_capacity(
            root, safety_backup_path, manifest, capacity_probe
        )
        archive_parent_guard.assert_unchanged()
        root_parent_guard.assert_unchanged()
        root_guard.assert_unchanged()
        safety_parent_guard.assert_unchanged()
        safety_artifact = _create_backup_artifact_owned(root, safety_backup_path)
        work: Path | None = None
        rollback: Path | None = None
        journal = _restore_journal_path(root)
        committed = False
        try:
            work = Path(
                tempfile.mkdtemp(prefix="neutral-restore-", dir=str(root.parent))
            ).absolute()
            rollback = Path(
                tempfile.mkdtemp(prefix="neutral-rollback-", dir=str(root.parent))
            ).absolute()
            if _path_exists(journal):
                raise RestoreError("another restore lifecycle journal is active")
            with DirectoryGuard(work) as work_guard, DirectoryGuard(
                rollback
            ) as rollback_guard:
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
                    category
                    for category in categories
                    if _path_exists(root / category)
                ]
                lifecycle: dict[str, object] = {
                    "journal_schema": RESTORE_JOURNAL_SCHEMA,
                    "root_name": root.name,
                    "work_name": work.name,
                    "rollback_name": rollback.name,
                    "safety_backup": str(safety_backup_path),
                    "safety_manifest": safety_artifact.manifest.payload(),
                    "safety_archive_sha256": safety_artifact.archive_sha256,
                    "safety_archive_size": safety_artifact.archive_size,
                    "safety_device": safety_artifact.device,
                    "safety_inode": safety_artifact.inode,
                    "incoming_manifest": manifest.payload(),
                    "categories": categories,
                    "original_present": original_present,
                    "moved": [],
                    "installed": [],
                    "active_category": None,
                    "phase": "prepared",
                }
                _create_restore_journal(journal, lifecycle, root_parent_guard)
                for category in categories:
                    current = root / category
                    if _path_exists(current):
                        assert_safe_descendant(root_guard, current)
                        lifecycle.update(
                            active_category=category, phase="relocating_old"
                        )
                        _write_restore_journal(
                            journal, lifecycle, root_parent_guard
                        )
                        destination = rollback / category
                        assert_safe_descendant(
                            rollback_guard,
                            destination,
                            allow_missing_leaf=True,
                        )
                        os.rename(current, destination)
                        _flush_directory(root)
                        _flush_directory(rollback)
                        assert_safe_descendant(rollback_guard, destination)
                        moved = lifecycle["moved"]
                        assert isinstance(moved, list)
                        moved.append(category)
                        lifecycle["phase"] = "old_relocated"
                        _write_restore_journal(
                            journal, lifecycle, root_parent_guard
                        )
                        if fault_hook:
                            fault_hook(
                                f"restore.after_live_relocated:{category}"
                            )
                    incoming = work / category
                    assert_safe_descendant(work_guard, incoming)
                    if _path_exists(current):
                        raise RestoreError("live category was claimed during restore")
                    lifecycle.update(
                        active_category=category, phase="promoting_new"
                    )
                    _write_restore_journal(journal, lifecycle, root_parent_guard)
                    os.rename(incoming, current)
                    _flush_directory(work)
                    _flush_directory(root)
                    assert_safe_descendant(root_guard, current)
                    installed = lifecycle["installed"]
                    assert isinstance(installed, list)
                    installed.append(category)
                    lifecycle["phase"] = "new_promoted"
                    _write_restore_journal(journal, lifecycle, root_parent_guard)
                    if fault_hook:
                        fault_hook(
                            f"restore.after_incoming_promoted:{category}"
                        )
                lifecycle.update(active_category=None, phase="verifying")
                _write_restore_journal(journal, lifecycle, root_parent_guard)
                for category in categories:
                    _fsync_tree(root / category, root_guard)
                _verify_live_matches_manifest(
                    root, manifest, label="restored data"
                )
                lifecycle["phase"] = "completed"
                _write_restore_journal(journal, lifecycle, root_parent_guard)
                committed = True
                if fault_hook:
                    fault_hook("restore.after_completed_journal")
            terminal = _finish_terminal_restore(
                root,
                journal,
                lifecycle,
                manifest,
                root_parent_guard,
            )
            return RestoreResult(
                manifest, safety_artifact, terminal.terminal_state
            )
        except BaseException as exc:
            if committed:
                terminal = _reconcile_restore_owned(root)
                return RestoreResult(
                    manifest, safety_artifact, terminal.terminal_state
                )
            try:
                if _path_exists(journal):
                    rollback_result = _reconcile_restore_owned(root)
                    if rollback_result.terminal_state != "ROLLED_BACK":
                        raise RestoreError(
                            "restore rollback requires explicit recovery"
                        )
                else:
                    _verify_live_matches_manifest(
                        root,
                        safety_artifact.manifest,
                        label="unchanged live data",
                    )
                    if work is not None:
                        _remove_restore_workspace(work, root_parent_guard)
                    if rollback is not None:
                        _remove_restore_workspace(rollback, root_parent_guard)
            except BaseException as rollback_exc:
                raise RestoreError(
                    "restore failed and automatic rollback needs recovery "
                    "from the safety backup"
                ) from rollback_exc
            raise RestoreError("restore failed; live data was rolled back") from exc


def restore_backup_result(
    archive_path: Path,
    live_root: Path,
    *,
    safety_backup_path: Path | None = None,
    expected_manifest: BackupManifest | None = None,
    fault_hook: Callable[[str], None] | None = None,
    capacity_probe: Callable[[Path], int] | None = None,
) -> RestoreResult:
    """Restore under one root owner and return a truthful terminal result."""

    root = _safe_directory(live_root, "live data root", create=True)
    with _DataRootOwner(root):
        return _restore_backup_result_owned(
            archive_path,
            root,
            safety_backup_path=safety_backup_path,
            expected_manifest=expected_manifest,
            fault_hook=fault_hook,
            capacity_probe=capacity_probe,
        )


def restore_backup(
    archive_path: Path,
    live_root: Path,
    *,
    safety_backup_path: Path | None = None,
    expected_manifest: BackupManifest | None = None,
    fault_hook: Callable[[str], None] | None = None,
    capacity_probe: Callable[[Path], int] | None = None,
) -> Path:
    """Compatibility facade returning the bound pre-restore safety path."""

    return restore_backup_result(
        archive_path,
        live_root,
        safety_backup_path=safety_backup_path,
        expected_manifest=expected_manifest,
        fault_hook=fault_hook,
        capacity_probe=capacity_probe,
    ).safety_backup.path


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
        expected_manifest: BackupManifest | None = None,
        fault_hook: Callable[[str], None] | None = None,
    ) -> Path:
        return restore_backup(
            archive_path,
            self.live_root,
            safety_backup_path=safety_backup_path,
            expected_manifest=expected_manifest,
            fault_hook=fault_hook,
        )


# A manager name reads naturally in application wiring; retain one canonical
# implementation so both names have exactly the same validation semantics.
BackupManager = BackupRestore
make_backup = create_backup


__all__ = [
    "ArchiveRejected", "BackupArtifact", "BackupError", "BackupManager",
    "BackupManifest", "BackupRestore", "BackupRestoreBusy", "ManifestEntry",
    "ReconcileResult", "RestoreError", "RestoreResult",
    "RESTORE_CAPACITY_RESERVE_BYTES", "SUPPORTED_CATEGORIES", "SCHEMA_VERSION",
    "VOICE_PROFILE_SCHEMA", "create_backup", "create_backup_artifact",
    "make_backup", "reconcile_restore", "reconcile_restore_result",
    "restore_backup", "restore_backup_result", "validate_backup",
]
