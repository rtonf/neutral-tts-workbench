# Clean public-source lane policy

These instructions apply to the owning assistant working in this
repository. This repository is a neutral, source-only development lane whose
public identity and capabilities are defined entirely by its current target
state.

The owning assistant performs all implementation and review directly. Do not
start, resume, delegate to, or use subagents, including read-only reviewers.
Self-verification must not be described as independent review.

## Protected and reference trees

- The exact protected read-only root is recorded only in the ignored private
  compliance ledger. Never edit, overwrite, delete, move, regenerate, package
  from, or use it as a test-output destination.
- Read the protected tree only to compute the approved before/after immutable
  manifest: sorted relative paths, file sizes, per-file SHA-256 values, and
  directory paths. Stop immediately if the after value differs.
- The exact reference-source root is also recorded only in the private ledger.
  Treat it as read-only. Import only explicitly allowlisted, content-hash-pinned
  generic files through the exporter. Never copy its Git metadata or history.
- Preserve all user-owned, ignored, untracked, and unrelated workspace data.
  Destructive cleanup is permitted only inside a newly created, path-verified
  staging directory under this repository.

## Public surface construction

- Construct the public surface from the approved current-state allowlist and
  the positive `TARGET_STATE` brief. Do not construct a broad candidate and
  subtract material afterward.
- Keep the neutral working product name in `src/neutral_tts/identity.py`.
  Other source and documentation must reference that setting rather than
  inventing or hard-coding another product identity.
- Upstream names may appear only where compatibility or legally required
  licensing/attribution needs exact identification. This lane must not imply
  official-product, endorsement, partnership, or redistribution approval.
- Mandatory license, copyright, and third-party attribution is public-required
  information. It must not be hidden by privacy or branding scans.
- Raw user text, audio, profiles, voice labels, local absolute paths, generated
  history, credentials, internal evidence, session data, caches, model/runtime
  payloads, and unapproved native dependencies must never enter the public
  source tree, Git history, staging candidate, archive, installer, metadata, or
  binary strings.

## Pink Elephant Guard

- `TARGET_STATE` is the sole source for public names, claims, examples,
  screenshots, sample data, and feature descriptions.
- `SURFACE` includes the README, documentation, source comments and strings,
  UI and CLI output, tests and samples, schemas, notices, SBOMs, candidate
  trees, reachable Git history, archives, and future binary strings.
- Detailed negative comparison rules live only in the ignored private scan
  policy. Public files must not reproduce, explain, paraphrase, or draw
  attention to those rules.
- `VISIBLE_EXCEPTIONS` are limited to exact license, copyright, compatibility,
  safety, and audit disclosures in paths explicitly scoped by the allowlist.
  An exception never suppresses credential, local-path, or user-data checks.
- Before accepting a candidate, scan literal, semantic, rationale, and
  attention-leak classes across the candidate tree, reachable Git history,
  every archive, and any package or sample surface in scope.

## Release status

- This lane is source-only and `NO-GO` for public binary release.
- Build and test local distribution candidates in isolated development
  locations using components with verified provenance and applicable rights.
  Candidate construction precedes clean-device acceptance. Keep runtime and
  model payloads outside the source repository and protected installations.
- Public repository creation, push, release upload, and publication require
  the user's approval of the exact destination and final candidate. External
  messages, paid services, certificate purchases, and host security changes
  require their own explicit authorization.
- Do not call a source test, local run, or current-device result Production.
- Unresolved rights block the affected use or distribution operation. Continue
  independent implementation and synthetic verification. Record uncertainty
  without inventing permission or equating missing evidence with prohibition.

## Current correction sequence

- Finish one bounded capability per candidate. First correct lifecycle
  ownership across processes and state-specific required journal integrity
  fields. Verify active-owner contention, process death, and damaged completed
  journals without treating a path guard as a transaction lock.
- Next connect recovery to the supported application startup and generation
  path. Acceptance must exercise those entry points, not only call recovery
  helpers directly. Keep demo output explicitly labeled as demo output.
- Then establish a real synthesis, save, playback, history, and restart path
  in the private development runtime. Record exact component versions,
  artifacts, upstream sources, and distribution conditions separately from
  the current source-only bill of materials.
- Freeze each review candidate by content identity. A later change to reviewed
  implementation or configuration invalidates the affected acceptance claim.
- The owning assistant implements fixes and verifies the fixed candidate
  directly, including failure injection at data-integrity boundaries.

## Stop and reporting rules

- Revise the hypothesis and state transitions when a failure recurs. Elapsed
  time or review counts alone do not end authorized implementation work.
- Stop affected operations for unexpected protected-data changes, exposed
  credentials, concrete data hazards, unavailable required environments, or
  unresolved decisions reserved to the user. Continue other work and present
  the exact missing decision or environmental requirement.
- Keep source completion, focused tests, full tests, clean commit, package,
  clean-device use, and human acceptance as separate states.
- Report each gate with facts, changed files, tests, remaining `NO-GO` items,
  and the protected-tree before/after comparison.
