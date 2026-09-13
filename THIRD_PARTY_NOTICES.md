# Third-party conditions and notices

The application's own source is MIT, copyright 2026 rtonf. The Windows
source-layout distribution contains its installer, launcher and application
source. Inference packages and model assets are acquired separately by setup.
Their conditions apply to local execution and subsequent use of generated
audio. Model and runtime redistribution are outside this distribution.

Exact official sources, versions and artifact hashes are in
`config/inference-sources.lock.json`, `config/inference-wheels.lock.json` and
`config/inference-assets.lock.json`. [Component conditions](docs/INFERENCE_COMPONENTS.md)
bind the TTS weights, embedded tokenizer, codec and watermark to those records.
Original package license files and notices remain in the installed environment.
The installation inventory distinguishes those artifacts from the source SBOM.

The Irodori-TTS engine is MIT. Its model and tokenizer card also specifies
explicit consent for voice replication and restrictions on misleading uses.
DACVAE code is Apache-2.0; the fine-tuned codec weights retain the underlying
Apache-2.0 conditions alongside the derivative model card's MIT declaration.
AudioSeal code and weights are MIT. Local packaging modifications and the
deterministic safetensors conversion are documented in the component guide.
Preserve upstream licenses and copyright notices with permitted copies.

OmegaConf 2.0.0 in the fixed environment was developed by Omry Yadan. Its
wheel contains the following custom permissive license, including an
acknowledgement requirement. The literal placeholder is preserved from that
license; this source does not recast it as MIT:

> Copyright (c) 2018 Omry Yadan
> All rights reserved.
>
> Redistribution and use in source and binary forms are permitted
> provided that the above copyright notice and this paragraph are
> duplicated in all such forms and that any documentation,
> advertising materials, and other materials related to such
> distribution and use acknowledge that the software was developed
> by the &lt;organization&gt;. The name of the
> &lt;organization&gt; may not be used to endorse or promote products derived
> from this software without specific prior written permission.
> THIS SOFTWARE IS PROVIDED ``AS IS'' AND WITHOUT ANY EXPRESS OR
> IMPLIED WARRANTIES, INCLUDING, WITHOUT LIMITATION, THE IMPLIED
> WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE.

The complete native notices supplied by NumPy, SciPy, PyTorch, SoundFile,
soxr and the other wheels must remain with their installations. SoundFile's
native libraries and soxr include LGPL conditions; LLVM-derived libraries
include Apache-2.0 with the LLVM exception. CUDA and related NVIDIA libraries
retain NVIDIA's applicable terms. A Python package's top-level license does
not replace these native licenses. [CUDA 12.8 terms](https://docs.nvidia.com/cuda/archive/12.8.1/eula/index.html)
and the installed license inventory provide corresponding references.
The playback path uses Python's Windows audio support and the operating system.
Python/Tcl/Tk and GPU drivers are separately installed prerequisites.

The optional `sbintuitions/modernbert-ja-310m` tokenizer-only setup has its own
fixed notices and is separate from the embedded generation tokenizer. The
SilentCipher checkpoint permission inquiry remains unresolved and that
checkpoint is outside this setup. `sounddevice` and PortAudio are excluded
compatibility records in the source dependency ledger; the playback path does
not install them. Exclusion records do not grant redistribution permission.

Watermarks do not establish recording rights, speaker identity or consent.
Record the recording-use permission, voice-replication consent and permitted
output uses separately. The user supplies their own authorized reference audio.
The source archive's exact-commit SPDX and CycloneDX documents describe included
project files. See the inference locks and component conditions for separately
acquired dependencies.
