# Feature contracts

## Safe WAV filename templates

The template engine accepts only these placeholders:

`date`, `time`, `title`, `voice`, `quality`, `reading`, `job_id_short`, and
`sequence`.

The default template contains no voice or personal identifier. Templates are a
single filename component, not a path. Positional or nested fields, conversions,
format specifications, separators, drive/UNC syntax, alternate-data-stream
syntax, traversal, control characters, unpaired surrogates, and user-supplied
extensions are outside the contract.

The engine normalizes Unicode to NFC, sanitizes substituted values for Windows,
handles reserved device names, enforces component/byte/full-path bounds, and
owns the `.wav` extension. Preview is pure: it does not create a directory,
reserve a job, or call an engine. Collision identity is NFC plus case-folding.
The first candidate uses the rendered base name; later candidates use `-002`,
`-003`, and so on. Publication uses an atomic no-replace operation and must
preserve both the staged source and every pre-existing destination.

Publication accepts three separate values: an absolute, regular staged WAV;
an absolute, existing output directory whose path chain contains no symlink or
Windows reparse point; and one validated product-owned `.wav` basename. It does
not accept an arbitrary destination path or a legacy two-argument form. The
current standard-library implementation publishes only by a same-filesystem
hard link; it fails closed rather than exposing a partial copy or cleanup race.
Before linking, the staged file must parse as a stable, non-empty mono PCM16
RIFF/WAVE stream. Its declared RIFF length and every padded chunk extent must
match the bounded physical file, its data extent must be positive and
frame-aligned, and its sample rate must be in the supported range through
768,000 Hz. A regular file with only a WAV-looking name, a truncated declared
extent, or an out-of-contract sample rate is not a valid staged output.

## Shared pre-generation plan

Dictionary application, neutral normalization, exact tokenizer counting,
token-budget chunking, quality/reading settings, and the inter-chunk pause
policy form one immutable plan. Estimation and a future approved generator must
receive that same plan. A character heuristic must never be labelled as the
authoritative tokenizer plan.

The current source core receives a tokenizer callback from its caller. It does
not import an inference runtime, initialize a GPU, load model weights, or invoke
a sampler. A future adapter must construct the exact pinned tokenizer without
constructing the inference runtime, and generation must reject tokenizer/plan
drift before any sampler authorization.

## Estimate semantics

The preview reports source and synthesis character counts, exact supplied-token
count, chunk/sampler-call count, inter-chunk boundary count, pause duration,
coarse audio-duration range, mono PCM16 WAV byte range, peak staging range, and
device/quality generation-time range.

Mono PCM16 storage uses the bounded sample-rate range and the formula:

`44-byte WAV header + frames × 2 bytes`.

Peak staging includes per-chunk WAVs, stitch/candidate copies, final output, and
bounded metadata overhead. Output storage and peak temporary storage are
separate values.

Calibration confidence has three states:

- `unknown`: no valid successful history; duration/storage policy remains broad
  and generation time is not presented as known;
- `low_sample`: fewer than the documented threshold of matching successful
  receipts; ranges are deliberately widened;
- `calibrated`: enough matching, valid receipts exist for the same device,
  quality, reading, and tokenizer identity.

Displayed ranges are rounded outward. They are planning aids, not countdowns or
completion guarantees.

Calibration keys contain exactly four bounded ASCII identifiers: device class,
quality, reading, and tokenizer identity. Delimiters, whitespace, local paths,
Unicode display labels, and arbitrary caller-provided keys are not public
calibration identities.

## Receipt and privacy contract

A successful receipt freezes the original estimate beside actual aggregate
counts, audio facts, wall time, staging peak, and an `in_range`,
`outside_range`, or `unknown` relation. An out-of-range result does not rewrite
the estimate. Calibration accepts only structurally valid successful receipts
with a matching generic calibration key.

Public estimate/receipt payloads contain no raw source text, synthesis text,
chunk text, local absolute path, profile path, private runtime object, or
generated history from another product lane.

Only the receipt-facing calibration entry point is public. Its parser requires
the exact receipt schema and exact plan, estimate, and actual aggregate shapes;
their digests, keys, and counts must agree. Unknown fields and incomplete or
forged receipt-like dictionaries are rejected rather than used as history.

## Application admission and demo contract

`ApplicationService` is the framework-neutral boundary between persisted
settings, preview, backend authorization, safe publication, and receipt
feedback. Admission recomputes the pure preview and binds source/settings,
plan, estimate, collision-resolved filename, and the physical identities of all
configured operational roots. Different path spellings, including filesystem
aliases, cannot make output, recovery, restore, or install roots equal or
nested. Any drift rejects the request before backend use.
The final collision selection must still equal the previewed candidate; it may
not silently move to a suffix after the user approved a preview.

`preview()` itself performs no startup reconciliation and creates no recovery
files. It may read physical identities for explicitly configured roots and the
output directory to select the collision candidate. The ordinary CLI `preview`
command is also a startup entry: it reconciles first and then calls the pure
preview method.

The backend receives the immutable internal text plan and one fresh private
staging pathname. Raw plan text is necessary for an eventual approved adapter,
but is never part of the application view-model or receipt. Publication remains
bound to the output directory verified at admission even if a mutable settings
store changes while the backend is running.

Before the atomic no-replace publication, the service checks the backend's
actual sample rate, duration, byte size, and aggregate plan counts against the
staged PCM16 WAV and the admitted plan. These checks and strict receipt
construction complete before the run reaches the durable `ready` state.

`startup()` and `run()` use one shared identity-bound recovery context. It binds
the preflight recovery identity to the operating-system owner, holds the output
directory guard, and verifies the complete configured-root snapshot before
reconciliation. It reconciles the configured generation root and only the
explicitly supplied restore live-data and install application/user-data roots.
`run()` retains the context through durable completion. Startup recovery never
invokes the generation backend. Its public status is separate from the
`NEW_GENERATION` result, and a competing ordinary startup is rejected while the
owner is live.

The supplied `DEMO_SILENCE_ONLY` adapter is synthetic workflow evidence only.
It writes silence, not speech, and its code and JSON output must always say so.
Demo labeling requires the exact supplied adapter rather than a caller-provided
identity string, and its complete PCM payload is verified as zero-valued before
publication. It is not an engine implementation, tokenizer compatibility
result, model test, audio-quality test, or Production evidence. See
`docs/APPLICATION_WORKFLOW.md` for the complete port and CLI contract.

## Recovery and backup contract

Each recoverable run owns a unique staging directory and a durable canonical
journal. State is flushed at externally visible transitions. Publication uses
no-replace hard links, so an existing output is preserved. General recovery
library callers can request deterministic collision names; application-admitted
runs publish only the exact approved name. Startup reconciliation quarantines malformed, incomplete,
cancelled, failed, and orphan staging state without deleting a completed
output. A completed journal is accepted only when its staged and published
size/SHA-256 facts are complete, mutually consistent, and match the surviving
files. Journal bytes must equal the canonical serialization for the exact
state-specific field set; duplicate keys, unknown fields, alternate encodings,
and noncanonical ordering or whitespace are quarantined. Structured
reconciliation evidence distinguishes recovered publication, verified
completed output, and quarantined run state without reporting a backend rerun.
Fault injection covers ready and publication boundaries.

Backups include exactly settings, history, dictionary data, and approved
generic voice-profile metadata. The versioned manifest lists all supported
categories plus each relative path, category, size, SHA-256, and an aggregate
SHA-256. Restore validates the complete deterministic ZIP before mutation,
creates and validates a safety backup, stages all categories, and rolls every
changed category back if any transition fails. A durable phase journal lets
startup reconciliation restore the safety snapshot after process death. See
`docs/DATA_PROTECTION.md`.

## Source-only lifecycle and release-foundation contract

The install-layout contract accepts only manifest-bound source files under one
application root with an external user-data root. Fresh install never replaces
an existing root. Update retains the prior complete layout, and explicit
rollback retains the displaced layout. Uninstall relocates only exact
manifest-owned files; user data is never an implicit target. Each transition is
journaled before mutation and has an idempotent startup reconciler. One
process-scoped operating-system lock covers reconciliation and the complete
lifecycle transition; process death releases it for the next startup. The
owned implementation verifies that the live lock handle is bound to the same
application root and operation before any journal access or mutation.

The release record binds an internal candidate identifier to exact source and
artifact hashes, provenance, update/rollback rules, a signature target, and
clean-Windows checks. It can represent only an unsigned, pending,
`NO_GO_PUBLIC_BINARY` state. No launcher or installer artifact is produced.
