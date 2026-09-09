# Framework-neutral application workflow

The application layer joins the existing filename, plan, estimate, and receipt
contracts without selecting a GUI, web framework, synthesis engine, sampler,
or runtime. `ApplicationService` accepts a tokenizer-count callback, a
`PersistedSettings` snapshot (or `SettingsStore` adapter), and an optional
backend implementing the small `Backend` protocol.

The workflow is:

1. Load one settings snapshot containing the filename context, plan and pause
   settings, tokenizer identity, calibration, output directory, and generation
   recovery root. This application host owns generation recovery only. Restore
   and install lifecycle operations use their separate low-level contracts.
2. Call `preview(source_text)`. The service builds one deterministic text plan,
   estimate, rendered filename, and collision-resolved candidate. This method
   only reads settings, configured root identities, and the output directory's
   collision view. It does not create recovery state, reconcile a root, or
   invoke a backend.
3. Display only `ApplicationPreview.public_payload()`. It contains hashes,
   counts, ranges, and a sanitized filename, but no raw source/synthesis text or
   filesystem path.
4. Call `run(source_text, preview)` for execution. `admit(source_text, preview)`
   exposes the same validation boundary for hosts that need to inspect an
   admission before execution. Admission recomputes the plan and estimate,
   verifies every digest and settings binding, and rejects source, settings,
   tokenizer, calibration, physical directory identity, root overlap, or
   filename collision drift. Output and recovery roots must be physically
   separate even when different path spellings identify the same directory.
5. `startup()` and `run()` enter the same identity-bound recovery context. It
   gives the preflight recovery identity to the operating-system owner, holds a
   guard for the admitted output directory, and revalidates the generation-root
   digest before reconciliation. It reconciles generation journals only.
   `startup()` then exits; `run()` retains the same context through generation
   and publication. Every normal return or exception releases the context, and
   process termination releases the OS claim for the next startup.
6. The recovery manager creates a durable `prepared` journal and a private
   per-run staging directory. A backend writes exactly the private staging path
   in `GenerationRequest` and
   returns `BackendResult` plus `ActualRun` facts. The request necessarily holds
   the raw plan for an eventual approved adapter; it is internal-only and must
   never be serialized or logged.
7. Before the durable `ready` transition, the service checks the staged artifact
   is stable mono PCM16 WAV data. The RIFF extent and every chunk extent must fit
   the exact file, the data extent must be non-empty and frame-aligned, and the
   sample rate must be no greater than 768,000 Hz. It verifies sample rate,
   duration, and byte count against the backend's actual facts and constructs
   the strict receipt, which rejects plan-count drift.
8. Publication is the final side effect: a same-filesystem hard link with
   atomic no-replace semantics into the exact output name bound at admission.
   A newly occupied name rejects the run instead of selecting another suffix
   after admission. The durable `completed` transition records matching staged
   and published size/SHA-256 evidence.
   `ApplicationOutcome.feedback_payload()` then exposes actual-versus-estimate
   receipt feedback without source text or paths.

`ApplicationService.startup()` performs identity-bound, explicitly scoped
reconciliation without invoking a backend. Ordinary CLI `preview` calls this
startup entry before its pure preview. CLI `demo-run` receives the startup
result from the same recovery context used by `run()` before the new generation
begins. Public JSON distinguishes `STARTUP_RECOVERY` from `NEW_GENERATION` and
states `backend_invoked: false` for startup recovery. Completed WAV files are
verified and retained; incomplete, failed, cancelled, malformed, and orphan run
state is moved to quarantine.

`PersistedSettings` retains fields for separately hosted restore and install
lifecycle integration. Supplying any of those fields to `ApplicationService`
raises `UnsupportedLifecycleIntegrationError` before an output or recovery root
is inspected, created, locked, or reconciled. The standalone backup/restore and
install APIs remain available under their dedicated ownership contracts.

## Explicit demo adapter

`DEMO_SILENCE_ONLY` is the only supplied backend. It creates a valid 100 ms,
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
