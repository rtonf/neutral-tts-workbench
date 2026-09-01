# Neutral TTS Workbench (development name)

This is a neutral, source-only TTS development lane for six bounded capabilities:

1. safe, previewable WAV filename templates with Windows-safe normalization
   and no-replace collision sequencing;
2. a pre-generation estimate that reuses one deterministic text plan and shows
   counts plus coarse duration, storage, staging, and device/quality timing
   ranges without invoking a sampler;
3. a framework-neutral application service and CLI that connect settings,
   preview, generation admission, safe publication, and receipt feedback;
4. a fail-closed dependency lock plus deterministic SPDX/CycloneDX source-archive tools
   for local internal source review;
5. cancellation/crash reconciliation and versioned, integrity-checked data
   backup with automatic pre-restore safety backup and rollback;
6. a source-only owned-files layout contract for synthetic install, update,
   rollback, and owned-only uninstall testing, plus unsigned provenance and
   clean-Windows acceptance schemas.

The working display name is defined once in `src/neutral_tts/identity.py`. The
Python module name is deliberately neutral. This repository is not an official
or partnered product and currently contains no synthesis engine or public
binary payload.

## Current status

The lane is `NO-GO` for public publication, public release packaging,
installers, model/runtime bundling, and Production claims. A local,
source-only `INTERNAL_SOURCE_REVIEW` archive is permitted as compliance
evidence; it is not redistribution approval or a release candidate. See
`docs/RELEASE_NO_GO.md` and the public license matrix for the unresolved gates.

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
