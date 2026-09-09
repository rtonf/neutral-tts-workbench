"""Privacy-bounded history derived from completed recovery evidence.

History is not a second publication mechanism.  A pending sidecar is made
durable after application WAV validation and before recovery-owned publication;
it becomes completed only when the matching recovery record and published bytes
agree.  Startup can therefore finish the sidecar after a process loss without
constructing a synthesis backend.
"""

from __future__ import annotations

import hashlib
import os
import re
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .fs_safety import (
    DirectoryGuard,
    FilesystemBoundaryError,
    assert_safe_descendant,
    is_reparse,
    make_directories,
)
from .planning import normalize_public_id
from .receipts import parse_success_receipt
from .resilience import (
    RecoveryError,
    RunRecord,
    _atomic_json,
    _decode_canonical_object,
    _file_digest,
    _safe_name,
    _safe_run_id,
)


HISTORY_SCHEMA = "neutral-tts-history-v1"
_SHA256 = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_HISTORY_KEYS = {
    "schema_version",
    "run_id",
    "state",
    "output_basename",
    "output_size",
    "output_sha256",
    "receipt",
    "backend_identity",
    "runtime_config_digest",
    "voice_rights_manifest_sha256",
    "created_at_utc",
    "completed_at_utc",
}


class HistoryError(RuntimeError):
    """History evidence is unavailable, malformed, or inconsistent."""


def _digest(value: object, *, label: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise HistoryError(f"{label} is invalid")
    return value


def _timestamp(
    value: object, *, required: bool
) -> tuple[str | None, datetime | None]:
    if value is None:
        if required:
            raise HistoryError("history timestamp is invalid")
        return None, None
    if not isinstance(value, str) or not value.endswith("Z"):
        raise HistoryError("history timestamp is invalid")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise HistoryError("history timestamp is invalid") from exc
    if parsed.tzinfo != timezone.utc or parsed.isoformat().replace("+00:00", "Z") != value:
        raise HistoryError("history timestamp is not canonical UTC")
    return value, parsed


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _validated_payload(value: dict[str, Any]) -> dict[str, Any]:
    if set(value) != _HISTORY_KEYS or value.get("schema_version") != HISTORY_SCHEMA:
        raise HistoryError("history schema fields are invalid")
    try:
        run_id = _safe_run_id(value["run_id"])
        output_basename = _safe_name(value["output_basename"])
        backend_identity = normalize_public_id(
            value["backend_identity"], label="backend identity"
        )
    except (RecoveryError, TypeError, ValueError) as exc:
        raise HistoryError("history identity is invalid") from exc
    if output_basename != value["output_basename"]:
        raise HistoryError("history output name is not canonical")
    if Path(output_basename).suffix.casefold() != ".wav":
        raise HistoryError("history output name is not WAV")
    if backend_identity != value["backend_identity"]:
        raise HistoryError("history backend identity is not canonical")
    state = value.get("state")
    if state not in {"pending", "completed"}:
        raise HistoryError("history state is invalid")
    size = value.get("output_size")
    if type(size) is not int or size < 44:
        raise HistoryError("history output size is invalid")
    output_sha256 = _digest(value.get("output_sha256"), label="output hash")
    runtime_digest = _digest(
        value.get("runtime_config_digest"), label="runtime configuration digest"
    )
    rights_digest = _digest(
        value.get("voice_rights_manifest_sha256"), label="voice rights digest"
    )
    receipt = deepcopy(value.get("receipt"))
    calibration_key = receipt.get("calibration_key") if isinstance(receipt, dict) else None
    if not isinstance(calibration_key, str) or parse_success_receipt(
        receipt, calibration_key
    ) is None:
        raise HistoryError("history receipt is invalid")
    created, created_value = _timestamp(
        value.get("created_at_utc"), required=True
    )
    completed_at, completed_value = _timestamp(
        value.get("completed_at_utc"), required=state == "completed"
    )
    if state == "pending" and completed_at is not None:
        raise HistoryError("pending history has a completion timestamp")
    if state == "completed" and (
        completed_value is None
        or created_value is None
        or completed_value < created_value
    ):
        raise HistoryError("completed history timestamp is invalid")
    return {
        "schema_version": HISTORY_SCHEMA,
        "run_id": run_id,
        "state": state,
        "output_basename": output_basename,
        "output_size": size,
        "output_sha256": output_sha256,
        "receipt": receipt,
        "backend_identity": backend_identity,
        "runtime_config_digest": runtime_digest,
        "voice_rights_manifest_sha256": rights_digest,
        "created_at_utc": created,
        "completed_at_utc": completed_at,
    }


@dataclass(frozen=True, slots=True)
class HistoryRecord:
    run_id: str
    output_basename: str
    output_size: int
    output_sha256: str
    receipt: dict[str, Any]
    backend_identity: str
    runtime_config_digest: str
    voice_rights_manifest_sha256: str
    created_at_utc: str
    completed_at_utc: str
    output_path: Path

    def public_payload(self) -> dict[str, object]:
        actual = deepcopy(self.receipt["actual"])
        return {
            "run_id": self.run_id,
            "state": "VERIFIED",
            "output": {
                "wav_name": self.output_basename,
                "bytes": self.output_size,
                "sha256": self.output_sha256,
            },
            "backend": {"identity": self.backend_identity},
            "actual": actual,
            "estimate_relation": self.receipt["estimate_relation"],
            "completed_at_utc": self.completed_at_utc,
        }


@dataclass(frozen=True, slots=True)
class HistoryRecovery:
    recovered_pending_records: int = 0
    verified_completed_records: int = 0
    quarantined_records: int = 0

    @property
    def status(self) -> str:
        if self.recovered_pending_records or self.quarantined_records:
            return "RECOVERED"
        if self.verified_completed_records:
            return "VERIFIED"
        return "CLEAN"

    def public_payload(self) -> dict[str, object]:
        return {
            "status": self.status,
            "recovered_pending_records": self.recovered_pending_records,
            "verified_completed_records": self.verified_completed_records,
            "quarantined_records": self.quarantined_records,
        }


class HistoryStore:
    """Canonical sidecars protected by the application's recovery ownership."""

    def __init__(self, recovery_directory: Path, output_directory: Path) -> None:
        if not all(
            isinstance(path, Path) and path.is_absolute()
            for path in (recovery_directory, output_directory)
        ):
            raise HistoryError("history roots must be absolute directories")
        self.recovery_directory = recovery_directory.absolute()
        self.output_directory = output_directory.absolute()
        try:
            self._recovery_guard = DirectoryGuard(self.recovery_directory)
            self._output_guard = DirectoryGuard(self.output_directory)
            self.history_directory = make_directories(
                self._recovery_guard, self.recovery_directory / "history"
            )
            self.quarantine_directory = make_directories(
                self._recovery_guard, self.recovery_directory / "quarantine"
            )
            self._history_guard = DirectoryGuard(self.history_directory)
            self._quarantine_guard = DirectoryGuard(self.quarantine_directory)
        except (OSError, FilesystemBoundaryError) as exc:
            self.close()
            raise HistoryError("history filesystem boundary is unavailable") from exc

    def _assert_roots(self) -> None:
        try:
            for guard in (
                self._recovery_guard,
                self._output_guard,
                self._history_guard,
                self._quarantine_guard,
            ):
                guard.assert_unchanged()
        except FilesystemBoundaryError as exc:
            raise HistoryError("history filesystem boundary changed") from exc

    def _path(self, run_id: str) -> Path:
        try:
            safe = _safe_run_id(run_id)
        except RecoveryError as exc:
            raise HistoryError("history run identity is invalid") from exc
        return self.history_directory / f"{safe}.json"

    def _load(self, path: Path) -> dict[str, Any]:
        try:
            self._assert_roots()
            assert_safe_descendant(self._history_guard, path)
            if not path.is_file() or is_reparse(path):
                raise ValueError("history sidecar is not a regular file")
            payload = _validated_payload(
                _decode_canonical_object(path.read_bytes())
            )
            if path.absolute() != self._path(payload["run_id"]):
                raise ValueError("history sidecar name differs from its run identity")
            return payload
        except (OSError, RecoveryError, FilesystemBoundaryError, ValueError) as exc:
            raise HistoryError("history sidecar is invalid") from exc

    def prepare(
        self,
        *,
        run_id: str,
        output_basename: str,
        staged_path: Path,
        receipt: dict[str, Any],
        backend_identity: str,
        runtime_config_digest: str,
        voice_rights_manifest_sha256: str,
    ) -> None:
        path = self._path(run_id)
        self._assert_roots()
        if os.path.lexists(path):
            raise HistoryError("history run already exists")
        try:
            output_basename = _safe_name(output_basename)
            size, output_sha256 = _file_digest(staged_path)
        except RecoveryError as exc:
            raise HistoryError("staged history evidence is invalid") from exc
        payload = _validated_payload(
            {
                "schema_version": HISTORY_SCHEMA,
                "run_id": run_id,
                "state": "pending",
                "output_basename": output_basename,
                "output_size": size,
                "output_sha256": output_sha256,
                "receipt": receipt,
                "backend_identity": backend_identity,
                "runtime_config_digest": runtime_config_digest,
                "voice_rights_manifest_sha256": voice_rights_manifest_sha256,
                "created_at_utc": _now_utc(),
                "completed_at_utc": None,
            }
        )
        try:
            _atomic_json(path, payload, self._history_guard)
        except (OSError, FilesystemBoundaryError) as exc:
            raise HistoryError("pending history could not be made durable") from exc

    @staticmethod
    def _matching_recovery(
        payload: dict[str, Any], records: tuple[RunRecord, ...]
    ) -> RunRecord:
        matches = [record for record in records if record.run_id == payload["run_id"]]
        if len(matches) != 1:
            raise HistoryError("matching recovery record is unavailable")
        record = matches[0]
        if (
            record.state != "completed"
            or record.output_name != payload["output_basename"]
            or record.published_size != payload["output_size"]
            or record.published_sha256 != payload["output_sha256"]
        ):
            raise HistoryError("recovery record does not prove publication")
        return record

    def _verify_output(self, payload: dict[str, Any]) -> Path:
        target = self.output_directory / payload["output_basename"]
        try:
            self._assert_roots()
            assert_safe_descendant(self._output_guard, target)
            if _file_digest(target) != (
                payload["output_size"],
                payload["output_sha256"],
            ):
                raise HistoryError("published history bytes changed")
        except (OSError, RecoveryError, FilesystemBoundaryError) as exc:
            raise HistoryError("published history output is invalid") from exc
        return target

    def complete(
        self,
        run_id: str,
        *,
        recovery_records: tuple[RunRecord, ...],
    ) -> HistoryRecord:
        path = self._path(run_id)
        payload = self._load(path)
        self._matching_recovery(payload, recovery_records)
        target = self._verify_output(payload)
        if payload["state"] == "pending":
            payload = _validated_payload(
                {
                    **payload,
                    "state": "completed",
                    "completed_at_utc": _now_utc(),
                }
            )
            try:
                _atomic_json(path, payload, self._history_guard)
            except (OSError, FilesystemBoundaryError) as exc:
                raise HistoryError("completed history could not be made durable") from exc
        return self._record(payload, target)

    @staticmethod
    def _record(payload: dict[str, Any], target: Path) -> HistoryRecord:
        completed_at = payload["completed_at_utc"]
        if payload["state"] != "completed" or not isinstance(completed_at, str):
            raise HistoryError("history record is not completed")
        return HistoryRecord(
            run_id=payload["run_id"],
            output_basename=payload["output_basename"],
            output_size=payload["output_size"],
            output_sha256=payload["output_sha256"],
            receipt=deepcopy(payload["receipt"]),
            backend_identity=payload["backend_identity"],
            runtime_config_digest=payload["runtime_config_digest"],
            voice_rights_manifest_sha256=payload["voice_rights_manifest_sha256"],
            created_at_utc=payload["created_at_utc"],
            completed_at_utc=completed_at,
            output_path=target,
        )

    def _quarantine(self, path: Path) -> None:
        self._assert_roots()
        source = path.absolute()
        if source.parent != self.history_directory or not os.path.lexists(source):
            raise HistoryError("invalid history entry is outside its owned directory")
        token = hashlib.sha256(path.name.encode("utf-8", "surrogatepass")).hexdigest()[:16]
        destination = self.quarantine_directory / f"history-{token}.json"
        counter = 0
        while os.path.lexists(destination):
            counter += 1
            destination = self.quarantine_directory / f"history-{token}-{counter}.json"
        try:
            assert_safe_descendant(
                self._quarantine_guard, destination, allow_missing_leaf=True
            )
            self._history_guard.assert_unchanged()
            if (
                self._history_guard.fd is not None
                and self._quarantine_guard.fd is not None
            ):
                os.replace(
                    source.name,
                    destination.name,
                    src_dir_fd=self._history_guard.fd,
                    dst_dir_fd=self._quarantine_guard.fd,
                )
            else:
                os.replace(source, destination)
            self._history_guard.assert_unchanged()
            self._quarantine_guard.assert_unchanged()
        except (OSError, FilesystemBoundaryError) as exc:
            raise HistoryError("invalid history could not be quarantined") from exc

    def reconcile(
        self, recovery_records: tuple[RunRecord, ...]
    ) -> tuple[tuple[HistoryRecord, ...], HistoryRecovery]:
        if not isinstance(recovery_records, tuple) or any(
            not isinstance(record, RunRecord) for record in recovery_records
        ):
            raise TypeError("recovery_records must be a tuple of RunRecord values")
        self._assert_roots()
        records: list[HistoryRecord] = []
        recovered = 0
        verified = 0
        quarantined = 0
        for path in sorted(
            self.history_directory.iterdir(), key=lambda item: item.name.casefold()
        ):
            try:
                if path.suffix != ".json":
                    raise HistoryError("unexpected history entry")
                payload = self._load(path)
                was_pending = payload["state"] == "pending"
                record = self.complete(
                    payload["run_id"], recovery_records=recovery_records
                )
                records.append(record)
                if was_pending:
                    recovered += 1
                else:
                    verified += 1
            except HistoryError:
                self._quarantine(path)
                quarantined += 1
        def completed_key(record: HistoryRecord) -> tuple[datetime, str]:
            _canonical, instant = _timestamp(
                record.completed_at_utc, required=True
            )
            assert instant is not None
            return instant, record.run_id

        records.sort(key=completed_key)
        return (
            tuple(records),
            HistoryRecovery(recovered, verified, quarantined),
        )

    def close(self) -> None:
        for name in (
            "_quarantine_guard",
            "_history_guard",
            "_output_guard",
            "_recovery_guard",
        ):
            guard = getattr(self, name, None)
            if guard is not None:
                guard.close()
                setattr(self, name, None)

    def __enter__(self) -> "HistoryStore":
        return self

    def __exit__(self, _type: object, _value: object, _traceback: object) -> None:
        self.close()


__all__ = [
    "HISTORY_SCHEMA",
    "HistoryError",
    "HistoryRecord",
    "HistoryRecovery",
    "HistoryStore",
]
