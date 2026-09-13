"""Narrow adapters for the fixed engine; never use a packaged-object loader."""
from __future__ import annotations

import io
import wave
from contextlib import contextmanager
from pathlib import Path

from .local_runtime import LocalRuntimeError, MAX_REFERENCE_BYTES, _read


CODEC_ARGUMENTS = {"encoder_dim": 64, "encoder_rates": [2, 8, 10, 12], "latent_dim": 1024,
                   "decoder_dim": 1536, "decoder_rates": [12, 10, 8, 2], "n_codebooks": 16,
                   "codebook_size": 1024, "codebook_dim": 32, "quantizer_dropout": False,
                   "sample_rate": 48000}


@contextmanager
def codec_loader(config):
    import torch
    from dacvae import DACVAE
    original = DACVAE.__dict__["load"]

    def load(cls, path):
        if Path(path) != config.codec:
            raise LocalRuntimeError("The codec selection changed during load.")
        record = config.assets[config.codec]
        data = _read(config.codec, limit=record["size"], digest=record["sha256"])
        payload = torch.load(io.BytesIO(data), map_location="cpu", weights_only=True)
        if type(payload) is not dict or set(payload) != {"state_dict", "metadata"}:
            raise LocalRuntimeError("The codec checkpoint structure is unsupported.")
        if payload["metadata"].get("kwargs") != CODEC_ARGUMENTS:
            raise LocalRuntimeError("The codec architecture differs from the fixed configuration.")
        model = cls(**CODEC_ARGUMENTS)
        model.load_state_dict(payload["state_dict"], strict=True)
        model.metadata = payload["metadata"]
        return model

    DACVAE.load = classmethod(load)
    try:
        yield
    finally:
        DACVAE.load = original


@contextmanager
def reference_reader(module, references):
    """Decode only previously admitted PCM WAV bytes, with no native fallback."""
    import numpy as np
    import torch
    expected = dict(references)
    original = module._load_audio

    def load(path):
        target = Path(path)
        if target not in expected:
            raise LocalRuntimeError("The engine requested an unselected reference.")
        data = _read(target, limit=MAX_REFERENCE_BYTES, digest=expected[target])
        from .reference_setup import inspect_reference
        inspect_reference(data)
        with wave.open(io.BytesIO(data), "rb") as wav:
            rate = wav.getframerate()
            samples = np.frombuffer(wav.readframes(wav.getnframes()), dtype="<i2").astype(np.float32) / 32768
        return torch.from_numpy(samples).reshape(1, -1), rate

    module._load_audio = load
    try:
        yield
    finally:
        module._load_audio = original
