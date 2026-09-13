"""Content identity for installed code and native files, without importing them.

This detects environment drift. It is not a dependency license approval or a
substitute for a reproducible installation lock and actual import validation.
"""
from __future__ import annotations

import importlib.metadata
import hashlib
import json
import sys
import os
import re
import sysconfig
import platform
import subprocess
import shutil
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .resilience import _file_digest


PACKAGES = ("transformers", "tokenizers", "irodori-tts", "silentcipher", "audioseal", "dacvae", "descript-audiotools", "torch", "torchaudio",
            "numpy", "safetensors", "soundfile", "scipy", "einops", "omegaconf", "sentencepiece", "protobuf")
SUFFIXES = {".py", ".pyd", ".dll", ".so", ".dylib", ".json", ".yaml", ".yml", ".model", ".txt", ".pth"}


def _code_files(root):
    for directory, names, files in os.walk(root, followlinks=False):
        names[:] = sorted(n for n in names if n not in {"__pycache__", "site-packages", "test", "tests"})
        for name in sorted(files):
            path = Path(directory) / name
            if path.suffix.lower() in SUFFIXES:
                yield path


def _installed_closure():
    pending, seen = list(PACKAGES), set()
    while pending:
        name = re.sub(r"[-_.]+", "-", pending.pop()).lower()
        if name in seen:
            continue
        seen.add(name)
        try:
            distribution = importlib.metadata.distribution(name)
        except importlib.metadata.PackageNotFoundError:
            yield name, None
            continue
        yield name, distribution
        if distribution.requires:
            # Installed inference/tokenizer packages already require packaging.
            # Markers are evaluated for this actual interpreter and platform.
            from packaging.requirements import Requirement
            for value in distribution.requires:
                requirement = Requirement(value)
                if requirement.marker is None or requirement.marker.evaluate({"extra": ""}):
                    pending.append(requirement.name)


def _host_identity():
    # Retain only a digest in receipts. Device/driver identity matters when a
    # private history backup is moved to another host or its GPU is replaced.
    identity = [platform.node(), platform.platform(), platform.machine(), platform.processor()]
    executable = shutil.which("nvidia-smi")
    if executable:
        result = subprocess.run(
            [executable, "--query-gpu=uuid,name,driver_version,memory.total", "--format=csv,noheader,nounits"],
            capture_output=True, timeout=15, check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if result.returncode != 0 or len(result.stdout) > 65536:
            raise ValueError("GPU environment identity is unavailable.")
        identity.append(result.stdout.decode("utf-8", "strict").strip())
    return hashlib.sha256(json.dumps(identity, ensure_ascii=True).encode()).hexdigest()


def environment_digest():
    entries = [("python", sys.version, _file_digest(Path(sys.executable).absolute()))]
    entries.append(("host", _host_identity()))
    own = Path(__file__).absolute().parent
    for path in sorted(own.glob("*.py")):
        entries.append(("application", path.name, _file_digest(path)))
    identities = {}
    for name, distribution in _installed_closure():
        if distribution is None:
            entries.append((name, "absent"))
            continue
        entries.append((name, distribution.version))
        files = distribution.files
        if files is None:
            raise ValueError("Installed package file inventory is unavailable.")
        package_roots = set()
        seen_roots = set()
        for relative in sorted(files, key=str):
            path = Path(distribution.locate_file(relative)).absolute()
            if path.suffix.lower() in SUFFIXES or path.name in {"METADATA", "RECORD", "WHEEL"}:
                identities[path] = (name, str(relative).replace("\\", "/"))
            first = str(relative).replace("\\", "/").split("/")[0]
            if first not in seen_roots and first not in {".", ".."} and not first.endswith((".dist-info", ".data")):
                seen_roots.add(first)
                folder = Path(distribution.locate_file(first)).absolute()
                if folder.is_dir():
                    package_roots.add(folder)
        # Include added/unrecorded code and data inside selected package roots.
        # A stale wheel RECORD must not hide an injected module or native file.
        for root in package_roots:
            for path in _code_files(root):
                identities[path] = (name, root.name + "/" + path.relative_to(root).as_posix())
    stdlib = Path(sysconfig.get_path("stdlib")).absolute()
    for path in _code_files(stdlib):
        identities[path] = ("python-stdlib", path.relative_to(stdlib).as_posix())
    for root in (Path(sys.base_prefix) / "DLLs", Path(sys.base_prefix)):
        if root.is_dir():
            for path in sorted(root.iterdir()):
                if path.is_file() and path.suffix.lower() in {".dll", ".pyd"}:
                    identities[path] = ("python-native", path.name)
    ordered = sorted(identities.items(), key=lambda item: item[1])
    # Read every byte on every call; bounded I/O concurrency only, no cache of
    # previous approvals, and the existing replacement-during-read checks stay.
    with ThreadPoolExecutor(max_workers=4) as pool:
        for (_, label), digest in zip(ordered, pool.map(_file_digest, (p for p, _ in ordered))):
            entries.append((*label, digest))
    return hashlib.sha256(json.dumps(sorted(entries, key=str), ensure_ascii=True, sort_keys=True).encode()).hexdigest()
