# Tokenizer-only environment and assets

The optional setup uses `sbintuitions/modernbert-ja-310m`, revision
`77675fc96a7e445e982e2ba90246b816efc74ec6`. Its official
[LICENSE](https://huggingface.co/sbintuitions/modernbert-ja-310m/blob/77675fc96a7e445e982e2ba90246b816efc74ec6/LICENSE)
and model card declare MIT with copyright 2025 SB Intuitions. The selected scope
is local tokenization, including commercial workflows. Preserve the notice with
copies. This conclusion does not approve a TTS model, a reference voice or a
watermark checkpoint. No tokenizer payload is included in the source candidate.

The five exact file sizes and SHA-256 values are embedded in `setup.py` and are
verified before configuration is written. The setup preserves the license and
a private review record alongside the assets. The selected files are tokenizer
JSON, tokenizer configuration, special-token mapping, SentencePiece data and
LICENSE. No Python file or pickle is loaded from the asset directory.

On the current Windows/Python 3.13 host, Transformers 4.57.1, tokenizers 0.22.1,
SentencePiece 0.2.1 and protobuf 6.33.5 were selected for tokenizer testing. The
full platform-specific hashed requirements are in
`config/tokenizer-windows-py313.lock.txt`. Install in a new dedicated environment:

```powershell
python -m venv <tokenizer-environment>
<tokenizer-environment>/Scripts/python.exe -m pip install --require-hashes --only-binary=:all: -r config/tokenizer-windows-py313.lock.txt
```

This lock covers local tokenizer use on Windows x64/Python 3.13. It is not the
inference environment lock, and it is not portable to other Python ABI versions.
Official package metadata and included license files must remain available in
the environment. Dependencies use their own licenses (including Apache, BSD,
MIT, MPL, CNRI-Python and PSF terms), separately from the application MIT license.
No wheel/native redistribution is approved by this source candidate.

The previously inspected Transformers 5.14.1 environment selected a slow loading
path that disagreed with the pinned tokenizer JSON. The application now requires
the fast tokenizer and rejects incompatible loaders. The fixed environment was
checked against direct JSON tokenization. This result establishes tokenization
only; the real model still needs tests with the same assets and dependency lock.
