"""Model-free local tokenizer admission shared by preview and generation."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from .irodori_backend import IrodoriTokenizerCounter
from .local_runtime import (
    LocalRuntimeError, MAX_CONFIG_BYTES, _absolute, _digest, _local_execution,
    _object, _read, _verify_asset,
)
from .fs_safety import is_reparse


def digest_object(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                    ensure_ascii=True).encode()).hexdigest()


def tokenizer_identity(assets, vocab, bos):
    return "tokenizer-" + digest_object({
        "files": sorted((Path(path).name, rec["size"], rec["sha256"])
                        for path, rec in assets.items() if rec["role"] == "tokenizer"),
        "vocab": vocab, "bos": bos,
    })[:48]


@dataclass(frozen=True)
class TokenizerBinding:
    token_counter: object
    tokenizer_identity: str
    config_digest: str


def load_tokenizer(path: Path) -> TokenizerBinding:
    """Load a private tokenizer-only configuration; never inspect other assets.

    Evidence is a locally preserved review document, not a legal inference from
    a boolean. The setup catalog determines which official bytes may be acquired.
    """
    path = _absolute(str(path))
    raw = _read(path, limit=MAX_CONFIG_BYTES)
    data = _object(raw)
    if set(data) != {"schema_version", "directory", "files", "vocab_size", "bos_token_id",
                    "evidence", "evidence_sha256", "source", "revision", "use_scope"}:
        raise LocalRuntimeError("Tokenizer configuration fields are invalid.")
    if data["schema_version"] != "neutral-tokenizer-v1" or data["use_scope"] != "local_tokenization":
        raise LocalRuntimeError("Tokenizer configuration scope is unsupported.")
    if not isinstance(data["source"], str) or not data["source"].startswith("https://"):
        raise LocalRuntimeError("The official tokenizer source is missing.")
    revision = data["revision"]
    if not isinstance(revision, str) or len(revision) != 40 or any(c not in "0123456789abcdef" for c in revision):
        raise LocalRuntimeError("An immutable source revision is required.")
    evidence_path = _absolute(data["evidence"])
    evidence_hash = _digest(data["evidence_sha256"])
    _read(evidence_path, limit=MAX_CONFIG_BYTES, digest=evidence_hash)
    directory = _absolute(data["directory"])
    files = data["files"]
    if type(files) is not list or not 2 <= len(files) <= 16:
        raise LocalRuntimeError("Tokenizer file inventory is invalid.")
    assets = {}
    for item in files:
        if type(item) is not dict or set(item) != {"name", "size", "sha256"}:
            raise LocalRuntimeError("Tokenizer file entry is invalid.")
        name = item["name"]
        if name not in {"tokenizer.json", "tokenizer_config.json", "special_tokens_map.json", "added_tokens.json", "config.json", "tokenizer.model"}:
            raise LocalRuntimeError("Tokenizer file is not a supported asset.")
        if type(item["size"]) is not int or not 0 < item["size"] <= 64 * 1024**2:
            raise LocalRuntimeError("Tokenizer asset exceeds its size bound.")
        target = directory / name
        if target in assets:
            raise LocalRuntimeError("Tokenizer file is duplicated.")
        assets[target] = {"role": "tokenizer", "size": item["size"], "sha256": _digest(item["sha256"])}
    if any(directory / name not in assets for name in ("tokenizer.json", "tokenizer_config.json")):
        raise LocalRuntimeError("Required tokenizer files are missing.")

    def verify():
        _read(path, limit=MAX_CONFIG_BYTES, digest=hashlib.sha256(raw).hexdigest())
        _read(evidence_path, limit=MAX_CONFIG_BYTES, digest=evidence_hash)
        if any(is_reparse(child) or not child.is_file() or child not in assets for child in directory.iterdir()):
            raise LocalRuntimeError("Tokenizer directory contains unrecorded data.")
        for target, record in assets.items():
            _verify_asset(target, record)

    verify()
    counter = IrodoriTokenizerCounter.from_local_directory(
        directory, expected_vocab_size=data["vocab_size"], expected_bos_token_id=data["bos_token_id"])

    def count(text):
        with _local_execution():
            verify()
            result = counter(text)
            verify()
            return result

    return TokenizerBinding(count, tokenizer_identity(assets, data["vocab_size"], data["bos_token_id"]),
                            hashlib.sha256(raw).hexdigest())
