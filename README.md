# Neutral TTS Workbench (development name)

This is a neutral, source-only TTS development lane for eight bounded capabilities:

1. safe, previewable WAV filename templates with Windows-safe normalization
   and no-replace collision sequencing;
2. a pre-generation estimate that reuses one deterministic text plan and shows
   counts plus coarse duration, storage, staging, and device/quality timing
   ranges without invoking a sampler;
3. a framework-neutral application service and CLI that connect settings,
   pure preview, startup reconciliation, durable generation admission, safe
   publication, and receipt feedback;
4. a fail-closed dependency lock plus deterministic SPDX/CycloneDX source-archive tools
   for local internal source review;
5. cancellation/crash reconciliation and versioned, integrity-checked data
   backup with automatic pre-restore safety backup and rollback;
6. a source-only owned-files layout contract for synthetic install, update,
   rollback, and owned-only uninstall testing, plus unsigned provenance and
   clean-Windows acceptance schemas;
7. an optional, explicitly configured Irodori-TTS compatible backend and
   identity-checked tokenizer-only counter that keep model/runtime/reference
   assets outside the source package; and
8. recovery-bound, hash-verified receipt history plus synchronous same-byte WAV
   playback through the Windows standard library.

The working display name is defined once in `src/neutral_tts/identity.py`. The
Python module name is deliberately neutral. This repository is not an official
or partnered product and contains no synthesis engine, voice/reference asset,
model/runtime payload, or public binary payload. The compatible backend is a
source adapter. The supplied local host verifies an explicit asset inventory
and private rights-record bindings before using an installed inference package.
See [local runtime configuration](docs/LOCAL_RUNTIME.md).

## Current status

The project source is licensed under [MIT](LICENSE), copyright 2026 rtonf.
Third-party components keep their own conditions. Public release readiness is
`NO-GO`: real-model, dependency-rights, installer, clean-device, signature, and
human acceptance work remains. Isolated local candidates using verified
components may be constructed for acceptance testing. See
[release status](docs/RELEASE_NO_GO.md) for the current boundaries.

For source development with Python 3.11 or later, run commands from the source
checkout with `PYTHONPATH=src`. Run a bounded preview with
`python -m neutral_tts.cli preview --text-file input.txt`. `demo-run` with the
same input writes an explicitly labeled silence-only test WAV. The CLI accepts
up to 4096 Unicode characters. `--text-file -` reads standard input and keeps
the input text out of command arguments. Optional synthesis dependencies are
separate from this standard-library workflow.

The source contracts are documented in `docs/FEATURE_CONTRACTS.md`; the
application flow and explicit silence-only demo are documented in
`docs/APPLICATION_WORKFLOW.md`; and reproducible internal source packaging is
documented in `docs/SOURCE_ONLY_PACKAGING.md`. Clean export and surface-scan
commands remain in `docs/EXPORT_AND_SCAN.md`. Plain-language data protection is
in `docs/DATA_PROTECTION.md`; lifecycle details are in
`docs/INSTALL_UPDATE_CONTRACT.md`; and the unsigned release boundary is in
`docs/RELEASE_FOUNDATION.md`.

## Intended source workflow

1. Run the clean exporter with a private, hash-pinned allowlist into a new
   staging directory.
2. Scan staging before promotion.
3. Develop and test only the neutral source surface.
4. Scan the worktree, candidate commit tree, reachable history, and any future
   archive independently.
5. Compare the separately accepted protected tree against its immutable
   before-manifest and stop on any mismatch.

No generated text, audio, receipt history, profile, local path, model, runtime,
or credential belongs in this repository.
