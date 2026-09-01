# Unsigned source-candidate foundation

The release foundation defines deterministic records for future review. It
does not create a launcher, installer, signature, certificate, or public
release.

`release_contract.py` binds a candidate identifier to one exact Git commit and
tree plus SHA-256 values for the dependency lock, install-layout manifest,
SPDX SBOM, CycloneDX SBOM, and internal source archive. Its provenance record
states that the build is source-contract-only, used no network, includes no
external payload, and makes no SLSA conformance claim.

The record validator checks digest syntax and internal consistency; it does not
have artifact paths and therefore cannot prove bytes from a digest alone. A
consumer must independently hash each immutable artifact (the source-archive
verifier already does this for its archive and generated metadata) before
treating the declared digest set as evidence.

The signature target is the SHA-256 of canonical signable fields. Its only
valid current state is `UNSIGNED`; algorithm, signature, and certificate fields
must remain null. A future signing implementation requires a new approved
contract and independent signature verification.

The clean-Windows template lists the physical clean snapshot, offline execution,
layout-integrity, demo-workflow, external-payload, update/rollback, owned-only
uninstall, and user-data-preservation checks. This repository accepts only the
canonical `PENDING` template. Digest-shaped `PASSED` or `FAILED` edits are
rejected because there is no trusted physical-device verifier or authority key
in this source tree. A future externally attested result requires a separately
approved verifier and schema.
