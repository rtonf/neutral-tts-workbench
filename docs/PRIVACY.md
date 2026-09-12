# Local data and distribution

Input text remains in the desktop window and a private worker pipe. The current
desktop does not retain source text across restart. Dictionary entries and
settings are saved in the selected application data folder. Preview records
contain source/dictionary hashes and plan metadata. Completed history contains
receipts, filename, content hashes and timing; it does not contain the input text.

The user's screen displays selected input files, configuration files, data folder
and the full WAV destination. These displays are intentional local controls.
Console JSON and generic error responses omit private paths and raw exception
text. There is no application telemetry or network listener. The setup actions
contact only the configured official package/asset sources to obtain the pinned
dependencies. Operation without networking still requires physical offline
acceptance on a separate test environment.

Backups contain private settings, local paths and dictionary contents. They do
not include WAVs, models, recordings, external assets, interpreter selection,
partial downloads or transient generation work. Store backups and audio
separately. Restore verifies the existing WAV before exposing a history item.
Uninstall preserves user data and audio, and preserves modified or unknown
application files. Old workspaces are retained for recovery; they can contain
staged audio and are private. This candidate does not automate their deletion.
