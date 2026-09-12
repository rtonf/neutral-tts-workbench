# Unreleased local Windows source candidate

- Added a Tk desktop for input, persisted settings/dictionary, preview,
  generation, selected destination, verified history/playback and recovery.
- Replaced ordinary CLI demo preview with configured real-tokenizer preview.
  The explicit synthetic command is now `demo-preview`. Separate generation
  requires the saved preview ticket.
- Added pinned tokenizer acquisition, license preservation, hashing, resumable
  setup and a dedicated Windows/Python 3.13 dependency environment.
- Added private worker processes with Windows process-tree lifetime ownership,
  cancellation recovery, shared data locks and stale-plan rejection.
- Integrated actual settings/dictionary/history with backup and restore. WAVs
  and external assets are excluded; missing/tampered WAVs are not playable.
- Added local Python/Tk installer and launcher using the existing owned-files
  update, rollback and uninstall transactions. Application files and user data
  are separate. Python/Tk is a prerequisite, not part of this candidate.

Known limitations: real inference and saved-WAV watermark detection remain
unaccepted; inference setup still needs a reviewed complete configuration;
engine/native dependency closure, new Windows/GPU tests, the 9 Windows
capability-dependent reparse cases, physical offline operation, signing and
human listening remain open. There is no EXE/MSI or bundled runtime. The desktop
keeps at most 128 completed records per data folder in this candidate. Input is
limited to 4096 characters, dictionary expansion to 16384, and boundary pauses
to 10 seconds. No source or binary release has been approved or published.

This development version remains `0.1.0.dev0`; commit and tree identify each
immutable local candidate. Final release notes will name the approved version,
tag, hashes, signatures, tested hardware and remaining user-facing limitations.
