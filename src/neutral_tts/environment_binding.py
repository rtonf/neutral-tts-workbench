"""Content identity for installed code and native files, without importing them.

This detects environment drift. It is not a dependency license approval or a
substitute for a reproducible installation lock and actual import validation.
"""
from __future__ import annotations

import importlib.metadata
import hashlib
import json
import sys
from pathlib import Path

from .resilience import _file_digest


PACKAGES = ("transformers", "tokenizers", "irodori-tts", "silentcipher", "torch", "torchaudio",
            "numpy", "safetensors", "soundfile", "scipy", "einops", "omegaconf", "sentencepiece", "protobuf")
SUFFIXES = {".py", ".pyd", ".dll", ".so", ".dylib", ".json", ".yaml", ".yml"}


def environment_digest():
    entries = [("python", sys.version, _file_digest(Path(sys.executable).absolute()))]
    own = Path(__file__).absolute().parent
    for path in sorted(own.glob("*.py")):
        entries.append(("application", path.name, _file_digest(path)))
    for name in PACKAGES:
        try:
            distribution = importlib.metadata.distribution(name)
        except importlib.metadata.PackageNotFoundError:
            entries.append((name, "absent"))
            continue
        entries.append((name, distribution.version))
        files = distribution.files
        if files is None:
            raise ValueError("Installed package file inventory is unavailable.")
        for relative in sorted(files, key=str):
            path = Path(distribution.locate_file(relative)).absolute()
            if path.suffix.lower() in SUFFIXES:
                entries.append((name, str(relative).replace("\\", "/"), _file_digest(path)))
    return hashlib.sha256(json.dumps(entries, ensure_ascii=True, sort_keys=True).encode()).hexdigest()
