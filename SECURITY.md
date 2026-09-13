# Reporting security problems

声の無断利用・誤認・ライセンス情報などの相談は、[権利侵害・悪用の相談手順](docs/ABUSE_REPORTING.md)を参照してください。
公開Issueに音声、同意書、身分証、連絡先、私有パスを掲載せず、まず個人情報を含めずに連絡方法を相談してください。
非公開脆弱性報告を一般の権利相談用の常設窓口とは扱っていません。

When the public repository becomes available, use GitHub's private vulnerability
reporting for `rtonf/neutral-tts-workbench` if that feature is enabled. Do not put
credentials, private voices, raw input text, recordings or local absolute paths
in a public issue. If private reporting is not available, open a minimal public
issue requesting a private contact route, without exploit details or personal
data. A response time is not promised for this development candidate.

Reports should identify the version/commit, operating system, affected operation,
expected and observed behavior, and a reproduction using synthetic data. Source
code, dependency substitution, archive handling, file ownership and publication
integrity are within scope. Keep installed assets and the Python environment
trusted and separately reviewed. The local source installer is unsigned.

Neither an audio watermark nor successful detection proves speaker identity,
consent or lawful use. Do not describe watermarking as complete protection from
editing, removal, impersonation or misuse.
