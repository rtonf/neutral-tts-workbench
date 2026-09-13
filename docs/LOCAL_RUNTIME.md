# Local runtime configuration

The `real-run` command uses the supplied local host. It requires a separately
installed, reviewed inference environment and a private runtime JSON file.
For normal desktop use, [guided inference setup](DESKTOP_GUIDE.md) creates this
private configuration, downloads the fixed reviewed artifacts and saves the
separate interpreter selection. Users do not need to write JSON by hand.
The configuration below documents the lower-level host interface. Model and
native-runtime bytes are separately downloaded, not included in the source
installer. Actual current-host tests and remaining acceptance conditions are
listed in [the product scope](PRODUCT_SCOPE.md).

Run the host in a dedicated CLI process with `PYTHONPATH=src` when using a source
checkout. After preparing the private configuration and input:

```powershell
python -m neutral_tts.cli real-run --runtime-config runtime.json --text-file input.txt --output-dir output --staging-dir staging
python -m neutral_tts.cli history --output-dir output --staging-dir staging
python -m neutral_tts.cli startup --runtime-config runtime.json --output-dir output --staging-dir staging
```

Use the `run_id` returned by history with `play --run-id` and the same
output/staging roots. Playback uses Windows `winsound` and rechecks the recorded
WAV. History and startup do not load the model. Output and staging must be
separate directories. The text limit is 4096 Unicode characters; `--text-file -`
reads standard input. UTF-8 input files may include a BOM.

## Private configuration fields

All these keys are required:

| Field | Value |
|---|---|
| `schema_version` | `neutral-local-runtime-v1` |
| `asset_root` | Absolute local directory holding the reviewed assets |
| `assets` | List of `{path, role, size, sha256}` objects; paths relative to asset_root, roles model/tokenizer/codec/watermark |
| `model` | Relative path to the selected `.safetensors` checkpoint |
| `codec` | Relative path to the selected codec `weights.pth` file |
| `tokenizer` | Relative tokenizer directory immediately beside the selected checkpoint |
| `watermark` | Relative watermark checkpoint directory |
| `device` | `cpu` or `cuda`; actual compatibility requires runtime testing |
| `precision` | `fp32`, or `bf16` with CUDA; actual compatibility requires runtime testing |
| `vocab_size`, `bos_token_id` | Exact tokenizer vocabulary size and BOS identifier |
| `rights_manifest` | Absolute path to a private rights-review record |
| `rights_manifest_sha256` | SHA-256 of that exact record |
| `references` | One to four `{path, sha256}` entries with absolute reference WAV paths |

The tokenizer inventory must include `tokenizer.json` and
`tokenizer_config.json`. The watermark inventory must include `hparams.yaml`,
`enc_c.ckpt`, `dec_c.ckpt`, and `dec_m_0.ckpt`. Record every additional file in
either directory. All inventory hashes must be lowercase SHA-256 and sizes
must match the files. Reference audio must be nonempty mono PCM16 WAV at
44.1 or 48 kHz, at most 30 seconds and 6 MiB per file. Configuration and rights
records are limited to 256 KiB. Keep these files outside the public repository.

The rights record uses schema `neutral-local-rights-v1`. It contains the exact
`references` list, `asset_inventory_sha256`, and separate `local_execution`,
`recording_use`, `voice_replication`, and `generated_audio_use` review entries.
Each entry contains `reviewed: true`, `reviewed_use` equal to that scope,
`unresolved: []`, the absolute `evidence_path`, and `evidence_sha256`. The host
opens and hashes the actual evidence file during admission and reference checks;
a digest alone is rejected. Calculate the inventory digest from the UTF-8 bytes of
`json.dumps(assets, sort_keys=True, separators=(",", ":"))`.

These are operator review records, not an automated license decision. Hashes
identify records and assets; the operator must actually check the cited
evidence, applicable licenses, scope, speaker consent, and permitted output
use. Local execution review does not approve public samples or redistribution.
An owner approving an execution does not grant rights held by a third party.
The unresolved watermark-weight conditions remain a distribution and execution
decision; do not create a positive record merely to bypass that question.

## Execution boundaries and remaining validation

The host imports the fixed installed inference package only at generation.
Install Python code and native dependencies from the exact reviewed dependency
lock in an isolated environment. A compromised Python installation is outside
this input-file validator's boundary. The configuration cannot select an
arbitrary Python module or command.

Model admission streams file hashes before loading and rechecks them at runtime
construction. The tokenizer uses `local_files_only=True` and
`trust_remote_code=False`. Hub offline and telemetry settings apply only to the
dedicated process during execution and are restored on exit. Watermark loading
receives explicit local checkpoint paths. A missing watermark prevents output
publication. These settings still require physical-offline verification of the
complete selected environment on a separate test device.

The process requests restricted PyTorch checkpoint loading through
`TORCH_FORCE_WEIGHTS_ONLY_LOAD=1`. It does not retry using unrestricted pickle
loading or add unknown globals. See the
[PyTorch serialization documentation](https://docs.pytorch.org/docs/main/notes/serialization.html).
Compatibility of the selected codec and watermark weights with restricted
loading must be tested. This setting does not make untrusted model files safe.

Private paths and source text are omitted from CLI JSON; Python runtime output
is suppressed while the model runs. Complete dependency/native logging behavior
requires real-runtime verification. Use one operation per dedicated process;
upstream loading and output/environment settings are process global. Existing
generation ownership still serializes a shared staging root.

References are rehashed before and after chunk synthesis while directory guards
hold their paths. Reference files and asset directories must remain under the
operator's control throughout execution. No final-WAV watermark decoder result,
speaker quality judgment, clean-Windows result, or signature result is inferred
from these admission checks.
