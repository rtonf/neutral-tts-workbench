# Unreleased (main after 0.1.0rc3)

- 保存名テンプレートで、固定文字と値の連結が `nul`・`con`・`LPT1`・`CONIN$` 等のWindows予約デバイス名になる場合、プレビュー段階で先頭に `_` を付けるよう修正しました。従来はプレビューが通っても保存時の検査で拒否され、生成完了後に保存できませんでした。`CONIN$`・`CONOUT$` も予約名として扱います。

# 0.1.0rc3 — unsigned Windows release candidate

0.1.0rc1の公開後のCI不備を修正したrc2は、mainでの検査で追加の問題が判明したため、tagとReleaseを公開していません。rc3はその追加修正版です。

- POSIXの進捗表示は、検査済みの親ディレクトリーからリンクを追わず通常ファイルを開き、開いた時点の完全なスナップショットを読みます。正常な原子的更新と同時でも読み取れ、同じ開いたファイル内での変更、非通常ファイル、過大・不正なJSONは拒否します。資産や設定の厳格な読み取り検査は変更していません。
- Windowsの資産ロックは、短縮名とハンドルが返す正式名を正規化して比較します。同じファイルを名前の表記差だけで拒否する問題を修正しました。既存の書き込みハンドル、ロック中の書き込み・改名、異なる対象は引き続き拒否します。
- rc2の修正（一時生成の非空テストポリシーと禁止マーカー拒否、Python実体からのCI実行、POSIX symlinkの故障注入、HEADと全履歴の公開面検査、matrixのfail-fast無効化）を引き継いでいます。

Windowsでの追加関連試験は17件中15件が成功、失敗・エラーは0です。POSIX専用の新しい2件はWindowsではskipで、Ubuntuでの実行を必要とします。元の並行読み取り試験は両OSに残しており、失敗ケースをskipへ変更していません。GitHubの同じSHAでWindows/Ubuntuの試験・公開面検査・ビルドが両方成功するまで、この修正版のtag・Releaseを公開しません。

変更は進捗の読み取りと資産ロックのパス比較、および関連試験・説明です。固定モデル・依存カタログ・利用条件、生成・保存・復旧の方式は維持しています。アプリのコード変更は環境識別に反映され、以前の時間見積りの校正値を無条件に再利用しません。過去の実音声・試聴・権限依存試験と、今回の修正の検証結果は区別します。Ubuntuのソース検査はLinux版TTSの製品対応や実機動作の証明ではありません。

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

The candidate version is `0.1.0rc3`; its release tag is `v0.1.0-rc.3`.
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
