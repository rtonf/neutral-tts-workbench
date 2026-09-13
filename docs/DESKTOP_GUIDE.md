# Local Windows desktop candidate

## 生成前に確認すること

[権利・同意・利用者の責任](VOICE_USE.md)を確認してから参照音声を登録してください。
録音物・実演・文章等の利用権、話者本人の声の複製への明示同意、生成音声の用途は別々です。
話者の許可だけで他の権利まで処理できるとは限りません。公開サイトや他の音声合成サービスの素材も条件を確認します。
私的試験の許可をネット公開・広告・販売・第三者提供に広げず、公開時は合成音声と分かる表示・文脈にしてください。
登録は利用者の申告で、本人や許諾の認証ではありません。MITと法令・モデル条件は区別します。

生成画面の「権利・責任・相談先」、参照登録と初回セットアップの案内ボタンで、同梱文書を読めます。
別ウィンドウの選択欄から責任・個人情報・相談・日本法・部品条件を切り替え、スクロールして確認できます。
「閉じる」で元の入力へ戻れます。文書表示はネットへ接続せず、入力や音声も送信しません。
相談方法は[ABUSE_REPORTING](ABUSE_REPORTING.md)、保存と相談資料の扱いは[PRIVACY](PRIVACY.md)を参照してください。

参照を条件とする既存モデルの推論であり、声ごとの追加学習は不要です。
参照は1～4本、mono PCM16、44.1/48 kHz、各30秒・6 MiB以内。30秒は受付上限であり品質保証ではありません。
試したのは約3.47秒・3.44秒の2本で、30秒1本・話者類似度・初見の初心者による操作は未評価です。
必要なPython/Tk、対応確認済みPC、初回取得と容量、文章入力は[README](../README.md)の手順に従ってください。

This candidate adds a Tk desktop and a working source installer. It is unsigned
and requires a separately installed Python with Tk. It does not bundle Python,
native libraries, inference engines, models or reference recordings. The agreed
acceptance uses an isolated installation on the current Windows host. No new
Windows or physical offline result is inferred from it.

## Start and configure

1. From the repository, run `python launch_desktop.pyw`, or set `PYTHONPATH=src`
   in PowerShell and run `python -m neutral_tts.desktop`. Python 3.13.1/Tk 8.6 is
   the current host test condition. The fixed Windows setup requires x64 Python
   3.13; the broader source-core version declaration is not a Windows support claim.
2. The first-run tab shows the user data folder and WAV destination. Select a
   dedicated folder outside the application and data folders. Existing history
   stays associated with that destination; use a separate data folder when
   switching destinations after generation.
3. Run the environment check. For real speech, use "参照音声と利用範囲を登録" to
   select one to four mono PCM16 WAVs at 44.1/48 kHz, each at most 30 seconds and
   6 MiB. Enter recording permission, explicit voice replication consent and the
   generated-audio use scope separately. An optional local evidence attachment
   is supported. The form preserves the originals and stores private copies.
4. Open "実生成の初回セットアップ". Read the Japanese use conditions and linked
   component review, select an empty dedicated setup folder with 16 GiB free,
   choose CUDA/fp32 (or the currently unverified CPU/fp32 mode), and start setup.
   The app acquires fixed source archives, builds reproducible source wheels
   without executing upstream setup scripts, downloads fixed binary wheels and
   about 3.59 GB of model assets, then writes the tokenizer/runtime/rights
   configuration. The AudioSeal alternative is explicitly identified. Runtime
   weights are not included in the application ZIP. Complete files are checked
   and reused after a failed transfer; ordinary failures clean their temporary
   download. Process termination can leave temporary files in the dedicated
   setup folder. A differing completed file is rejected for investigation.
5. The tokenizer-only buttons remain available when only preview is needed.
   The tokenizer download button lists the official
   source and immutable revision, downloads about 8.6 MB, checks exact sizes and
   SHA-256 values, preserves the license, and writes the configuration. Re-run it
   after a failed transfer; completed files are verified and reused. Existing
   mismatching files are rejected, not overwritten. Partial transfers are retained
   under `.download-*` for local inspection, outside source distribution.
   On Windows/Python 3.13 use its dedicated-environment button to install the 20
   hash-pinned tokenizer dependencies into an empty selected folder. Setup keeps
   a local interpreter binding and reuses it after restart. Other configurations
   need the environment described in [TOKENIZER_SETUP.md](TOKENIZER_SETUP.md). Launch with
   `python -m neutral_tts.desktop --worker-python <python-executable>` to select
   it. No inference engine or PyTorch is installed by this tokenizer setup.
6. Enter optional dictionary rows as surface text, TAB, reading. Save settings
   and dictionary before preview. Input text is kept in the current window;
   settings and dictionary are stored only in the selected local data folder.
7. Preview uses the real tokenizer. It reports chunks, tokens, filename and the
   user's complete destination path. Uncalibrated duration/storage ranges are
   deliberately broad; generation-time estimates are unavailable without valid
   measurements. They are not performance claims.
8. Generate from the preview. The runtime uses the same embedded tokenizer and
   rejects changed conditions. The configuration is created by the wizard;
   hand-writing the JSON described in [LOCAL_RUNTIME.md](LOCAL_RUNTIME.md) is
   unnecessary for the fixed setup. History offers verified playback and a
   separate AudioSeal check of the final saved WAV. A successful watermark
   result does not prove identity or consent.

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
changes, including same-version code changes, transitive dependencies, added
package modules, Python library files and native binaries. It does not establish
license approval. The installation uses the exact wheel/source/asset catalogs;
final environment reproduction and real use are separate acceptance evidence.

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
Current-host installation, real generation, update/rollback/interruption,
restart/playback and uninstall with retained user data have been exercised.
The 9 capability-dependent reparse cases passed in the authorized elevated
run, and short, ordinary and long human listening samples were accepted.
New Windows and physical-offline use are unverified;
code signing is outside the agreed release scope. Final public acquisition
requires publication approval. See [measured acceptance](WINDOWS_ACCEPTANCE.md)
and [reproducible builds](WINDOWS_BUILD.md). A checksum detects changed bytes;
it is not a publisher signature. Preserve Windows security protections.
