"""Explicit alternative watermark, using pinned tensor-only weights and 16 kHz.

This format has a different detector and payload from SilentCipher. The fixed
architecture follows the official AudioSeal 16-bit model cards. Upstream code
and model weights retain their MIT license, including their attribution.
"""
from __future__ import annotations

from pathlib import Path

from .local_runtime import _verify_asset, LocalRuntimeError


CONVERTED = {
    "generator_base.safetensors": {"size": 58730640, "sha256": "7ff7ba741df5ab9e09ab88ad1e2f0b9304bb3afca92d4f54989ced665fa8515f"},
    "detector_base.safetensors": {"size": 34602488, "sha256": "9559b48a147b0ec06cd71cfd4ce946f336835756da063a12010294e943c9c459"},
}
PAYLOAD = (0, 1, 0, 0, 1, 1, 1, 0, 0, 1, 0, 1, 0, 1, 0, 0)


def load_model(directory, *, detector=False):
    """No checkpoint loader, network fallback, or safe-global expansion."""
    import torch
    from safetensors.torch import load as load_tensors
    from audioseal.builder import (AudioSealWMConfig, AudioSealDetectorConfig, SEANetConfig,
                                   DecoderConfig, create_generator, create_detector)
    from audioseal.loader import convert_state_dict_for_scriptable_model
    from .local_runtime import _read
    name = "detector_base.safetensors" if detector else "generator_base.safetensors"
    path = Path(directory) / name
    record = CONVERTED[name]
    _verify_asset(path, record)
    state = load_tensors(_read(path, limit=record["size"], digest=record["sha256"]))
    # Explicit constructor values from fixed official model cards, not the
    # checkpoint's serialized configuration and not an arbitrary YAML resolver.
    backbone = SEANetConfig(channels=1, dimension=128, n_filters=32, n_residual_layers=1,
        ratios=[8, 5, 4, 2], activation="ELU", activation_params={"alpha": 1.0},
        norm="none", norm_params={}, kernel_size=7, last_kernel_size=7,
        residual_kernel_size=3, dilation_base=2, causal=False, pad_mode="constant",
        true_skip=True, compress=2, lstm=2, disable_norm_outer_blocks=0)
    if detector:
        model = create_detector(AudioSealDetectorConfig(nbits=16, seanet=backbone), device=torch.device("cpu"))
    else:
        decoder = DecoderConfig(final_activation=None, final_activation_params=None, trim_right_ratio=1.0)
        model = create_generator(AudioSealWMConfig(nbits=16, seanet=backbone, decoder=decoder), device=torch.device("cpu"))
    state = convert_state_dict_for_scriptable_model(state)
    normalized = {}
    for key, tensor in state.items():
        if key.endswith(".weight_g"):
            base = key[:-len("weight_g")]
            companion = base + "weight_v"
            if companion not in state:
                raise LocalRuntimeError("A watermark weight-normalization pair is missing.")
            normalized[base + "weight"] = torch._weight_norm(state[companion], tensor, 0)
        elif key.endswith(".weight_v"):
            if key[:-len("weight_v")] + "weight_g" not in state:
                raise LocalRuntimeError("A watermark weight-normalization pair is missing.")
        else:
            normalized[key] = tensor
    # Every expected parameter and buffer must be supplied. No partial or
    # missing-key acceptance is inherited from the upstream convenience loader.
    model.load_state_dict(normalized, strict=True)
    return model.eval()


class AudioSealWatermarker:
    def __init__(self, directory):
        self.model = load_model(directory)
        self.ready = True
        self.last_gains = []

    def encode_batch(self, audios, *, sample_rate):
        import torch
        from torchaudio.functional import resample
        if sample_rate not in {44100, 48000}:
            raise LocalRuntimeError("The watermark sample rate is unsupported.")
        result = []
        self.last_gains = []
        with torch.inference_mode():
            for audio in audios:
                vector = audio.detach().float().cpu().reshape(1, 1, -1)
                if vector.shape[-1] < sample_rate // 4 or vector.shape[-1] > 120 * sample_rate or not torch.isfinite(vector).all():
                    raise LocalRuntimeError("The watermark input is invalid or exceeds its duration bound.")
                low = resample(vector, sample_rate, 16000)
                message = torch.tensor([PAYLOAD], dtype=torch.int32)
                delta = self.model.get_watermark(low, message=message)
                high = resample(delta, 16000, sample_rate)[..., :vector.shape[-1]]
                if high.shape != vector.shape:
                    raise LocalRuntimeError("The watermark changed the output frame count.")
                marked = vector + high
                if not torch.isfinite(marked).all():
                    raise LocalRuntimeError("The watermarked output is non-finite.")
                # Linear gain applies to the complete marked waveform, retaining
                # sample relationships and reserving two percent PCM headroom.
                peak = float(marked.abs().max())
                gain = min(1.0, 0.98 / peak) if peak else 1.0
                marked = marked * gain
                self.last_gains.append(gain)
                result.append(marked.reshape(audio.shape))
        return result


def detect_samples(directory, samples, *, sample_rate):
    import torch
    from torchaudio.functional import resample
    if sample_rate not in {44100, 48000} or not 0 < len(samples) <= 120 * sample_rate:
        raise LocalRuntimeError("The detection input size or sample rate is unsupported.")
    with torch.inference_mode():
        model = load_model(directory, detector=True)
        audio = torch.tensor(samples, dtype=torch.float32).reshape(1, 1, -1)
        if not torch.isfinite(audio).all():
            raise LocalRuntimeError("The detection input is non-finite.")
        score, message = model.detect_watermark(resample(audio, sample_rate, 16000))
        return {"method": "audioseal-16bit", "watermarked_frame_fraction": float(score[0]),
                "payload_matches": tuple(message[0].tolist()) == PAYLOAD,
                "proves_identity_or_consent": False}


def detect_saved_wav(directory, record):
    """Inspect the exact history-bound PCM bytes in bounded detector windows."""
    import io
    import math
    import struct
    import wave
    import torch
    from torchaudio.functional import resample
    from .local_runtime import _read, _local_execution
    data = _read(record.output_path, limit=min(record.output_size, 192 * 1024**2), digest=record.output_sha256)
    with wave.open(io.BytesIO(data), "rb") as wav:
        if wav.getnchannels() != 1 or wav.getsampwidth() != 2 or wav.getframerate() not in {44100, 48000}:
            raise LocalRuntimeError("The saved audio format is unsupported.")
        rate, frames = wav.getframerate(), wav.getnframes()
        windows = []
        total_square, peak, clipped = 0.0, 0.0, 0
        with _local_execution(), torch.inference_mode():
            model = load_model(directory, detector=True)
            while wav.tell() < frames:
                raw = wav.readframes(min(20 * rate, frames - wav.tell()))
                values = [s[0] / 32768 for s in struct.iter_unpack("<h", raw)]
                if not values:
                    raise LocalRuntimeError("The saved audio is truncated.")
                total_square += sum(v * v for v in values)
                peak = max(peak, max(map(abs, values)))
                clipped += sum(abs(v) >= 32767 / 32768 for v in values)
                audio = torch.tensor(values, dtype=torch.float32).reshape(1, 1, -1)
                if audio.shape[-1] < rate // 4:
                    audio = torch.nn.functional.pad(audio, (0, rate // 4 - audio.shape[-1]))
                score, message = model.detect_watermark(resample(audio, rate, 16000))
                windows.append({"frames": len(values), "fraction": float(score[0]),
                                "payload_matches": tuple(message[0].tolist()) == PAYLOAD})
        return {"method": "audioseal-16bit", "watermarked_frame_fraction": sum(v["fraction"] * v["frames"] for v in windows) / frames,
                "payload_matches": all(v["payload_matches"] for v in windows), "windows": windows,
                "sha256": record.output_sha256, "sample_rate": rate, "seconds": frames / rate,
                "peak": peak, "rms": math.sqrt(total_square / frames), "clipped_samples": clipped,
                "proves_identity_or_consent": False}
