# Unreleased local Windows source candidate

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

Current-host evidence includes ordinary and two-chunk real speech, final PCM16
watermark checks, restored history with WAV retention, and cancellation followed
by successful generation. Final dependency notices, installed-candidate
acceptance, the 9 capability-dependent reparse cases and broader listening
remain open. Other host/OS/GPU conditions and physical offline operation remain
unverified. The current host is the agreed installation/uninstallation test
scope. There is no EXE/MSI or bundled runtime. The desktop
keeps at most 128 completed records per data folder in this candidate. Input is
limited to 4096 characters, dictionary expansion to 16384, and boundary pauses
to 10 seconds. No source or binary release has been approved or published.

This development version remains `0.1.0.dev0`; commit and tree identify each
immutable local candidate. Final release notes will name the approved version,
tag, SHA-256 hashes, unsigned status, tested hardware and remaining user-facing limitations.

This release is specified as an unsigned Python/Tk source-layout ZIP. Code
signing is outside the release specification, not a passed test. Python 3.13
with Tcl/Tk is a separate prerequisite. Warnings associated with an actual
GitHub download will be checked after publication approval; their appearance
cannot be inferred from a locally copied archive.
Follow [download verification](UNSIGNED_DISTRIBUTION.md) and preserve Windows
security protections.
