"""Shared lazy runtime binding for callable and module entry points."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from .planning import normalize_public_id


@dataclass(frozen=True, slots=True)
class RealRuntimeBinding:
    """Public-safe identities plus lazy host objects for one real run.

    Constructing a binding must not create an inference runtime; only the
    backend's generate operation may do that.
    """

    token_counter: Callable[[str], int]
    backend: object
    tokenizer_identity: str
    runtime_config_digest: str
    voice_rights_manifest_sha256: str
    device_class: str = "unknown-device"

    def __post_init__(self) -> None:
        if not callable(self.token_counter):
            raise TypeError("real token counter must be callable")
        object.__setattr__(
            self, "tokenizer_identity",
            normalize_public_id(self.tokenizer_identity, label="tokenizer identity"),
        )
        object.__setattr__(
            self, "device_class", normalize_public_id(self.device_class, label="device class"),
        )
        for value in (self.runtime_config_digest, self.voice_rights_manifest_sha256):
            if not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
                raise ValueError("real runtime binding digest is invalid")
