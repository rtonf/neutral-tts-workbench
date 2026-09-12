# Windows candidate feature scope

This is the implementation contract for the next local candidate. Public release
remains pending the evidence in the distribution gates.

| User operation | Starting implementation | Completion evidence | Required input |
| --- | --- | --- | --- |
| Enter text and dictionary readings | CLI, normalization and dictionary core | GUI and UTF-8 input, bounded input, persisted dictionary | User text; 4096 source characters, 144 characters/256 tokens per chunk |
| Preview and estimate | Synthetic CLI counter; lazy real counter API | Real tokenizer only, shared plan, fixed filename, changed-condition rejection | Verified local tokenizer; no inference or watermark weights |
| Generate and select destination | Lazy local backend and atomic WAV publication | GUI worker, cancellation/recovery, verified saved WAV | Reviewed runtime/assets, recording rights, explicit voice consent |
| Play, history, restart | Verified history and playback API | GUI through the same history verification | Windows audio device; generated WAV |
| First setup | Manually authored local JSON | Guided selection, checks, saved configuration, recoverable failure | Official pinned assets and evidence; unresolved assets remain blocked |
| Backup and restore | Owned, journaled category transactions | Actual settings/dictionary/history and restored WAV checks | User-selected archive; external WAV/assets are excluded |
| Install, update, rollback, remove | Owned source-layout transactions | Working launcher/installer, interrupted update recovery, preserved user data | Local unsigned candidate; fresh Windows acceptance remains required |

The current host has Python 3.13.1 and Tk 8.6. Optional package imports and a small
CUDA calculation were checked on an 8 GiB GPU. These facts do not establish TTS
compatibility or minimum GPU/VRAM requirements. Real inference, new Windows
installation, physical offline operation, signing, and listening acceptance remain
unverified. The generation adapter requires mono PCM16 at 48 kHz; reference WAV
admission accepts mono PCM16 at 44.1/48 kHz, up to 30 seconds. These are software
admission limits, not measured quality recommendations.
