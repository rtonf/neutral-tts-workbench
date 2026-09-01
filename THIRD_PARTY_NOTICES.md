# Third-party notices and unresolved components

No third-party binary, model, tokenizer, codec, runtime, native library, or
voice asset is included in this source lane.

The following names identify compatibility targets or components known to need
future license and redistribution review. Listing a name is technical
provenance, not a legal conclusion, endorsement claim, partnership claim, or
permission to redistribute:

- Irodori-TTS compatible engine boundary — not bundled; license and
  redistribution approval not established here.
- SilentCipher checkpoint — not bundled; redistribution approval not
  established here.
- `sbintuitions/modernbert-ja-310m` tokenizer assets — not bundled;
  redistribution approval not established here.
- `semantic-dacvae-japanese-32dim` codec weights — not bundled;
  redistribution approval not established here.
- `sounddevice` — not bundled; identified as MIT-licensed in private technical
  provenance, but the intended public payload is not approved.
- PortAudio — not bundled; identified as MIT-licensed in private technical
  provenance, but the intended public payload is not approved.

Before any component is added, supply its exact version/revision, upstream
source, complete license text, copyright notice, required notices, content
hashes, dependency closure, and written redistribution decision. Generate the
SBOM and manifest from the exact candidate, not from this prose list.

The public license matrix records exact known values and explicit nulls for
version, source, artifact hash, license, redistribution, notice, approval, and
authority fields. A null or pending value is a blocker, not permission inferred
from a component name or compatibility target.

The machine-checked decision source is
`config/dependency-closure.lock.json`. Unknown URLs, versions, hashes, or rights
stay explicit and fail closed. For a local internal source-review archive,
`tools/generate_source_metadata.py` deterministically produces exact-commit
SPDX 2.3 and CycloneDX 1.7 documents for included project files, plus a
generated notice that lists these excluded records. Neither generated metadata nor this notice asserts
permission to redistribute an excluded component.
