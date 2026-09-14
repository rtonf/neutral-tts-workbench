"""Fixed inference setup with resumable acquisition and private configuration."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import urllib.request
import urllib.error
import uuid
from pathlib import Path

from .cli import _safe_directory
from .fs_safety import DirectoryGuard, assert_safe_descendant, open_exclusive_binary
from .local_runtime import _read, _verify_asset, _absolute, _object, LocalRuntimeConfig
from .workbench import Workbench, WorkbenchError, read_private, write_private


ROOT = Path(__file__).absolute().parents[2]


class SetupDownloadError(WorkbenchError):
    pass


def _progress_object(data):
    if not 0 < len(data) <= 4096:
        raise WorkbenchError("Setup progress exceeds its size bound.")
    value = _object(data)
    if set(value) != {"phase", "completed", "total"} or value["phase"] not in {"sources", "wheels", "install", "assets", "configure", "complete"}:
        raise WorkbenchError("Setup progress has an invalid shape.")
    if any(type(value[k]) is not int for k in ("completed", "total")) or not 0 <= value["completed"] <= value["total"] <= 256:
        raise WorkbenchError("Setup progress has invalid counts.")
    return value


def _read_posix_progress(path):
    # A replaced leaf may name a newer snapshot while this descriptor still
    # reads the complete preceding one. Never follow a link or read a device.
    with DirectoryGuard(path.parent) as guard:
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=guard.fd)
        try:
            before = os.fstat(fd)
            if not stat.S_ISREG(before.st_mode) or not 0 < before.st_size <= 4096:
                raise WorkbenchError("Setup progress has an unsupported size or type.")
            data = os.read(fd, 4097)
            after = os.fstat(fd)
        finally:
            os.close(fd)
        identity = lambda item: (item.st_dev, item.st_ino, item.st_size, item.st_mtime_ns)
        if identity(before) != identity(after) or len(data) != before.st_size:
            raise WorkbenchError("Setup progress changed within the open snapshot.")
        guard.assert_unchanged()
        return _progress_object(data)


def read_progress(path):
    """Read advisory progress without blocking the worker's atomic replacement.

    This is not a configuration/admission reader. On Windows its handle shares
    deletion so an observer cannot interrupt a setup transaction's status write.
    """
    path = Path(path)
    # Do not stat the leaf through Python's Windows pathname helpers before
    # opening the sharing-enabled handle: that brief observer handle can itself
    # prevent the writer from replacing the status file.
    path = _absolute(str(path.parent)) / path.name
    if os.name != "nt":
        return _read_posix_progress(path)
    import ctypes
    import msvcrt
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel.CreateFileW
    create.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                      wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    create.restype = wintypes.HANDLE
    with DirectoryGuard(path.parent):
        handle = create(str(path), 0x80000000, 7, None, 3, 0x00200000, None)
        if handle in (None, ctypes.c_void_p(-1).value):
            raise OSError("Setup progress is not currently readable.")
        try:
            fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
        except BaseException:
            close = kernel.CloseHandle
            close.argtypes = [wintypes.HANDLE]
            close(handle)
            raise
        with os.fdopen(fd, "rb") as stream:
            data = stream.read(4097)
        return _progress_object(data)


def _open_download(url, opener):
    # Both hosts are official PyTorch distribution endpoints. A CDN rejection
    # may use the canonical endpoint for the same hash-pinned artifact.
    urls = [url]
    if url.startswith("https://download-r2.pytorch.org/"):
        urls.append(url.replace("https://download-r2.pytorch.org/", "https://download.pytorch.org/", 1))
    for selected in urls:
        for attempt in range(2):
            try:
                return opener(urllib.request.Request(selected, headers={"User-Agent": "Neutral-TTS-Setup/0.1"}), timeout=30)
            except (urllib.error.URLError, TimeoutError):
                if attempt == 1 and selected == urls[-1]:
                    raise SetupDownloadError("The fixed official download is unavailable. Retry setup.") from None


def catalog():
    result = {}
    for part in ("assets", "sources", "wheels", "notices"):
        path = ROOT / "config" / ("inference-" + part + ".lock.json")
        result[part] = json.loads(_read(path, limit=256 * 1024))
    return result


def _identity(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _download(target, record, *, opener=urllib.request.urlopen):
    """Verified no-replace promotion; partial downloads can be retried safely."""
    if target.exists():
        _verify_asset(target, {"size": record.get("size", target.stat().st_size), "sha256": record["sha256"]})
        return
    parent = _safe_directory(str(target.parent), label="setup asset folder")
    temporary = parent / (".download-" + uuid.uuid4().hex)
    bound = record.get("size", 4 * 1024**3)
    try:
        with DirectoryGuard(parent) as guard:
            with _open_download(record["url"], opener) as response, open_exclusive_binary(guard, temporary) as stream:
                digest = hashlib.sha256()
                count = 0
                while True:
                    block = response.read(min(1024 * 1024, bound + 1 - count))
                    if not block:
                        break
                    count += len(block)
                    if count > bound:
                        raise WorkbenchError("A download exceeds its fixed size bound.")
                    digest.update(block)
                    stream.write(block)
                stream.flush()
                os.fsync(stream.fileno())
            if digest.hexdigest() != record["sha256"] or ("size" in record and count != record["size"]):
                raise WorkbenchError("Downloaded content differs from the fixed catalog.")
            assert_safe_descendant(guard, target, allow_missing_leaf=True)
            os.link(temporary, target)
    finally:
        # Only the exact random temporary file created by this operation.
        with DirectoryGuard(parent) as guard:
            assert_safe_descendant(guard, temporary, allow_missing_leaf=True)
            if temporary.exists():
                temporary.unlink()


def _bytes_once(target, data):
    if target.exists():
        _verify_asset(target, {"size": len(data), "sha256": hashlib.sha256(data).hexdigest()})
        return
    _safe_directory(str(target.parent), label="setup generated file folder")
    temporary = target.parent / (".build-" + uuid.uuid4().hex)
    with DirectoryGuard(target.parent) as guard:
        try:
            with open_exclusive_binary(guard, temporary) as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            assert_safe_descendant(guard, target, allow_missing_leaf=True)
            os.link(temporary, target)
        finally:
            assert_safe_descendant(guard, temporary, allow_missing_leaf=True)
            if temporary.exists():
                temporary.unlink()


def _configure_owned(workbench, assets, selected, *, device, precision):
    """Bind the reviewed catalog and actual operator statements to exact bytes."""
    if device not in {"cpu", "cuda"} or precision not in {"fp32", "bf16"} or (device == "cpu" and precision != "fp32"):
        raise WorkbenchError("The chosen execution mode is unsupported.")
    if not selected.get("configured"):
        raise WorkbenchError("Register reference audio and its three permission scopes first.")
    from .audioseal_runtime import CONVERTED
    from .tensor_checkpoint import extract_safetensors
    records = catalog()["assets"]["assets"]
    inventory = []
    for record in records:
        source = assets / record["path"]
        _verify_asset(source, record)
        if record["role"] == "watermark_source":
            output = "audioseal/tensors/" + source.stem + ".safetensors"
            converted = CONVERTED[Path(output).name]
            target = assets / output
            if target.exists():
                _verify_asset(target, converted)
            else:
                raw = _read(source, limit=record["size"], digest=record["sha256"])
                tensor_bytes = extract_safetensors(raw, expected_sha256=record["sha256"])
                if len(tensor_bytes) != converted["size"] or hashlib.sha256(tensor_bytes).hexdigest() != converted["sha256"]:
                    raise WorkbenchError("The tensor conversion differs from the fixed result.")
                _bytes_once(target, tensor_bytes)
            inventory.append(dict(path=output, role="watermark", **converted))
        else:
            inventory.append({k: record[k] for k in ("path", "role", "size", "sha256")})
    configurations = _safe_directory(str(workbench.root / "local-runtime" / uuid.uuid4().hex), label="local runtime configuration")
    review = _read(ROOT / "docs" / "INFERENCE_COMPONENTS.md", limit=256 * 1024)
    review_path = configurations / "component-review.md"
    _bytes_once(review_path, review)
    catalog_path = configurations / "catalog.json"
    write_private(catalog_path, catalog())
    # This record describes the actual reviewed local execution, including
    # conditions. It does not attest to permissions beyond the voice selection.
    execution = configurations / "execution-review.json"
    write_private(execution, {"reviewed_use": "local_execution", "review_document": str(review_path),
        "review_sha256": hashlib.sha256(review).hexdigest(), "catalog": str(catalog_path),
        "catalog_sha256": hashlib.sha256(_read(catalog_path, limit=256 * 1024)).hexdigest(),
        "distribution": "Source layout only; downloaded runtime and voices are not bundled.",
        "watermark": "audioseal-16bit-cpu", "voice_permissions": selected["permissions"]})
    references = [{"path": r["path"], "sha256": r["sha256"]} for r in selected["references"]]
    evidence = Path(selected["selection_file"])
    from .reference_setup import SCOPES
    rights = {"schema_version": "neutral-local-rights-v1", "asset_inventory_sha256": _identity(inventory),
              "references": references}
    for scope in ("local_execution", *SCOPES):
        proof = execution if scope == "local_execution" else evidence
        rights[scope] = {"reviewed": True, "reviewed_use": scope, "unresolved": [],
                         "basis": "fixed_component_review" if scope == "local_execution" else "operator_statement",
                         "scope_statement": "Local execution subject to the retained component conditions." if scope == "local_execution" else selected["permissions"][scope],
                         "evidence_path": str(proof), "evidence_sha256": hashlib.sha256(_read(proof, limit=256 * 1024)).hexdigest()}
    rights_path = configurations / "rights.json"
    write_private(rights_path, rights)
    tokenizer = configurations / "tokenizer.json"
    model = next(r for r in records if r["role"] == "model")
    write_private(tokenizer, {"schema_version": "neutral-tokenizer-v1", "directory": str(assets / "tokenizer"),
        "files": [{"name": Path(r["path"]).name, "size": r["size"], "sha256": r["sha256"]} for r in records if r["role"] == "tokenizer"],
        "vocab_size": 102400, "bos_token_id": 1, "evidence": str(review_path),
        "evidence_sha256": hashlib.sha256(review).hexdigest(), "source": model["source"],
        "revision": model["revision"], "use_scope": "local_tokenization"})
    runtime = configurations / "runtime.json"
    write_private(runtime, {"schema_version": "neutral-local-runtime-v2", "asset_root": str(assets),
        "assets": inventory, "model": "model.safetensors", "codec": "codec/weights.pth",
        "tokenizer": "tokenizer", "watermark": "audioseal/tensors", "watermark_kind": "audioseal-16bit-cpu",
        "device": device, "precision": precision, "vocab_size": 102400, "bos_token_id": 1,
        "rights_manifest": str(rights_path), "rights_manifest_sha256": hashlib.sha256(_read(rights_path, limit=256 * 1024)).hexdigest(),
        "references": references})
    LocalRuntimeConfig(runtime)
    return {"runtime_config": str(runtime), "tokenizer_config": str(tokenizer),
            "watermark": "audioseal-16bit-cpu", "inference_accepted": False}


def configure_inference(data_root, *, asset_directory, device="cuda", precision="fp32"):
    from .reference_setup import _current_references_owned
    workbench = Workbench(Path(data_root))
    with workbench.owned():
        return _configure_owned(workbench, Path(asset_directory).absolute(),
                                _current_references_owned(workbench), device=device, precision=precision)


def _write_progress(status, phase, completed, total):
    import time
    for attempt in range(3):
        try:
            write_private(status, {"phase": phase, "completed": completed, "total": total})
            return
        except PermissionError as error:
            if getattr(error, "winerror", None) not in {5, 32}:
                raise
            if attempt < 2:
                time.sleep(0.02)
    # This advisory file cannot admit assets or change active configuration.
    # A Windows observer may temporarily retain the preceding snapshot.


def provision_inference(data_root, *, setup_directory, device="cuda", precision="fp32", conditions_accepted=False):
    if conditions_accepted is not True:
        raise WorkbenchError("Confirm the displayed component and watermark conditions first.")
    if os.name != "nt" or sys.version_info[:2] != (3, 13):
        raise WorkbenchError("The fixed inference setup requires Windows x64/Python 3.13.")
    import struct
    if struct.calcsize("P") != 8:
        raise WorkbenchError("A 64-bit Python installation is required.")
    from .reference_setup import _current_references_owned
    from .source_wheels import source_wheel
    workbench = Workbench(Path(data_root))
    with workbench.owned():
        selected = _current_references_owned(workbench)
        if not selected.get("configured"):
            raise WorkbenchError("Register reference audio and permissions before inference setup.")
        fixed = catalog()
        root = _safe_directory(str(setup_directory), label="inference setup")
        if root == workbench.root or root in workbench.root.parents:
            raise WorkbenchError("Choose a separate folder or a dedicated data subfolder for inference setup.")
        marker = root / ".neutral-inference-setup.json"
        expected = {"schema_version": "neutral-inference-setup-v1", "catalog_sha256": _identity(fixed)}
        if marker.exists():
            if read_private(marker) != expected:
                raise WorkbenchError("This setup folder belongs to another catalog. Select an empty folder.")
        elif any(root.iterdir()):
            raise WorkbenchError("Select an empty setup folder.")
        else:
            write_private(marker, expected)
        if shutil.disk_usage(root).free < 16 * 1024**3:
            raise WorkbenchError("Setup needs 16 GiB free space for downloads, installation and recovery.")
        status = workbench.root / "setup-progress.json"
        def progress(phase, completed, total):
            _write_progress(status, phase, completed, total)
        wheelhouse = _safe_directory(str(root / "wheels"), label="fixed wheel cache")
        source_area = _safe_directory(str(root / "sources"), label="fixed source cache")
        progress("sources", 0, len(fixed["sources"]["recipes"]))
        requirements = []
        for index, recipe in enumerate(fixed["sources"]["recipes"], 1):
            source = source_area / (recipe["id"] + (".zip" if recipe["format"] == "zip" else ".tar.gz"))
            _download(source, recipe)
            filename, wheel = source_wheel(_read(source, limit=recipe["size"], digest=recipe["sha256"]), recipe)
            if filename != recipe["wheel_file"] or hashlib.sha256(wheel).hexdigest() != recipe["wheel_sha256"]:
                raise WorkbenchError("The source wheel differs from the reviewed build.")
            _bytes_once(wheelhouse / filename, wheel)
            requirements.append(recipe["distribution"] + "==" + recipe["version"] + " --hash=sha256:" + recipe["wheel_sha256"])
            progress("sources", index, len(fixed["sources"]["recipes"]))
        for index, item in enumerate(fixed["wheels"]["wheels"], 1):
            progress("wheels", index - 1, len(fixed["wheels"]["wheels"]))
            _download(wheelhouse / item["filename"], item)
            requirements.append(item["name"] + "==" + item["version"] + " --hash=sha256:" + item["sha256"])
        requirements_path = root / "requirements.lock.txt"
        _bytes_once(requirements_path, ("\n".join(sorted(requirements)) + "\n").encode())
        progress("install", 0, 1)
        notices = _safe_directory(str(root / "notices"), label="fixed third-party license notices")
        for record in fixed["notices"]["notices"]:
            _download(notices / (record["id"] + "-LICENSE.txt"), record)
        environment = root / "environment"
        child_environment = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONUTF8="1",
                                 HF_HUB_DISABLE_TELEMETRY="1", DO_NOT_TRACK="1")
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        def run(args):
            subprocess.run(args, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           env=child_environment, creationflags=flags)
        run([sys.executable, "-I", "-m", "venv", str(environment)])
        executable = environment / "Scripts" / "python.exe"
        run([str(executable), "-I", "-m", "pip", "--isolated", "install", "--no-index", "--find-links", str(wheelhouse),
             "--only-binary=:all:", "--require-hashes", "--no-deps", "--no-compile", "--disable-pip-version-check", "-r", str(requirements_path)])
        run([str(executable), "-I", "-m", "pip", "check"])
        assets = _safe_directory(str(root / "assets"), label="fixed inference assets")
        for index, record in enumerate(fixed["assets"]["assets"], 1):
            progress("assets", index - 1, len(fixed["assets"]["assets"]))
            _download(assets / record["path"], record)
        progress("configure", 0, 1)
        result = _configure_owned(workbench, assets, selected, device=device, precision=precision)
        binding = {"python": str(executable), "sha256": hashlib.sha256(executable.read_bytes()).hexdigest(),
                   "lock_sha256": _identity(fixed)}
        # Commit interpreter selection only after all assets and configuration
        # are admitted. Failed setup preserves the previous active environment.
        write_private(workbench.root / "local-interpreter.json", binding)
        progress("complete", 1, 1)
        return dict(result, worker_python=str(executable), setup_directory=str(root))
