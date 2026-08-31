# Release status: NO-GO

This repository is not a public binary release candidate. It may be used for
neutral source development and synthetic tests only.

The current source lane deliberately contains no engine checkout, model,
tokenizer assets, codec weights, voice profile, reference audio, playback
overlay, Python runtime, CUDA runtime, native library, launcher, installer, or
generated output. Adding any of those components is blocked until its exact
dependency closure, license, notices, redistribution permission, privacy and
consent basis, and content hashes have been independently approved.

Current blockers include:

- the project source has no approved distribution license yet;
- Irodori-TTS compatibility does not establish endorsement or redistribution
  permission for its source, runtime, or related assets;
- the SilentCipher checkpoint has no approved redistribution record here;
- the `sbintuitions/modernbert-ja-310m` tokenizer assets have no approved
  redistribution record here;
- the `semantic-dacvae-japanese-32dim` codec weights have no approved
  redistribution record here;
- `sounddevice` and PortAudio attribution is identified, but the exact public
  playback bundle remains unapproved;
- CPython, CUDA, GPU/native libraries, transitive Python dependencies, and
  platform binaries require a strict dependency closure, SBOM, and notices;
- an alternative voice requires documented identity/consent and redistribution
  rights, and no private or previously accepted voice asset may be substituted;
- signed manifests, a single signed launcher/installer, backup/restore,
  crash/cancel recovery, clean physical-offline Windows beta evidence, and final
  security/legal/human acceptance are absent.

Therefore do not create or publish an installer, release archive, GitHub
repository/release, model/runtime bundle, or Production claim from this lane.
