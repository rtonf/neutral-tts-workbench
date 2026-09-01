# Data protection in plain language

The recovery layer keeps each generation attempt in its own staging area. If
work is cancelled or interrupted, completed output is left alone and unfinished
state is moved to quarantine for review. Repeating the same output request
chooses a new filename instead of replacing an existing file.

A backup is a versioned ZIP with a manifest and SHA-256 hash for every file. It
covers settings, history, dictionary data, and approved generic voice-profile
metadata. Unsafe paths, links, unexpected data types, oversized archives, and
damaged content are rejected before live data changes.

Restore first makes a safety backup of the current data. All supported
categories then change as one transaction. If the transaction fails, the prior
data is restored and the safety backup remains available. A small phase journal
is flushed before each rename. `reconcile_restore()` uses that journal at
startup to restore the exact safety snapshot after process termination or power
loss; `restore_backup()` runs the reconciler before starting another restore.

Update keeps the prior complete application layout as a rollback copy. It does
not move, migrate, or erase the separate user-data root. Uninstall relocates
only files listed in the owned-files manifest; unknown files and user data are
left in place. Recovery and uninstall copies are retained rather than silently
discarded. Install, update, explicit rollback, and uninstall also write a
flushed lifecycle journal. `reconcile_install_lifecycle()` must run during
startup before the application root is used; every lifecycle entry point calls
it automatically before beginning a new transition.

Critical roots are held by verified non-delete-sharing directory handles on
Windows. Staged files are created exclusively and their opened handle is proven
to resolve below the guarded root before candidate bytes are written. Any
reparse, identity, or parent-chain change fails closed.

These are source-contract and synthetic-test results. Physical clean-device,
offline, signing, and human acceptance remain pending.
