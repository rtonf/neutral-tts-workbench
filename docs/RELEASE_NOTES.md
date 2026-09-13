# 0.1.0rc2 — unsigned Windows release candidate

0.1.0rc1の公開後、GitHub Actionsで公開ソースの検査構成の不備を確認しました。この版はその修正です。
参照音声・生成・保存・復旧のアプリ実行コードと固定モデル/依存物は0.1.0rc1と同一です。

- 開発者の非公開設定に依存していた配布検査テストを、一時フォルダーに作るテスト専用ポリシーへ変更しました。検査対象へ禁止マーカーを入れると配布を拒否する試験も追加しています。
- CIではリンクを解決したPython実体から試験を実行します。アプリ側のリンク拒否・ファイル差替え検査は変更していません。
- ディレクトリー差替えの試験は、Windowsでjunction、Linuxでsymlinkを使用します。故障注入中のskipが通常エラーとして処理される問題を解消します。
- Git公開面検査に対象commitと履歴を明示し、checkoutで全履歴を取得します。両OSの結果を残すため、片方の失敗による他方の自動取消しを止めました。

Windowsでの関連38試験は失敗・エラー・skipなし。GitHubのWindows/Ubuntu検査が両方成功することを、この修正版のRelease公開条件とします。
過去の実音声・試聴・権限依存試験の結果と、今回のCI修正の検証結果は区別します。

生成前に[権利・同意・利用者の責任](VOICE_USE.md)を確認してください。
録音・実演・文章等の権利、話者の声の複製への明示同意、公開・商用・第三者提供等の用途を区別します。
利用者の無許諾・違法利用による紛争等を開発者が代わって引き受けるものではありません。免責は法令の範囲内で、
開発者自身の故意・重過失等の排除できない責任を免除しません。MIT本文とその許諾は維持しています。
[プライバシー](PRIVACY.md)、[権利侵害・悪用の相談](ABUSE_REPORTING.md)、[日本法・AI指針への対応と限界](JAPAN_AI_COMPLIANCE.md)を追加しました。
生成・参照登録・初回セットアップから同梱文書を表示できます。同意欄は本人や許諾の認証ではありません。

短い参照WAVを条件とする既存モデルの推論で、声ごとの追加学習は不要です。試験参照は約3.47秒と3.44秒の2本。
30秒1本、話者類似度、初見の初心者による導入は未評価です。公開時は合成音声と分かる表示・文脈にしてください。

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

Short, ordinary and long human listening samples were accepted. The 9
capability-dependent symlink/reparse cases passed in one authorized elevated
run, with zero skips, failures or errors. The ordinary full-suite run passed
257 cases and retained its 9 recorded skips; the extra run closes their
capability coverage without rewriting the original result. Other OS/GPU/CPU and
physical-offline conditions remain unverified. A fresh Windows system is not
the agreed current-host acceptance scope. The desktop retains at most 128
completed history records per data folder. Input is limited to 4096 characters,
dictionary expansion to 16384, and boundary pauses to 10 seconds.

The candidate version is `0.1.0rc2`; its release tag is `v0.1.0-rc.2`.
The Release identifies the exact approved commit and artifact hashes. The
preparation-stage acceptance record remains separate from publication approval
and actual GitHub clone/download verification.

This release is specified as an unsigned Python/Tk source-layout ZIP. Code
signing is outside the release specification, not a passed test. Python 3.13
with Tcl/Tk is a separate prerequisite. Warnings associated with an actual
GitHub download will be checked after publication approval; their appearance
cannot be inferred from a locally copied archive.
Follow [download verification](UNSIGNED_DISTRIBUTION.md) and preserve Windows
security protections.
