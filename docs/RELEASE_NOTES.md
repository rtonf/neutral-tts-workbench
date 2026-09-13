# 0.1.0rc1 — unsigned Windows release candidate

- Added a Tk desktop for input, persisted settings/dictionary, preview,
  generation, selected destination, verified history/playback and recovery.
- Replaced ordinary CLI demo preview with configured real-tokenizer preview.
  The explicit synthetic command is now `demo-preview`. Separate generation
  requires the saved preview ticket.
- Added pinned tokenizer acquisition, license preservation, hashing, resumable
  setup and a dedicated Windows/Python 3.13 dependency environment.
- Added full inference setup with fixed source, wheel and model catalogs,
  separate reference-use statements, restricted codec loading and an explicit
  AudioSeal alternative with saved-WAV detection. The GUI now completes real
  input, preview, generation, save, playback, history and recovery operations.
- Connected verified success receipts to time estimates for the same assets,
  references and installed environment. Software, device or driver changes
  invalidate those calibration values. Preview does not load inference weights.
- Added private worker processes with Windows process-tree lifetime ownership,
  cancellation recovery, shared data locks and stale-plan rejection.
- Integrated actual settings/dictionary/history with backup and restore. WAVs
  and external assets are excluded; missing/tampered WAVs are not playable.
- Added local Python/Tk installer and launcher using the existing owned-files
  update, rollback and uninstall transactions. Application files and user data
  are separate. Python/Tk is a prerequisite, not part of this candidate.
- Corrected product rollback to require the retained application folder. Setup
  displays that folder and provides journal recovery after an interrupted update.
  Synthetic history is visibly labeled as silence and shows the selected WAV path.
- Fixed synchronous memory playback on Python 3.11-3.13, whose Windows audio
  module does not expose the newer explicit synchronous flag.

Current-host acceptance now includes a fresh guided setup from a locally
copied/extracted ZIP, ordinary and two-chunk real speech, final PCM16 watermark
checks, playback/history/restart, backup/restore, GPU-active cancellation and
successful retry. Install/update/rollback/interrupted-update recovery and
uninstall retained every tested WAV, settings file and unlisted user note.
See [observed acceptance](WINDOWS_ACCEPTANCE.md) for the exact functional
commit, measurements, counts and their limits.

The Windows builder now embeds SPDX/CycloneDX metadata for its selected source
payload and supports a deterministic ZIP. The external inference inventory
remains separate from the application's own MIT source. README contains the
Japanese installation and first-generation steps. The setup dialog links the
CUDA/cuDNN conditions separately from the model and Python package licenses.

Ordinary/long human listening and the 9 capability-dependent symlink/reparse
cases remain pending. The short sentence was accepted. Other OS/GPU/CPU and
physical-offline conditions remain unverified. A fresh Windows system is not
the agreed current-host acceptance scope. The desktop retains at most 128
completed history records per data folder. Input is limited to 4096 characters,
dictionary expansion to 16384, and boundary pauses to 10 seconds.

The candidate version is `0.1.0rc1`; the proposed Git tag is `v0.1.0-rc.1`.
Final publication approval, the exact final commit and artifact hashes, and
actual GitHub acquisition checks remain required. No source or binary release
has been approved or published by this record.

This release is specified as an unsigned Python/Tk source-layout ZIP. Code
signing is outside the release specification, not a passed test. Python 3.13
with Tcl/Tk is a separate prerequisite. Warnings associated with an actual
GitHub download will be checked after publication approval; their appearance
cannot be inferred from a locally copied archive.
Follow [download verification](UNSIGNED_DISTRIBUTION.md) and preserve Windows
security protections.
