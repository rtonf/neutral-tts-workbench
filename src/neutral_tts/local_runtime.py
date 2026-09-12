"""Explicit local asset binding for the optional installed inference package.

Configuration and rights records are private operator inputs. Hash checks bind
reviewed bytes; they cannot establish that a rights declaration is legally valid.
The Python environment itself must be installed from a reviewed dependency lock.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import logging
import os
import stat
import wave
from contextlib import ExitStack, contextmanager, redirect_stderr, redirect_stdout
from pathlib import Path, PurePosixPath
from types import SimpleNamespace

from .fs_safety import DirectoryGuard, assert_safe_descendant, is_reparse
from .irodori_backend import IrodoriBackend, IrodoriBackendHost, IrodoriTokenizerCounter


SCHEMA = "neutral-local-runtime-v1"
MAX_CONFIG_BYTES = 256 * 1024
MAX_REFERENCE_BYTES = 6 * 1024 * 1024
_ROLES = {"model", "tokenizer", "codec", "watermark"}


class LocalRuntimeError(ValueError):
    """A local runtime input failed validation before model admission."""


def _digest(value: object) -> str:
    if type(value) is not str or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
        raise LocalRuntimeError("A SHA-256 binding is invalid.")
    return value


def _absolute(value: object) -> Path:
    if type(value) is not str or not Path(value).is_absolute():
        raise LocalRuntimeError("An explicit absolute local path is required.")
    path = Path(os.path.abspath(value))
    for part in (path, *path.parents):
        if is_reparse(part):
            raise LocalRuntimeError("A local path is redirected.")
    return path


def _read(path: Path, *, limit: int, digest: str | None = None) -> bytes:
    with DirectoryGuard(path.parent) as guard:
        assert_safe_descendant(guard, path)
        before = path.lstat()
        if not stat.S_ISREG(before.st_mode) or not 0 < before.st_size <= limit:
            raise LocalRuntimeError("A local file has an unsupported size or type.")
        with path.open("rb") as stream:
            opened = os.fstat(stream.fileno())
            data = stream.read(limit + 1)
            after = os.fstat(stream.fileno())
        current = path.lstat()
        identity = lambda item: (item.st_dev, item.st_ino, item.st_size, item.st_mtime_ns)
        if not (identity(before) == identity(opened) == identity(after) == identity(current)):
            raise LocalRuntimeError("A local file changed while reading.")
        assert_safe_descendant(guard, path)
        if len(data) != before.st_size or (digest and hashlib.sha256(data).hexdigest() != digest):
            raise LocalRuntimeError("A local file differs from its recorded bytes.")
        return data


def _object(data: bytes) -> dict:
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise LocalRuntimeError("A configuration key is duplicated.")
            result[key] = value
        return result

    result = json.loads(data, object_pairs_hook=unique)
    if type(result) is not dict:
        raise LocalRuntimeError("A configuration object is required.")
    return result


def _relative(root: Path, value: object) -> Path:
    if type(value) is not str or not value or "\\" in value or ":" in value:
        raise LocalRuntimeError("An asset path is invalid.")
    relative = PurePosixPath(value)
    if relative.is_absolute() or any(p in {"", ".", ".."} for p in value.split("/")):
        raise LocalRuntimeError("An asset path escapes its root.")
    return _absolute(str(root.joinpath(*relative.parts)))


def _verify_asset(path: Path, record: dict) -> None:
    # Stream large checkpoints; never deserialize them during admission.
    with DirectoryGuard(path.parent) as guard:
        assert_safe_descendant(guard, path)
        before = path.lstat()
        if not stat.S_ISREG(before.st_mode) or before.st_size != record["size"]:
            raise LocalRuntimeError("An asset has an unexpected size or type.")
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            opened = os.fstat(stream.fileno())
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
            after = os.fstat(stream.fileno())
        current = path.lstat()
        identity = lambda item: (item.st_dev, item.st_ino, item.st_size, item.st_mtime_ns)
        if not (identity(before) == identity(opened) == identity(after) == identity(current)):
            raise LocalRuntimeError("An asset changed during admission.")
        assert_safe_descendant(guard, path)
        if digest.hexdigest() != record["sha256"]:
            raise LocalRuntimeError("An asset differs from its recorded bytes.")


@contextmanager
def _local_execution():
    # This host is intended for a dedicated CLI process. These settings apply
    # to that process only; the user's network and host configuration are intact.
    values = {
        "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1",
        "HF_HUB_DISABLE_TELEMETRY": "1", "DO_NOT_TRACK": "1",
        "TORCH_FORCE_WEIGHTS_ONLY_LOAD": "1",
        "TORCH_FORCE_NO_WEIGHTS_ONLY_LOAD": "0",
    }
    before = {key: os.environ.get(key) for key in values}
    disabled = logging.root.manager.disable
    try:
        os.environ.update(values)
        logging.disable(logging.CRITICAL)
        with open(os.devnull, "w", encoding="utf-8") as quiet:
            with redirect_stdout(quiet), redirect_stderr(quiet):
                yield
    finally:
        logging.disable(disabled)
        for key, value in before.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


class _ReferenceLease:
    def __init__(self, paths, guard):
        self.paths = tuple(paths)
        self.guard = guard
        self.stack = ExitStack()
        self.active = False

    def __enter__(self):
        try:
            for parent in sorted({p for path in self.paths for p in path.parents}, key=str):
                self.stack.enter_context(DirectoryGuard(parent))
            self.active = True
            self.verify()
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def verify(self):
        if not self.active or tuple(self.guard()) != self.paths:
            raise LocalRuntimeError("Reference audio is no longer admitted.")

    def __exit__(self, *args):
        self.active = False
        return self.stack.__exit__(*args)


class _InstalledAdapter:
    def create_runtime(self, config):
        self.execution = _local_execution()
        self.execution.__enter__()
        try:
            config.verify()
            self.module = importlib.import_module("irodori_tts.inference_runtime")
            silentcipher = importlib.import_module("silentcipher")
            original = self.module.SilentCipherWatermarker

            def local_watermark(*, device, model_type):
                config.verify_role("watermark")
                return silentcipher.get_model(
                    model_type=model_type, device=device,
                    ckpt_path=str(config.watermark),
                    config_path=str(config.watermark / "hparams.yaml"),
                )

            self.module.SilentCipherWatermarker = type(
                "LocalWatermarker", (original,), {"_load_backend": staticmethod(local_watermark)}
            )
            try:
                key = self.module.RuntimeKey(
                    checkpoint=str(config.model), codec_repo=str(config.codec),
                    model_device=config.device, codec_device=config.device,
                    model_precision=config.precision, codec_precision=config.precision,
                    codec_deterministic_encode=True, codec_deterministic_decode=True,
                    compile_model=False, compile_dynamic=False,
                )
                return self.module.InferenceRuntime.from_key(key)
            finally:
                self.module.SilentCipherWatermarker = original
        except BaseException:
            self.execution.__exit__(None, None, None)
            raise

    @staticmethod
    def count_tokens(runtime, text):
        return runtime.tokenizer.encode(text).numel()

    @staticmethod
    def watermark_ready(runtime):
        return bool(runtime.watermarker is not None and runtime.watermarker.ready)

    @staticmethod
    def reference_lease(paths, *, reference_guard):
        return _ReferenceLease(paths, reference_guard)

    def synthesize(self, runtime, request, *, log_fn, reference_lease):
        reference_lease.verify()
        values = vars(request).copy()
        values["ref_wavs"] = [str(path) for path in values.pop("reference_paths")]
        return runtime.synthesize(self.module.SamplingRequest(**values), log_fn=None)

    def close_runtime(self, runtime):
        try:
            runtime.unload()
        finally:
            self.execution.__exit__(None, None, None)


class LocalRuntimeConfig:
    def __init__(self, path: Path):
        self.path = _absolute(str(path))
        raw = _read(self.path, limit=MAX_CONFIG_BYTES)
        self.digest = hashlib.sha256(raw).hexdigest()
        data = _object(raw)
        required = {"schema_version", "asset_root", "assets", "model", "codec",
                    "tokenizer", "watermark", "device", "precision", "vocab_size",
                    "bos_token_id", "rights_manifest", "rights_manifest_sha256", "references"}
        if set(data) != required or data["schema_version"] != SCHEMA:
            raise LocalRuntimeError("The local runtime configuration schema is unsupported.")
        self.root = _absolute(data["asset_root"])
        self.device, self.precision = data["device"], data["precision"]
        if self.device not in {"cpu", "cuda"} or self.precision not in {"fp32", "bf16"}:
            raise LocalRuntimeError("The runtime device or precision is unsupported.")
        if self.device == "cpu" and self.precision != "fp32":
            raise LocalRuntimeError("CPU execution requires fp32 precision.")
        self.vocab, self.bos = data["vocab_size"], data["bos_token_id"]
        if type(self.vocab) is not int or not 1 < self.vocab <= 1_000_000:
            raise LocalRuntimeError("The tokenizer vocabulary is invalid.")
        if type(self.bos) is not int or not 0 <= self.bos < self.vocab:
            raise LocalRuntimeError("The tokenizer BOS identity is invalid.")
        assets = data["assets"]
        if type(assets) is not list or not 1 <= len(assets) <= 256:
            raise LocalRuntimeError("The runtime asset inventory is invalid.")
        self.assets = {}
        for record in assets:
            if type(record) is not dict or set(record) != {"path", "role", "size", "sha256"}:
                raise LocalRuntimeError("An asset inventory entry is invalid.")
            target = _relative(self.root, record["path"])
            if target in self.assets or record["role"] not in _ROLES:
                raise LocalRuntimeError("An asset role or identity is invalid.")
            if type(record["size"]) is not int or not 0 < record["size"] <= 16 * 1024**3:
                raise LocalRuntimeError("An asset size is invalid.")
            _digest(record["sha256"])
            self.assets[target] = record
        self.model, self.codec, self.tokenizer, self.watermark = (
            _relative(self.root, data[key]) for key in ("model", "codec", "tokenizer", "watermark")
        )
        if self.model.suffix != ".safetensors" or self.codec.suffix != ".pth":
            raise LocalRuntimeError("The selected model or codec format is unsupported.")
        if self.tokenizer != self.model.parent / "tokenizer":
            raise LocalRuntimeError("Tokenizer assets must be beside the selected checkpoint.")
        selected = {self.model: "model", self.codec: "codec"}
        for directory, names, role in (
            (self.tokenizer, ("tokenizer.json", "tokenizer_config.json"), "tokenizer"),
            (self.watermark, ("hparams.yaml", "enc_c.ckpt", "dec_c.ckpt", "dec_m_0.ckpt"), "watermark"),
        ):
            if not directory.is_dir():
                raise LocalRuntimeError("A selected asset directory is unavailable.")
            for name in names:
                selected[directory / name] = role
            for child in directory.rglob("*"):
                if is_reparse(child) or (child.is_file() and child not in self.assets):
                    raise LocalRuntimeError("An asset directory contains unrecorded or redirected data.")
        if any(path not in self.assets or self.assets[path]["role"] != role for path, role in selected.items()):
            raise LocalRuntimeError("A selected asset is absent from the inventory.")
        self.rights_path = _absolute(data["rights_manifest"])
        self.rights_digest = _digest(data["rights_manifest_sha256"])
        rights = _object(_read(self.rights_path, limit=MAX_CONFIG_BYTES, digest=self.rights_digest))
        inventory_digest = hashlib.sha256(json.dumps(assets, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        if rights.get("schema_version") != "neutral-local-rights-v1" or rights.get("asset_inventory_sha256") != inventory_digest:
            raise LocalRuntimeError("The rights record does not bind this asset inventory.")
        for scope in ("local_execution", "recording_use", "voice_replication", "generated_audio_use"):
            entry = rights.get(scope)
            if type(entry) is not dict or entry.get("reviewed") is not True:
                raise LocalRuntimeError("A required rights review is incomplete.")
            _digest(entry.get("evidence_sha256"))
        references = data["references"]
        if type(references) is not list or not 1 <= len(references) <= 4 or rights.get("references") != references:
            raise LocalRuntimeError("Reference audio is not bound to the rights record.")
        self.references = []
        for reference in references:
            if type(reference) is not dict or set(reference) != {"path", "sha256"}:
                raise LocalRuntimeError("A reference audio record is invalid.")
            self.references.append((_absolute(reference["path"]), _digest(reference["sha256"])))
        self.verify()

    def verify_role(self, role):
        for path, record in self.assets.items():
            if record["role"] == role:
                _verify_asset(path, record)

    def verify_references(self):
        _read(self.rights_path, limit=MAX_CONFIG_BYTES, digest=self.rights_digest)
        for path, digest in self.references:
            data = _read(path, limit=MAX_REFERENCE_BYTES, digest=digest)
            import io
            with wave.open(io.BytesIO(data), "rb") as wav:
                if wav.getnchannels() != 1 or wav.getsampwidth() != 2 or wav.getframerate() not in {44100, 48000}:
                    raise LocalRuntimeError("Reference audio must be mono PCM16 at 44.1 or 48 kHz.")
                if not 0 < wav.getnframes() <= 30 * wav.getframerate():
                    raise LocalRuntimeError("Reference audio must be at most 30 seconds.")
                if len(wav.readframes(wav.getnframes())) != wav.getnframes() * 2:
                    raise LocalRuntimeError("Reference audio is truncated.")
        return tuple(path for path, _ in self.references)

    def verify(self):
        _read(self.path, limit=MAX_CONFIG_BYTES, digest=self.digest)
        for path, record in self.assets.items():
            _verify_asset(path, record)
        self.verify_references()


def load_local_runtime(path: Path):
    """Return a lazy binding after local byte/rights-record checks, without sampling."""
    from .runtime_binding import RealRuntimeBinding

    config = LocalRuntimeConfig(path)
    counter = IrodoriTokenizerCounter.from_local_directory(
        config.tokenizer, expected_vocab_size=config.vocab, expected_bos_token_id=config.bos
    )

    def count(text):
        with _local_execution():
            config.verify_role("tokenizer")
            return counter(text)

    host = IrodoriBackendHost(
        adapter_factory=_InstalledAdapter, runtime_resources=config,
        synthesis_request_factory=lambda **values: SimpleNamespace(**values),
        reference_paths=config.verify_references(), reference_guard=config.verify_references,
    )
    return RealRuntimeBinding(
        token_counter=count, backend=IrodoriBackend(host),
        tokenizer_identity=f"local-tokenizer-{config.digest[:16]}",
        runtime_config_digest=config.digest, voice_rights_manifest_sha256=config.rights_digest,
        device_class=f"local-{config.device}-{config.precision}",
    )
