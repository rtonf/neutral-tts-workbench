# Release status: final acceptance pending

The Windows release format is an unsigned Python/Tk source-layout ZIP. Own
source is MIT, copyright 2026 rtonf. The user supplies Python 3.13 with Tcl/Tk
and an authorized reference recording. Guided setup acquires a separate fixed
inference environment. Publication requires approval of the concrete final
commit and distribution bytes.

The following six acceptance areas remain distinct:

| Area | Evidence available | Remaining work |
| --- | --- | --- |
| Real speech and quality | Short, ordinary and two-chunk desktop generation; exact preview tokenization; saved PCM16 watermark detection; playback/history; short listening accepted | Broader human listening; final-candidate checks after packaging/display changes. Measurements and scope are recorded in WINDOWS_ACCEPTANCE.md |
| Rights and fixed dependencies | Official fixed TTS/tokenizer/codec/AudioSeal records, safe conversion, 105 wheel artifacts, retained licenses and native-file hashes; separate voice permission scopes | Final-candidate SBOM/ZIP byte verification; local-use conditions are separate from excluded runtime redistribution |
| User data and recovery | Actual GUI generation/restore/cancel/retry; WAV retention; shared ownership and transactional lifecycle implementation | Install/update/rollback/interruption/recovery/uninstall passed on the stated functional candidate; final display/package delta checks remain |
| Setup and distribution | Real reference and inference setup, fixed downloads and hash verification, isolated environment, source installer | Download-like acquisition and installed use passed; build and verify the final ZIP with completed SBOM/documents |
| Windows acceptance | Current Windows 11 host, CUDA/fp32 RTX2060 SUPER 8GiB, responsive real GUI, restart and real-data preserving uninstall | Current-host installation/uninstallation is the agreed scope; capability-dependent reparse tests remain unpassed; other OS/GPU and physical-offline conditions remain unverified |
| Publication and retrieval | Target rtonf/neutral-tts-workbench and own-source licensing settled; unsigned specification settled | Exact commit/tag/version, final hashes and approval; limited-reference publication, clone/Release retrieval and link/hash checks |

[Component conditions](INFERENCE_COMPONENTS.md) distinguish local use from
runtime/model redistribution. The installer contains application source;
external model, tokenizer, codec, watermark, runtime and native payloads are
obtained under their own conditions. [Third-party notices](../THIRD_PARTY_NOTICES.md)
retain exact compatibility and attribution disclosures. Unresolved permission
for an excluded component does not authorize its use or redistribution.

Code signing is outside this release specification (今回の配布仕様では対象外).
It is not a signature-validation success. Hash, data-safety and applicable
license checks remain required. See [unsigned distribution](UNSIGNED_DISTRIBUTION.md).
Current-host results are not clean-Windows or physical-offline evidence.

Local source-review archives marked INTERNAL_SOURCE_REVIEW remain private
verification artifacts. They are separate from the Windows installer candidate
and do not by themselves complete product acceptance. The public gates file
keeps source, model, tokenizer, codec, native, playback, installer and signing
payload decisions explicit; neither an excluded payload nor a test skip is a
passed use or redistribution gate.
