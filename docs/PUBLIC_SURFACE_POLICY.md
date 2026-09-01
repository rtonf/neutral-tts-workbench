# Public surface policy

## Target state

This repository contains a neutral, standard-library-first TTS source core for
safe output naming and pre-generation planning/estimation. It contains no
model, runtime, voice, generated audio, user data, installer, or release
payload. Product identity is a replaceable working setting, not a claim of an
official or partnered product.

The public surface is assembled only from these positive classes:

- neutral Python source under `src/neutral_tts/`;
- deterministic tests with synthetic data under `tests/`;
- source-export and public-surface scanning tools under `tools/`;
- build metadata containing no machine-specific paths;
- public design, release-status, license-matrix, and attribution documents.

No binary asset class is currently permitted. The public asset allowlist is
therefore empty.

Public text and metadata are authored only from this positive target state.
The private scan policy supplies literal, semantic, rationale, and attention
checks without placing their rule values in the public tree. Those checks run
against source, UI/CLI strings, samples, SBOMs, the candidate tree, reachable
Git history, and every archive in scope.

## Required disclosure exceptions

Exact third-party names, license identifiers, copyright notices, compatibility
statements, and unresolved redistribution statuses may appear in the public
license matrix, third-party notices, and release-status document. These are
required disclosures and are not privacy exceptions for any other path.
They also do not suppress credential, local-path, user-data, or unrelated
surface checks.

## Fail-closed rule

An unclassified file, symlink/reparse point, archive member, binary payload,
private pattern match, credential-like string, local path, generated audio,
model/runtime payload, or disallowed extension fails the scan. A file is not
public merely because it is ignored by Git. Staging and built archives must be
scanned independently, and the final commit tree plus reachable Git history
must be scanned at the candidate gate.
