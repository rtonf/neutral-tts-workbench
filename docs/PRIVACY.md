# 個人情報と端末内データ

入力文は画面と専用ワーカー内で扱い、再起動後のために保存しません。設定・辞書は選択したデータフォルダーへ保存します。
計画には入力/辞書のハッシュ等、完成履歴にはファイル名・出力ハッシュ・所要時間等が入り、入力文そのものは入りません。
参照音声、許可に関する申告、任意の根拠資料は端末内の非公開コピーとして保存します。これらを本人・許諾の認証に使う機能ではありません。

画面は本人が選んだファイルと保存先のフルパスを表示します。アプリにはテレメトリーや常駐ネットワーク待受け、参照音声・入力文を取得先へ送信する処理はありません。
初回取得はHugging Face・GitHub・PyPI・PyTorch公式配布先へ通信し、相手先はIPアドレスや取得ファイル等を知り得ます。各配布先の規約・プライバシー方針も適用されます。
物理オフライン試験は未実施です。保存データの独自暗号化は行わないため、OSのアカウント・アクセス権・端末を保護してください。
識別可能な人物の録音・同意資料、入力や辞書等は個人情報や秘密を含み得ます。共有先を確認し、公開リポジトリへ保存しないでください。

通常バックアップには設定・辞書・履歴と私有パスが入ります。WAV、参照録音、根拠資料、モデル、専用環境、選択Python、途中の取得物や作業データは別途非公開で保管してください。
アンインストールは利用者データ・音声を保持します。復旧用の旧作業フォルダーにも音声等が残り得て、自動削除しません。不要なデータは利用者が保存先と必要なバックアップを確認して管理してください。

## 問い合わせで受け取る情報

提供責任者rtonfは、[相談手順](ABUSE_REPORTING.md)で受領した情報を、申出確認、連絡、修正・再発防止、必要な法的対応に限って使用します。
広告、公開デモ、学習、別用途の音声生成へ転用しません。最初の公開Issueには個人音声・同意書・身分証・連絡先・私有パス等を載せないでください。
非公開の方法と目的・必要範囲を先に案内し、双方で確認できた場合だけ最小限の資料を受領します。現在、専用非公開フォームやメール窓口はありません。

相談記録は公開ソースから分離し、担当者だけがアクセスできる保管先を使います。外部共有は必要性・範囲と本人同意または法令上の根拠を確認してから行います。
保存の必要性は対応完了時とその後の見直し時に確認し、不要な個人情報は削除する方針です。紛争・法的義務で必要な場合は目的に必要な範囲で保護して保持します。
一律の永久保存や解決直後の完全消去は約束しません。訂正・削除等の相談も、同じ入口から機微情報を公開せずに行えます。
漏えい等を認識した場合は、影響確認・拡大防止と、法令に基づく報告・本人通知の要否を判断します。
GitHub上の投稿には同社の仕様・規約が適用され、外部の複製やキャッシュまで開発者が削除できるものではありません。
法的根拠と限界は[日本法への対応](JAPAN_AI_COMPLIANCE.md)を参照してください。

## Technical storage details

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
dependencies. Physical-offline operation has not been validated and is outside the agreed
current-host acceptance scope.

Backups contain private settings, local paths and dictionary contents. They do
not include WAVs, models, recordings, external assets, interpreter selection,
partial downloads or transient generation work. Store backups and audio
separately. Restore verifies the existing WAV before exposing a history item.
Uninstall preserves user data and audio, and preserves modified or unknown
application files. Old workspaces are retained for recovery; they can contain
staged audio and are private. This candidate does not automate their deletion.
