"""Pinned tokenizer-only acquisition into a user-selected local asset folder."""
from __future__ import annotations

import hashlib
import os
import shutil
import sys
import subprocess
import urllib.request
import uuid
from pathlib import Path

from .cli import _safe_directory
from .fs_safety import DirectoryGuard, assert_safe_descendant, open_exclusive_binary
from .local_runtime import _verify_asset
from .workbench import Workbench, WorkbenchError, write_private


def provision_tokenizer_environment(data_root, environment_directory):
    """Create/resume only an application-marked Windows Python 3.13 venv."""
    if os.name != "nt" or sys.version_info[:2] != (3, 13):
        raise WorkbenchError("The reviewed automatic tokenizer environment requires Windows/Python 3.13.")
    from .local_runtime import _read
    from .workbench import read_private
    lock = Path(__file__).absolute().parents[2] / "config" / "tokenizer-windows-py313.lock.txt"
    lock_bytes = _read(lock, limit=64 * 1024)
    lock_digest = hashlib.sha256(lock_bytes).hexdigest()
    workbench = Workbench(Path(data_root))
    with workbench.owned():
        root = _safe_directory(str(environment_directory), label="tokenizer environment")
        marker = root / ".neutral-tokenizer-environment.json"
        expected = {"schema_version": "neutral-tokenizer-environment-v1", "lock_sha256": lock_digest}
        if marker.exists():
            if read_private(marker) != expected:
                raise WorkbenchError("The selected environment belongs to another installation.")
        elif any(root.iterdir()):
            raise WorkbenchError("Select an empty environment folder.")
        else:
            write_private(marker, expected)
        if shutil.disk_usage(root).free < 700 * 1024**2:
            raise WorkbenchError("Tokenizer environment setup requires 700 MiB free space.")
        child_environment = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", HF_HUB_DISABLE_TELEMETRY="1")
        # The enclosing desktop worker owns all children with a Windows job.
        # Cancellation kills pip and venv children before releasing data ownership.
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        subprocess.run([sys.executable, "-I", "-m", "venv", str(root)], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=child_environment, creationflags=flags)
        executable = root / "Scripts" / "python.exe"
        subprocess.run([str(executable), "-I", "-m", "pip", "--isolated", "install",
                        "--index-url", "https://pypi.org/simple", "--require-hashes", "--only-binary=:all:",
                        "--no-deps", "--disable-pip-version-check", "-r", str(lock)], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=child_environment, creationflags=flags)
        subprocess.run([str(executable), "-I", "-m", "pip", "check"], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=flags)
        binding = {"python": str(executable), "sha256": hashlib.sha256(executable.read_bytes()).hexdigest(),
                   "lock_sha256": lock_digest}
        # Executable selection stays machine-local; it is never imported from an
        # archive. Restoring a backup cannot substitute an executable to launch.
        write_private(workbench.root / "local-interpreter.json", binding)
        return {"worker_python": str(executable), "lock_sha256": lock_digest}


# Reviewed official repository LICENSE and model card at this exact revision.
# No model, codec, inference engine or watermark payload is acquired here.
SOURCE = "https://huggingface.co/sbintuitions/modernbert-ja-310m"
REVISION = "77675fc96a7e445e982e2ba90246b816efc74ec6"
FILES = {
    "tokenizer.model": (1831879, "008293028e1a9d9a1038d9b63d989a2319797dfeaa03f171093a57b33a3a8277"),
    "tokenizer.json": (6724873, "0a94ac9a0a02c067bdef25b72ae9f4ee33f48f552e55988d444f6d25eeb1d062"),
    "tokenizer_config.json": (3806, "fbab9c962596d0c493da0a6c10e3183d71e3705ea63c2d8e57a2678a65543246"),
    "special_tokens_map.json": (968, "30bf8256f9a1eb3287af2a9b7940465e29a38aad4a459a3e773bb3a14bd34c0f"),
    "LICENSE": (1070, "284353c80e0d52c06e97af62fd40e4dd1d253fa28f5d1e756121ea1d5d441509"),
}


def environment():
    import platform
    import importlib.metadata
    packages = {}
    for name in ("transformers", "tokenizers", "torch", "torchaudio"):
        try:
            packages[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            packages[name] = None
    return {"os": platform.system(), "python": platform.python_version(), "packages": packages,
            "download_bytes": sum(size for size, _ in FILES.values()),
            "inference_requirements_verified": False, "signature": "unsigned"}


def acquire_tokenizer(data_root, asset_directory, *, opener=urllib.request.urlopen):
    workbench = Workbench(Path(data_root))
    with workbench.owned():
        assets = _safe_directory(str(asset_directory), label="asset directory")
        tokenizer = _safe_directory(str(assets / "tokenizer"), label="tokenizer directory")
        if shutil.disk_usage(assets).free < 2 * sum(size for size, _ in FILES.values()):
            raise WorkbenchError("Insufficient free space for tokenizer setup.")
        for name, (size, digest) in FILES.items():
            target = (assets if name == "LICENSE" else tokenizer) / name
            record = {"size": size, "sha256": digest}
            if os.path.lexists(target):
                _verify_asset(target, record)
                continue
            # A failed transfer leaves no final asset. Re-running verifies and
            # reuses complete files; it starts the failed file again.
            temporary = assets / (".download-" + uuid.uuid4().hex)
            with DirectoryGuard(assets) as guard, DirectoryGuard(target.parent) as target_guard:
                with opener(SOURCE + "/resolve/" + REVISION + "/" + name, timeout=30) as response:
                    with open_exclusive_binary(guard, temporary) as stream:
                        total = 0
                        observed = hashlib.sha256()
                        while True:
                            block = response.read(min(1024 * 1024, size + 1 - total))
                            if not block:
                                break
                            total += len(block)
                            if total > size:
                                raise WorkbenchError("Asset exceeds its pinned size.")
                            observed.update(block)
                            stream.write(block)
                        stream.flush()
                        os.fsync(stream.fileno())
                if total != size or observed.hexdigest() != digest:
                    raise WorkbenchError("Downloaded bytes differ from the fixed asset.")
                assert_safe_descendant(target_guard, target, allow_missing_leaf=True)
                os.link(temporary, target)
                temporary.unlink()
                _verify_asset(target, record)
        review = {
            "source": SOURCE, "revision": REVISION, "license": "MIT",
            "license_file": str(assets / "LICENSE"), "license_sha256": FILES["LICENSE"][1],
            "copyright": "2025 SB Intuitions", "reviewed_on": "2026-09-12",
            "reviewed_assets": {name: {"size": size, "sha256": digest} for name, (size, digest) in FILES.items()},
            "scope": "Local tokenization, including commercial workflows. Preserve the MIT notice with copies.",
            "unreviewed": ["TTS voice rights", "inference model", "codec", "watermark weights", "runtime redistribution"],
        }
        evidence = assets / "tokenizer-review.json"
        write_private(evidence, review)
        config = assets / "tokenizer-settings.json"
        write_private(config, {
            "schema_version": "neutral-tokenizer-v1", "directory": str(tokenizer),
            "files": [{"name": name, "size": size, "sha256": digest} for name, (size, digest) in FILES.items() if name != "LICENSE"],
            "vocab_size": 102400, "bos_token_id": 1,
            "evidence": str(evidence), "evidence_sha256": hashlib.sha256(evidence.read_bytes()).hexdigest(),
            "source": SOURCE, "revision": REVISION, "use_scope": "local_tokenization",
        })
        return {"tokenizer_config": str(config), "environment": environment(),
                "retained_license": str(assets / "LICENSE"), "inference_ready": False}
