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
policy form one immutable plan. Estimation and every generator receive that
same plan. A character heuristic must never be labelled as the authoritative
tokenizer plan.

The source core receives a tokenizer callback from its caller. It does not
import an inference runtime, initialize a GPU, load model weights, or invoke a
sampler. The optional compatible counter lazily constructs a host-selected
local tokenizer, checks its expected vocabulary and BOS identity, and does not
construct the inference runtime. Exact asset-manifest verification remains a
host responsibility. The optional compatible backend repeats every chunk count
through the runtime tokenizer and rejects tokenizer/plan drift before any
sampler call.

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
  quality, reading, and tokenizer identity. Hosted real runs additionally
  select only history with the same private runtime-configuration and
  voice-rights-manifest hashes before applying this public four-part key.

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

## Application admission, compatible backend, and demo contract

`ApplicationService` is the framework-neutral boundary between persisted
settings, preview, backend authorization, safe publication, and receipt
feedback. Each public operation obtains one content-fixed settings snapshot at
entry. Nested configuration is copied into the immutable contract types, and
the same snapshot is used through preview recomputation, admission, recovery,
backend work, and publication. A store change after the snapshot is returned
applies to the next public operation rather than being mixed into the current
one. Admission binds source/settings, plan, estimate, collision-resolved
filename, and the physical identities of the generation output and recovery
roots. Different path spellings, including filesystem aliases, cannot make
those roots equal or nested. Any drift rejects the request before backend use.
The final collision selection must still equal the previewed candidate; it may
not silently move to a suffix after the user approved a preview.

`preview()` itself performs no startup reconciliation and creates no recovery
files. It may read the generation-root identities and the output directory to
select the collision candidate. The ordinary CLI `preview` command is also a
startup entry: it reconciles first and then calls the pure preview method.

The backend receives the immutable internal text plan and one fresh private
staging pathname. Raw plan text is necessary for a host-approved adapter,
but is never part of the application view-model or receipt. Publication remains
bound to the output directory in the operation snapshot even if a mutable
settings store changes while the operation is running. Roots that exist only in
the later store value are not read or mutated by the in-progress operation. The
next public operation reads the later snapshot and rejects an old preview or
unsupported lifecycle configuration.

Before the atomic no-replace publication, the service checks the backend's
actual sample rate, duration, byte size, and aggregate plan counts against the
staged PCM16 WAV and the admitted plan. These checks and strict receipt
construction complete before the run reaches the durable `ready` state.

The optional Irodori-TTS compatible source adapter is host-injected and keeps
optional imports, runtime resources, reference paths, and rights evidence out
of public payloads. Its tokenizer counter validates the pinned vocabulary and
BOS identity without creating an inference runtime. Generation requires
watermark readiness, checks every runtime token count before sampling, holds a
host-verified reference lease, accepts bounded 48 kHz mono sample data, inserts
the plan's exact boundary pauses, writes one application-owned staging WAV, and
always closes an opened runtime. The host supplies the exact adapter and
resource objects; this repository supplies no engine or asset payload.
Its repository tests use fake adapter/runtime objects and synthetic PCM; they
do not establish real-model execution, voice quality, consent, or
redistribution approval.

`startup()` and `run()` use one shared identity-bound recovery context. It binds
the preflight recovery identity to the operating-system owner, holds the output
directory guard, and verifies the generation-root snapshot before
reconciliation. It reconciles generation state first and optional history
sidecars second, and `run()` retains the context through durable completion.
Startup recovery never invokes the generation backend. Its public status is
separate from the `NEW_GENERATION` result, and a competing ordinary startup is
rejected while the owner is live.

Restore and install lifecycle integration is a separate hosting boundary.
Supplying any corresponding root field to `ApplicationService`, or any reserved
lifecycle-root option to the ordinary CLI, returns an explicit unsupported
configuration error before generation roots are inspected, created, locked, or
reconciled and before backend identity is read. The standalone backup/restore
and install APIs retain their own contracts below.

The supplied `DEMO_SILENCE_ONLY` adapter is synthetic workflow evidence only.
It writes silence, not speech, and its code and JSON output must always say so.
Demo labeling requires the exact supplied adapter rather than a caller-provided
identity string, and its complete PCM payload is verified as zero-valued before
publication. It is not an engine implementation, tokenizer compatibility
result, model test, audio-quality test, or Production evidence. See
`docs/APPLICATION_WORKFLOW.md` for the complete port and CLI contract.

## Verified history and playback contract

History is an optional second phase of a recovery-owned generation, not a
second publication mechanism. After WAV and receipt validation but before
publication, a canonical pending sidecar binds the run ID, requested output
basename, staged size/SHA-256, source-free receipt, backend identity, runtime
configuration digest, and voice-rights-manifest digest. It becomes completed
only when a completed recovery record names the same output, carries the same
published size/SHA-256, and the current regular output file still has those
exact bytes. Process loss after the publication link is recoverable: the next
startup completes the generation journal first and then the matching history
sidecar without tokenizer or backend access. Invalid, unprovable, changed, or
linked history entries are moved to quarantine.

History payloads expose a bounded run ID, WAV basename, size/SHA-256, public
backend identity, receipt aggregate facts, relation, and completion time. They
do not expose source text, runtime/reference paths, runtime configuration,
rights documents, or their private contents. The two binding digests remain in
the private sidecar and are used to isolate calibration evidence.

Playback accepts one verified completed history record, rereads the exact
regular non-link output while checking stable file identity, compares its
size/SHA-256 with history, validates a non-empty mono PCM16 WAV, and gives the
same verified byte string to synchronous Windows `winsound` playback. Startup,
history listing, and playback do not import or load the synthesis runtime.

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
