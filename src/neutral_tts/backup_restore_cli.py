"""Explicit standalone host for the bounded backup/restore contract.

This adapter is deliberately separate from generation startup.  It selects no
application data on its own: the operator supplies one dedicated data root,
and :mod:`neutral_tts.backup_restore` admits only its four supported JSON data
categories.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .backup_restore import (
    BackupManifest,
    create_backup,
    reconcile_restore,
    restore_backup,
    validate_backup,
)
from .fs_safety import DirectoryGuard


HOST_SCHEMA = "neutral-tts-backup-host-v1"
RESTORE_WORKSPACE_RESERVE_BYTES = 1024 * 1024


class BackupRestoreHostError(RuntimeError):
    """The explicit host rejected an unsafe or incomplete operation."""


def _absolute(value: str | Path) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    return path.absolute()


def _exists(path: Path) -> bool:
    return os.path.lexists(path)


def _available_bytes(directory: Path) -> int:
    return shutil.disk_usage(directory).free


def _manifest_payload(manifest: BackupManifest) -> dict[str, object]:
    return {
        "aggregate_sha256": manifest.aggregate_sha256,
        "categories": list(manifest.categories),
        "entry_count": len(manifest.entries),
        "expanded_bytes": sum(entry.size for entry in manifest.entries),
        "schema_version": manifest.schema_version,
    }


@dataclass(frozen=True, slots=True)
class BackupRestoreHost:
    """Bind one explicit command to existing guarded lifecycle primitives."""

    capacity_probe: Callable[[Path], int] = _available_bytes

    def backup(self, data_root: Path, archive_path: Path) -> dict[str, object]:
        root = _absolute(data_root)
        archive = _absolute(archive_path)
        if (
            root.parent == root
            or not root.is_dir()
            or not archive.parent.is_dir()
            or _exists(archive)
        ):
            raise BackupRestoreHostError("backup endpoints are unavailable")
        with DirectoryGuard(root) as root_guard, DirectoryGuard(
            archive.parent
        ) as archive_parent_guard:
            if root_guard.contains_final(archive.parent.resolve(strict=True)):
                raise BackupRestoreHostError("backup output must be outside the data root")
            created = create_backup(root, archive)
            root_guard.assert_unchanged()
            archive_parent_guard.assert_unchanged()
            manifest = validate_backup(created)
            root_guard.assert_unchanged()
            archive_parent_guard.assert_unchanged()
        return {
            "archive": _manifest_payload(manifest),
            "terminal_state": "COMPLETED",
        }

    def restore(
        self,
        archive_path: Path,
        data_root: Path,
        safety_backup_path: Path,
    ) -> dict[str, object]:
        archive = _absolute(archive_path)
        root = _absolute(data_root)
        safety = _absolute(safety_backup_path)
        if (
            root.parent == root
            or not root.is_dir()
            or not archive.parent.is_dir()
            or not safety.parent.is_dir()
            or _exists(safety)
        ):
            raise BackupRestoreHostError("restore endpoints are unavailable")
        with (
            DirectoryGuard(archive.parent) as archive_parent_guard,
            DirectoryGuard(root.parent) as root_parent_guard,
            DirectoryGuard(root) as root_guard,
            DirectoryGuard(safety.parent) as safety_parent_guard,
        ):
            manifest = validate_backup(archive)
            archive_parent_guard.assert_unchanged()
            root_parent_guard.assert_unchanged()
            root_guard.assert_unchanged()
            safety_parent_guard.assert_unchanged()
            required = (
                sum(entry.size for entry in manifest.entries)
                + RESTORE_WORKSPACE_RESERVE_BYTES
            )
            available = self.capacity_probe(root.parent)
            if (
                isinstance(available, bool)
                or not isinstance(available, int)
                or available < required
            ):
                raise BackupRestoreHostError("restore capacity is unavailable")
            archive_parent_guard.assert_unchanged()
            root_parent_guard.assert_unchanged()
            root_guard.assert_unchanged()
            safety_parent_guard.assert_unchanged()
            created_safety = restore_backup(
                archive,
                root,
                safety_backup_path=safety,
                expected_manifest=manifest,
            )
            archive_parent_guard.assert_unchanged()
            root_parent_guard.assert_unchanged()
            root_guard.assert_unchanged()
            safety_parent_guard.assert_unchanged()
            safety_manifest = validate_backup(created_safety)
        return {
            "restored": _manifest_payload(manifest),
            "safety_backup": _manifest_payload(safety_manifest),
            "terminal_state": "COMPLETED",
        }

    def recover(self, data_root: Path) -> dict[str, object]:
        root = _absolute(data_root)
        if root.parent == root or not root.is_dir():
            raise BackupRestoreHostError("recovery endpoint is unavailable")
        with DirectoryGuard(root.parent) as parent_guard, DirectoryGuard(
            root
        ) as root_guard:
            reconciled = reconcile_restore(root)
            parent_guard.assert_unchanged()
            root_guard.assert_unchanged()
        return {
            "reconciled": reconciled,
            "terminal_state": "RECOVERED" if reconciled else "NO_PENDING_RESTORE",
        }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="neutral-tts-backup")
    commands = parser.add_subparsers(dest="command", required=True)

    backup = commands.add_parser("backup")
    backup.add_argument("--data-root", required=True)
    backup.add_argument("--archive", required=True)

    restore = commands.add_parser("restore")
    restore.add_argument("--archive", required=True)
    restore.add_argument("--data-root", required=True)
    restore.add_argument("--safety-backup", required=True)

    recover = commands.add_parser("recover")
    recover.add_argument("--data-root", required=True)
    return parser


def main(
    argv: list[str] | None = None,
    *,
    capacity_probe: Callable[[Path], int] | None = None,
) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    host = BackupRestoreHost(capacity_probe or _available_bytes)
    try:
        if args.command == "backup":
            result = host.backup(
                _absolute(args.data_root),
                _absolute(args.archive),
            )
        elif args.command == "restore":
            result = host.restore(
                _absolute(args.archive),
                _absolute(args.data_root),
                _absolute(args.safety_backup),
            )
        else:
            result = host.recover(_absolute(args.data_root))
        payload: dict[str, object] = {
            "command": args.command,
            "result": result,
            "schema_version": HOST_SCHEMA,
            "status": "SUCCEEDED",
        }
        code = 0
    except Exception:
        payload = {
            "command": args.command,
            "error": {
                "code": "BACKUP_RESTORE_REJECTED",
                "message": "The requested backup/restore operation could not be completed.",
            },
            "schema_version": HOST_SCHEMA,
            "status": "ERROR",
        }
        code = 2
    print(
        json.dumps(
            payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return code


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())


__all__ = [
    "BackupRestoreHost",
    "BackupRestoreHostError",
    "HOST_SCHEMA",
    "RESTORE_WORKSPACE_RESERVE_BYTES",
    "main",
]
