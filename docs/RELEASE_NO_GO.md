# Release acceptance and publication scope

The Windows release format is an unsigned Python/Tk source-layout ZIP. Own
source is MIT, copyright 2026 rtonf. The user supplies Python 3.13 with Tcl/Tk
and an authorized reference recording. Guided setup acquires a separate fixed
inference environment under the individual component conditions.

| Acceptance area | Evidence and scope | Publication boundary |
| --- | --- | --- |
| Real speech and quality | Short, ordinary and two-chunk real speech; exact tokenizer plan; final PCM16 watermark detection; playback/history; all three private listening samples accepted | Acceptance applies to the tested samples and configuration |
| Rights and fixed dependencies | Official fixed TTS/tokenizer/codec/AudioSeal sources, restricted tensor conversion, 105 exact wheel artifacts, retained licenses and native hashes; separate recording/voice/output permission scopes | Application source is MIT; externally acquired components and each user's recordings retain their own terms |
| User data and recovery | Actual GUI backup/restore and GPU-active cancellation/retry; install/update/rollback/interrupted-update recovery; restart/playback; uninstall retained all tested data and WAVs | WAVs, references and external assets require separate backups; app rollback does not restore those assets |
| Setup and distribution | Fresh guided reference and inference setup, official downloads, hash checks, isolated environment; installed ZIP use; scoped SPDX/CycloneDX SBOMs and deterministic archive | Release bytes are identified by exact commit and SHA-256; source-review archives marked INTERNAL_SOURCE_REVIEW remain private artifacts |
| Windows acceptance | Agreed current Windows 11 host, CUDA/fp32 RTX2060 SUPER 8 GiB; responsive GUI; 257 full-suite passes plus nine previously skipped capability methods passed in a separately authorized elevated run | Other OS/GPU/CPU, minimum VRAM, fresh Windows and physical-offline conditions are unverified; no host security settings were changed |
| Publication and retrieval | Target rtonf/neutral-tts-workbench; version0.1.0rc4/tagv0.1.0-rc.4; exact artifacts and limited-ref publication plan | The owner must approve the final candidate before repository creation, push or Release upload. Actual public clone/download/hash/link checks follow publication |

The precise tested commits, private listening scope, memory/VRAM measurements
and split test runs are recorded in [Windows acceptance](WINDOWS_ACCEPTANCE.md).
Source and Windows ZIP have different file sets; see [build instructions](WINDOWS_BUILD.md).
Source, exact Git history, final archive contents and mandatory disclosures are
checked before publication. Final Release metadata identifies the immutable
commit and artifact hashes; approval of source publication does not approve
redistribution of excluded model/runtime payloads.

[Component conditions](INFERENCE_COMPONENTS.md) and
[third-party notices](../THIRD_PARTY_NOTICES.md) preserve required license,
compatibility and attribution statements. The eight entries in the public
payload ledger retain their preparation-stage decisions for source, model,
tokenizer, codec, native runtime, playback, installer and signing. Its binary
NO-GO and excluded-payload decisions do not declare that an unsigned source-layout
ZIP contains or licenses those external binaries. Final source-layout publication
approval is recorded with the Release rather than asserted retroactively in a
preparation-stage ledger. The application source does not grant a user's voice
or audio publication rights.

Code signing is outside this release specification (今回の配布仕様では対象外).
It is not a signature-validation success. Hash and data-integrity checks remain
required. See [unsigned distribution](UNSIGNED_DISTRIBUTION.md). Watermark
presence does not prove identity, consent or origin and cannot prevent all misuse.
