# Reproduce the unsigned Windows source-layout ZIP

Build from the exact approved source commit with Python 3.13. The build itself
uses Git and the Python standard library. It downloads no model, runtime or
voice assets. Obtain the source through the repository and check out the exact
commit published with the Release.

From the checkout, create a fresh output parent and run:

```powershell
New-Item -ItemType Directory -Path .staging
python tools/build_windows_candidate.py --commit HEAD --destination .staging/windows --archive .staging/windows-unsigned.zip
Get-FileHash -Algorithm SHA256 -LiteralPath .staging/windows-unsigned.zip
```

Use a different fresh destination/archive name for another build. Existing
outputs are rejected. The builder extracts a positive selection of application,
launcher, configuration, notice and documentation files from the selected Git
commit. It validates the full source tree against its allowlist and rejects
Git links or non-file entries. The fixed commit also supplies the dependency
lock and allowlist; working-tree edits are not substituted into the payload.

The Windows ZIP contains `SBOM.spdx.json` (SPDX 2.3) and `SBOM.cdx.json`
(CycloneDX 1.7) for the selected source payload. Their file lists exclude the
generated SBOMs and `OWNED_FILES.json` to avoid self-references. The ownership
manifest covers all application files, including both SBOMs, with sizes and
SHA-256 hashes. SPDX verification codes cover only their declared file list.
The external runtime is described separately in `config/inference-inventory.json`;
it is not represented as bundled application code.

ZIP entries have a fixed timestamp, ordering, permissions and compression.
Repeated builds with the same source commit and Python/zlib build must produce
identical bytes. Compare the reported source commit/tree, ownership file digest
and ZIP SHA-256 with the Release. Changes to the compression implementation can
change ZIP bytes even if all uncompressed files match.

The GitHub source archive and Windows source-layout ZIP have different file
sets. Full-source SPDX/CycloneDX metadata can be generated separately using
`tools/generate_source_metadata.py`. Archives explicitly marked
`INTERNAL_SOURCE_REVIEW` are private verification artifacts; they are not the
Windows distribution. Building a candidate does not publish it or approve it.

CI checks source tests, public source content and a local Windows-layout build.
CI does not receive reference audio, runtime weights or private test evidence,
and does not claim real TTS, human listening or clean-Windows validation.
