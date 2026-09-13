"""Crash-safe, cancellation-aware run staging and publication.

The module intentionally has no knowledge of a synthesis engine.  A producer
is given an isolated directory and may write one or more files there.  A
completed output is published with a no-replace operation and is never removed
by recovery.  Journals are small canonical JSON documents and are flushed at
each externally visible transition so recovery can make a deterministic
decision after cancellation or power loss.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import stat
import threading
import unicodedata
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterable

from .fs_safety import (
    DirectoryGuard,
    FilesystemBoundaryError,
    assert_safe_descendant,
    open_exclusive_binary,
)


class RecoveryError(RuntimeError):
    """Base class for recovery and publication failures."""


class RunCancelled(RecoveryError):
    """The run was cancelled before publication."""


CancellationError = RunCancelled


class JournalCorrupt(RecoveryError):
    """A journal cannot be trusted for reconciliation."""


class FaultInjected(RecoveryError):
    """A caller supplied fault injector simulated a crash/power loss."""


_RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,95}\Z", re.ASCII)
_SHA256 = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_STATES = frozenset({"prepared", "producing", "ready", "publishing", "completed", "cancelled", "failed"})
_TERMINAL = frozenset({"completed", "cancelled", "failed"})
_ALLOWED_TRANSITIONS = {
    "prepared": frozenset({"producing", "cancelled", "failed"}),
    "producing": frozenset({"ready", "cancelled", "failed"}),
    "ready": frozenset({"publishing", "cancelled", "failed"}),
    "publishing": frozenset({"completed", "failed"}),
    "completed": frozenset(),
    "cancelled": frozenset(),
    "failed": frozenset(),
}
_COMMON_JOURNAL_KEYS = frozenset(
    {
        "requested_output",
        "run_id",
        "schema_version",
        "staging_directory",
        "state",
    }
)
_JOURNAL_KEYS_BY_STATE = {
    "prepared": _COMMON_JOURNAL_KEYS,
    "producing": _COMMON_JOURNAL_KEYS,
    "ready": _COMMON_JOURNAL_KEYS | {"staged_sha256", "staged_size"},
    "publishing": _COMMON_JOURNAL_KEYS
    | {"output_name", "staged_sha256", "staged_size"},
    "completed": _COMMON_JOURNAL_KEYS
    | {
        "output_name",
        "published_sha256",
        "published_size",
        "staged_sha256",
        "staged_size",
    },
    "cancelled": _COMMON_JOURNAL_KEYS,
    "failed": _COMMON_JOURNAL_KEYS | {"error"},
}
_REPARSE_ATTRIBUTE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")


def _reject_duplicate_pairs(
    pairs: list[tuple[str, Any]],
) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for key, value in pairs:
        if key in payload:
            raise ValueError("journal contains duplicate keys")
        payload[key] = value
    return payload


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"journal contains non-finite number: {value}")


def _decode_canonical_object(raw: bytes) -> dict[str, Any]:
    payload = json.loads(
        raw.decode("utf-8", "strict"),
        object_pairs_hook=_reject_duplicate_pairs,
        parse_constant=_reject_json_constant,
    )
    if not isinstance(payload, dict):
        raise ValueError("journal root is not an object")
    if raw != _canonical(payload):
        raise ValueError("journal is not canonical JSON")
    return payload


def _flush_directory(directory: Path) -> None:
    """Best effort directory durability; Windows does not always permit it."""
    try:
        flags = os.O_RDONLY
        if hasattr(os, "O_DIRECTORY"):
            flags |= os.O_DIRECTORY
        fd = os.open(str(directory), flags)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError as exc:
        if exc.errno not in (errno.EINVAL, errno.ENOTSUP, errno.EACCES, errno.EPERM):
            raise


def _atomic_json(path: Path, payload: object, guard: DirectoryGuard | None = None) -> None:
    # Avoid extending an already long quarantine reason basename on Windows.
    temporary = path.with_name(".tmp-" + uuid.uuid4().hex)
    data = _canonical(payload)
    try:
        handle_context = (
            open_exclusive_binary(guard, temporary)
            if guard is not None
            else temporary.open("xb")
        )
        with handle_context as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if guard is not None:
            guard.assert_unchanged()
        os.replace(temporary, path)
        _flush_directory(path.parent)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _safe_root(value: Path, label: str) -> Path:
    if not isinstance(value, Path) or not value.is_absolute():
        raise RecoveryError(f"{label} must be an absolute directory")
    current = value.absolute()
    leaf_exists = os.path.lexists(current)
    existing = current if leaf_exists else current.parent
    if not existing.is_dir():
        raise RecoveryError(f"{label} parent is unavailable")
    for component in (existing, *existing.parents):
        try:
            info = component.lstat()
        except OSError as exc:
            raise RecoveryError(f"{label} is unavailable") from exc
        if component.is_symlink() or bool(
            getattr(info, "st_file_attributes", 0) & _REPARSE_ATTRIBUTE
        ):
            raise RecoveryError(f"{label} contains a reparse point")
    if not leaf_exists:
        try:
            current.mkdir()
        except OSError as exc:
            raise RecoveryError(f"{label} could not be created") from exc
    if not current.is_dir() or current.is_symlink():
        raise RecoveryError(f"{label} is unavailable")
    return current


def _safe_run_id(value: str) -> str:
    if not isinstance(value, str) or _RUN_ID.fullmatch(value) is None or ".." in value:
        raise RecoveryError("run_id is invalid")
    return value


def _safe_name(value: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise RecoveryError("requested output name is invalid")
    # A requested output is a filename, never a relative path.  In particular
    # reject drive/UNC, ADS, and separator aliases on every host.
    if any(token in value for token in ("/", "\\", ":")) or value in {".", ".."}:
        raise RecoveryError("requested output must be a Windows-safe filename")
    if value.endswith((".", " ")) or any(ord(c) < 32 for c in value):
        raise RecoveryError("requested output name is invalid")
    return unicodedata.normalize("NFC", value)


def _collision_target(output: Path, requested: str) -> Path:
    """Choose a deterministic no-replace destination, including case clashes."""
    base = Path(requested)
    stem, suffix = base.stem, base.suffix
    occupied = {
        unicodedata.normalize("NFC", p.name).casefold()
        for p in output.iterdir()
        if not p.is_symlink()
    }
    identity = unicodedata.normalize("NFC", requested).casefold()
    if identity not in occupied:
        return output / requested
    for index in range(1, 1_000_000):
        candidate = f"{stem} ({index}){suffix}"
        if unicodedata.normalize("NFC", candidate).casefold() not in occupied:
            return output / candidate
    raise RecoveryError("unable to allocate a collision-free output name")


def _exact_target(output: Path, requested: str) -> Path:
    """Bind publication to the exact admitted name, including case clashes."""
    identity = unicodedata.normalize("NFC", requested).casefold()
    for candidate in output.iterdir():
        if unicodedata.normalize("NFC", candidate.name).casefold() == identity:
            raise RecoveryError("requested output is no longer available")
    return output / requested


def _file_digest(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    try:
        before = path.stat()
        if path.is_symlink() or bool(
            getattr(path.lstat(), "st_file_attributes", 0) & _REPARSE_ATTRIBUTE
        ) or not stat.S_ISREG(before.st_mode):
            raise RecoveryError("run payload is not a regular non-link file")
        with path.open("rb") as handle:
            opened = os.fstat(handle.fileno())
            while chunk := handle.read(1024 * 1024):
                size += len(chunk)
                digest.update(chunk)
        after = path.stat()
    except RecoveryError:
        raise
    except OSError as exc:
        raise RecoveryError("run payload is unreadable") from exc
    identities = {
        (item.st_dev, item.st_ino, item.st_size, item.st_mtime_ns)
        for item in (before, opened, after)
    }
    if len(identities) != 1 or size != before.st_size:
        raise RecoveryError("run payload changed during inspection")
    return size, digest.hexdigest()


def _journal_file_fact(payload: dict[str, Any], prefix: str) -> tuple[int, str]:
    size = payload.get(f"{prefix}_size")
    digest = payload.get(f"{prefix}_sha256")
    if (
        isinstance(size, bool)
        or not isinstance(size, int)
        or size <= 0
        or not isinstance(digest, str)
        or _SHA256.fullmatch(digest) is None
    ):
        raise ValueError(f"{prefix} output evidence is incomplete")
    return size, digest


def _validate_run_payload(payload: dict[str, Any]) -> dict[str, Any]:
    state = payload.get("state")
    if not isinstance(state, str):
        raise ValueError("journal state is invalid")
    expected = _JOURNAL_KEYS_BY_STATE.get(state)
    observed = set(payload)
    history_bound = (
        expected is not None
        and state in {"publishing", "completed"}
        and observed == expected | {"history_binding_sha256"}
    )
    if expected is None or (observed != expected and not history_bound):
        raise ValueError("journal fields do not match its state")
    if payload.get("schema_version") != "neutral-tts-run-v1":
        raise ValueError("schema mismatch")
    try:
        run_id = _safe_run_id(payload["run_id"])
        requested = _safe_name(payload["requested_output"])
    except RecoveryError as exc:
        raise ValueError(str(exc)) from exc
    if requested != payload["requested_output"]:
        raise ValueError("requested output is not canonically normalized")
    if payload["staging_directory"] != run_id:
        raise ValueError("invalid staging directory")
    if state in {"ready", "publishing", "completed"}:
        _journal_file_fact(payload, "staged")
    if history_bound and (
        not isinstance(payload.get("history_binding_sha256"), str)
        or _SHA256.fullmatch(payload["history_binding_sha256"]) is None
    ):
        raise ValueError("history binding evidence is invalid")
    if state in {"publishing", "completed"}:
        try:
            output_name = _safe_name(payload["output_name"])
        except RecoveryError as exc:
            raise ValueError("output name is invalid") from exc
        if output_name != payload["output_name"]:
            raise ValueError("output name is not canonically normalized")
    if state == "completed":
        if _journal_file_fact(payload, "staged") != _journal_file_fact(
            payload, "published"
        ):
            raise ValueError("completed output evidence is inconsistent")
    if state == "failed" and (
        not isinstance(payload["error"], str) or not payload["error"]
    ):
        raise ValueError("failed journal error is invalid")
    return payload


def _load_canonical_run_journal(path: Path) -> dict[str, Any]:
    return _validate_run_payload(_decode_canonical_object(path.read_bytes()))


def _fsync_file(path: Path) -> None:
    # A write can be acknowledged by the filesystem before its bytes are
    # durable.  Flush the staged payload before recording the READY state.
    with path.open("rb+") as handle:
        handle.flush()
        os.fsync(handle.fileno())


@dataclass(frozen=True, slots=True)
class RunRecord:
    run_id: str
    requested_output: str
    output_name: str | None
    state: str
    staging_directory: Path
    journal_path: Path
    error: str | None = None
    published_size: int | None = None
    published_sha256: str | None = None
    history_binding_sha256: str | None = None


@dataclass(frozen=True, slots=True)
class _ProducedArtifact:
    """In-memory handoff from application validation to recovery hashing."""

    path: Path
    size: int
    sha256: str


@dataclass(frozen=True, slots=True)
class RecoveryReport:
    """Structured startup reconciliation evidence without private paths."""

    records: tuple[RunRecord, ...]
    recovered_publications: int
    verified_completed_outputs: int
    quarantined_runs: int


class RunHandle:
    """A single isolated run and its durable journal."""

    def __init__(self, manager: "RecoveryManager", record: RunRecord) -> None:
        self.manager = manager
        self.record = record

    @property
    def run_id(self) -> str:
        return self.record.run_id

    @property
    def staging_directory(self) -> Path:
        return self.record.staging_directory

    @property
    def staging_path(self) -> Path:
        return self.staging_directory / "output"

    def _transition(self, state: str, **extra: Any) -> None:
        self.manager._transition(self, state, **extra)

    def produce(self, producer: Callable[..., Any]) -> Path:
        """Run a producer against the private output path, then mark it ready."""
        lock = self.manager._run_lock(self.run_id)
        with lock:
            if self.record.state != "prepared":
                raise RecoveryError("run is not prepared")
            self._transition("producing")
        try:
            result = producer(self.staging_path)
            # ``Path.write_bytes`` returns an integer byte count; producer
            # callbacks commonly return that value accidentally.  Only an
            # explicit path-like result changes the declared staged path.
            expected_fact: tuple[int, str] | None = None
            if isinstance(result, _ProducedArtifact):
                candidate = result.path
                expected_fact = (result.size, result.sha256)
            else:
                candidate = (
                    self.staging_path
                    if result is None or not isinstance(result, (str, os.PathLike))
                    else Path(result)
                )
            self.manager._assert_run_staging(self.run_id, candidate)
            if (
                candidate.absolute() != self.staging_path.absolute()
                or not candidate.is_file()
                or candidate.is_symlink()
            ):
                raise RecoveryError("producer did not create the declared staged output")
            _fsync_file(candidate)
            size, digest = _file_digest(candidate)
            if size <= 0:
                raise RecoveryError("staged output is empty")
            if expected_fact is not None and expected_fact != (size, digest):
                raise RecoveryError("staged output changed after application validation")
            with lock:
                if self.manager.is_cancelled(self.run_id) or self.record.state == "cancelled":
                    raise RunCancelled("run cancelled")
                if self.record.state != "producing":
                    raise RecoveryError("run state changed while producing")
                self._transition("ready", staged_size=size, staged_sha256=digest)
            return candidate
        except RunCancelled:
            self.cancel()
            raise
        except FaultInjected:
            raise
        except Exception as exc:
            with lock:
                if self.manager.is_cancelled(self.run_id) or self.record.state == "cancelled":
                    raise RunCancelled("run cancelled") from exc
                if self.record.state not in _TERMINAL:
                    self._transition("failed", error=f"{type(exc).__name__}: {exc}")
            raise

    def publish(
        self,
        *,
        allow_collision_suffix: bool = True,
        history_binding_sha256: str | None = None,
    ) -> Path:
        """Atomically publish without replacement.

        Library callers retain deterministic suffix allocation by default.
        Application admission uses exact-name mode so a name cannot change
        after the user-approved preview.
        """
        lock = self.manager._run_lock(self.run_id)
        with lock:
            if history_binding_sha256 is not None and (
                not isinstance(history_binding_sha256, str)
                or _SHA256.fullmatch(history_binding_sha256) is None
            ):
                raise RecoveryError("history binding evidence is invalid")
            if self.record.state != "ready":
                raise RecoveryError("run is not ready for publication")
            if self.manager.is_cancelled(self.run_id):
                self.cancel()
                raise RunCancelled("run cancelled")
            self.manager._assert_roots()
            try:
                assert_safe_descendant(
                    self.manager._journal_guard, self.record.journal_path
                )
                ready = _load_canonical_run_journal(self.record.journal_path)
            except (OSError, FilesystemBoundaryError, ValueError) as exc:
                raise JournalCorrupt("ready journal is unreadable") from exc
            self.manager._assert_run_staging(self.run_id, self.staging_path)
            staged_size, staged_sha256 = _file_digest(self.staging_path)
            if (
                ready.get("state") != "ready"
                or ready.get("staged_size") != staged_size
                or ready.get("staged_sha256") != staged_sha256
            ):
                raise JournalCorrupt("staged output does not match the ready journal")
            target = (
                _collision_target(self.manager.output_directory, self.record.requested_output)
                if allow_collision_suffix
                else _exact_target(self.manager.output_directory, self.record.requested_output)
            )
            publishing_evidence: dict[str, Any] = {
                "output_name": target.name,
                "staged_size": staged_size,
                "staged_sha256": staged_sha256,
            }
            if history_binding_sha256 is not None:
                publishing_evidence["history_binding_sha256"] = (
                    history_binding_sha256
                )
            self._transition("publishing", **publishing_evidence)
            try:
                # Acquiring READY -> PUBLISHING while holding the per-run lock
                # is the linearization point.  A cancellation that returns
                # before this point wins; after it, publication wins.
                for _ in range(1_000_000):
                    self.manager._assert_roots()
                    assert_safe_descendant(
                        self.manager._output_guard, target, allow_missing_leaf=True
                    )
                    try:
                        os.link(self.staging_path, target)
                        break
                    except FileExistsError as exc:
                        if not allow_collision_suffix:
                            raise RecoveryError(
                                "requested output is no longer available"
                            ) from exc
                        target = _collision_target(
                            self.manager.output_directory, self.record.requested_output
                        )
                else:
                    raise RecoveryError("unable to publish without replacement")
                size, digest = _file_digest(target)
                if (size, digest) != (staged_size, staged_sha256):
                    raise RecoveryError("published output differs from staged evidence")
                _flush_directory(target.parent)
                self.manager._fault("publication_linked")
                completed_evidence: dict[str, Any] = {
                    "output_name": target.name,
                    "staged_size": staged_size,
                    "staged_sha256": staged_sha256,
                    "published_size": size,
                    "published_sha256": digest,
                }
                if self.record.history_binding_sha256 is not None:
                    completed_evidence["history_binding_sha256"] = (
                        self.record.history_binding_sha256
                    )
                self._transition("completed", **completed_evidence)
                return target
            except Exception as exc:
                if isinstance(exc, FaultInjected):
                    raise
                if self.record.state == "publishing":
                    self._transition("failed", error=f"{type(exc).__name__}: {exc}")
                raise

    def cancel(self) -> None:
        with self.manager._run_lock(self.run_id):
            if self.record.state in _TERMINAL:
                return
            if self.record.state == "publishing":
                raise RecoveryError("publication was already claimed")
            self.manager._mark_cancelled(self.run_id)
            self._transition("cancelled")


class RecoveryManager:
    """Own journals, private per-run staging, and deterministic reconciliation."""

    def __init__(self, recovery_directory: Path, output_directory: Path, *, fault_injector: Callable[[str], None] | None = None) -> None:
        self.recovery_directory = _safe_root(recovery_directory, "recovery directory")
        self.output_directory = _safe_root(output_directory, "output directory")
        recovery_key = os.path.normcase(str(self.recovery_directory))
        output_key = os.path.normcase(str(self.output_directory))
        try:
            common = os.path.normcase(os.path.commonpath((recovery_key, output_key)))
        except ValueError as exc:
            raise RecoveryError("recovery and output roots are incompatible") from exc
        if common in {recovery_key, output_key}:
            raise RecoveryError("recovery and output roots must be separate")
        try:
            with DirectoryGuard(self.recovery_directory) as recovery_guard, DirectoryGuard(
                self.output_directory
            ) as output_guard:
                if (
                    recovery_guard.identity == output_guard.identity
                    or recovery_guard.contains_final(output_guard.final_path)
                    or output_guard.contains_final(recovery_guard.final_path)
                ):
                    raise RecoveryError(
                        "recovery and output roots must be physically separate"
                    )
        except FilesystemBoundaryError as exc:
            raise RecoveryError("recovery root identity is unavailable") from exc
        self.journal_directory = self.recovery_directory / "journals"
        self.staging_root = self.recovery_directory / "staging"
        self.quarantine_directory = self.recovery_directory / "quarantine"
        for path in (self.journal_directory, self.staging_root, self.quarantine_directory):
            try:
                path.mkdir(exist_ok=True)
                if not path.is_dir() or path.is_symlink() or bool(
                    getattr(path.lstat(), "st_file_attributes", 0) & _REPARSE_ATTRIBUTE
                ):
                    raise RecoveryError("recovery child directory is redirected")
            except OSError as exc:
                raise RecoveryError("recovery child directory is unavailable") from exc
        try:
            self._recovery_guard = DirectoryGuard(self.recovery_directory)
            self._output_guard = DirectoryGuard(self.output_directory)
            self._journal_guard = DirectoryGuard(self.journal_directory)
            self._staging_root_guard = DirectoryGuard(self.staging_root)
            self._quarantine_guard = DirectoryGuard(self.quarantine_directory)
        except FilesystemBoundaryError as exc:
            raise RecoveryError("recovery filesystem boundary is unsafe") from exc
        self._cancelled: set[str] = set()
        self._registry_lock = threading.RLock()
        self._run_locks: dict[str, threading.RLock] = {}
        self._handles: dict[str, RunHandle] = {}
        self._staging_guards: dict[str, DirectoryGuard] = {}
        self.fault_injector = fault_injector

    def _assert_roots(self) -> None:
        try:
            for guard in (
                self._recovery_guard,
                self._output_guard,
                self._journal_guard,
                self._staging_root_guard,
                self._quarantine_guard,
            ):
                guard.assert_unchanged()
        except FilesystemBoundaryError as exc:
            raise RecoveryError("recovery filesystem boundary changed") from exc

    def _run_lock(self, run_id: str) -> threading.RLock:
        safe = _safe_run_id(run_id)
        with self._registry_lock:
            return self._run_locks.setdefault(safe, threading.RLock())

    def _assert_run_staging(self, run_id: str, candidate: Path) -> None:
        with self._registry_lock:
            guard = self._staging_guards.get(_safe_run_id(run_id))
        if guard is None:
            raise RecoveryError("run staging guard is unavailable")
        try:
            assert_safe_descendant(guard, candidate)
        except FilesystemBoundaryError as exc:
            raise RecoveryError("run staging boundary changed") from exc

    def _mark_cancelled(self, run_id: str) -> None:
        with self._registry_lock:
            self._cancelled.add(_safe_run_id(run_id))

    def _fault(self, transition: str) -> None:
        if self.fault_injector is not None:
            self.fault_injector(transition)

    def _payload(self, handle: RunHandle, state: str, **extra: Any) -> dict[str, Any]:
        data: dict[str, Any] = {
            "schema_version": "neutral-tts-run-v1",
            "run_id": handle.record.run_id,
            "requested_output": handle.record.requested_output,
            "state": state,
            "staging_directory": handle.record.staging_directory.name,
        }
        data.update(extra)
        return data

    def _transition(self, handle: RunHandle, state: str, **extra: Any) -> None:
        with self._run_lock(handle.run_id):
            if state not in _STATES:
                raise RecoveryError("unknown run state")
            initial_prepared = (
                state == "prepared"
                and handle.record.state == "prepared"
                and not os.path.lexists(handle.record.journal_path)
            )
            if not initial_prepared and state not in _ALLOWED_TRANSITIONS[handle.record.state]:
                raise RecoveryError(
                    f"illegal run transition: {handle.record.state} -> {state}"
                )
            self._assert_roots()
            payload = self._payload(handle, state, **extra)
            _atomic_json(handle.record.journal_path, payload, self._journal_guard)
            handle.record = RunRecord(
                handle.run_id,
                handle.record.requested_output,
                extra.get("output_name", handle.record.output_name),
                state,
                handle.staging_directory,
                handle.record.journal_path,
                extra.get("error", handle.record.error),
                extra.get("published_size", handle.record.published_size),
                extra.get("published_sha256", handle.record.published_sha256),
                extra.get(
                    "history_binding_sha256",
                    handle.record.history_binding_sha256,
                ),
            )
            self._fault(state)

    def start(self, requested_output: str, *, run_id: str | None = None) -> RunHandle:
        requested_output = _safe_name(requested_output)
        run_id = _safe_run_id(run_id or ("run-" + uuid.uuid4().hex))
        with self._run_lock(run_id):
            self._assert_roots()
            journal = self.journal_directory / f"{run_id}.json"
            if os.path.lexists(journal):
                raise RecoveryError("run_id already exists")
            staging = self.staging_root / run_id
            if os.path.lexists(staging):
                raise RecoveryError("run staging already exists")
            staging.mkdir()
            try:
                staging_guard = DirectoryGuard(staging)
                self._staging_root_guard.assert_unchanged()
            except (OSError, FilesystemBoundaryError) as exc:
                raise RecoveryError("run staging could not be safely created") from exc
            _flush_directory(self.staging_root)
            record = RunRecord(run_id, requested_output, None, "prepared", staging, journal)
            handle = RunHandle(self, record)
            with self._registry_lock:
                self._staging_guards[run_id] = staging_guard
                self._handles[run_id] = handle
            self._transition(handle, "prepared")
            return handle

    def is_cancelled(self, run_id: str) -> bool:
        with self._registry_lock:
            return run_id in self._cancelled

    def cancel(self, run_id: str) -> None:
        safe = _safe_run_id(run_id)
        with self._run_lock(safe):
            with self._registry_lock:
                handle = self._handles.get(safe)
            if handle is None:
                self._mark_cancelled(safe)
                return
            handle.cancel()

    def execute(self, requested_output: str, producer: Callable[..., Any], *, run_id: str | None = None) -> Path:
        handle = self.start(requested_output, run_id=run_id)
        handle.produce(producer)
        return handle.publish()

    def _quarantine(self, path: Path, reason: str) -> Path:
        # Stable names make reconciliation idempotent and auditable after a
        # second process restart.  A numeric suffix handles a pre-existing
        # quarantine from an earlier attempt without overwriting it.
        self._assert_roots()
        if path.parent == self.staging_root:
            with self._registry_lock:
                guard = self._staging_guards.pop(path.name, None)
            if guard is not None:
                guard.close()
        suffix = hashlib.sha256(reason.encode("utf-8", "strict")).hexdigest()[:16]
        destination = self.quarantine_directory / (path.name + "-" + suffix)
        index = 1
        while os.path.lexists(destination):
            destination = self.quarantine_directory / (path.name + "-" + suffix + f"-{index}")
            index += 1
        try:
            os.rename(path, destination)
        except OSError as exc:
            raise RecoveryError(f"cannot quarantine {path.name}: {reason}") from exc
        self._quarantine_guard.assert_unchanged()
        _atomic_json(
            destination.with_suffix(destination.suffix + ".reason.json"),
            {"reason": reason},
            self._quarantine_guard,
        )
        return destination

    def _read_record(self, journal: Path) -> tuple[dict[str, Any], Path]:
        try:
            if journal.is_symlink() or bool(
                getattr(journal.lstat(), "st_file_attributes", 0) & _REPARSE_ATTRIBUTE
            ) or not journal.is_file():
                raise ValueError("unsafe journal file")
            payload = _load_canonical_run_journal(journal)
            run_id = payload["run_id"]
            requested = payload["requested_output"]
            state = payload["state"]
            staging_name = payload["staging_directory"]
            staging = self.staging_root / staging_name
            staging_exists = os.path.lexists(staging)
            if staging_exists and (
                not staging.is_dir()
                or staging.is_symlink()
                or bool(getattr(staging.lstat(), "st_file_attributes", 0) & _REPARSE_ATTRIBUTE)
            ):
                raise ValueError("unsafe staging")
            if not staging_exists and state not in _TERMINAL:
                raise ValueError("missing staging")
            return payload, staging
        except Exception as exc:
            raise JournalCorrupt(str(exc)) from exc

    def reconcile_report(self) -> RecoveryReport:
        """Reconcile journals and return privacy-bounded startup evidence."""
        self._assert_roots()
        records: list[RunRecord] = []
        journal_run_ids: set[str] = set()
        recovered_publications = 0
        verified_completed_outputs = 0
        quarantined_runs = 0
        for journal in sorted(self.journal_directory.glob("*.json"), key=lambda p: p.name.casefold()):
            try:
                payload, staging = self._read_record(journal)
                run_id = payload["run_id"]
                journal_run_ids.add(run_id)
                state = payload["state"]
                if state == "completed":
                    output_name = payload.get("output_name")
                    if not isinstance(output_name, str):
                        raise ValueError("completed run has no output")
                    staged_fact = _journal_file_fact(payload, "staged")
                    published_fact = _journal_file_fact(payload, "published")
                    if staged_fact != published_fact:
                        raise ValueError("completed output evidence is inconsistent")
                    try:
                        target = self.output_directory / _safe_name(output_name)
                    except RecoveryError as exc:
                        raise ValueError("completed output name is invalid") from exc
                    self._output_guard.assert_unchanged()
                    if not target.is_file() or target.is_symlink():
                        raise ValueError("completed output is missing")
                    try:
                        if _file_digest(target) != published_fact:
                            raise ValueError("completed output hash mismatch")
                        if os.path.lexists(staging):
                            if _file_digest(staging / "output") != staged_fact:
                                raise ValueError("completed staged evidence drifted")
                    except RecoveryError as exc:
                        raise ValueError("completed output evidence is unreadable") from exc
                    verified_completed_outputs += 1
                elif state == "publishing":
                    output_name = payload.get("output_name")
                    if not isinstance(output_name, str):
                        raise ValueError("publishing journal evidence is incomplete")
                    declared_staged_fact = _journal_file_fact(payload, "staged")
                    try:
                        target = self.output_directory / _safe_name(output_name)
                    except RecoveryError as exc:
                        raise ValueError("publishing output name is invalid") from exc
                    staged = staging / "output"
                    try:
                        observed_staged_fact = _file_digest(staged)
                    except RecoveryError as exc:
                        raise ValueError("publishing staged evidence is unreadable") from exc
                    if observed_staged_fact != declared_staged_fact:
                        raise ValueError("publishing staged evidence drifted")
                    if os.path.lexists(target):
                        try:
                            published_size, published_sha256 = _file_digest(target)
                        except RecoveryError as exc:
                            raise ValueError("publishing target is unreadable") from exc
                        if (published_size, published_sha256) != declared_staged_fact:
                            raise ValueError("publishing target is ambiguous")
                        completed_payload = {
                            **payload,
                            "state": "completed",
                            "published_size": published_size,
                            "published_sha256": published_sha256,
                        }
                        _atomic_json(journal, completed_payload, self._journal_guard)
                        recovered_publications += 1
                        records.append(
                            RunRecord(
                                run_id,
                                payload["requested_output"],
                                output_name,
                                "completed",
                                staging,
                                journal,
                                published_size=published_size,
                                published_sha256=published_sha256,
                                history_binding_sha256=payload.get(
                                    "history_binding_sha256"
                                ),
                            )
                        )
                        continue
                    self._quarantine(staging, "publication did not reach a completed output")
                    self._quarantine(journal, "publication did not reach a completed output")
                    quarantined_runs += 1
                    continue
                elif state in {"prepared", "producing", "ready"}:
                    self._quarantine(staging, "incomplete or ambiguous run")
                    self._quarantine(journal, "incomplete or ambiguous journal")
                    quarantined_runs += 1
                    continue
                elif state in {"cancelled", "failed"} and staging.exists():
                    # Cancellation/failure is known, but retaining private
                    # temporary data indefinitely is not useful.  Quarantine
                    # it for inspection instead of deleting it.
                    self._quarantine(staging, "cancelled or failed run")
                    quarantined_runs += 1
                if state == "cancelled":
                    self._mark_cancelled(run_id)
                record = RunRecord(
                    run_id,
                    payload["requested_output"],
                    payload.get("output_name"),
                    state,
                    staging,
                    journal,
                    payload.get("error"),
                    payload.get("published_size"),
                    payload.get("published_sha256"),
                    payload.get("history_binding_sha256"),
                )
                records.append(record)
            except JournalCorrupt as exc:
                quarantined_runs += 1
                # Try to quarantine a referenced staging directory even when
                # the journal itself is malformed.  Never trust malformed
                # text as a path unless it passes the same run-id grammar.
                try:
                    if (
                        journal.is_symlink()
                        or bool(getattr(journal.lstat(), "st_file_attributes", 0) & _REPARSE_ATTRIBUTE)
                        or not journal.is_file()
                    ):
                        raise ValueError("unsafe journal")
                    decoded = _decode_canonical_object(journal.read_bytes())
                    candidate = decoded.get("run_id")
                    if isinstance(candidate, str) and _RUN_ID.fullmatch(candidate):
                        staging = self.staging_root / candidate
                        if staging.exists() or staging.is_symlink():
                            self._quarantine(staging, "invalid journal staging")
                except Exception:
                    pass
                self._quarantine(journal, str(exc))
            except ValueError as exc:
                quarantined_runs += 1
                if os.path.lexists(staging):
                    self._quarantine(staging, "invalid lifecycle evidence")
                self._quarantine(journal, str(exc))
        # A power loss can occur before the journal reaches disk.  Any
        # unreferenced run directory is therefore ambiguous and is quarantined
        # deterministically as well.
        for staging in sorted(self.staging_root.iterdir(), key=lambda p: p.name.casefold()):
            if staging.name not in journal_run_ids:
                self._quarantine(staging, "orphan staging directory")
                quarantined_runs += 1
        return RecoveryReport(
            tuple(records),
            recovered_publications,
            verified_completed_outputs,
            quarantined_runs,
        )

    def reconcile(self) -> tuple[RunRecord, ...]:
        """Compatibility view of reconciled terminal run records."""
        return self.reconcile_report().records

    def close(self) -> None:
        """Release non-delete-sharing directory handles owned by the manager."""
        with self._registry_lock:
            staging_guards = tuple(self._staging_guards.values())
            self._staging_guards.clear()
        for guard in staging_guards:
            guard.close()
        for guard in (
            self._quarantine_guard,
            self._staging_root_guard,
            self._journal_guard,
            self._output_guard,
            self._recovery_guard,
        ):
            guard.close()

    def __enter__(self) -> "RecoveryManager":
        return self

    def __exit__(self, _type: object, _value: object, _traceback: object) -> None:
        self.close()

    # Compatibility-friendly semantic aliases for callers that call startup
    # reconciliation "recover" or run admission "begin".
    recover = reconcile
    begin = start


__all__ = ["CancellationError", "FaultInjected", "JournalCorrupt", "RecoveryError", "RecoveryManager", "RecoveryReport", "RunCancelled", "RunHandle", "RunRecord"]
