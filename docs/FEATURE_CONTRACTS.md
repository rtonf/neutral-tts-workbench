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
RIFF/WAVE stream with a bounded positive sample rate. A regular file with only
a WAV-looking name is not a valid staged output.

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
