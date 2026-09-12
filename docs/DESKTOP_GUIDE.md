# Local Windows desktop candidate

This candidate adds a Tk desktop and a working source installer. It is unsigned
and requires a separately installed Python with Tk. It does not bundle Python,
native libraries, inference engines, models or reference recordings. Fresh
Windows acceptance and real speech acceptance remain incomplete.

## Start and configure

1. From the repository, run `python launch_desktop.pyw`, or set `PYTHONPATH=src`
   in PowerShell and run `python -m neutral_tts.desktop`. Python 3.13.1/Tk 8.6 is
   the current host test condition; the declared Python floor is 3.11 and has not
   yet been tested on a new Windows device.
2. The first-run tab shows the user data folder and WAV destination. Select a
   dedicated folder outside the application and data folders. Existing history
   stays associated with that destination; use a separate data folder when
   switching destinations after generation.
3. Run the environment check. The tokenizer download button lists the official
   source and immutable revision, downloads about 8.6 MB, checks exact sizes and
   SHA-256 values, preserves the license, and writes the configuration. Re-run it
   after a failed transfer; completed files are verified and reused. Existing
   mismatching files are rejected, not overwritten. Partial transfers are retained
   under `.download-*` for local inspection, outside source distribution.
4. On Windows/Python 3.13 use the dedicated-environment button to install the 20
   hash-pinned tokenizer dependencies into an empty selected folder. Setup keeps
   a local interpreter binding and reuses it after restart. Other configurations
   need the environment described in [TOKENIZER_SETUP.md](TOKENIZER_SETUP.md). Launch with
   `python -m neutral_tts.desktop --worker-python <python-executable>` to select
   it. No inference engine or PyTorch is installed by this tokenizer setup.
5. Enter optional dictionary rows as surface text, TAB, reading. Save settings
   and dictionary before preview. Input text is kept in the current window;
   settings and dictionary are stored only in the selected local data folder.
6. Preview uses the real tokenizer. It reports chunks, tokens, filename and the
   user's complete destination path. Uncalibrated duration/storage ranges are
   deliberately broad; generation-time estimates are unavailable without valid
   measurements. They are not performance claims.
7. Generation requires the reviewed local runtime configuration described in
   [LOCAL_RUNTIME.md](LOCAL_RUNTIME.md), including rights evidence files and a
   consented recording. The setup screen currently accepts that configuration;
   guided creation of a complete inference configuration remains unfinished.
   No unreviewed watermark checkpoint is downloaded by setup.

The explicit synthetic-test checkbox uses a demo counter and generates silence.
It is suitable for exercising save, history and recovery without a voice. It
never establishes real TTS quality or watermark detection.

## Preview, generation and recovery

The desktop sends one request over a private pipe to a dedicated worker process.
The worker alone changes offline/logging settings. There is no network listener,
telemetry, raw-text command argument or persistent engine log. Heavy operations
disable competing controls while the GUI event loop remains active. Cancel stops
the worker; the desktop then runs journal recovery. A publication already
completed at cancellation may remain in verified history. Final WAVs are never
overwritten or silently renamed after preview.

A private preview ticket binds source hash, dictionary, settings, tokenizer
assets, runtime configuration bytes, installed code/native-file fingerprint,
directory identities and planned filename. Changed conditions require another
preview. Runtime history also binds the environment fingerprint; this detects
changes but does not establish a reproducible dependency closure or license
approval. The package/file inventory still needs full engine-closure acceptance.

Console equivalents after desktop setup:

Run these from the application/source folder. Replace `<environment-folder>`
with the dedicated Python environment selected during setup. The CLI uses the
interpreter you invoke; it does not follow the desktop's interpreter selection.
Use the same reviewed inference environment for real generation once available.

```powershell
$env:PYTHONPATH = 'src'
& '<environment-folder>\Scripts\python.exe' -m neutral_tts.cli preview --data-root <data-folder> --text-file <input.txt>
& '<environment-folder>\Scripts\python.exe' -m neutral_tts.cli generate --data-root <data-folder> --text-file <input.txt>
```

`demo-preview` is the explicit name of the former synthetic console preview.
The lower-level `real-run` entry point creates its own immediate preview; use
`preview` followed by `generate` for a separately confirmed plan.

## Backup, restore and lifecycle

Settings are in `settings/app.json`, dictionary rules in `dictionary/rules.json`,
and the portable completed-history snapshot in `history/completed.json`. The
active workspaces and their journals remain under `work/`. Preview and active
workspace selection are outside the backup categories. Backups contain no WAV,
reference recording, model, external license document, transient work or log.
Backups do contain private local paths and dictionary contents; keep them private.

Restore first creates a safety backup, then uses the existing journaled category
transaction. A marker outside those categories ensures that restart reconstructs
history from the reconciled snapshot. It retains the previous workspace. Each
restored journal and sidecar is checked against the actual final WAV. Missing or
modified WAVs are quarantined from usable history, without deleting the WAV.
Restore refuses a different WAV destination. The current backup snapshot limit
is 128 verified records and the archive size limits in the backup contract apply.

Generation, backup, restore, install, update, rollback and uninstall in this host
share the data-root owner lock. Existing lower-level APIs retain their explicit
unsupported-argument checks and their own operational locks.

Build a local installer candidate from a committed version:

```powershell
python tools/build_windows_candidate.py --commit HEAD --destination <fresh-folder>
```

Open `windows_setup.pyw` inside the resulting folder. Select the candidate,
application folder ending in `neutral-tts-workbench`, and external user-data
folder. Install/Update/Rollback/Uninstall use the owned-files manifest and lifecycle
journal. Open `launch_desktop.pyw` in the installed application. Uninstall moves
only unchanged owned application files into a recovery sibling; unknown files,
user data and WAVs are retained. Use the installer from the candidate folder for
uninstall and close the installed desktop first.

After an update, the installer displays the retained previous application folder
and selects it in the candidate field. To roll back, select that retained sibling
folder (including its `OWNED_FILES.json`), confirm both paths, then use Rollback.
Select the new distribution folder again for a subsequent update. The retained
folder is not a backup of settings or WAVs. Uninstall and recovery accept no
candidate argument; they use the selected application and data folders.

If an update is interrupted, open `windows_setup.pyw` from the separate candidate
folder, select the same application and data folders, and use the recovery button
before retrying. Recovery validates the journal and retained bytes; it does not
guess which files can be removed. An unverified or externally changed state needs
attention and is retained. Keep all retained folders until recovery completes.

This is a Python/Tk source installer, not an EXE/MSI or a self-contained runtime.
New Windows installation, 9 capability-dependent reparse tests, physical offline
testing, code signing, final speech/watermark tests and public acquisition tests
remain release gates. A checksum detects changed bytes; it is not a publisher
signature. Do not disable host security to run a candidate.
