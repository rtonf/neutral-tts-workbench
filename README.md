# Neutral TTS Workbench (development name)

This is a new, source-only TTS development lane for two public-facing
capabilities:

1. safe, previewable WAV filename templates with Windows-safe normalization
   and no-replace collision sequencing;
2. a pre-generation estimate that reuses one deterministic text plan and shows
   counts plus coarse duration, storage, staging, and device/quality timing
   ranges without invoking a sampler.

The working display name is defined once in `src/neutral_tts/identity.py`. The
Python module name is deliberately neutral. This repository is not an official
or partnered product and currently contains no synthesis engine or public
binary payload.

## Current status

The lane is `NO-GO` for publication, packaging, installers, model/runtime
bundling, and Production claims. See `docs/RELEASE_NO_GO.md` and the public
license matrix for the unresolved gates.

The source contracts are documented in `docs/FEATURE_CONTRACTS.md`; the clean
export and scan commands are documented in `docs/EXPORT_AND_SCAN.md`.

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
