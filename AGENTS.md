# Clean public-source lane policy

These instructions apply to the root agent and every delegated agent in this
repository. This repository is a new, neutral, source-only development lane.
It is not a modification, continuation, release branch, or package rebuild of
the separately accepted personal product.

## Protected and reference trees

- The exact protected personal-product root is recorded only in the ignored
  private compliance ledger. Never edit, overwrite, delete, move, regenerate,
  package from, or use it as a test-output destination.
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

- Construct the public surface from the approved current-state allowlist.
  Never copy a broader tree and then remove private, historical, branded, or
  unapproved material.
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

## Release status

- This lane is source-only and `NO-GO` for public binary release.
- Do not build or publish a model/runtime bundle, installer, GitHub release, or
  release upload while any redistribution, license, notice, signature, clean
  Windows, recovery, offline, voice-rights, or human-acceptance gate is open.
- Do not call a source test, local run, or current-device result Production.
- Unknown legal interpretation remains a blocker; agents do not make legal
  conclusions or infer redistribution permission.

## Required subagent model and dispatch evidence

- Every project subagent must run the effective model `gpt-5.6-luna`.
- Allowed reasoning efforts are exactly `medium`, `high`, and `max`.
- Every spawn must explicitly set `model: gpt-5.6-luna`, the chosen reasoning
  effort, and `fork_turns: none` or the smallest bounded history needed.
- Every delegation prompt must begin with:

  ```text
  MODEL REQUIREMENT: gpt-5.6-luna
  REASONING EFFORT: medium|high|max
  SELECTION REASON: <why this level fits>
  OWNERSHIP: <files and responsibility>
  ```

- A code-changing agent must be told that other agents share the codebase, must
  not revert their changes, and must adapt to concurrent edits.
- Self-report is not model evidence. The parent must inspect the effective local
  turn context and record `turn_context.model` and `turn_context.effort` in the
  single ignored delegation audit log. A result without observable effective
  evidence is not gate evidence and must be independently reproduced.
- Use `medium` for bounded inventory, mechanical tests, straightforward docs,
  or a small contract. Use `high` for implementation, integration design,
  root-cause work, and independent code review. Use `max` only for foundational
  architecture, security/data-integrity blockers, release blockers, or the
  final adversarial release review.
- Keep at most two delegated agents active. Give each agent disjoint file
  ownership. Limit review/fix work to two cycles and do not allow unbounded
  child spawning.

## Stop and reporting rules

- Stop and redesign after the same failure category occurs twice.
- One blocker has a 90-minute total limit across filenames, stages, agents, and
  approaches. At 45 minutes report: hypothesis, real end-to-end attempt, new
  facts, remaining budget, and `CONTINUE` or `STOP`.
- Keep source completion, focused tests, full tests, clean commit, package,
  clean-device use, and human acceptance as separate states.
- Report each gate with facts, changed files, tests, remaining `NO-GO` items,
  and the protected-tree before/after comparison.
