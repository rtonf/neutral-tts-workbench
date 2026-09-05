# Framework-neutral application workflow

The application layer joins the existing filename, plan, estimate, and receipt
contracts without selecting a GUI, web framework, synthesis engine, sampler,
or runtime. `ApplicationService` accepts a tokenizer-count callback, a
`PersistedSettings` snapshot (or `SettingsStore` adapter), and an optional
backend implementing the small `Backend` protocol.

The workflow is:

1. Load one settings snapshot containing the filename context, plan and pause
   settings, tokenizer identity, calibration, output directory, and generation
   recovery root. A restore live-data root and an install application/user-data
   root pair are optional and are used only when explicitly configured.
2. Call `preview(source_text)`. The service builds one deterministic text plan,
   estimate, rendered filename, and collision-resolved candidate. This method
   only reads settings and the configured output directory. It does not create
   recovery state, reconcile a root, or invoke a backend.
3. Display only `ApplicationPreview.public_payload()`. It contains hashes,
   counts, ranges, and a sanitized filename, but no raw source/synthesis text or
   filesystem path.
4. Call `run(source_text, preview)` for execution. `admit(source_text, preview)`
   exposes the same validation boundary for hosts that need to inspect an
   admission before execution. Admission recomputes the plan and estimate,
   verifies every digest and settings binding, and rejects source, settings,
   tokenizer, calibration, directory, or filename collision drift.
5. `run()` takes a nonblocking operating-system owner claim for the configured
   generation recovery root. While holding that claim, it reconciles generation
   journals and the explicitly configured restore/install roots. The claim
   remains held through generation and publication and is released on every
   normal return or exception; process termination releases the OS claim for the
   next startup.
6. The recovery manager creates a durable `prepared` journal and a private
   per-run staging directory. A backend writes exactly the private staging path
   in `GenerationRequest` and
   returns `BackendResult` plus `ActualRun` facts. The request necessarily holds
   the raw plan for an eventual approved adapter; it is internal-only and must
   never be serialized or logged.
7. Before the durable `ready` transition, the service checks the staged artifact is stable mono
   PCM16 WAV data and verifies its sample rate, duration, and byte count against
   the backend's actual facts. It also constructs the strict receipt, which
   rejects plan-count drift.
8. Publication is the final side effect: a same-filesystem hard link with
   atomic no-replace semantics into the exact output name bound at admission.
   A newly occupied name rejects the run instead of selecting another suffix
   after admission. The durable `completed` transition records matching staged
   and published size/SHA-256 evidence.
   `ApplicationOutcome.feedback_payload()` then exposes actual-versus-estimate
   receipt feedback without source text or paths.

`ApplicationService.startup()` performs the same explicitly scoped
reconciliation without invoking a backend. Ordinary CLI `preview` calls this
startup entry before its pure preview. CLI `demo-run` receives the startup
result from `run()` before the new generation begins. Public JSON distinguishes
`STARTUP_RECOVERY` from `NEW_GENERATION` and states `backend_invoked: false` for
startup recovery. Completed WAV files are verified and retained; incomplete,
failed, cancelled, malformed, and orphan run state is moved to quarantine.

## Explicit demo adapter

`DEMO_SILENCE_ONLY` is the only supplied backend. It creates a valid 100 ms,
16 kHz, mono PCM16 WAV containing silence. It does not synthesize speech and it
does not import a model, tokenizer asset, sampler, GPU library, audio runtime,
or native dependency. All CLI output labels it as `DEMO_SILENCE_ONLY`,
`speech: false`, and `audio_kind: silence`; a successful demo must not be
reported as real TTS generation.

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
CLI also accepts `--restore-live-data-root` and the paired
`--install-app-root` / `--install-user-data-root` options. Only supplied roots
are reconciled; the CLI does not discover or sweep other directories.

After installation, the same commands are available through the
`neutral-tts` console entry point. The CLI emits deterministic JSON shapes and
discards exception text; errors use a fixed privacy-safe code/message instead
of echoing source text, credentials, or local paths.
