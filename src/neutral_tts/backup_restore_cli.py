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
    BackupArtifact,
    BackupManifest,
    BackupRestoreBusy,
    RESTORE_CAPACITY_RESERVE_BYTES,
    create_backup_artifact,
    reconcile_restore_result,
    restore_backup_result,
    validate_backup,
)


HOST_SCHEMA = "neutral-tts-backup-host-v1"
RESTORE_WORKSPACE_RESERVE_BYTES = RESTORE_CAPACITY_RESERVE_BYTES


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


def _artifact_payload(artifact: BackupArtifact) -> dict[str, object]:
    payload = _manifest_payload(artifact.manifest)
    payload.update(
        archive_sha256=artifact.archive_sha256,
        archive_size=artifact.archive_size,
    )
    return payload


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
        artifact = create_backup_artifact(root, archive)
        return {
            "archive": _artifact_payload(artifact),
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
        manifest = validate_backup(archive)
        result = restore_backup_result(
            archive,
            root,
            safety_backup_path=safety,
            expected_manifest=manifest,
            capacity_probe=self.capacity_probe,
        )
        return {
            "restored": _manifest_payload(result.restored_manifest),
            "safety_backup": _artifact_payload(result.safety_backup),
            "terminal_state": result.terminal_state,
        }

    def recover(self, data_root: Path) -> dict[str, object]:
        root = _absolute(data_root)
        if root.parent == root or not root.is_dir():
            raise BackupRestoreHostError("recovery endpoint is unavailable")
        result = reconcile_restore_result(root)
        return {
            "reconciled": result.reconciled,
            "terminal_state": result.terminal_state,
        }


class _FixedJsonArgumentParser(argparse.ArgumentParser):
    def error(self, _message: str) -> None:
        raise BackupRestoreHostError("backup/restore arguments are invalid")


def _parser() -> argparse.ArgumentParser:
    parser = _FixedJsonArgumentParser(prog="neutral-tts-backup")
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
    arguments = sys.argv[1:] if argv is None else argv
    command = (
        arguments[0]
        if arguments and arguments[0] in {"backup", "restore", "recover"}
        else "unknown"
    )
    try:
        args = parser.parse_args(arguments)
        command = args.command
        host = BackupRestoreHost(capacity_probe or _available_bytes)
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
            "status": (
                "INCOMPLETE"
                if str(result.get("terminal_state", "")).endswith(
                    ("_PENDING", "_UNVERIFIED")
                )
                else "SUCCEEDED"
            ),
        }
        code = 3 if payload["status"] == "INCOMPLETE" else 0
    except BackupRestoreBusy:
        payload = {
            "command": command,
            "error": {
                "code": "BACKUP_RESTORE_BUSY",
                "message": "Another backup/restore operation is active.",
            },
            "schema_version": HOST_SCHEMA,
            "status": "ERROR",
        }
        code = 2
    except Exception:
        payload = {
            "command": command,
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
