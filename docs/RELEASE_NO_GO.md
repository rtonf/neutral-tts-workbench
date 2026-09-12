# Release status: NO-GO

This repository is not a public binary release candidate. Project source is
MIT licensed, copyright 2026 rtonf. Source development, synthetic testing, and
isolated candidate construction can proceed within each component's verified
conditions. Public upload requires approval of a concrete final candidate.

The current source lane deliberately contains no engine checkout, model,
tokenizer assets, codec weights, voice profile, reference audio, bundled
playback runtime, Python runtime, CUDA runtime, native playback/library
payload, or generated output. A first-party Python/Tk launcher and source
installer are available as local candidates; they require separately installed
Python/Tk. Each external payload requires its
own exact dependency closure, license, notices, applicable use/distribution
conditions, privacy and consent basis, and content hashes. An unresolved
component blocks the operations that depend on it. The standard-library same-byte playback source
contract does not approve or supply such a runtime payload.

Current blockers include:

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
- signature execution, a signed launcher/installer, clean physical-offline
  Windows beta evidence, and final security/legal/human acceptance are absent;
- recovery, backup/restore, and source-only lifecycle contracts now have
  synthetic tests, but they are not clean-device, installer, signing, or human
  acceptance evidence.

Public repository creation, push, and Release upload remain subject to final
candidate approval. Local test launchers and installer candidates using verified
components may be built before clean-device acceptance. Candidate construction
does not constitute public release approval.

A deterministic ZIP explicitly marked `INTERNAL_SOURCE_REVIEW` may be created
locally under ignored staging to verify source-only packaging. That ZIP is
non-public compliance evidence, contains only an exact allowlisted Git source
tree plus generated source metadata, and does not satisfy any open license,
redistribution, binary, installer, signing, clean-device, or human-acceptance
gate. The builder and verifier reject a public audience while these gates are
open.

`compliance/distribution-gates.public.json` keeps model, tokenizer, codec,
native-runtime, playback, installer, and signing decisions separate. A passed
gate requires its own exact evidence and approval authority; progress on one
does not approve another.
