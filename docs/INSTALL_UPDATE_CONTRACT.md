# Source-only install, update, rollback, and uninstall contract

`src/neutral_tts/install_contract.py` is a filesystem contract, not an
installer or launcher. `tools/build_install_layout.py` creates a fresh test
layout from an explicit positive list of source files. It refuses executable,
model, audio, runtime, native, wheel, and archive payloads.

## Layout and ownership

- Every payload path is canonical, relative, NFC-normalized, Windows-safe, and
  free of traversal, drive, UNC, ADS, reserved-device, control-character, and
  reparse aliases.
- Application files live below one manifest-named application root.
- User data is a separate root that may not equal, contain, or be contained by
  the application root.
- `OWNED_FILES.json` binds the source commit/tree, candidate identifier,
  per-file role, byte size, SHA-256, aggregate SHA-256, demo-only scope, and
  the rule that user data is never implicitly deleted.
- A layout is exact: every owned byte must match and an unlisted payload blocks
  install or update verification.

## Lifecycle transitions

Fresh install stages and verifies a complete layout, then promotes it only if
the application root is absent. It has no replace fallback.

Every lifecycle operation writes and flushes an intent/phase journal before a
rename. `reconcile_install_lifecycle()` deterministically resolves an
interrupted install, update, rollback, or uninstall before startup continues;
the public lifecycle functions invoke it before starting a new operation.

Update verifies both complete layouts, stages the new one, relocates the
current one to a fresh rollback root, and promotes the new one. Any injected or
observed failure restores the prior root; a successfully displaced layout is
retained for explicit rollback.

Rollback relocates the current layout to a fresh retained root before restoring
the selected prior layout. It never overwrites the current root.

Uninstall preflights every owned file and relocates only exact hash matches to
a fresh recovery root. Modified owned files block the operation. Unlisted files
remain under the application root, and the user-data root is never an implicit
target.

Critical root identities are revalidated across promotions. Windows directory
guards deny replacement while staged bytes are created, and each exclusive
file handle is verified below its intended root before payload bytes are
written. A reparse or identity race is an error, never a successful lifecycle
result.

The current contract relies on same-volume Windows rename and hard-link
semantics. Cross-volume promotion fails closed. It is synthetic evidence only,
not a public distribution artifact or clean-device acceptance result.
