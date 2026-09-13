# Unsigned Windows distribution

今回のWindows配布形式は未署名のPython/Tkソース配置ZIPです。コード署名は
「今回の配布仕様では対象外」です。署名済み・署名検証合格とは扱いません。
Python 3.13の64 bit版とTcl/Tkを別途用意する必要があります。

公開元は [rtonf/neutral-tts-workbench](https://github.com/rtonf/neutral-tts-workbench)
です。対象Releaseに記載されたバージョン、tag、完全なcommit、配布ZIPと
SHA256SUMS.txtを確認してください。ローカルで作成した開発候補と公開Releaseは
それぞれのcommitとファイルハッシュで識別します。

ダウンロードしたZIPを展開する前に、PowerShellで確認します。
ファイル名は対象Releaseに記載された実際の名前を指定してください。

```powershell
Get-FileHash -Algorithm SHA256 -LiteralPath '.\downloaded-release.zip'
```

出力されたHashと、対象ReleaseのSHA256SUMS.txtに記載されたZIPの64桁の値を
照合します。相違があれば実行せず、公開元とバージョンを再確認してください。
ハッシュの一致は、比較した基準値と同じファイルであることを確認します。
発行者の本人確認やコード署名、プログラムが無害であることの保証ではありません。
ZIPと基準値が同時に差し替えられた場合、同じ場所からのハッシュ取得だけでは
発行者を証明できません。

展開後のwindows_setup.pywでアプリとデータの保存場所を選択します。
具体的な導入・更新・復旧・削除は [desktop guide](DESKTOP_GUIDE.md) に従います。
同梱OWNED_FILES.jsonによる配置ファイルの検査も署名の代用ではありません。

Windowsでは、ブラウザー、ダウンロード元の記録、ファイル形式、関連付けられた
Python、組織のポリシーにより警告や実行制限が異なります。これはEXE/MSI形式の
インストーラーではなく、警告が必ず特定の画面になるとは約束しません。
新規Windows VMは今回の検証範囲に含まれません。合意した検証範囲は現在のWindows
ホストです。GitHubからの実ダウンロードに伴う表示は公開承認後に確認します。
DefenderやSmartScreenを恒久的に無効化する導入手順は提供しません。
警告が解消できない場合は、対象Release、ハッシュ、警告の文言を確認してください。
機微なローカルパスや録音内容を公開の問い合わせに添付しないでください。
