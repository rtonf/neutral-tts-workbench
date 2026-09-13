# Windows candidate feature scope

This is the implementation contract for the next local candidate. Public release
remains pending the evidence in the distribution gates.

| User operation | Current implementation | Completion evidence | Required input |
| --- | --- | --- | --- |
| Enter text and dictionary readings | CLI, normalization and dictionary core | GUI and UTF-8 input, bounded input, persisted dictionary | User text; 4096 source characters, 144 characters/256 tokens per chunk |
| Preview and estimate | Shared real-tokenizer plan in CLI/GUI | Fixed filename, exact count, changed-condition rejection; timing calibration separately qualified | Verified local tokenizer; no inference or watermark weights |
| Generate and select destination | Lazy local backend and atomic WAV publication | GUI worker, cancellation/recovery, verified saved WAV | Reviewed runtime/assets, recording rights, explicit voice consent |
| Play, history, restart | Verified history and playback API | GUI through the same history verification | Windows audio device; generated WAV |
| First setup | Reference permission form and fixed inference setup dialog | Download/retry, hash checks, source wheels, isolated environment, automatically written configuration | Consented reference; Windows x64/Python 3.13; 16 GiB setup space |
| Backup and restore | Owned, journaled category transactions | Actual settings/dictionary/history and restored WAV checks | User-selected archive; external WAV/assets are excluded |
| Install, update, rollback, remove | Owned source-layout transactions | Launcher/installer, interrupted update recovery, preserved user data, exact final ZIP acceptance | Unsigned candidate; current-host download-like install/use/remove acceptance |

The current host has Windows, Python 3.13.1, Tk 8.6 and an RTX 2060 SUPER with
8 GiB VRAM. Actual short-sentence inference in the fixed CUDA/fp32 configuration
produced a 3.32-second WAV; peak allocated GPU memory in that run was
4,424,197,120 bytes. The saved PCM file had zero clipped samples and its
AudioSeal payload was detected. The operator reported intelligible speech with
no obvious anomaly for that sentence. Ordinary and two-chunk speech,
GPU-active cancellation/recovery, fresh setup and installer lifecycle results
are in [Windows acceptance](WINDOWS_ACCEPTANCE.md); this measurement does not establish minimum VRAM or other GPUs.
The fixed setup uses Python 3.13 x64. CPU inference and other Python versions
are unverified. New Windows and physically disconnected operation are not
claimed by the agreed current-host acceptance. Code signing is outside this
unsigned distribution specification, not a passed check.

The generation adapter requires mono PCM16 at 48 kHz; reference WAV
admission accepts mono PCM16 at 44.1/48 kHz, up to 30 seconds. These are software
admission limits, not measured quality recommendations.
