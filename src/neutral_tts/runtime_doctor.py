"""Sanitized, model-free checks for one machine-local optional runtime.

Only a fixed ``python -I -S`` identity probe, ``.dist-info`` metadata reads,
bounded native-file checks, and an optional read-only driver query are used.
Matching metadata never means that ABI, CUDA, model loading, or synthesis works.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
from dataclasses import dataclass
from email.parser import BytesParser
from enum import Enum
from pathlib import Path
from threading import Event, Thread
from typing import Callable, Mapping, Sequence
from urllib.parse import urlsplit


_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.!+_-]{0,127}$")
_EXACT = re.compile(
    r"^([A-Za-z0-9][A-Za-z0-9._-]{0,127})=="
    r"([A-Za-z0-9][A-Za-z0-9.!+_-]{0,127})$"
)
_VCS = re.compile(
    r"^([A-Za-z0-9][A-Za-z0-9._-]{0,127})\s*@\s*"
    r"git\+(https://[^\s@]+)@([0-9a-fA-F]{40})$"
)
_DRIVER = re.compile(r"^[0-9]{1,5}(?:\.[0-9]{1,5}){1,3}$")
_REPARSE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
_MAX_LOCK_BYTES = 1024 * 1024
_MAX_METADATA_BYTES = 1024 * 1024
_MAX_DIRECT_URL_BYTES = 64 * 1024
_MAX_NATIVE_HASH_BYTES = 64 * 1024 * 1024
_MAX_REQUIREMENTS = 512
_MAX_REQUIREMENT_CHARS = 2048
_MAX_ROOTS = 4
_MAX_ROOT_ENTRIES = 4096
_MAX_NATIVES = 32
_MAX_ISSUES = 128
_MAX_PYTHON_STDOUT_BYTES = 4096
_MAX_PYTHON_STDERR_BYTES = 1024
_MAX_GPU_STDOUT_BYTES = 8192
_MAX_GPU_STDERR_BYTES = 2048
_MAX_GPU_ROWS = 16
_MAX_GPU_ROW_CHARS = 256


class RuntimeCheckStatus(str, Enum):
    VERIFIED = "VERIFIED"
    MISSING = "MISSING"
    MISMATCH = "MISMATCH"
    UNVERIFIED = "UNVERIFIED"


@dataclass(frozen=True, slots=True)
class RuntimeCheck:
    check_id: str
    status: RuntimeCheckStatus
    reason: str
    details: Mapping[str, object] | None = None

    def to_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "id": self.check_id,
            "status": self.status.value,
            "reason": self.reason,
        }
        if self.details:
            result["details"] = dict(self.details)
        return result


@dataclass(frozen=True, slots=True)
class NativeLibrarySpec:
    component_id: str
    path: Path
    expected_sha256: str | None = None

    def __post_init__(self) -> None:
        if _ID.fullmatch(self.component_id) is None:
            raise ValueError("invalid native component id")
        if self.expected_sha256 is not None and _SHA256.fullmatch(
            self.expected_sha256
        ) is None:
            raise ValueError("invalid native SHA-256")


@dataclass(frozen=True, slots=True)
class RuntimeDoctorRequest:
    """Host-only declarations for a metadata probe.

    Executable paths must come from trusted, machine-local host configuration.
    Regular-file and reparse checks are not provenance checks or a sandbox.
    """

    python_executable: Path
    expected_python_version: str
    requirements_lock: Path
    expected_requirements_sha256: str
    metadata_roots: tuple[Path, ...]
    additional_requirements: tuple[str, ...] = ()
    native_libraries: tuple[NativeLibrarySpec, ...] = ()
    query_gpu_driver: bool = True
    nvidia_smi_executable: Path | None = None
    expected_python_implementation: str = "cpython"
    expected_pointer_bits: int = 64

    def __post_init__(self) -> None:
        if _VERSION.fullmatch(self.expected_python_version) is None:
            raise ValueError("invalid Python version")
        if _SHA256.fullmatch(self.expected_requirements_sha256) is None:
            raise ValueError("invalid requirements SHA-256")
        if self.expected_python_implementation not in {"cpython", "pypy"}:
            raise ValueError("invalid Python implementation")
        if self.expected_pointer_bits not in {32, 64}:
            raise ValueError("invalid pointer width")
        if not 1 <= len(self.metadata_roots) <= _MAX_ROOTS:
            raise ValueError("invalid metadata-root count")
        paths = (
            self.python_executable,
            self.requirements_lock,
            *self.metadata_roots,
            *(item.path for item in self.native_libraries),
        )
        if any(not isinstance(path, Path) or not path.is_absolute() for path in paths):
            raise ValueError("runtime paths must be absolute")
        if (
            self.nvidia_smi_executable is not None
            and (
                not isinstance(self.nvidia_smi_executable, Path)
                or not self.nvidia_smi_executable.is_absolute()
            )
        ):
            raise ValueError("nvidia-smi path must be absolute")
        if len(self.additional_requirements) > _MAX_REQUIREMENTS:
            raise ValueError("too many additional requirements")
        if any(
            not isinstance(item, str)
            or not item.strip()
            or len(item) > _MAX_REQUIREMENT_CHARS
            or "\n" in item
            or "\r" in item
            for item in self.additional_requirements
        ):
            raise ValueError("invalid additional requirement")
        ids = [item.component_id for item in self.native_libraries]
        if len(ids) > _MAX_NATIVES or len(ids) != len(set(ids)):
            raise ValueError("invalid native-library declarations")


@dataclass(frozen=True, slots=True)
class RuntimeDoctorReport:
    checks: tuple[RuntimeCheck, ...]
    distribution_summary: Mapping[str, int]

    @property
    def status(self) -> str:
        states = {item.status for item in self.checks}
        if states.intersection(
            {RuntimeCheckStatus.MISSING, RuntimeCheckStatus.MISMATCH}
        ):
            return "NOT_READY"
        return "INCOMPLETE"

    @property
    def declared_metadata_matches(self) -> bool:
        if any(
            item.status in {RuntimeCheckStatus.MISSING, RuntimeCheckStatus.MISMATCH}
            for item in self.checks
        ):
            return False
        summary = self.distribution_summary
        if (
            summary.get("verified") != summary.get("declared_total")
            or summary.get("unverified") != summary.get("unexpected")
        ):
            return False
        ignored = {
            "installed_distributions",
            "native_abi_and_loadability",
            "cuda_runtime_compatibility",
            "real_runtime_and_generation",
        }
        return all(
            item.status is RuntimeCheckStatus.VERIFIED
            or item.check_id in ignored
            or (
                item.check_id == "gpu_driver_metadata"
                and item.reason == "gpu_driver_query_was_not_requested"
            )
            for item in self.checks
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": "neutral-tts-runtime-doctor-v1",
            "scope": "MODEL_FREE_METADATA_ONLY",
            "status": self.status,
            "declared_metadata_matches": self.declared_metadata_matches,
            "execution_ready": False,
            "checks": [item.to_dict() for item in self.checks],
            "distribution_summary": dict(self.distribution_summary),
            "limitations": [
                "optional runtime packages were not imported",
                "model tokenizer codec watermark and reference assets were not opened",
                "native ABI and DLL loadability were not tested",
                "CUDA runtime availability was not tested",
                "real synthesis save playback and generated output were not tested",
            ],
        }


@dataclass(frozen=True, slots=True)
class _PythonObservation:
    executable: Path
    version: str
    implementation: str
    pointer_bits: int


PythonProbe = Callable[[Path], _PythonObservation | None]
GpuProbe = Callable[[], Mapping[str, object] | None]


def _canonical(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def _is_reparse(path: Path) -> bool:
    metadata = path.lstat()
    return path.is_symlink() or bool(
        getattr(metadata, "st_file_attributes", 0) & _REPARSE
    )


def _safe_path(path: Path, *, directory: bool = False) -> bool:
    try:
        absolute = path.absolute()
        mode = absolute.lstat().st_mode
        if (stat.S_ISDIR(mode) if directory else stat.S_ISREG(mode)) is False:
            return False
        if _is_reparse(absolute):
            return False
        return not any(parent.exists() and _is_reparse(parent) for parent in absolute.parents)
    except OSError:
        return False


@dataclass(frozen=True, slots=True)
class _BoundedFileEvidence:
    size: int
    sha256: str | None = None
    data: bytes | None = None


def _file_snapshot(value: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _inspect_regular_file(
    path: Path,
    *,
    max_bytes: int,
    capture: bool,
) -> tuple[str, _BoundedFileEvidence | None]:
    """Read at most max-plus-one bytes and reject path or content drift."""

    absolute = path.absolute()
    if not _safe_path(absolute):
        return "unsafe", None
    try:
        lexical_before = absolute.lstat()
        with absolute.open("rb") as handle:
            opened_before = os.fstat(handle.fileno())
            if (
                not stat.S_ISREG(opened_before.st_mode)
                or not os.path.samestat(lexical_before, opened_before)
            ):
                return "unsafe", None
            if opened_before.st_size > max_bytes:
                return "oversized", _BoundedFileEvidence(opened_before.st_size)
            digest = hashlib.sha256()
            parts: list[bytes] | None = [] if capture else None
            total = 0
            while total <= max_bytes:
                block = handle.read(min(1024 * 1024, max_bytes + 1 - total))
                if not block:
                    break
                total += len(block)
                if total > max_bytes:
                    return "oversized", _BoundedFileEvidence(total)
                digest.update(block)
                if parts is not None:
                    parts.append(block)
            opened_after = os.fstat(handle.fileno())
        lexical_after = absolute.lstat()
        if (
            not _safe_path(absolute)
            or not os.path.samestat(opened_after, lexical_after)
            or _file_snapshot(opened_before) != _file_snapshot(opened_after)
            or _file_snapshot(lexical_before) != _file_snapshot(lexical_after)
            or total != opened_before.st_size
        ):
            return "changed", None
        return "ok", _BoundedFileEvidence(
            total,
            digest.hexdigest(),
            b"".join(parts) if parts is not None else None,
        )
    except OSError:
        return "unsafe", None


@dataclass(frozen=True, slots=True)
class _BoundedProcessResult:
    returncode: int
    stdout: bytes
    stderr: bytes


def _run_bounded_process(
    argv: Sequence[str],
    *,
    cwd: Path,
    env: Mapping[str, str],
    timeout_seconds: float,
    stdout_limit: int,
    stderr_limit: int,
) -> _BoundedProcessResult | None:
    """Run one trusted host probe while bounding captured bytes and time."""

    process: subprocess.Popen[bytes] | None = None
    invalid = Event()
    stdout = bytearray()
    stderr = bytearray()

    def drain(stream: object, target: bytearray, limit: int) -> None:
        try:
            while True:
                block = stream.read(4096)  # type: ignore[attr-defined]
                if not block:
                    return
                remaining = limit + 1 - len(target)
                if remaining > 0:
                    target.extend(block[:remaining])
                if len(target) > limit or len(block) > remaining:
                    invalid.set()
                    if process is not None:
                        try:
                            process.kill()
                        except OSError:
                            pass
        except (OSError, ValueError):
            invalid.set()

    try:
        process = subprocess.Popen(
            list(argv),
            cwd=str(cwd),
            env=dict(env),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if process.stdout is None or process.stderr is None:
            process.kill()
            return None
        readers = (
            Thread(
                target=drain,
                args=(process.stdout, stdout, stdout_limit),
                daemon=True,
            ),
            Thread(
                target=drain,
                args=(process.stderr, stderr, stderr_limit),
                daemon=True,
            ),
        )
        for reader in readers:
            reader.start()
        try:
            process.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            invalid.set()
            process.kill()
            process.wait(timeout=2)
        for reader in readers:
            reader.join(timeout=1)
        if any(reader.is_alive() for reader in readers):
            invalid.set()
            process.stdout.close()
            process.stderr.close()
            for reader in readers:
                reader.join(timeout=1)
        for stream in (process.stdout, process.stderr):
            try:
                stream.close()
            except OSError:
                pass
        if invalid.is_set() or any(reader.is_alive() for reader in readers):
            return None
        return _BoundedProcessResult(process.returncode, bytes(stdout), bytes(stderr))
    except (OSError, RuntimeError, subprocess.SubprocessError):
        if process is not None and process.poll() is None:
            try:
                process.kill()
            except OSError:
                pass
        if process is not None:
            for stream in (process.stdout, process.stderr):
                if stream is not None:
                    try:
                        stream.close()
                    except OSError:
                        pass
        return None


def _probe_environment() -> dict[str, str]:
    names = (
        "COMSPEC", "CommonProgramFiles", "CommonProgramFiles(x86)",
        "CommonProgramW6432", "DriverData", "NUMBER_OF_PROCESSORS", "PATH",
        "PATHEXT", "PROCESSOR_ARCHITECTURE", "ProgramData", "ProgramFiles",
        "ProgramFiles(x86)", "ProgramW6432", "SYSTEMDRIVE", "SYSTEMROOT",
        "TEMP", "TMP", "WINDIR",
    )
    result = {name: os.environ[name] for name in names if name in os.environ}
    result.update(
        PYTHONIOENCODING="utf-8",
        PYTHONNOUSERSITE="1",
        PYTHONSAFEPATH="1",
        PYTHONUTF8="1",
    )
    return result


def _default_python_probe(executable: Path) -> _PythonObservation | None:
    if not _safe_path(executable):
        return None
    source = (
        "import json,struct,sys;print(json.dumps({"
        "'bits':struct.calcsize('P')*8,'executable':sys.executable,"
        "'implementation':sys.implementation.name,"
        "'version':'.'.join(map(str,sys.version_info[:3]))},"
        "sort_keys=True,separators=(',',':')))"
    )
    try:
        result = _run_bounded_process(
            [str(executable), "-I", "-S", "-c", source],
            cwd=executable.parent,
            env=_probe_environment(),
            timeout_seconds=10,
            stdout_limit=_MAX_PYTHON_STDOUT_BYTES,
            stderr_limit=_MAX_PYTHON_STDERR_BYTES,
        )
        if result is None or result.returncode or result.stderr:
            return None
        lines = result.stdout.decode("utf-8", errors="strict").splitlines()
        if len(lines) != 1 or len(lines[0]) > _MAX_PYTHON_STDOUT_BYTES:
            return None
        value = json.loads(lines[0])
        version = value.get("version") if isinstance(value, dict) else None
        implementation = value.get("implementation") if isinstance(value, dict) else None
        bits = value.get("bits") if isinstance(value, dict) else None
        observed = value.get("executable") if isinstance(value, dict) else None
        if (
            not isinstance(version, str)
            or _VERSION.fullmatch(version) is None
            or implementation not in {"cpython", "pypy"}
            or bits not in {32, 64}
            or not isinstance(observed, str)
            or not Path(observed).is_absolute()
        ):
            return None
        return _PythonObservation(Path(observed), version, implementation, bits)
    except (OSError, ValueError, UnicodeError):
        return None


def _python_check(request: RuntimeDoctorRequest, probe: PythonProbe) -> RuntimeCheck:
    if not _safe_path(request.python_executable):
        return RuntimeCheck(
            "python_runtime", RuntimeCheckStatus.MISSING,
            "declared_python_executable_is_unavailable",
        )
    observed = probe(request.python_executable)
    if observed is None:
        return RuntimeCheck(
            "python_runtime", RuntimeCheckStatus.UNVERIFIED,
            "isolated_standard_library_probe_failed",
        )
    if (
        not isinstance(observed, _PythonObservation)
        or _VERSION.fullmatch(observed.version) is None
        or observed.implementation not in {"cpython", "pypy"}
        or observed.pointer_bits not in {32, 64}
        or not isinstance(observed.executable, Path)
        or not observed.executable.is_absolute()
    ):
        return RuntimeCheck(
            "python_runtime", RuntimeCheckStatus.UNVERIFIED,
            "isolated_standard_library_probe_returned_invalid_metadata",
        )
    try:
        same_file = observed.executable.samefile(request.python_executable)
    except OSError:
        same_file = False
    expected_identity = {
        "version": request.expected_python_version,
        "implementation": request.expected_python_implementation,
        "pointer_bits": request.expected_pointer_bits,
    }
    observed_identity = {
        "version": observed.version,
        "implementation": observed.implementation,
        "pointer_bits": observed.pointer_bits,
    }
    if not same_file or expected_identity != observed_identity:
        return RuntimeCheck(
            "python_runtime", RuntimeCheckStatus.MISMATCH,
            "isolated_python_identity_differs_from_declared_contract",
            {"expected": expected_identity, "observed": observed_identity},
        )
    return RuntimeCheck(
        "python_runtime", RuntimeCheckStatus.VERIFIED,
        "isolated_standard_library_probe_matches_declared_identity",
        observed_identity,
    )


def _parse_requirement(line: str) -> tuple[str, str | None, str | None, str | None] | None:
    exact = _EXACT.fullmatch(line)
    if exact:
        name, version = exact.groups()
        return _canonical(name), version, None, None
    vcs = _VCS.fullmatch(line)
    if not vcs:
        return None
    name, url, commit = vcs.groups()
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https" or not parsed.netloc or parsed.username
        or parsed.password or parsed.query or parsed.fragment
    ):
        return None
    return _canonical(name), None, url.rstrip("/"), commit.lower()


def _load_expectations(
    request: RuntimeDoctorRequest,
) -> tuple[RuntimeCheck, dict[str, tuple[str | None, str | None, str | None]] | None, int]:
    path = request.requirements_lock
    if not _safe_path(path):
        return RuntimeCheck(
            "requirements_lock", RuntimeCheckStatus.MISSING,
            "requirements_lock_is_unavailable",
        ), None, 0
    try:
        file_status, evidence = _inspect_regular_file(
            path, max_bytes=_MAX_LOCK_BYTES, capture=True
        )
        if file_status != "ok" or evidence is None or evidence.data is None:
            raise ValueError("oversized")
        raw = evidence.data
        digest = hashlib.sha256(raw).hexdigest()
        if digest != request.expected_requirements_sha256:
            return RuntimeCheck(
                "requirements_lock", RuntimeCheckStatus.MISMATCH,
                "requirements_lock_sha256_differs",
                {"bytes": len(raw), "sha256": digest},
            ), None, 0
        lines = [
            line.strip() for line in raw.decode("utf-8").splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        additional = [item.strip() for item in request.additional_requirements]
        if len(lines) + len(additional) > _MAX_REQUIREMENTS:
            raise ValueError("too many requirements")
        lines.extend(additional)
        expectations: dict[str, tuple[str | None, str | None, str | None]] = {}
        rejected = 0
        for line in lines:
            parsed = (
                _parse_requirement(line)
                if len(line) <= _MAX_REQUIREMENT_CHARS
                else None
            )
            if parsed is None:
                rejected += 1
                continue
            name, version, url, commit = parsed
            identity = version, url, commit
            if name in expectations and expectations[name] != identity:
                rejected += 1
            else:
                expectations[name] = identity
        check = RuntimeCheck(
            "requirements_lock", RuntimeCheckStatus.VERIFIED,
            "requirements_lock_sha256_matches",
            {"bytes": len(raw), "sha256": digest},
        )
        return check, None if rejected else expectations, rejected
    except (OSError, UnicodeError, ValueError):
        return RuntimeCheck(
            "requirements_lock", RuntimeCheckStatus.UNVERIFIED,
            "requirements_lock_could_not_be_read_safely",
        ), None, 0


def _scan_metadata(
    roots: Sequence[Path],
) -> tuple[dict[str, list[tuple[str, Mapping[str, object] | None]]] | None, int]:
    installed: dict[str, list[tuple[str, Mapping[str, object] | None]]] = {}
    errors = 0
    for root in roots:
        if not _safe_path(root, directory=True):
            return None, 1
        try:
            entries = tuple(root.iterdir())
        except OSError:
            return None, 1
        if len(entries) > _MAX_ROOT_ENTRIES:
            return None, 1
        for entry in entries:
            suffix = entry.name.casefold()
            metadata_name = (
                "METADATA" if suffix.endswith(".dist-info")
                else "PKG-INFO" if suffix.endswith(".egg-info")
                else None
            )
            if metadata_name is None:
                continue
            if not _safe_path(entry, directory=True):
                return None, 1
            metadata_path = entry / metadata_name
            if not _safe_path(metadata_path):
                return None, 1
            metadata_status, metadata_evidence = _inspect_regular_file(
                metadata_path,
                max_bytes=_MAX_METADATA_BYTES,
                capture=True,
            )
            if (
                metadata_status != "ok"
                or metadata_evidence is None
                or metadata_evidence.data is None
            ):
                errors += 1
                continue
            direct_url = entry / "direct_url.json"
            direct: Mapping[str, object] | None = None
            try:
                direct_url.lstat()
            except FileNotFoundError:
                pass
            except OSError:
                return None, 1
            else:
                if not _safe_path(direct_url):
                    return None, 1
                direct_status, direct_evidence = _inspect_regular_file(
                    direct_url,
                    max_bytes=_MAX_DIRECT_URL_BYTES,
                    capture=True,
                )
                if (
                    direct_status != "ok"
                    or direct_evidence is None
                    or direct_evidence.data is None
                ):
                    errors += 1
                    continue
                try:
                    parsed_direct = json.loads(
                        direct_evidence.data.decode("utf-8", errors="strict")
                    )
                except (UnicodeError, ValueError, json.JSONDecodeError):
                    errors += 1
                    continue
                if not isinstance(parsed_direct, dict):
                    errors += 1
                    continue
                direct = parsed_direct
            try:
                metadata = BytesParser().parsebytes(
                    metadata_evidence.data,
                    headersonly=True,
                )
                name, version = metadata.get("Name"), metadata.get("Version")
                if (
                    not isinstance(name, str)
                    or _NAME.fullmatch(name) is None
                    or not isinstance(version, str)
                    or _VERSION.fullmatch(version) is None
                ):
                    raise ValueError("invalid identity")
                installed.setdefault(_canonical(name), []).append((version, direct))
            except (UnicodeError, ValueError):
                errors += 1
    return installed, errors


def _vcs_matches(
    expected_url: str, expected_commit: str, direct: Mapping[str, object] | None,
) -> bool | None:
    if direct is None:
        return None
    url, info = direct.get("url"), direct.get("vcs_info")
    if not isinstance(url, str) or not isinstance(info, dict):
        return None
    commit, vcs = info.get("commit_id"), info.get("vcs")
    if not isinstance(commit, str) or not isinstance(vcs, str):
        return None
    return (
        vcs == "git" and url.rstrip("/") == expected_url
        and commit.lower() == expected_commit
    )


def _distribution_check(
    expectations: dict[str, tuple[str | None, str | None, str | None]] | None,
    rejected: int,
    roots: Sequence[Path],
) -> tuple[RuntimeCheck, dict[str, int]]:
    counts = {
        "declared_total": len(expectations or {}), "verified": 0, "missing": 0,
        "mismatch": 0, "unverified": rejected, "unexpected": 0,
    }
    if expectations is None:
        return RuntimeCheck(
            "installed_distributions", RuntimeCheckStatus.UNVERIFIED,
            "verified_lock_is_unavailable_or_contains_unsupported_entries",
            {"rejected_entries": rejected},
        ), counts
    installed, metadata_errors = _scan_metadata(roots)
    if installed is None:
        return RuntimeCheck(
            "installed_distributions", RuntimeCheckStatus.MISSING,
            "declared_distribution_metadata_root_is_unavailable",
        ), counts
    issues: list[dict[str, str]] = []

    def issue(component: str, status: str, reason: str, **values: str) -> None:
        if len(issues) < _MAX_ISSUES:
            issues.append({"component": component, "status": status, "reason": reason, **values})

    for name, (version, url, commit) in sorted(expectations.items()):
        found = installed.get(name, [])
        if not found:
            counts["missing"] += 1
            issue(name, "MISSING", "metadata_absent")
        elif len(found) != 1:
            counts["unverified"] += 1
            issue(name, "UNVERIFIED", "duplicate_metadata")
        elif version is not None and found[0][0] != version:
            counts["mismatch"] += 1
            issue(
                name, "MISMATCH", "version_differs",
                expected=version, observed=found[0][0],
            )
        elif version is not None:
            counts["verified"] += 1
        else:
            matches = _vcs_matches(url or "", commit or "", found[0][1])
            if matches is True:
                counts["verified"] += 1
            elif matches is False:
                counts["mismatch"] += 1
                issue(name, "MISMATCH", "vcs_identity_differs")
            else:
                counts["unverified"] += 1
                issue(name, "UNVERIFIED", "vcs_metadata_absent_or_invalid")
    counts["unverified"] += metadata_errors
    for name in sorted(set(installed).difference(expectations)):
        counts["unexpected"] += 1
        counts["unverified"] += 1
        issue(name, "UNVERIFIED", "not_declared_by_lock")
    if counts["mismatch"]:
        status, reason = RuntimeCheckStatus.MISMATCH, "distribution_identity_differs"
    elif counts["missing"]:
        status, reason = RuntimeCheckStatus.MISSING, "distribution_metadata_is_missing"
    elif counts["unverified"]:
        status, reason = RuntimeCheckStatus.UNVERIFIED, "distribution_metadata_is_ambiguous"
    else:
        status, reason = RuntimeCheckStatus.VERIFIED, "all_declared_distribution_metadata_matches"
    details: dict[str, object] = {"summary": counts}
    if issues:
        details["issues"] = issues
    omitted = counts["missing"] + counts["mismatch"] + counts["unverified"] - len(issues)
    if omitted > 0:
        details["issues_omitted"] = omitted
    return RuntimeCheck("installed_distributions", status, reason, details), counts


def _native_check(spec: NativeLibrarySpec) -> RuntimeCheck:
    check_id = f"native_library:{spec.component_id}"
    if not _safe_path(spec.path):
        return RuntimeCheck(
            check_id, RuntimeCheckStatus.MISSING,
            "declared_native_library_is_unavailable",
        )
    try:
        if spec.expected_sha256 is None:
            before = spec.path.lstat()
            after = spec.path.lstat()
            if not _safe_path(spec.path) or _file_snapshot(before) != _file_snapshot(after):
                raise OSError("native path changed")
            return RuntimeCheck(
                check_id, RuntimeCheckStatus.VERIFIED,
                "regular_file_presence_verified_only",
                {"bytes": after.st_size, "evidence": "presence_only"},
            )
        file_status, evidence = _inspect_regular_file(
            spec.path,
            max_bytes=_MAX_NATIVE_HASH_BYTES,
            capture=False,
        )
        if file_status == "oversized" and evidence is not None:
            return RuntimeCheck(
                check_id, RuntimeCheckStatus.UNVERIFIED,
                "native_library_exceeds_bounded_hash_limit",
                {
                    "bytes": evidence.size,
                    "hash_limit_bytes": _MAX_NATIVE_HASH_BYTES,
                },
            )
        if file_status != "ok" or evidence is None or evidence.sha256 is None:
            raise OSError("native path changed")
        if evidence.sha256 != spec.expected_sha256:
            return RuntimeCheck(
                check_id, RuntimeCheckStatus.MISMATCH,
                "native_library_sha256_differs",
                {"bytes": evidence.size, "sha256": evidence.sha256},
            )
        return RuntimeCheck(
            check_id, RuntimeCheckStatus.VERIFIED,
            "native_library_sha256_matches",
            {"bytes": evidence.size, "sha256": evidence.sha256},
        )
    except OSError:
        return RuntimeCheck(
            check_id, RuntimeCheckStatus.UNVERIFIED,
            "native_library_could_not_be_read_safely",
        )


def _default_gpu_probe(executable: Path) -> Mapping[str, object] | None:
    if not _safe_path(executable):
        return None
    try:
        result = _run_bounded_process(
            [
                str(executable),
                "--query-gpu=driver_version,name,memory.total",
                "--format=csv,noheader,nounits",
            ],
            cwd=executable.parent,
            env=_probe_environment(),
            timeout_seconds=5,
            stdout_limit=_MAX_GPU_STDOUT_BYTES,
            stderr_limit=_MAX_GPU_STDERR_BYTES,
        )
        if result is None or result.returncode or result.stderr:
            return None
        lines = result.stdout.decode("utf-8", errors="strict").splitlines()
        if not 1 <= len(lines) <= _MAX_GPU_ROWS:
            return None
        rows: list[tuple[str, int]] = []
        for line in lines:
            if len(line) > _MAX_GPU_ROW_CHARS:
                return None
            parts = [part.strip() for part in line.split(",", 2)]
            if len(parts) != 3 or _DRIVER.fullmatch(parts[0]) is None:
                return None
            if not parts[2].isdecimal() or len(parts[2]) > 12:
                return None
            memory = int(parts[2])
            if memory <= 0:
                return None
            rows.append((parts[0], memory))
        return {
            "gpu_count": len(rows),
            "driver_versions": sorted({item[0] for item in rows}),
            "minimum_memory_mib": min(item[1] for item in rows),
        }
    except (OSError, ValueError, UnicodeError):
        return None


def _gpu_check(
    query: bool,
    executable: Path | None,
    probe: GpuProbe | None,
) -> RuntimeCheck:
    if not query:
        return RuntimeCheck(
            "gpu_driver_metadata", RuntimeCheckStatus.UNVERIFIED,
            "gpu_driver_query_was_not_requested",
        )
    if probe is None and executable is None:
        return RuntimeCheck(
            "gpu_driver_metadata", RuntimeCheckStatus.MISSING,
            "trusted_nvidia_smi_executable_was_not_declared",
        )
    observed = probe() if probe is not None else _default_gpu_probe(executable)
    if observed is None:
        return RuntimeCheck(
            "gpu_driver_metadata", RuntimeCheckStatus.MISSING,
            "bounded_nvidia_driver_query_is_unavailable",
        )
    count = observed.get("gpu_count")
    versions = observed.get("driver_versions")
    memory = observed.get("minimum_memory_mib")
    if (
        type(count) is not int
        or not 1 <= count <= _MAX_GPU_ROWS
        or not isinstance(versions, (list, tuple))
        or not 1 <= len(versions) <= _MAX_GPU_ROWS
        or any(
            not isinstance(item, str) or _DRIVER.fullmatch(item) is None
            for item in versions
        )
        or type(memory) is not int
        or not 1 <= memory <= 10**12
    ):
        return RuntimeCheck(
            "gpu_driver_metadata", RuntimeCheckStatus.UNVERIFIED,
            "gpu_driver_metadata_was_malformed",
        )
    value = {
        "gpu_count": count,
        "driver_versions": sorted(set(versions)),
        "minimum_memory_mib": memory,
    }
    return RuntimeCheck(
        "gpu_driver_metadata", RuntimeCheckStatus.VERIFIED,
        "bounded_read_only_driver_metadata_query_succeeded", value,
    )


def run_runtime_doctor(
    request: RuntimeDoctorRequest,
    *, python_probe: PythonProbe | None = None, gpu_probe: GpuProbe | None = None,
) -> RuntimeDoctorReport:
    """Inspect declared metadata while keeping optional runtime code unloaded."""

    lock_check, expectations, rejected = _load_expectations(request)
    distribution_check, summary = _distribution_check(
        expectations, rejected, request.metadata_roots
    )
    checks = [
        _python_check(request, python_probe or _default_python_probe),
        lock_check,
        distribution_check,
        *(_native_check(item) for item in request.native_libraries),
        _gpu_check(
            request.query_gpu_driver,
            request.nvidia_smi_executable,
            gpu_probe,
        ),
        RuntimeCheck(
            "native_abi_and_loadability", RuntimeCheckStatus.UNVERIFIED,
            "file_metadata_cannot_prove_native_abi_or_dll_loadability",
        ),
        RuntimeCheck(
            "cuda_runtime_compatibility", RuntimeCheckStatus.UNVERIFIED,
            "driver_metadata_cannot_prove_cuda_runtime_compatibility",
        ),
        RuntimeCheck(
            "real_runtime_and_generation", RuntimeCheckStatus.UNVERIFIED,
            "runtime_import_model_load_and_synthesis_are_out_of_scope",
        ),
    ]
    return RuntimeDoctorReport(tuple(checks), summary)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="neutral-tts-runtime-doctor",
        description=(
            "Inspect host-declared metadata using only explicitly trusted local "
            "executables; this is not a sandbox or an execution-readiness test."
        ),
    )
    parser.add_argument("--python-executable", required=True)
    parser.add_argument("--expected-python-version", required=True)
    parser.add_argument("--expected-python-implementation", default="cpython")
    parser.add_argument("--expected-pointer-bits", type=int, default=64)
    parser.add_argument("--requirements-lock", required=True)
    parser.add_argument("--requirements-lock-sha256", required=True)
    parser.add_argument("--metadata-root", action="append", required=True)
    parser.add_argument("--expected-distribution", action="append", default=[])
    parser.add_argument(
        "--native-library", action="append", nargs=3, default=[],
        metavar=("ID", "PATH", "SHA256_OR_PRESENCE_ONLY"),
    )
    parser.add_argument("--nvidia-smi-executable")
    parser.add_argument("--skip-gpu-query", action="store_true")
    return parser


def _request(args: argparse.Namespace) -> RuntimeDoctorRequest:
    native = tuple(
        NativeLibrarySpec(
            component, Path(path),
            None if digest == "presence-only" else digest.lower(),
        )
        for component, path, digest in args.native_library
    )
    return RuntimeDoctorRequest(
        python_executable=Path(args.python_executable),
        expected_python_version=args.expected_python_version,
        expected_python_implementation=args.expected_python_implementation,
        expected_pointer_bits=args.expected_pointer_bits,
        requirements_lock=Path(args.requirements_lock),
        expected_requirements_sha256=args.requirements_lock_sha256.lower(),
        metadata_roots=tuple(Path(value) for value in args.metadata_root),
        additional_requirements=tuple(args.expected_distribution),
        native_libraries=native,
        query_gpu_driver=not args.skip_gpu_query,
        nvidia_smi_executable=(
            Path(args.nvidia_smi_executable)
            if args.nvidia_smi_executable is not None
            else None
        ),
    )


def main(
    argv: list[str] | None = None,
    *, python_probe: PythonProbe | None = None, gpu_probe: GpuProbe | None = None,
) -> int:
    args = _parser().parse_args(argv)
    try:
        report = run_runtime_doctor(
            _request(args), python_probe=python_probe, gpu_probe=gpu_probe
        )
        payload = report.to_dict()
        code = 2 if report.status == "NOT_READY" else 3
    except Exception:
        payload = {
            "schema_version": "neutral-tts-runtime-doctor-v1",
            "scope": "MODEL_FREE_METADATA_ONLY",
            "status": "ERROR",
            "execution_ready": False,
            "error": {
                "code": "DIAGNOSTIC_REJECTED",
                "message": "The runtime diagnostic could not be completed.",
            },
        }
        code = 2
    print(json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")))
    return code


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())


__all__ = [
    "NativeLibrarySpec", "RuntimeCheck", "RuntimeCheckStatus",
    "RuntimeDoctorReport", "RuntimeDoctorRequest", "main", "run_runtime_doctor",
]
