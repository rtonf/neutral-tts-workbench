# Reproducible source-only packaging foundation

This repository can build a local `INTERNAL_SOURCE_REVIEW` ZIP from one exact
Git commit. The result is review evidence only. It is not a public source
release, binary release, installer, runtime bundle, redistribution approval,
or Production artifact.

## Fail-closed dependency closure

`config/dependency-closure.schema.json` defines the exact lock shape and
`config/dependency-closure.lock.json` records the current decisions. Every
component has fields for its source URL, wheel/archive URL, exact commit or
version, SHA-256, SPDX license expression/status, redistribution status,
notice requirement/status, approval status/authority, bundle decision, and
reason. Unknown facts remain `null` with an unapproved status; they are never
filled by inference. The only bundled component is the exact project source
Git tree.

Validate the internal lock:

```powershell
python tools/dependency_closure.py `
  --lock config/dependency-closure.lock.json
```

The public gate intentionally fails while the project license or any external
closure/redistribution fact is unresolved:

```powershell
python tools/dependency_closure.py `
  --lock config/dependency-closure.lock.json `
  --public
```

The lock stores the exact baseline commit at which the packaging foundation
was introduced. To avoid a self-referential tracked hash, the actual candidate
commit and tree are supplied to the builder and recorded in generated metadata
inside the archive.

## Exact generated metadata

For an exact commit, `tools/generate_source_metadata.py` generates:

- `SBOM.spdx.json`: deterministic SPDX 2.3 JSON for the included project source
  files only, with SHA-1 and SHA-256 file checksums, package verification code,
  per-file license information, and explicit `DESCRIBES`/`CONTAINS`
  relationships;
- `SBOM.cdx.json`: deterministic CycloneDX 1.7 JSON for the same exact source
  files, with SHA-1/SHA-256 hashes, a commit-bound subject, an explicit source
  assembly graph, and `incomplete_first_party_only` composition status so it
  cannot imply that excluded third-party/runtime closure is complete;
- `THIRD_PARTY_NOTICES.generated.md`: a deterministic decision table for every
  excluded closure record, clearly marked as not bundled and not approved.

Both SBOM timestamps come from the Git commit timestamp. Their namespaces or
serial number, component/file identifiers, order, and JSON encoding are
deterministic. Neither SBOM represents excluded model, tokenizer, codec,
runtime, native, or voice records as included components.

## Archive construction

First create or choose an existing private output parent under ignored
`.staging`. Then build from one exact 40-character commit:

```powershell
$commit = git rev-parse HEAD
python tools/build_source_archive.py `
  --root . `
  --commit $commit `
  --output ".staging/neutral-tts-source-review-$($commit.Substring(0,12)).zip" `
  --lock config/dependency-closure.lock.json `
  --allowlist compliance/source-asset-allowlist.json `
  --audience INTERNAL_SOURCE_REVIEW `
  --private-policy .private-compliance/public-scan-policy.json
```

The builder reads regular blobs from that Git tree only. It rejects symlinks,
submodules, non-allowlisted paths, nested archives, audio, model/checkpoint,
runtime/native, voice, database, executable, cache, and generated-history
payloads. It uses a newly created contained staging directory, deterministic
ZIP member order/metadata, no-overwrite creation, archive surface scanning,
and same-filesystem atomic hard-link promotion. A public or unspecified
audience is rejected.

`SOURCE_MANIFEST.json` records the exact source commit, source tree, closure
lock digest, and size/SHA-256 of every archive member except the manifest
itself. This exception avoids an impossible self-hash.

## Independent verification and reproducibility

Verify structure, canonical metadata, Git/tree equality, manifest hashes,
SPDX checksums/package verification code, CycloneDX source hashes/assembly,
forbidden content policy, and the public-surface scan. The verifier fails
closed unless the repository, lock, allowlist, exact commit, internal audience,
and private scan policy are all supplied:

```powershell
python tools/verify_source_archive.py `
  --archive ".staging/neutral-tts-source-review-$($commit.Substring(0,12)).zip" `
  --root . `
  --lock config/dependency-closure.lock.json `
  --allowlist compliance/source-asset-allowlist.json `
  --commit $commit `
  --audience INTERNAL_SOURCE_REVIEW `
  --private-policy .private-compliance/public-scan-policy.json
```

For reproducibility evidence, build the same commit to two previously absent
filenames and compare their SHA-256 values byte-for-byte. Existing destinations
are never overwritten. Keep these local candidates ignored under `.staging`;
do not upload, publish, attach them to a release, or call them Production.
