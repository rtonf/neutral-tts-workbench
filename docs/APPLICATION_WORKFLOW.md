# Framework-neutral application workflow

The application layer joins the existing filename, plan, estimate, receipt,
recovery, and optional history contracts without selecting a GUI or web
framework. `ApplicationService` accepts a tokenizer-count callback, a
`PersistedSettings` snapshot (or `SettingsStore` adapter), and an optional
backend implementing the small `Backend` protocol. The core does not select or
load a synthesis engine. A separate compatible adapter can receive lazy,
machine-local runtime objects from a host.

The workflow is:

1. Each public operation loads exactly one settings snapshot containing the
   filename context, plan and pause settings, tokenizer identity, calibration,
   output directory, generation recovery root, sample-rate range, and optional
   history bindings. The service copies nested
   configuration into its immutable contract types at entry and uses that
   content-fixed snapshot for the entire operation. A later `SettingsStore`
   change applies to the next public operation; the operation already in
   progress neither mixes snapshots nor chases the latest store value. This
   application host owns generation recovery only. Restore and install
   lifecycle operations use their separate low-level contracts.
2. Call `preview(source_text)`. The service builds one deterministic text plan,
   estimate, rendered filename, and collision-resolved candidate. This method
   only reads settings, configured root identities, and the output directory's
   collision view. It does not create recovery state, reconcile a root, or
   invoke a backend.
3. Display only `ApplicationPreview.public_payload()`. It contains hashes,
   counts, ranges, and a sanitized filename, but no raw source/synthesis text or
   filesystem path.
4. Call `run(source_text, preview)` for execution. `run()` obtains its own entry
   snapshot and uses it for preview recomputation, admission, recovery, backend
   work, and publication. `admit(source_text, preview)` exposes the same
   validation boundary for hosts that need to inspect an admission before
   execution and likewise uses one entry snapshot. Admission verifies every
   digest and settings binding and rejects source, settings, tokenizer,
   calibration, physical directory identity, root overlap, or filename
   collision drift. A safe settings change between the earlier `preview()` call
   and this operation makes the preview stale; lifecycle-root configuration is
   rejected as unsupported. Output and recovery roots must be physically
   separate even when different path spellings identify the same directory.
5. `startup()` and `run()` enter the same identity-bound recovery context. It
   gives the preflight recovery identity to the operating-system owner, holds a
   guard for the admitted output directory, and revalidates the generation-root
   digest before reconciliation. Generation journals are reconciled first.
   When history is enabled, its sidecars are then reconciled under that same
   owner and checked against the completed journal plus the exact published
   size and SHA-256. `startup()` then exits; `run()` retains the same context
   through generation, publication, and history completion. Every normal
   return or exception releases the context, and process termination releases
   the OS claim for the next startup.
6. The recovery manager creates a durable `prepared` journal and a private
   per-run staging directory. A backend writes exactly the private staging path
   in `GenerationRequest` and
   returns `BackendResult` plus `ActualRun` facts. The request necessarily holds
   the raw plan for a host-approved adapter; it is internal-only and must
   never be serialized or logged. A backend that can prove the exact bytes it
   intended to emit also returns their SHA-256 in `BackendResult`.
7. Before the durable `ready` transition, the service checks the staged artifact
   is stable mono PCM16 WAV data. The RIFF extent and every chunk extent must fit
   the exact file, the data extent must be non-empty and frame-aligned, and the
   sample rate must be no greater than 768,000 Hz. It verifies sample rate,
   duration, byte count, and any backend-supplied intended-byte SHA-256 against
   one stable descriptor read and constructs the strict receipt, which rejects
   plan-count drift. Recovery hashes the file again before `ready` and rejects
   a change from the application-validated size/SHA-256 handoff.
8. With history enabled, the service first writes a canonical `pending`
   sidecar containing the source-free receipt, public backend identity, staged
   size/SHA-256, and hashes of the host's runtime configuration and voice-rights
   manifest. A SHA-256 over every immutable sidecar claim is recorded in the
   recovery journal before publication. Publication then creates a
   same-filesystem hard link with atomic no-replace semantics at the exact
   output name bound at admission. A newly occupied name rejects the run
   instead of selecting another suffix after admission. The recovery journal's
   durable `completed` transition records the sidecar binding and matching
   staged and published size/SHA-256 evidence; the linked target is rehashed
   before that transition. Only then can history become `completed`. History
   also reparses the published WAV and checks its byte count, sample rate, and
   frame-derived duration against the receipt. A later startup can finish that
   second phase after process loss without constructing a backend.
   `ApplicationOutcome.feedback_payload()` exposes actual-versus-estimate
   receipt feedback without source text or paths.

`ApplicationService.startup()` performs identity-bound, explicitly scoped
reconciliation without invoking a backend. Ordinary CLI `preview` calls this
startup entry before its pure preview. CLI `demo-run` receives the startup
result from the same recovery context used by `run()` before the new generation
begins. Public JSON distinguishes `STARTUP_RECOVERY` from `NEW_GENERATION` and
states `backend_invoked: false` for startup recovery. Completed WAV files are
verified and retained; incomplete, failed, cancelled, malformed, and orphan run
state is moved to quarantine. Enabled history lists only records whose sidecar,
completed journal evidence, and current output bytes all agree.

`PersistedSettings` retains fields for separately hosted restore and install
lifecycle integration. Supplying any of those fields to `ApplicationService`
as the entry snapshot raises `UnsupportedLifecycleIntegrationError` before an
output or recovery root is inspected, created, locked, or reconciled, and
before backend identity is read. If a store changes after returning a supported
entry snapshot, the in-progress operation remains bound to that snapshot and
cannot access roots added only to the later store value. The next public
operation reads the later value and rejects it. The standalone backup/restore
and install APIs remain available under their dedicated ownership contracts.

The separate `neutral-tts-backup` host exposes explicit `backup`, `restore`,
and `recover` commands without being called by generation or startup. The
operator selects a dedicated data root; the existing backup contract considers
only `settings`, `history`, `dictionary`, and `voice_profiles`, accepts only its
bounded JSON data types, and ignores unrelated root files. It never selects an
entire user directory, audio, model/runtime payloads, or executables. Backup
publication and the caller-selected pre-restore safety archive are fresh-file,
no-overwrite operations. Restore validates the archive and available workspace
capacity before delegating mutation, then reuses the existing safety snapshot,
directory guards, rollback journal, final integrity verification, and explicit
recovery operation. Its JSON result reports only bounded counts, digests, and a
terminal state; local paths and exception text are discarded.

For a synthetic or separately approved data root, invoke the host explicitly:

```powershell
$env:PYTHONPATH = ".\src"
python -m neutral_tts.backup_restore_cli backup `
  --data-root .staging/synthetic-data `
  --archive .staging/synthetic-data.zip
python -m neutral_tts.backup_restore_cli restore `
  --archive .staging/synthetic-data.zip `
  --data-root .staging/empty-restore-target `
  --safety-backup .staging/pre-restore-safety.zip
python -m neutral_tts.backup_restore_cli recover `
  --data-root .staging/empty-restore-target
```

## Optional compatible backend

`IrodoriTokenizerCounter` lazily loads only a host-selected local tokenizer and
checks its expected vocabulary size and BOS token identity. It counts the BOS
token exactly as the compatible runtime wrapper does; it does not construct an
inference runtime. `IrodoriBackend` accepts a host-supplied adapter factory,
runtime resources, synthesis-request type, reference paths, and a reference
integrity/rights guard. Optional packages are imported only by the supplied
host or by the counter's first use.

Before the first sampler call, the backend verifies watermark readiness and
recounts every admitted chunk through the runtime tokenizer. It holds the
host's reference lease through sampling, accepts only the bounded `balanced` /
`standard` profile, and rejects a changed reference, token drift, non-48 kHz
result, non-mono or non-finite sample data, out-of-range samples, and a chunk
longer than 30 seconds. Accepted chunks are converted to PCM16, separated by
the exact admitted boundary pause, and written once to the application-owned
staging path. The writer hashes the deterministic WAV header, PCM, and pauses
as they are emitted, verifies the closed staging file has the same bytes, and
returns that digest for the application/recovery handoff. Runtime cleanup runs
on success, ordinary failure, and interruption.

The command parser exposes `real-run`, while construction remains an explicit
host boundary: a wrapper calls `neutral_tts.cli.main(...,
real_runtime_factory=...)` and returns a `RealRuntimeBinding`. The binding
contains only a tokenizer callback, backend object, bounded public identities,
and SHA-256 bindings for machine-local runtime configuration and voice-rights
evidence. The console entry point has no implicit plugin loader or arbitrary
module runner and rejects `real-run` when no host factory is supplied. Before
calling that factory, `real-run` rejects reserved lifecycle arguments, checks
the private configuration file boundary, performs a model-free preview/root
preflight, and reconciles existing recovery/history state. A rejected root,
template, source, or lifecycle configuration therefore cannot cause runtime
construction.

Successful hosted runs create verified history. The `startup`, `history`, and
`play` commands require no runtime factory and do not tokenize or invoke a
backend. `play` rereads the selected completed WAV, rejects links and byte/hash
drift, validates non-empty mono PCM16, and passes those same verified bytes to
synchronous `winsound` playback. Calibration for a new real run uses only
verified receipts with the same runtime-configuration hash, voice-rights hash,
device, quality, reading, and tokenizer identity.

This adapter source adds no package dependency or runtime payload. Component
selection, exact asset verification, voice consent/rights, and runtime
construction remain responsibilities of the machine-local host and the
separate distribution gates in `docs/RELEASE_NO_GO.md`. Repository tests use
fake adapter objects and synthetic PCM only; they are not real-model execution,
voice quality, consent, or redistribution evidence.

### Model-free machine runtime doctor

`neutral-tts-runtime-doctor` performs a bounded, sanitized inspection before a
host considers constructing the optional runtime. The caller supplies the
expected Python identity, the existing requirements-lock digest, one or more
installed-metadata roots, a bounded list of native files, and an explicit
`nvidia-smi` path when the GPU query is enabled. Executable paths must be
absolute paths selected by trusted, machine-local host configuration; they must
never come from source text, an end user, an archive, or another untrusted
input. The regular-file and reparse checks do not authenticate a binary, check
its signature or hash, contain child processes, or make arbitrary executables
safe. A trusted executable runs with the diagnostic process's authority. The
host is responsible for its provenance and directory/DLL trust before calling
the doctor. `nvidia-smi` is never resolved implicitly from `PATH`.

The command runs the declared Python executable only with a fixed `-I -S`
standard-library identity probe and runs `nvidia-smi` only with its fixed
read-only field query. Both probes use timeouts and streaming byte limits:
Python stdout/stderr are capped at 4096/1024 bytes; GPU stdout/stderr are capped
at 8192/2048 bytes, with at most 16 rows of 256 characters. An over-limit,
timed-out, malformed, or non-UTF-8 response is rejected without emitting the
response or an exception.

The doctor parses `.dist-info/METADATA` and `direct_url.json` itself, checks
exact `==` versions and Git commit identities from the lock, and never imports
the installed packages. Lock and `METADATA` reads are capped at 1 MiB, direct
URL metadata at 64 KiB, and additional requirements at 512 entries of 2048
characters. Reads use an opened regular-file descriptor, a max-plus-one loop,
and before/after file identity and size checks. A detected link, reparse point,
replacement, growth, truncation, or over-limit file fails closed.
Normalized distribution names, versions, and caller-declared native component
IDs are diagnostic identifiers and may appear in the local JSON report. Hosts
must use non-sensitive component IDs and must not publish the report as a
general telemetry payload.

Native files with an expected SHA-256 are hashed only below a fixed 64 MiB
limit, using the same max-plus-one and file-identity checks. Larger files are
`UNVERIFIED`; presence-only declarations establish only inspection-time regular
file presence. The report separates `VERIFIED`, `MISSING`, `MISMATCH`, and
`UNVERIFIED` results and always returns an overall `INCOMPLETE` or `NOT_READY`
status with `execution_ready` false. `NOT_READY` means a declared diagnostic
input is missing or mismatched, not that a real generation attempt failed. In
particular, matching Python and distribution metadata or observing GPU driver
metadata does not establish native ABI compatibility, DLL loadability, CUDA
usability, model loading, watermark readiness, synthesis, playback, or
generated-output acceptance.

The output and recovery roots are an owner-private local trust boundary. The
guards and hash chain detect pathname, byte, and evidence drift between stages;
they do not make files immutable against a different process that already has
authority to rewrite every file as the same operating-system account. A host
must apply account-appropriate ACLs and must not share these roots with an
untrusted writer.

## Explicit demo adapter

`DEMO_SILENCE_ONLY` is the only self-contained backend. It creates a valid 100 ms,
16 kHz, mono PCM16 WAV containing silence. It does not synthesize speech and it
does not import a model, tokenizer asset, sampler, GPU library, audio runtime,
or native dependency. Only the exact supplied adapter receives the demo label,
and the application verifies that its complete PCM payload is zero-valued before
publication. Its CLI output says `DEMO_SILENCE_ONLY`, `speech: false`, and
`audio_kind: silence`; a caller-controlled backend identity alone cannot obtain
that label. A successful demo must not be reported as real TTS generation.

The built-in demo tokenizer is also synthetic: it counts one token per Unicode
code point and is identified as `demo-tokenizer-v1`. It is not evidence that an
engine tokenizer or model is compatible.

Run a privacy-bounded preview:

```powershell
$env:PYTHONPATH = ".\src"
python -m neutral_tts.cli preview `
  --text "Synthetic example" `
  --output-dir .staging/demo-output `
  --staging-dir .staging/demo-stage
```

Exercise preview, admission, safe publication, and receipt feedback with
silence only:

```powershell
$env:PYTHONPATH = ".\src"
python -m neutral_tts.cli demo-run `
  --text "Synthetic example" `
  --output-dir .staging/demo-output `
  --staging-dir .staging/demo-stage
```

Here `--staging-dir` is the application-owned generation recovery root. The
CLI reserves `--restore-live-data-root`, `--install-app-root`, and
`--install-user-data-root` for a separate lifecycle host. Supplying any of them
returns `UNSUPPORTED_LIFECYCLE_INTEGRATION` before output or staging setup.

After installation, the same commands are available through the
`neutral-tts` console entry point. The CLI emits deterministic JSON shapes and
discards exception text; errors use a fixed privacy-safe code/message instead
of echoing source text, credentials, or local paths.
