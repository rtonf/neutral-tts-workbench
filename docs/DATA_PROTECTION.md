# Data protection in plain language

The recovery layer keeps each generation attempt in its own staging area. If
work is cancelled or interrupted, completed output is left alone and unfinished
state is moved to quarantine for review. Repeating the same output request
chooses a new filename instead of replacing an existing file. A completed run
is recognized only from exact canonical journal fields whose staged and
published size/SHA-256 evidence agrees with the surviving files; duplicate,
unknown, or alternate JSON representations are quarantined.

The normal startup and generation entries share one identity-bound recovery
context. It binds the recovery owner to the preflight identity, holds the output
directory guard, and validates the configured-root snapshot before recovery.
For generation it remains held through backend work, validation, and durable
publication. A live owner rejects a competing ordinary startup; normal return
and exceptions release the context, and process termination releases the claim
for the next startup. Backend output becomes ready only after WAV facts,
aggregate counts, and the success receipt agree. Startup recovery verifies or
reconciles durable evidence without invoking the backend or creating a second
publication.

Generation startup uses the configured recovery root. Restore startup uses an
explicit live-data root, and install lifecycle startup uses an explicit
application/user-data root pair. All configured operational roots must be
physically distinct and non-nested; alternate path spellings do not bypass this
boundary. Startup, preview, and admission bind the required physical identities
so replacement before use fails closed. Roots are never discovered or swept.

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
it automatically before beginning a new transition. The reconciler and every
mutation run under one operating-system process lock. A live owner blocks a
second process, while process termination releases the claim for recovery.
The owned implementation also verifies the live lock handle, application root,
and operation before it reads a lifecycle journal or changes lifecycle state.

Critical roots are held by verified non-delete-sharing directory handles on
Windows. Staged files are created exclusively and their opened handle is proven
to resolve below the guarded root before candidate bytes are written. Any
reparse, identity, or parent-chain change fails closed.

These are source-contract and synthetic-test results. Physical clean-device,
offline, signing, and human acceptance remain pending.
