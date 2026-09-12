from __future__ import annotations

import ast
import hashlib
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from neutral_tts.runtime_doctor import (
    NativeLibrarySpec,
    RuntimeCheckStatus,
    RuntimeDoctorRequest,
    _BoundedProcessResult,
    _PythonObservation,
    _default_gpu_probe,
    _default_python_probe,
    _native_check,
    _run_bounded_process,
    _scan_metadata,
    main,
    run_runtime_doctor,
)


VCS_COMMIT = "a" * 40


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_distribution(
    root: Path,
    name: str,
    version: str,
    *,
    direct_url: dict[str, object] | None = None,
) -> None:
    directory = root / f"{name.replace('-', '_')}-{version}.dist-info"
    directory.mkdir(parents=True)
    (directory / "METADATA").write_text(
        f"Metadata-Version: 2.4\nName: {name}\nVersion: {version}\n",
        encoding="utf-8",
    )
    if direct_url is not None:
        (directory / "direct_url.json").write_text(
            json.dumps(direct_url), encoding="utf-8"
        )


def _request(
    root: Path,
    lock_text: str,
    *,
    additional_requirements: tuple[str, ...] = (),
    native_libraries: tuple[NativeLibrarySpec, ...] = (),
    expected_lock_sha256: str | None = None,
    query_gpu_driver: bool = True,
) -> RuntimeDoctorRequest:
    executable = root / "runtime" / "python.exe"
    executable.parent.mkdir(parents=True, exist_ok=True)
    executable.write_bytes(b"synthetic-python")
    lock = root / "requirements.lock.txt"
    lock.write_text(lock_text, encoding="utf-8")
    metadata = root / "site-packages"
    metadata.mkdir(exist_ok=True)
    return RuntimeDoctorRequest(
        python_executable=executable,
        expected_python_version="3.13.1",
        requirements_lock=lock,
        expected_requirements_sha256=expected_lock_sha256 or _sha256(lock),
        metadata_roots=(metadata,),
        additional_requirements=additional_requirements,
        native_libraries=native_libraries,
        query_gpu_driver=query_gpu_driver,
    )


def _matching_python(request: RuntimeDoctorRequest) -> _PythonObservation:
    return _PythonObservation(
        request.python_executable,
        request.expected_python_version,
        request.expected_python_implementation,
        request.expected_pointer_bits,
    )


class RuntimeDoctorTests(unittest.TestCase):
    def test_matching_metadata_is_bounded_but_not_execution_readiness(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name).absolute()
            metadata = root / "site-packages"
            metadata.mkdir()
            _write_distribution(metadata, "Alpha_Pkg", "1.2.3")
            _write_distribution(
                metadata,
                "Vcs.Pkg",
                "0.4.0",
                direct_url={
                    "url": "https://example.invalid/runtime.git",
                    "vcs_info": {"vcs": "git", "commit_id": VCS_COMMIT},
                },
            )
            _write_distribution(metadata, "synthetic-playback", "0.5.6")
            executable = root / "runtime" / "python.exe"
            executable.parent.mkdir()
            executable.write_bytes(b"synthetic-python")
            lock = root / "requirements.lock.txt"
            lock.write_text(
                "Alpha_Pkg==1.2.3\n"
                f"Vcs.Pkg @ git+https://example.invalid/runtime.git@{VCS_COMMIT}\n",
                encoding="utf-8",
            )
            native = root / "native.dll"
            native.write_bytes(b"MZ-synthetic-native")
            request = RuntimeDoctorRequest(
                python_executable=executable,
                expected_python_version="3.13.1",
                requirements_lock=lock,
                expected_requirements_sha256=_sha256(lock),
                metadata_roots=(metadata,),
                additional_requirements=("synthetic-playback==0.5.6",),
                native_libraries=(
                    NativeLibrarySpec("playback", native, _sha256(native)),
                ),
            )

            report = run_runtime_doctor(
                request,
                python_probe=lambda _path: _matching_python(request),
                gpu_probe=lambda: {
                    "gpu_count": 1,
                    "driver_versions": ["610.74"],
                    "minimum_memory_mib": 8192,
                    "untrusted_extra": str(root),
                },
            )

            self.assertEqual(report.status, "INCOMPLETE")
            self.assertTrue(report.declared_metadata_matches)
            payload = report.to_dict()
            self.assertFalse(payload["execution_ready"])
            self.assertNotIn(str(root), json.dumps(payload, sort_keys=True))
            self.assertEqual(
                report.distribution_summary,
                {
                    "declared_total": 3,
                    "verified": 3,
                    "missing": 0,
                    "mismatch": 0,
                    "unverified": 0,
                    "unexpected": 0,
                },
            )
            statuses = {check.check_id: check.status for check in report.checks}
            self.assertIs(statuses["python_runtime"], RuntimeCheckStatus.VERIFIED)
            self.assertIs(statuses["requirements_lock"], RuntimeCheckStatus.VERIFIED)
            self.assertIs(
                statuses["installed_distributions"], RuntimeCheckStatus.VERIFIED
            )
            self.assertIs(
                statuses["native_library:playback"], RuntimeCheckStatus.VERIFIED
            )
            self.assertIs(
                statuses["gpu_driver_metadata"], RuntimeCheckStatus.VERIFIED
            )
            self.assertIs(
                statuses["cuda_runtime_compatibility"],
                RuntimeCheckStatus.UNVERIFIED,
            )
            self.assertIs(
                statuses["real_runtime_and_generation"],
                RuntimeCheckStatus.UNVERIFIED,
            )

    def test_missing_and_mismatched_inputs_remain_distinct(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name).absolute()
            request = _request(root, "alpha==1.2.3\nmissing-pkg==2.0\n")
            _write_distribution(root / "site-packages", "alpha", "9.9.9")
            missing_native = root / "absent.dll"
            request = RuntimeDoctorRequest(
                python_executable=request.python_executable,
                expected_python_version=request.expected_python_version,
                requirements_lock=request.requirements_lock,
                expected_requirements_sha256=request.expected_requirements_sha256,
                metadata_roots=request.metadata_roots,
                native_libraries=(NativeLibrarySpec("required-native", missing_native),),
            )
            observed = _PythonObservation(
                request.python_executable, "3.12.9", "cpython", 64
            )

            report = run_runtime_doctor(
                request,
                python_probe=lambda _path: observed,
                gpu_probe=lambda: None,
            )

            self.assertEqual(report.status, "NOT_READY")
            self.assertFalse(report.declared_metadata_matches)
            self.assertEqual(report.distribution_summary["mismatch"], 1)
            self.assertEqual(report.distribution_summary["missing"], 1)
            checks = {check.check_id: check for check in report.checks}
            self.assertIs(
                checks["python_runtime"].status, RuntimeCheckStatus.MISMATCH
            )
            self.assertIs(
                checks["installed_distributions"].status,
                RuntimeCheckStatus.MISMATCH,
            )
            issues = checks["installed_distributions"].details["issues"]
            self.assertEqual(
                {(item["component"], item["status"]) for item in issues},
                {("alpha", "MISMATCH"), ("missing-pkg", "MISSING")},
            )
            self.assertIs(
                checks["native_library:required-native"].status,
                RuntimeCheckStatus.MISSING,
            )
            self.assertIs(
                checks["gpu_driver_metadata"].status, RuntimeCheckStatus.MISSING
            )

    def test_vcs_commit_mismatch_and_missing_direct_url_are_reported(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name).absolute()
            request = _request(
                root,
                f"first @ git+https://example.invalid/first.git@{VCS_COMMIT}\n"
                f"second @ git+https://example.invalid/second.git@{VCS_COMMIT}\n",
                query_gpu_driver=False,
            )
            metadata = root / "site-packages"
            _write_distribution(
                metadata,
                "first",
                "1.0",
                direct_url={
                    "url": "https://example.invalid/first.git",
                    "vcs_info": {"vcs": "git", "commit_id": "b" * 40},
                },
            )
            _write_distribution(metadata, "second", "1.0")

            report = run_runtime_doctor(
                request,
                python_probe=lambda _path: _matching_python(request),
            )

            self.assertEqual(report.distribution_summary["mismatch"], 1)
            self.assertEqual(report.distribution_summary["unverified"], 1)
            check = next(
                item for item in report.checks if item.check_id == "installed_distributions"
            )
            self.assertIs(check.status, RuntimeCheckStatus.MISMATCH)

    def test_lock_digest_mismatch_prevents_distribution_claim(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name).absolute()
            request = _request(
                root,
                "alpha==1.2.3\n",
                expected_lock_sha256="0" * 64,
                query_gpu_driver=False,
            )
            _write_distribution(root / "site-packages", "alpha", "1.2.3")

            report = run_runtime_doctor(
                request,
                python_probe=lambda _path: _matching_python(request),
            )

            checks = {check.check_id: check for check in report.checks}
            self.assertIs(
                checks["requirements_lock"].status, RuntimeCheckStatus.MISMATCH
            )
            self.assertIs(
                checks["installed_distributions"].status,
                RuntimeCheckStatus.UNVERIFIED,
            )
            self.assertEqual(report.distribution_summary["declared_total"], 0)

    def test_unexpected_distribution_is_reported_as_unverified(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name).absolute()
            request = _request(root, "alpha==1.2.3\n", query_gpu_driver=False)
            metadata = root / "site-packages"
            _write_distribution(metadata, "alpha", "1.2.3")
            _write_distribution(metadata, "extra-tool", "9.0")

            report = run_runtime_doctor(
                request,
                python_probe=lambda _path: _matching_python(request),
            )

            self.assertEqual(report.distribution_summary["verified"], 1)
            self.assertEqual(report.distribution_summary["unexpected"], 1)
            self.assertEqual(report.distribution_summary["unverified"], 1)
            self.assertTrue(report.declared_metadata_matches)
            check = next(
                item for item in report.checks if item.check_id == "installed_distributions"
            )
            self.assertIs(check.status, RuntimeCheckStatus.UNVERIFIED)
            self.assertEqual(
                check.details["issues"],
                [
                    {
                        "component": "extra-tool",
                        "status": "UNVERIFIED",
                        "reason": "not_declared_by_lock",
                    }
                ],
            )

    def test_unsupported_lock_syntax_is_unverified_not_silently_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name).absolute()
            request = _request(
                root,
                "alpha>=1.2\n",
                query_gpu_driver=False,
            )

            report = run_runtime_doctor(
                request,
                python_probe=lambda _path: _matching_python(request),
            )

            check = next(
                item for item in report.checks if item.check_id == "installed_distributions"
            )
            self.assertIs(check.status, RuntimeCheckStatus.UNVERIFIED)
            self.assertEqual(check.details["rejected_entries"], 1)

    def test_unsafe_metadata_payload_is_rejected_before_parser(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name).absolute()
            metadata = root / "site-packages"
            metadata.mkdir()
            _write_distribution(metadata, "alpha", "1.0")

            from neutral_tts.runtime_doctor import _safe_path as real_safe_path

            def safety_check(path: Path, *, directory: bool = False) -> bool:
                if path.name == "METADATA":
                    return False
                return real_safe_path(path, directory=directory)

            with (
                patch(
                    "neutral_tts.runtime_doctor._safe_path",
                    side_effect=safety_check,
                ),
                patch("neutral_tts.runtime_doctor._inspect_regular_file") as reader,
            ):
                installed, errors = _scan_metadata((metadata,))

            self.assertIsNone(installed)
            self.assertEqual(errors, 1)
            reader.assert_not_called()

    def test_oversized_metadata_and_direct_url_are_rejected_before_read(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name).absolute()
            metadata = root / "site-packages"
            metadata.mkdir()
            _write_distribution(metadata, "large-metadata", "1.0")
            metadata_file = next(metadata.glob("*.dist-info")) / "METADATA"
            metadata_file.write_bytes(b"Name: large-metadata\nVersion: 1.0\n" + b"x" * 1048576)

            installed, errors = _scan_metadata((metadata,))

            self.assertEqual(installed, {})
            self.assertEqual(errors, 1)

            for child in metadata.iterdir():
                for payload in child.iterdir():
                    payload.unlink()
                child.rmdir()
            _write_distribution(
                metadata,
                "large-direct",
                "1.0",
                direct_url={"url": "https://example.invalid/source.git"},
            )
            direct_url = next(metadata.glob("*.dist-info")) / "direct_url.json"
            direct_url.write_bytes(b"{}" + b" " * 65536)

            installed, errors = _scan_metadata((metadata,))

            self.assertEqual(installed, {})
            self.assertEqual(errors, 1)

    def test_additional_requirement_limits_are_enforced_at_request_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name).absolute()
            with self.assertRaises(ValueError):
                _request(root, "alpha==1.0\n", additional_requirements=("x" * 2049,))

    def test_unverified_declared_probe_does_not_claim_metadata_match(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name).absolute()
            request = _request(root, "alpha==1.0\n", query_gpu_driver=False)
            _write_distribution(root / "site-packages", "alpha", "1.0")

            report = run_runtime_doctor(
                request,
                python_probe=lambda _path: None,
            )

            self.assertEqual(report.status, "INCOMPLETE")
            self.assertFalse(report.declared_metadata_matches)

    def test_missing_trusted_gpu_tool_is_not_path_resolved(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name).absolute()
            request = _request(root, "alpha==1.0\n")
            _write_distribution(root / "site-packages", "alpha", "1.0")

            report = run_runtime_doctor(
                request,
                python_probe=lambda _path: _matching_python(request),
            )

            check = next(
                item for item in report.checks if item.check_id == "gpu_driver_metadata"
            )
            self.assertIs(check.status, RuntimeCheckStatus.MISSING)
            self.assertEqual(
                check.reason,
                "trusted_nvidia_smi_executable_was_not_declared",
            )

    def test_presence_only_native_check_does_not_claim_abi_compatibility(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name).absolute()
            native = root / "large-runtime.dll"
            native.write_bytes(b"MZ")
            request = _request(
                root,
                "alpha==1.0\n",
                native_libraries=(NativeLibrarySpec("cuda-core", native),),
                query_gpu_driver=False,
            )
            _write_distribution(root / "site-packages", "alpha", "1.0")

            report = run_runtime_doctor(
                request,
                python_probe=lambda _path: _matching_python(request),
            )

            checks = {check.check_id: check for check in report.checks}
            presence = checks["native_library:cuda-core"]
            self.assertIs(presence.status, RuntimeCheckStatus.VERIFIED)
            self.assertEqual(presence.details["evidence"], "presence_only")
            self.assertIs(
                checks["native_abi_and_loadability"].status,
                RuntimeCheckStatus.UNVERIFIED,
            )
            self.assertFalse(report.to_dict()["execution_ready"])

    def test_changed_native_file_is_unverified_without_unbounded_hash(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            native = Path(name).absolute() / "native.dll"
            native.write_bytes(b"MZ-synthetic")
            spec = NativeLibrarySpec("native", native, hashlib.sha256(b"x").hexdigest())

            with patch(
                "neutral_tts.runtime_doctor._inspect_regular_file",
                return_value=("changed", None),
            ):
                check = _native_check(spec)

            self.assertIs(check.status, RuntimeCheckStatus.UNVERIFIED)
            self.assertEqual(check.reason, "native_library_could_not_be_read_safely")

    def test_cli_output_omits_private_paths_and_exception_text(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sensitive-marker-") as name:
            root = Path(name).absolute()
            request = _request(root, "alpha==1.0\n", query_gpu_driver=False)
            _write_distribution(root / "site-packages", "alpha", "1.0")
            output = io.StringIO()
            with redirect_stdout(output):
                code = main(
                    [
                        "--python-executable",
                        str(request.python_executable),
                        "--expected-python-version",
                        request.expected_python_version,
                        "--requirements-lock",
                        str(request.requirements_lock),
                        "--requirements-lock-sha256",
                        request.expected_requirements_sha256,
                        "--metadata-root",
                        str(request.metadata_roots[0]),
                        "--skip-gpu-query",
                    ],
                    python_probe=lambda _path: _matching_python(request),
                )
            payload = json.loads(output.getvalue())

            self.assertEqual(code, 3)
            self.assertEqual(payload["status"], "INCOMPLETE")
            self.assertFalse(payload["execution_ready"])
            serialized = json.dumps(payload, sort_keys=True)
            self.assertNotIn("sensitive-marker", serialized)
            self.assertNotIn(str(root), serialized)
            self.assertNotIn("python.exe", serialized)
            self.assertNotIn("requirements.lock.txt", serialized)

    def test_python_probe_uses_fixed_isolated_standard_library_command(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            executable = Path(name).absolute() / "runtime" / "python.exe"
            executable.parent.mkdir()
            executable.write_bytes(b"synthetic-python")
            output = json.dumps(
                {
                    "bits": 64,
                    "executable": str(executable),
                    "implementation": "cpython",
                    "version": "3.13.1",
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            completed = _BoundedProcessResult(
                returncode=0,
                stdout=(output + "\n").encode("utf-8"),
                stderr=b"",
            )
            with patch(
                "neutral_tts.runtime_doctor._run_bounded_process",
                return_value=completed,
            ) as invoked:
                observation = _default_python_probe(executable)

            self.assertIsNotNone(observation)
            command = invoked.call_args.args[0]
            options = invoked.call_args.kwargs
            self.assertEqual(command[0], str(executable))
            self.assertEqual(command[1:3], ["-I", "-S"])
            self.assertEqual(command[3], "-c")
            self.assertEqual(options["stdout_limit"], 4096)
            self.assertEqual(options["stderr_limit"], 1024)
            self.assertNotIn("PYTHONPATH", options["env"])
            self.assertNotIn("PYTHONHOME", options["env"])
            self.assertNotIn("VIRTUAL_ENV", options["env"])

    def test_bounded_process_rejects_stdout_overflow(self) -> None:
        result = _run_bounded_process(
            [
                sys.executable,
                "-I",
                "-S",
                "-c",
                "import os;os.write(1,b'x'*65536)",
            ],
            cwd=Path(sys.executable).parent,
            env=dict(os.environ),
            timeout_seconds=5,
            stdout_limit=128,
            stderr_limit=128,
        )

        self.assertIsNone(result)

    def test_gpu_probe_uses_explicit_path_fixed_argv_and_output_bounds(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            executable = Path(name).absolute() / "nvidia-smi.exe"
            executable.write_bytes(b"synthetic-trusted-host-tool")
            completed = _BoundedProcessResult(
                returncode=0,
                stdout=b"610.74, Synthetic GPU, 8192\n",
                stderr=b"",
            )
            with patch(
                "neutral_tts.runtime_doctor._run_bounded_process",
                return_value=completed,
            ) as invoked:
                result = _default_gpu_probe(executable)

            self.assertEqual(
                result,
                {
                    "gpu_count": 1,
                    "driver_versions": ["610.74"],
                    "minimum_memory_mib": 8192,
                },
            )
            command = invoked.call_args.args[0]
            options = invoked.call_args.kwargs
            self.assertEqual(command[0], str(executable))
            self.assertEqual(
                command[1:],
                [
                    "--query-gpu=driver_version,name,memory.total",
                    "--format=csv,noheader,nounits",
                ],
            )
            self.assertEqual(options["stdout_limit"], 8192)
            self.assertEqual(options["stderr_limit"], 2048)

    def test_module_has_no_optional_runtime_imports(self) -> None:
        source_path = Path(__file__).parents[1] / "src" / "neutral_tts" / "runtime_doctor.py"
        tree = ast.parse(source_path.read_text(encoding="utf-8"))
        imported_roots = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported_roots.update(alias.name.split(".", 1)[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported_roots.add(node.module.split(".", 1)[0])
        self.assertTrue(
            {"torch", "transformers", "silentcipher", "irodori_tts"}.isdisjoint(
                imported_roots
            )
        )


if __name__ == "__main__":
    unittest.main()
