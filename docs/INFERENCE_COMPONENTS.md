# Fixed inference components and permitted uses

日本語の[録音・声・生成物の利用条件](VOICE_USE.md)と[日本法・AI指針への対応](JAPAN_AI_COMPLIANCE.md)を併せて確認してください。
この部品確認は、上流モデルの全学習データの権利処理や個々の生成物の非侵害を保証しません。

This integration downloads third-party components into a separate, local setup
folder. Its source distribution contains no model weights or reference audio.
The exact source revisions and SHA-256 values are in
`config/inference-sources.lock.json`, `config/inference-assets.lock.json` and
`config/inference-wheels.lock.json`. Additional fixed license texts are in
`config/inference-notices.lock.json`; setup retains these beside the environment.
`config/inference-inventory.json` records 105 exact wheels, 296 native members
and 171 license/notice members, with hashes read from the verified archives.
This inventory includes transitive dependencies and their native contents.
Downloaded license files remain with their
packages. An application MIT license does not replace these conditions.

| Component | Exact source | Conditions for this integration |
| --- | --- | --- |
| Irodori-TTS engine | [8ca3acb58ab4e19ad6d594aaed6bafe3e88f7f71](https://github.com/Aratako/Irodori-TTS/tree/8ca3acb58ab4e19ad6d594aaed6bafe3e88f7f71) | MIT. Preserve license and attribution. The local integration wheel preserves Python source bytes and identifies its changed packaging metadata. |
| TTS weights and embedded tokenizer | [Irodori-TTS-v4-Small, 4c92c7ee2bb15c19a97cf4e86d24fd6bf33b0135](https://huggingface.co/Aratako/Irodori-TTS-v4-Small/tree/4c92c7ee2bb15c19a97cf4e86d24fd6bf33b0135) | The model card declares MIT with additional ethical restrictions: explicit consent for voice replication and no intentionally misleading synthetic speech. Local and commercial use remain subject to those restrictions and the user's separate rights in recordings and generated content. |
| Codec implementation | [DACVAE, 414c20785fc3a28373073ea8ef7a1316eeeaca6e](https://github.com/facebookresearch/dacvae/tree/414c20785fc3a28373073ea8ef7a1316eeeaca6e) | Apache-2.0. Preserve license, copyright and notices; identify modifications. |
| Codec weights | [Semantic-DACVAE-Japanese-32dim, 47376ee24834d7a05a48ebabfe3cde29b3c5e214](https://huggingface.co/Aratako/Semantic-DACVAE-Japanese-32dim/tree/47376ee24834d7a05a48ebabfe3cde29b3c5e214) | Derivative model card declares MIT. Preserve the underlying Apache-2.0 conditions of its DACVAE base as well. Meta's maintainer explicitly confirms that the base model weights are Apache-2.0 in [the license clarification](https://huggingface.co/facebook/dacvae-watermarked/discussions/1). That clarification resolves the stale license name in the base README. |
| Watermark code and weights | AudioSeal 0.2.0 wheel; [weights at 3c19eba53390776cf2cc9ed5f6c9ac67ce72ecba](https://huggingface.co/facebook/audioseal/tree/3c19eba53390776cf2cc9ed5f6c9ac67ce72ecba) | The [official announcement](https://github.com/facebookresearch/audioseal/blob/e63a8a0e5cdf7bb797159c92ba15961557fe9bd2/README.md) states that MIT covers the weights, including commercial use. Preserve MIT attribution with copies and conversions. |

The downloaded model's embedded tokenizer is used for both preview and
generation. The older tokenizer-only setup is a separate preview option; it
cannot be substituted into this inference configuration.

The AudioSeal choice is explicit. It uses a fixed 16-bit payload, a CPU
watermark stage and a different detector from SilentCipher. It is not
SilentCipher-compatible and cannot prove a speaker's identity, consent or
ownership. Detection is probabilistic and cannot prevent all alteration,
removal or impersonation. Any redistributed audio needs the applicable
recording, voice, text and output permissions independently of watermarking.

The original AudioSeal checkpoints fail restricted PyTorch loading because of
serialized configuration objects. Setup interprets a small, fixed subset of
their binary format as data, without executing pickle functions or constructing
serialized objects, and writes deterministic safetensors. Both source and
result hashes are fixed. Unsupported objects, tensor layouts or hashes cause
failure. Runtime loads only those safetensors with a fixed architecture and
strict parameter matching. Official safe-format support was requested in
[AudioSeal issue 107](https://github.com/facebookresearch/audioseal/issues/107).

The codec's fine-tuned model card specifies its decoder path. This integration
preserves that path and adds the explicitly selected AudioSeal watermark after
TTS decoding. The codec checkpoint is loaded directly with
`weights_only=True` and strict parameter matching; the upstream packaged-object
fallback is not called. References are read from validated PCM WAV bytes.

The unresolved SilentCipher weight permission inquiry remains separate in
[issue 9](https://github.com/sony/silentcipher/issues/9). This setup does not
download or run those weights. Adopting AudioSeal still requires real speech,
quality and saved-WAV detection acceptance before the product is released.

Python, Tcl/Tk, CUDA drivers and native package libraries have their own terms.
The current distribution requires an independently installed Windows x64
Python 3.13 with Tk. Python and driver binaries are not bundled. Installed
PyTorch wheels retain their BSD and third-party CUDA/native notices. The fixed
wheel catalog is the installation identity; it is not a claim that every
native library may be repackaged under MIT. Redistribution of an assembled
runtime is outside this source-layout distribution.

The tested CUDA wheel includes cuDNN native file version 9.10.2.21. Review the
[CUDA 12.8.1 license](https://docs.nvidia.com/cuda/archive/12.8.1/eula/index.html),
[cuDNN 9.10.2 license](https://docs.nvidia.com/deeplearning/cudnn/backend/v9.10.2/reference/eula.html)
and [cuDNN acknowledgements](https://docs.nvidia.com/deeplearning/cudnn/backend/v9.10.2/reference/acknowledgements.html).
NVIDIA grants installation and use under its own conditions, with separate
limits for redistribution, modification and reverse engineering. A frontend
MIT notice does not license the native cuDNN backend. This application does not
repackage these binaries; the fixed PyTorch distribution supplies them with its
retained notices. Their exact installed bytes are covered by the runtime
fingerprint and the native artifact inventory.

Four wheel distributions require separately retained license texts: ffmpy,
tokenizers, tqdm and tensorboard-data-server. Their notice URLs and hashes
are fixed in the additional notice catalog. The installed data-server 0.7.2
module includes its Apache-2.0 header; it matches the pinned source module
except for the release version stamp. OmegaConf 2.0.0 has a custom permissive
acknowledgement clause, reproduced in the main third-party notice.

Reference registration records three separate operator statements: permission
to use the recording, explicit permission for voice replication, and the
permitted uses of generated audio. An optional evidence attachment can be
retained locally. Checking a box or matching a hash cannot establish a legal
grant. The integration does not grant public, third-party or commercial use of
any voice. Verify that the intended use is within the recorded scope.

After watermarking, the output policy reduces the whole waveform by a uniform
gain only when needed to leave a peak of at most 0.98 before PCM16 conversion.
It does not clip samples. Detection acceptance uses the final saved PCM WAV,
including this gain and quantization. Non-finite output is rejected.
