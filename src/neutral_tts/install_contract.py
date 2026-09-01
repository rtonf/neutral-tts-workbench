"""Source-only install, upgrade, rollback, and uninstall contracts.

The implementation is intentionally a testable filesystem contract rather
than an installer or launcher.  It accepts only a manifest-bound source tree,
keeps application files below one root, keeps user data outside that root, and
never overwrites an existing installation or rollback tree.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import unicodedata
import uuid
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Final

from .fs_safety import (
    DirectoryGuard,
    FilesystemBoundaryError,
    assert_safe_descendant,
    make_directories,
    open_exclusive_binary,
)


INSTALL_LAYOUT_SCHEMA: Final = "neutral-install-layout-v1"
OWNED_FILES_MANIFEST: Final = "OWNED_FILES.json"
DEMO_VERIFICATION_SCOPE: Final = "DEMO_SOURCE_ONLY"
INSTALL_JOURNAL_SCHEMA: Final = "neutral-install-lifecycle-v1"
MAX_FILES: Final = 4096
MAX_FILE_BYTES: Final = 64 * 1024 * 1024
MAX_TOTAL_BYTES: Final = 256 * 1024 * 1024
MAX_MANIFEST_BYTES: Final = 4 * 1024 * 1024

_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_GIT_OBJECT = re.compile(r"[0-9a-f]{40}\Z")
_CANDIDATE = re.compile(r"[a-z0-9](?:[a-z0-9.-]{0,94}[a-z0-9])?\Z")
_ROLES = frozenset({"source", "configuration", "documentation", "metadata"})
_FORBIDDEN_SUFFIXES = frozenset(
    {
        ".7z", ".bin", ".ckpt", ".dll", ".dylib", ".exe", ".flac",
        ".lib", ".model", ".mp3", ".msi", ".npy", ".npz", ".ogg",
        ".onnx", ".pkl", ".pth", ".pt", ".pyd", ".safetensors", ".so",
        ".tar", ".tflite", ".wav", ".whl", ".zip",
    }
)
_FORBIDDEN_COMPONENTS = frozenset(
    {"bin", "installers", "models", "native", "runtime", "site-packages", "voices"}
)
_RESERVED = frozenset(
    {"aux", "con", "nul", "prn"}
    | {f"com{index}" for index in range(1, 10)}
    | {f"lpt{index}" for index in range(1, 10)}
)
_REPARSE_ATTRIBUTE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


class InstallContractError(ValueError):
    """Base class for a rejected install-layout operation."""


class InstallPathError(InstallContractError):
    """A path crosses or weakens the source-only ownership boundary."""


class InstallIntegrityError(InstallContractError):
    """A manifest or owned file fails deterministic integrity validation."""


class InstallStateError(InstallContractError):
    """The requested lifecycle transition is unsafe for the observed state."""


FaultHook = Callable[[str], None]


@dataclass(frozen=True, slots=True)
class InstallResult:
    action: str
    candidate_id: str
    app_root: Path
    retained_root: Path | None
    file_count: int


def _canonical_json(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def _is_reparse(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise InstallPathError("filesystem metadata is unavailable") from exc
    return path.is_symlink() or bool(
        getattr(metadata, "st_file_attributes", 0) & _REPARSE_ATTRIBUTE
    )


def _path_exists(path: Path) -> bool:
    return os.path.lexists(path)


def _check_existing_chain(path: Path, *, require_leaf: bool | None = None) -> Path:
    if not isinstance(path, Path) or not path.is_absolute():
        raise InstallPathError("filesystem roots must be absolute")
    absolute = path.absolute()
    leaf_exists = _path_exists(absolute)
    if require_leaf is True and not leaf_exists:
        raise InstallPathError("required filesystem root is absent")
    if require_leaf is False and leaf_exists:
        raise InstallStateError("fresh filesystem root already exists")
    current = absolute if leaf_exists else absolute.parent
    if not current.exists() or not current.is_dir():
        raise InstallPathError("filesystem root parent is unavailable")
    for component in (current, *current.parents):
        if _is_reparse(component):
            raise InstallPathError("filesystem root contains a reparse point")
    if leaf_exists and not absolute.is_dir():
        raise InstallPathError("filesystem root is not a directory")
    return absolute


def _separate_roots(app_root: Path, user_data_root: Path) -> tuple[Path, Path]:
    app = _check_existing_chain(app_root)
    user = _check_existing_chain(user_data_root)
    app_key = os.path.normcase(str(app))
    user_key = os.path.normcase(str(user))
    try:
        common = os.path.normcase(os.path.commonpath((app_key, user_key)))
    except ValueError as exc:
        raise InstallPathError("application and user-data roots are incompatible") from exc
    if common in {app_key, user_key}:
        raise InstallPathError("user data must be outside the application root")
    return app, user


def canonical_relative_path(value: str) -> str:
    """Return one canonical Windows-safe POSIX relative path."""

    if not isinstance(value, str) or not value or len(value) > 512:
        raise InstallPathError("owned path is empty or oversized")
    if unicodedata.normalize("NFC", value) != value:
        raise InstallPathError("owned path is not NFC-normalized")
    if "\\" in value or ":" in value or value.startswith(("/", "//")):
        raise InstallPathError("owned path is absolute, redirected, or ADS-like")
    if PureWindowsPath(value).drive or PureWindowsPath(value).root:
        raise InstallPathError("owned path uses drive or rooted syntax")
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or path.as_posix() != value:
        raise InstallPathError("owned path is not canonical relative syntax")
    for component in path.parts:
        if component in {"", ".", ".."}:
            raise InstallPathError("owned path contains traversal")
        if component != component.strip() or component.endswith((".", " ")):
            raise InstallPathError("owned path contains an ambiguous component")
        if any(ord(char) < 32 or ord(char) == 127 for char in component):
            raise InstallPathError("owned path contains a control character")
        if component.split(".", 1)[0].casefold() in _RESERVED:
            raise InstallPathError("owned path uses a reserved device name")
    if len(value.encode("utf-8")) > 1024:
        raise InstallPathError("owned path encoding is oversized")
    return value


def _validate_leaf_name(value: str, *, label: str) -> str:
    normalized = canonical_relative_path(value)
    if "/" in normalized:
        raise InstallPathError(f"{label} must be one path component")
    return normalized


def _safe_join(root: Path, relative: str) -> Path:
    canonical = canonical_relative_path(relative)
    target = root.joinpath(*PurePosixPath(canonical).parts)
    try:
        target.absolute().relative_to(root.absolute())
    except ValueError as exc:
        raise InstallPathError("owned path escaped its root") from exc
    current = target if _path_exists(target) else target.parent
    while current != root.parent:
        if _path_exists(current) and _is_reparse(current):
            raise InstallPathError("owned path contains a reparse point")
        if current == root:
            break
        current = current.parent
    return target


def _stable_file_fact(path: Path) -> tuple[int, str]:
    try:
        before = path.stat()
        if _is_reparse(path) or not stat.S_ISREG(before.st_mode):
            raise InstallPathError("owned payload is not a regular non-link file")
        if before.st_size > MAX_FILE_BYTES:
            raise InstallIntegrityError("owned payload exceeds its size limit")
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            opened = os.fstat(handle.fileno())
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
        after = path.stat()
    except InstallContractError:
        raise
    except OSError as exc:
        raise InstallIntegrityError("owned payload is unreadable") from exc
    identities = {
        (item.st_dev, item.st_ino, item.st_size, item.st_mtime_ns)
        for item in (before, opened, after)
    }
    if len(identities) != 1:
        raise InstallIntegrityError("owned payload changed during inspection")
    return before.st_size, digest.hexdigest()


def _reject_payload_class(relative: str, path: Path) -> None:
    parts = PurePosixPath(relative).parts
    if any(part.casefold() in _FORBIDDEN_COMPONENTS for part in parts[:-1]):
        raise InstallIntegrityError("payload belongs to a forbidden runtime category")
    if Path(parts[-1]).suffix.casefold() in _FORBIDDEN_SUFFIXES:
        raise InstallIntegrityError("payload extension is not source-only")
    try:
        with path.open("rb") as handle:
            prefix = handle.read(4)
    except OSError as exc:
        raise InstallIntegrityError("payload header is unreadable") from exc
    if prefix.startswith(b"MZ") or prefix == b"\x7fELF" or prefix in {
        b"\xcf\xfa\xed\xfe", b"\xfe\xed\xfa\xcf"
    }:
        raise InstallIntegrityError("native executable payload is forbidden")


def _validate_candidate_id(value: object) -> str:
    if not isinstance(value, str) or not _CANDIDATE.fullmatch(value):
        raise InstallIntegrityError("candidate identifier is invalid")
    return value


def _validate_git_object(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not _GIT_OBJECT.fullmatch(value):
        raise InstallIntegrityError(f"{label} must be one exact Git object")
    return value


def _manifest_entries(
    source_root: Path, included: Mapping[str, str]
) -> tuple[list[dict[str, object]], int]:
    if not isinstance(included, Mapping) or not included or len(included) > MAX_FILES:
        raise InstallIntegrityError("included path mapping is empty or oversized")
    entries: list[dict[str, object]] = []
    identities: set[str] = set()
    total = 0
    for raw_path, role in sorted(included.items()):
        relative = canonical_relative_path(raw_path)
        identity = unicodedata.normalize("NFC", relative).casefold()
        if identity in identities or relative == OWNED_FILES_MANIFEST:
            raise InstallIntegrityError("owned paths collide or reserve the manifest name")
        identities.add(identity)
        if role not in _ROLES:
            raise InstallIntegrityError("owned payload role is unsupported")
        path = _safe_join(source_root, relative)
        _reject_payload_class(relative, path)
        size, sha256 = _stable_file_fact(path)
        total += size
        if total > MAX_TOTAL_BYTES:
            raise InstallIntegrityError("owned payload total exceeds its size limit")
        entries.append({"path": relative, "role": role, "sha256": sha256, "size": size})
    return entries, total


def build_manifest(
    source_root: Path,
    included: Mapping[str, str],
    *,
    candidate_id: str,
    source_commit: str,
    source_tree: str,
    app_root_name: str,
) -> dict[str, Any]:
    """Create a deterministic source-only owned-files manifest."""

    root = _check_existing_chain(source_root, require_leaf=True)
    entries, total = _manifest_entries(root, included)
    manifest: dict[str, Any] = {
        "schema_version": INSTALL_LAYOUT_SCHEMA,
        "candidate_id": _validate_candidate_id(candidate_id),
        "verification_scope": DEMO_VERIFICATION_SCOPE,
        "source": {
            "commit": _validate_git_object(source_commit, label="source commit"),
            "tree": _validate_git_object(source_tree, label="source tree"),
        },
        "layout": {
            "app_root_name": _validate_leaf_name(app_root_name, label="application root name"),
            "manifest_path": OWNED_FILES_MANIFEST,
            "single_app_root": True,
            "user_data_external": True,
            "implicit_user_data_delete": False,
        },
        "files": entries,
        "file_count": len(entries),
        "total_bytes": total,
        "files_sha256": hashlib.sha256(_canonical_json(entries)).hexdigest(),
        "external_payloads": [],
    }
    validate_manifest(manifest)
    return manifest


def validate_manifest(value: object) -> dict[str, Any]:
    """Validate and return a manifest without trusting unknown fields."""

    if not isinstance(value, dict) or set(value) != {
        "candidate_id", "external_payloads", "file_count", "files",
        "files_sha256", "layout", "schema_version", "source", "total_bytes",
        "verification_scope",
    }:
        raise InstallIntegrityError("install manifest shape is not exact")
    if value.get("schema_version") != INSTALL_LAYOUT_SCHEMA:
        raise InstallIntegrityError("install manifest schema is unsupported")
    _validate_candidate_id(value.get("candidate_id"))
    if value.get("verification_scope") != DEMO_VERIFICATION_SCOPE:
        raise InstallIntegrityError("install manifest is outside demo verification scope")
    source = value.get("source")
    if not isinstance(source, dict) or set(source) != {"commit", "tree"}:
        raise InstallIntegrityError("install manifest source provenance is invalid")
    _validate_git_object(source.get("commit"), label="source commit")
    _validate_git_object(source.get("tree"), label="source tree")
    layout = value.get("layout")
    if not isinstance(layout, dict) or set(layout) != {
        "app_root_name", "implicit_user_data_delete", "manifest_path",
        "single_app_root", "user_data_external",
    }:
        raise InstallIntegrityError("install layout contract is invalid")
    _validate_leaf_name(layout.get("app_root_name"), label="application root name")
    if layout.get("manifest_path") != OWNED_FILES_MANIFEST or any(
        layout.get(key) is not expected
        for key, expected in (
            ("single_app_root", True),
            ("user_data_external", True),
            ("implicit_user_data_delete", False),
        )
    ):
        raise InstallIntegrityError("install layout weakens the ownership boundary")
    entries = value.get("files")
    if not isinstance(entries, list) or not entries or len(entries) > MAX_FILES:
        raise InstallIntegrityError("owned file list is empty or oversized")
    identities: set[str] = set()
    total = 0
    previous = ""
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {"path", "role", "sha256", "size"}:
            raise InstallIntegrityError("owned file fact shape is invalid")
        relative = canonical_relative_path(entry.get("path"))
        identity = relative.casefold()
        if identity in identities or (previous and relative <= previous):
            raise InstallIntegrityError("owned file paths collide or are not sorted")
        identities.add(identity)
        previous = relative
        if relative == OWNED_FILES_MANIFEST or entry.get("role") not in _ROLES:
            raise InstallIntegrityError("owned file fact is unsupported")
        size = entry.get("size")
        if isinstance(size, bool) or not isinstance(size, int) or not 0 <= size <= MAX_FILE_BYTES:
            raise InstallIntegrityError("owned file size is invalid")
        total += size
        if not isinstance(entry.get("sha256"), str) or not _SHA256.fullmatch(entry["sha256"]):
            raise InstallIntegrityError("owned file hash is invalid")
    if total > MAX_TOTAL_BYTES:
        raise InstallIntegrityError("owned payload total exceeds its size limit")
    if value.get("file_count") != len(entries) or value.get("total_bytes") != total:
        raise InstallIntegrityError("owned file aggregates do not match")
    expected_digest = hashlib.sha256(_canonical_json(entries)).hexdigest()
    if value.get("files_sha256") != expected_digest:
        raise InstallIntegrityError("owned file aggregate hash does not match")
    if value.get("external_payloads") != []:
        raise InstallIntegrityError("external payloads are forbidden in the demo layout")
    return value


def load_manifest(layout_root: Path) -> dict[str, Any]:
    root = _check_existing_chain(layout_root, require_leaf=True)
    path = root / OWNED_FILES_MANIFEST
    try:
        if _is_reparse(path) or not path.is_file() or path.stat().st_size > MAX_MANIFEST_BYTES:
            raise InstallIntegrityError("owned-files manifest is unavailable")
        raw = path.read_bytes()
        value = json.loads(raw.decode("utf-8", "strict"))
    except InstallContractError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise InstallIntegrityError("owned-files manifest is unreadable") from exc
    if raw != _canonical_json(value):
        raise InstallIntegrityError("owned-files manifest is not canonical JSON")
    return validate_manifest(value)


def _walk_files(root: Path) -> set[str]:
    found: set[str] = set()
    for current, dirs, files in os.walk(root, topdown=True, followlinks=False):
        current_path = Path(current)
        kept: list[str] = []
        for name in sorted(dirs):
            child = current_path / name
            if _is_reparse(child):
                raise InstallPathError("layout contains a reparse directory")
            kept.append(name)
        dirs[:] = kept
        for name in sorted(files):
            path = current_path / name
            if _is_reparse(path) or not path.is_file():
                raise InstallPathError("layout contains a non-regular file")
            found.add(path.relative_to(root).as_posix())
    return found


def verify_layout(layout_root: Path, *, exact: bool = True) -> dict[str, Any]:
    """Verify every owned byte and, by default, the exact layout surface."""

    root = _check_existing_chain(layout_root, require_leaf=True)
    manifest = load_manifest(root)
    expected = {OWNED_FILES_MANIFEST}
    for entry in manifest["files"]:
        relative = entry["path"]
        path = _safe_join(root, relative)
        _reject_payload_class(relative, path)
        size, digest = _stable_file_fact(path)
        if size != entry["size"] or digest != entry["sha256"]:
            raise InstallIntegrityError("installed owned file does not match its manifest")
        expected.add(relative)
    if exact and _walk_files(root) != expected:
        raise InstallIntegrityError("layout contains missing or unowned files")
    return manifest


def _fresh_sibling(root: Path, label: str) -> Path:
    for _ in range(32):
        candidate = root.parent / f".{root.name}.{label}-{uuid.uuid4().hex}"
        if not _path_exists(candidate):
            return candidate
    raise InstallStateError("unable to reserve a fresh lifecycle path")


def _flush_directory(directory: Path) -> None:
    try:
        descriptor = os.open(
            directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        )
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError:
        pass


def _journal_path(app: Path) -> Path:
    return app.parent / f".{app.name}.install-journal.json"


def _empty_lifecycle(action: str, app: Path) -> dict[str, Any]:
    return {
        "schema_version": INSTALL_JOURNAL_SCHEMA,
        "action": action,
        "app_name": app.name,
        "phase": "prepared",
        "active_path": None,
        "stage_name": None,
        "retained_name": None,
        "displaced_name": None,
        "recovery_name": None,
        "moved": [],
        "manifest_moved": False,
    }


def _atomic_lifecycle_json(
    path: Path, payload: dict[str, Any], parent_guard: DirectoryGuard
) -> None:
    temporary = path.with_name(path.name + ".tmp-" + uuid.uuid4().hex)
    try:
        with open_exclusive_binary(parent_guard, temporary) as handle:
            handle.write(_canonical_json(payload))
            handle.flush()
            os.fsync(handle.fileno())
        parent_guard.assert_unchanged()
        os.replace(temporary, path)
        _flush_directory(path.parent)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _write_lifecycle(
    app: Path, payload: dict[str, Any], parent_guard: DirectoryGuard
) -> None:
    _atomic_lifecycle_json(_journal_path(app), payload, parent_guard)


def _create_lifecycle(
    app: Path, payload: dict[str, Any], parent_guard: DirectoryGuard
) -> None:
    journal = _journal_path(app)
    try:
        with open_exclusive_binary(parent_guard, journal) as handle:
            handle.write(_canonical_json(payload))
            handle.flush()
            os.fsync(handle.fileno())
        _flush_directory(journal.parent)
    except FilesystemBoundaryError as exc:
        raise InstallStateError("another lifecycle operation claimed the journal") from exc


def _validate_sibling_name(value: object, app: Path, kind: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.startswith(f".{app.name}.{kind}-"):
        raise InstallStateError("lifecycle journal sibling name is invalid")
    if "/" in value or "\\" in value or ":" in value or value in {".", ".."}:
        raise InstallStateError("lifecycle journal sibling name is unsafe")
    return value


def _load_lifecycle(app: Path) -> dict[str, Any]:
    journal = _journal_path(app)
    try:
        raw = journal.read_bytes()
        payload = json.loads(raw.decode("utf-8", "strict"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise InstallStateError("install lifecycle journal is unreadable") from exc
    expected = {
        "action", "active_path", "app_name", "displaced_name", "manifest_moved",
        "moved", "phase", "recovery_name", "retained_name", "schema_version",
        "stage_name",
    }
    if (
        raw != _canonical_json(payload)
        or not isinstance(payload, dict)
        or set(payload) != expected
        or payload.get("schema_version") != INSTALL_JOURNAL_SCHEMA
        or payload.get("app_name") != app.name
        or payload.get("action") not in {"install", "upgrade", "rollback", "uninstall"}
        or not isinstance(payload.get("manifest_moved"), bool)
    ):
        raise InstallStateError("install lifecycle journal schema is invalid")
    moved = payload.get("moved")
    if not isinstance(moved, list) or len(moved) != len(set(moved)):
        raise InstallStateError("install lifecycle journal move set is invalid")
    for relative in moved:
        canonical_relative_path(relative)
    active = payload.get("active_path")
    if active is not None:
        canonical_relative_path(active)
    action = payload["action"]
    phases = {
        "install": {"prepared", "promoting", "completed"},
        "upgrade": {
            "prepared", "relocating_current", "current_relocated",
            "promoting_new", "new_promoted", "verifying", "completed",
        },
        "rollback": {
            "prepared", "relocating_current", "current_relocated",
            "promoting_prior", "prior_promoted", "completed",
        },
        "uninstall": {
            "prepared", "moving_owned", "owned_moved", "moving_manifest",
            "manifest_moved", "completed",
        },
    }
    if payload.get("phase") not in phases[action]:
        raise InstallStateError("install lifecycle journal phase is invalid")
    _validate_sibling_name(payload.get("stage_name"), app, "install-stage" if action == "install" else "upgrade-stage")
    if action == "upgrade":
        _validate_sibling_name(payload.get("retained_name"), app, "rollback")
    if action == "rollback":
        # A rollback source is a previously returned sibling and is therefore
        # constrained to this application's rollback naming contract.
        _validate_sibling_name(payload.get("retained_name"), app, "rollback")
        _validate_sibling_name(payload.get("displaced_name"), app, "rolled-back")
    if action == "uninstall":
        _validate_sibling_name(payload.get("recovery_name"), app, "uninstall-recovery")
    required_names = {
        "install": ("stage_name",),
        "upgrade": ("stage_name", "retained_name"),
        "rollback": ("retained_name", "displaced_name"),
        "uninstall": ("recovery_name",),
    }
    if any(not isinstance(payload.get(key), str) for key in required_names[action]):
        raise InstallStateError("install lifecycle journal roots are incomplete")
    allowed_names = set(required_names[action])
    if any(
        payload.get(key) is not None
        for key in {"stage_name", "retained_name", "displaced_name", "recovery_name"}
        - allowed_names
    ):
        raise InstallStateError("install lifecycle journal has unrelated roots")
    return payload


def _move_failed_tree(app: Path, parent_guard: DirectoryGuard, label: str) -> Path:
    destination = _fresh_sibling(app, label)
    assert_safe_descendant(parent_guard, destination, allow_missing_leaf=True)
    os.rename(app, destination)
    _flush_directory(app.parent)
    return destination


def reconcile_install_lifecycle(app_root: Path, user_data_root: Path) -> bool:
    """Reconcile one durable install/update/uninstall journal before startup."""

    app, _user = _separate_roots(app_root, user_data_root)
    journal = _journal_path(app)
    if not _path_exists(journal):
        return False
    with DirectoryGuard(app.parent) as parent_guard:
        assert_safe_descendant(parent_guard, journal)
        lifecycle = _load_lifecycle(app)
        action = lifecycle["action"]
        phase = lifecycle["phase"]
        stage = (
            app.parent / lifecycle["stage_name"]
            if isinstance(lifecycle.get("stage_name"), str)
            else None
        )
        retained = (
            app.parent / lifecycle["retained_name"]
            if isinstance(lifecycle.get("retained_name"), str)
            else None
        )
        displaced = (
            app.parent / lifecycle["displaced_name"]
            if isinstance(lifecycle.get("displaced_name"), str)
            else None
        )
        recovery = (
            app.parent / lifecycle["recovery_name"]
            if isinstance(lifecycle.get("recovery_name"), str)
            else None
        )

        if action == "install":
            if _path_exists(app):
                verify_layout(app)
                if stage is not None and _path_exists(stage):
                    raise InstallStateError("interrupted install has two candidate roots")
            elif stage is None or not _path_exists(stage):
                raise InstallStateError("interrupted install lost both roots")
            # If only staging exists, the install never became live.  Retain it
            # as evidence and clear the intent so a later explicit retry is safe.
        elif action == "upgrade":
            if retained is None:
                raise InstallStateError("upgrade journal has no retained root")
            # Until the completed phase is durably written, the retained prior
            # tree remains authoritative.  This makes faults after promotion
            # roll back rather than silently turning into a successful update.
            promoted = phase == "completed"
            if promoted:
                verify_layout(app)
                verify_layout(retained)
            elif _path_exists(retained):
                verify_layout(retained)
                if _path_exists(app):
                    verify_layout(app)
                    _move_failed_tree(app, parent_guard, "failed-upgrade")
                os.rename(retained, app)
                _flush_directory(app.parent)
                verify_layout(app)
            elif _path_exists(app):
                # Relocation did not occur; the old application is authoritative.
                verify_layout(app)
            else:
                raise InstallStateError("interrupted upgrade has no authoritative root")
        elif action == "rollback":
            if retained is None or displaced is None:
                raise InstallStateError("rollback journal roots are incomplete")
            promoted = phase == "completed"
            if promoted:
                verify_layout(app)
                verify_layout(displaced)
            elif _path_exists(displaced):
                verify_layout(displaced)
                if _path_exists(app):
                    verify_layout(app)
                    if _path_exists(retained):
                        raise InstallStateError("rollback source was claimed")
                    os.rename(app, retained)
                os.rename(displaced, app)
                _flush_directory(app.parent)
                verify_layout(app)
            elif _path_exists(app):
                verify_layout(app)
            else:
                raise InstallStateError("interrupted rollback has no authoritative root")
        else:
            if recovery is None or not _path_exists(recovery):
                raise InstallStateError("uninstall recovery root is unavailable")
            moved = list(lifecycle["moved"])
            active = lifecycle["active_path"]
            if phase == "moving_owned" and isinstance(active, str):
                source = _safe_join(app, active)
                target = _safe_join(recovery, active)
                if not _path_exists(source) and _path_exists(target) and active not in moved:
                    moved.append(active)
            manifest_moved = bool(lifecycle["manifest_moved"])
            if phase == "moving_manifest":
                source_manifest = app / OWNED_FILES_MANIFEST
                target_manifest = recovery / OWNED_FILES_MANIFEST
                if not _path_exists(source_manifest) and _path_exists(target_manifest):
                    manifest_moved = True
            recovery_manifest = load_manifest(recovery) if manifest_moved else None
            if phase == "completed":
                verify_layout(recovery)
            else:
                if not _path_exists(app):
                    app.mkdir()
                with DirectoryGuard(app) as app_guard, DirectoryGuard(recovery) as recovery_guard:
                    for relative in reversed(moved):
                        source = _safe_join(app, relative)
                        target = _safe_join(recovery, relative)
                        assert_safe_descendant(recovery_guard, target)
                        make_directories(app_guard, source.parent)
                        if _path_exists(source):
                            raise InstallStateError("uninstall rollback destination is claimed")
                        os.rename(target, source)
                    if manifest_moved:
                        source_manifest = app / OWNED_FILES_MANIFEST
                        target_manifest = recovery / OWNED_FILES_MANIFEST
                        assert_safe_descendant(recovery_guard, target_manifest)
                        if _path_exists(source_manifest):
                            raise InstallStateError("uninstall manifest rollback is ambiguous")
                        os.rename(target_manifest, source_manifest)
                if recovery_manifest is not None:
                    verify_layout(app, exact=False)
                else:
                    verify_layout(app, exact=False)
        journal.unlink()
        _flush_directory(journal.parent)
        return True


def _copy_verified_layout(source: Path, destination: Path, manifest: dict[str, Any]) -> None:
    _check_existing_chain(destination, require_leaf=False)
    destination.mkdir()
    try:
        with DirectoryGuard(source) as source_guard, DirectoryGuard(destination) as destination_guard:
            for entry in manifest["files"]:
                relative = entry["path"]
                origin = _safe_join(source, relative)
                assert_safe_descendant(source_guard, origin)
                target = _safe_join(destination, relative)
                make_directories(destination_guard, target.parent)
                data = origin.read_bytes()
                if len(data) != entry["size"] or hashlib.sha256(data).hexdigest() != entry["sha256"]:
                    raise InstallIntegrityError("source layout changed during copy")
                with open_exclusive_binary(destination_guard, target) as handle:
                    handle.write(data)
                    handle.flush()
                    os.fsync(handle.fileno())
            manifest_path = destination / OWNED_FILES_MANIFEST
            with open_exclusive_binary(destination_guard, manifest_path) as handle:
                handle.write(_canonical_json(manifest))
                handle.flush()
                os.fsync(handle.fileno())
    except (InstallContractError, FilesystemBoundaryError, OSError) as exc:
        if isinstance(exc, InstallContractError):
            raise
        if isinstance(exc, FilesystemBoundaryError):
            raise InstallPathError("layout staging boundary changed") from exc
        raise InstallStateError("layout staging failed") from exc
    verify_layout(destination)


def create_test_layout(
    source_root: Path,
    destination_root: Path,
    included: Mapping[str, str],
    *,
    candidate_id: str,
    source_commit: str,
    source_tree: str,
    app_root_name: str,
    fault_hook: FaultHook | None = None,
) -> dict[str, Any]:
    """Build a fresh source-only test layout; this is not an installer."""

    source = _check_existing_chain(source_root, require_leaf=True)
    destination = _check_existing_chain(destination_root, require_leaf=False)
    manifest = build_manifest(
        source,
        included,
        candidate_id=candidate_id,
        source_commit=source_commit,
        source_tree=source_tree,
        app_root_name=app_root_name,
    )
    stage = _fresh_sibling(destination, "layout-stage")
    try:
        with DirectoryGuard(destination.parent) as parent_guard:
            _copy_verified_layout(source, stage, manifest)
            if fault_hook:
                fault_hook("layout.before_promote")
            if _path_exists(destination):
                raise InstallStateError("test layout destination was claimed")
            assert_safe_descendant(parent_guard, stage)
            os.rename(stage, destination)
            _flush_directory(destination.parent)
            assert_safe_descendant(parent_guard, destination)
    except FilesystemBoundaryError as exc:
        raise InstallPathError("test layout filesystem boundary changed") from exc
    except OSError as exc:
        raise InstallStateError("test layout promotion failed; staging was retained") from exc
    return verify_layout(destination)


def install_layout(
    layout_root: Path,
    app_root: Path,
    user_data_root: Path,
    *,
    fault_hook: FaultHook | None = None,
) -> InstallResult:
    """Install a verified layout into a previously absent application root."""

    app, _user = _separate_roots(app_root, user_data_root)
    reconcile_install_lifecycle(app, user_data_root)
    layout = _check_existing_chain(layout_root, require_leaf=True)
    manifest = verify_layout(layout)
    _check_existing_chain(app, require_leaf=False)
    if app.name != manifest["layout"]["app_root_name"]:
        raise InstallPathError("application root name does not match the manifest")
    stage = _fresh_sibling(app, "install-stage")
    try:
        with DirectoryGuard(app.parent) as parent_guard:
            _copy_verified_layout(layout, stage, manifest)
            lifecycle = _empty_lifecycle("install", app)
            lifecycle["stage_name"] = stage.name
            _create_lifecycle(app, lifecycle, parent_guard)
            lifecycle["phase"] = "promoting"
            _write_lifecycle(app, lifecycle, parent_guard)
            if fault_hook:
                fault_hook("install.before_promote")
            if _path_exists(app):
                raise InstallStateError("application root was claimed")
            assert_safe_descendant(parent_guard, stage)
            os.rename(stage, app)
            _flush_directory(app.parent)
            assert_safe_descendant(parent_guard, app)
            verify_layout(app)
            lifecycle["phase"] = "completed"
            _write_lifecycle(app, lifecycle, parent_guard)
            _journal_path(app).unlink()
            _flush_directory(app.parent)
    except FilesystemBoundaryError as exc:
        raise InstallPathError("install filesystem boundary changed") from exc
    except OSError as exc:
        raise InstallStateError("install promotion failed; staging was retained") from exc
    return InstallResult("install", manifest["candidate_id"], app, None, manifest["file_count"])


def upgrade_layout(
    layout_root: Path,
    app_root: Path,
    user_data_root: Path,
    *,
    fault_hook: FaultHook | None = None,
) -> InstallResult:
    """Upgrade atomically while retaining the complete prior tree for rollback."""

    app, _user = _separate_roots(app_root, user_data_root)
    reconcile_install_lifecycle(app, user_data_root)
    layout = _check_existing_chain(layout_root, require_leaf=True)
    new_manifest = verify_layout(layout)
    old_manifest = verify_layout(app)
    if app.name != new_manifest["layout"]["app_root_name"]:
        raise InstallPathError("application root name does not match the new manifest")
    if new_manifest["candidate_id"] == old_manifest["candidate_id"]:
        raise InstallStateError("upgrade candidate is already installed")
    stage = _fresh_sibling(app, "upgrade-stage")
    rollback = _fresh_sibling(app, "rollback")
    try:
        with DirectoryGuard(app.parent) as parent_guard:
            _copy_verified_layout(layout, stage, new_manifest)
            lifecycle = _empty_lifecycle("upgrade", app)
            lifecycle["stage_name"] = stage.name
            lifecycle["retained_name"] = rollback.name
            _create_lifecycle(app, lifecycle, parent_guard)
            if fault_hook:
                fault_hook("upgrade.after_stage")
            before = app.stat(follow_symlinks=False)
            lifecycle["phase"] = "relocating_current"
            _write_lifecycle(app, lifecycle, parent_guard)
            os.rename(app, rollback)
            _flush_directory(app.parent)
            relocated = rollback.stat(follow_symlinks=False)
            if (before.st_dev, before.st_ino) != (relocated.st_dev, relocated.st_ino):
                raise InstallPathError("relocated rollback identity changed")
            if verify_layout(rollback) != old_manifest:
                raise InstallIntegrityError("relocated rollback manifest changed")
            lifecycle["phase"] = "current_relocated"
            _write_lifecycle(app, lifecycle, parent_guard)
            if fault_hook:
                fault_hook("upgrade.after_current_relocated")
            if _path_exists(app):
                raise InstallStateError("application root was claimed during upgrade")
            assert_safe_descendant(parent_guard, stage)
            lifecycle["phase"] = "promoting_new"
            _write_lifecycle(app, lifecycle, parent_guard)
            os.rename(stage, app)
            _flush_directory(app.parent)
            assert_safe_descendant(parent_guard, app)
            lifecycle["phase"] = "new_promoted"
            _write_lifecycle(app, lifecycle, parent_guard)
            if fault_hook:
                fault_hook("upgrade.after_new_promoted")
            lifecycle["phase"] = "verifying"
            _write_lifecycle(app, lifecycle, parent_guard)
            verify_layout(app)
            verify_layout(rollback)
            lifecycle["phase"] = "completed"
            _write_lifecycle(app, lifecycle, parent_guard)
            _journal_path(app).unlink()
            _flush_directory(app.parent)
    except BaseException as exc:
        try:
            if _path_exists(_journal_path(app)):
                reconcile_install_lifecycle(app, user_data_root)
        except BaseException as rollback_exc:
            raise InstallStateError(
                "upgrade failed and the durable journal needs startup reconciliation"
            ) from rollback_exc
        if isinstance(exc, InstallContractError):
            raise
        raise InstallStateError("upgrade failed; the prior layout was restored") from exc
    return InstallResult(
        "upgrade", new_manifest["candidate_id"], app, rollback, new_manifest["file_count"]
    )


def rollback_upgrade(
    app_root: Path,
    rollback_root: Path,
    user_data_root: Path,
    *,
    fault_hook: FaultHook | None = None,
) -> InstallResult:
    """Restore a retained prior layout while preserving the displaced layout."""

    app, _user = _separate_roots(app_root, user_data_root)
    reconcile_install_lifecycle(app, user_data_root)
    rollback = _check_existing_chain(rollback_root, require_leaf=True)
    if rollback.parent != app.parent or rollback == app:
        raise InstallPathError("rollback root must be a distinct sibling of the application root")
    current = verify_layout(app)
    prior = verify_layout(rollback)
    displaced = _fresh_sibling(app, "rolled-back")
    try:
        with DirectoryGuard(app.parent) as parent_guard:
            lifecycle = _empty_lifecycle("rollback", app)
            lifecycle["retained_name"] = rollback.name
            lifecycle["displaced_name"] = displaced.name
            _create_lifecycle(app, lifecycle, parent_guard)
            current_before = app.stat(follow_symlinks=False)
            lifecycle["phase"] = "relocating_current"
            _write_lifecycle(app, lifecycle, parent_guard)
            os.rename(app, displaced)
            _flush_directory(app.parent)
            current_after = displaced.stat(follow_symlinks=False)
            if (current_before.st_dev, current_before.st_ino) != (
                current_after.st_dev,
                current_after.st_ino,
            ):
                raise InstallPathError("displaced layout identity changed")
            if verify_layout(displaced) != current:
                raise InstallIntegrityError("displaced layout manifest changed")
            lifecycle["phase"] = "current_relocated"
            _write_lifecycle(app, lifecycle, parent_guard)
            if fault_hook:
                fault_hook("rollback.after_current_relocated")
            lifecycle["phase"] = "promoting_prior"
            _write_lifecycle(app, lifecycle, parent_guard)
            os.rename(rollback, app)
            _flush_directory(app.parent)
            assert_safe_descendant(parent_guard, app)
            lifecycle["phase"] = "prior_promoted"
            _write_lifecycle(app, lifecycle, parent_guard)
            verify_layout(app)
            verify_layout(displaced)
            lifecycle["phase"] = "completed"
            _write_lifecycle(app, lifecycle, parent_guard)
            _journal_path(app).unlink()
            _flush_directory(app.parent)
    except BaseException as exc:
        try:
            if _path_exists(_journal_path(app)):
                reconcile_install_lifecycle(app, user_data_root)
        except BaseException as restore_exc:
            raise InstallStateError("rollback failed and the current layout needs reconciliation") from restore_exc
        if isinstance(exc, InstallContractError):
            raise
        raise InstallStateError("rollback failed; the current layout was restored") from exc
    return InstallResult("rollback", prior["candidate_id"], app, displaced, prior["file_count"])


def _remove_empty_owned_directories(app: Path, entries: Iterable[dict[str, Any]]) -> None:
    directories = {
        _safe_join(app, str(PurePosixPath(entry["path"]).parent))
        for entry in entries
        if str(PurePosixPath(entry["path"]).parent) != "."
    }
    for directory in sorted(directories, key=lambda item: len(item.parts), reverse=True):
        try:
            directory.rmdir()
        except (FileNotFoundError, OSError):
            pass


def uninstall_owned(
    app_root: Path,
    user_data_root: Path,
    *,
    fault_hook: FaultHook | None = None,
) -> InstallResult:
    """Relocate only manifest-owned bytes; user data is never an implicit target."""

    app, _user = _separate_roots(app_root, user_data_root)
    reconcile_install_lifecycle(app, user_data_root)
    manifest = load_manifest(app)
    # Preflight every owned file before moving any byte.  Unknown files are
    # deliberately not inspected as owned and remain in place.
    for entry in manifest["files"]:
        path = _safe_join(app, entry["path"])
        size, digest = _stable_file_fact(path)
        if size != entry["size"] or digest != entry["sha256"]:
            raise InstallIntegrityError("modified owned file blocks uninstall")
    recovery = _fresh_sibling(app, "uninstall-recovery")
    try:
        with DirectoryGuard(app.parent) as parent_guard:
            recovery.mkdir()
            assert_safe_descendant(parent_guard, recovery)
            lifecycle = _empty_lifecycle("uninstall", app)
            lifecycle["recovery_name"] = recovery.name
            _create_lifecycle(app, lifecycle, parent_guard)
            with DirectoryGuard(app) as app_guard, DirectoryGuard(recovery) as recovery_guard:
                for entry in manifest["files"]:
                    relative = entry["path"]
                    source = _safe_join(app, relative)
                    target = _safe_join(recovery, relative)
                    assert_safe_descendant(app_guard, source)
                    make_directories(recovery_guard, target.parent)
                    lifecycle.update(active_path=relative, phase="moving_owned")
                    _write_lifecycle(app, lifecycle, parent_guard)
                    os.rename(source, target)
                    _flush_directory(source.parent)
                    _flush_directory(target.parent)
                    assert_safe_descendant(recovery_guard, target)
                    moved_paths = lifecycle["moved"]
                    assert isinstance(moved_paths, list)
                    moved_paths.append(relative)
                    lifecycle["phase"] = "owned_moved"
                    _write_lifecycle(app, lifecycle, parent_guard)
                    if fault_hook:
                        fault_hook("uninstall.after_owned_relocated")
                source_manifest = app / OWNED_FILES_MANIFEST
                target_manifest = recovery / OWNED_FILES_MANIFEST
                assert_safe_descendant(app_guard, source_manifest)
                lifecycle.update(active_path=OWNED_FILES_MANIFEST, phase="moving_manifest")
                _write_lifecycle(app, lifecycle, parent_guard)
                os.rename(source_manifest, target_manifest)
                _flush_directory(app)
                _flush_directory(recovery)
                lifecycle.update(
                    active_path=None, manifest_moved=True, phase="manifest_moved"
                )
                _write_lifecycle(app, lifecycle, parent_guard)
            _remove_empty_owned_directories(app, manifest["files"])
            try:
                app.rmdir()
            except OSError:
                pass
            verify_layout(recovery)
            lifecycle["phase"] = "completed"
            _write_lifecycle(app, lifecycle, parent_guard)
            _journal_path(app).unlink()
            _flush_directory(app.parent)
    except BaseException as exc:
        try:
            if _path_exists(_journal_path(app)):
                reconcile_install_lifecycle(app, user_data_root)
        except BaseException as rollback_exc:
            raise InstallStateError("uninstall failed and owned files need reconciliation") from rollback_exc
        if isinstance(exc, InstallContractError):
            raise
        raise InstallStateError("uninstall failed; owned files were restored") from exc
    return InstallResult(
        "uninstall", manifest["candidate_id"], app, recovery, manifest["file_count"]
    )


__all__ = [
    "DEMO_VERIFICATION_SCOPE", "INSTALL_JOURNAL_SCHEMA", "INSTALL_LAYOUT_SCHEMA", "OWNED_FILES_MANIFEST",
    "InstallContractError", "InstallIntegrityError", "InstallPathError", "InstallResult",
    "InstallStateError", "build_manifest", "canonical_relative_path", "create_test_layout",
    "install_layout", "load_manifest", "reconcile_install_lifecycle", "rollback_upgrade", "uninstall_owned",
    "upgrade_layout", "validate_manifest", "verify_layout",
]
