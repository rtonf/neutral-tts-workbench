# Framework-neutral application workflow

The application layer joins the existing filename, plan, estimate, and receipt
contracts without selecting a GUI, web framework, synthesis engine, sampler,
or runtime. `ApplicationService` accepts a tokenizer-count callback, a
`PersistedSettings` snapshot (or `SettingsStore` adapter), and an optional
backend implementing the small `Backend` protocol.

The workflow is:

1. Load one settings snapshot containing the filename context, plan and pause
   settings, tokenizer identity, calibration, and private output/staging
   directories.
2. Call `preview(source_text)`. The service builds one deterministic text plan,
   estimate, rendered filename, and collision-resolved candidate.
3. Display only `ApplicationPreview.public_payload()`. It contains hashes,
   counts, ranges, and a sanitized filename, but no raw source/synthesis text or
   filesystem path.
4. Call `admit(source_text, preview)` or `run(source_text, preview)`. Admission
   recomputes the plan and estimate, verifies every digest and settings binding,
   and rejects source, settings, tokenizer, calibration, directory, or filename
   collision drift.
5. A backend writes exactly the private staging path in `GenerationRequest` and
   returns `BackendResult` plus `ActualRun` facts. The request necessarily holds
   the raw plan for an eventual approved adapter; it is internal-only and must
   never be serialized or logged.
6. Before publication, the service checks the staged artifact is stable mono
   PCM16 WAV data and verifies its sample rate, duration, and byte count against
   the backend's actual facts. It also constructs the strict receipt, which
   rejects plan-count drift.
7. Publication is the final side effect: a same-filesystem hard link with
   atomic no-replace semantics into the output directory bound at admission.
   `ApplicationOutcome.feedback_payload()` then exposes actual-versus-estimate
   receipt feedback without source text or paths.

The service deliberately does not delete staging files. Staging lifecycle and
recovery belong to a future approved host application, which must use a
separately owned, verified staging area and a race-safe cleanup policy.

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

After installation, the same commands are available through the
`neutral-tts` console entry point. The CLI emits deterministic JSON shapes and
discards exception text; errors use a fixed privacy-safe code/message instead
of echoing source text, credentials, or local paths.
