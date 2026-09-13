# Observed Windows acceptance

The agreed scope is installation and removal on the current Windows host,
using a locally acquired and extracted candidate ZIP. It is not a fresh Windows
installation or a physical-offline test. The product is an unsigned Python/Tk
source-layout distribution with separately acquired inference assets.

The functional candidate at `7e019bd3f8bae9e76ded8ca67c46cf2c74fe9936`
was tested on Windows 11 x64, Python 3.13.1, Tk 8.6, RTX 2060 SUPER 8 GiB,
CUDA/fp32 and the exact committed inference catalogs. The updated packaging
and display candidate at `426732afa747ace3b475335ad68da4d9897eacd1` was also
installed and exercised with real generation. Later acceptance-record edits
change documentation and the preparation-stage decision ledger; executable
application code, build tools and inference catalogs remain unchanged. Final
artifact hashes are supplied with the Release.

| Operation | Actual observation |
| --- | --- |
| Initial setup | Fresh folder, new environment, official fixed downloads, retained licenses, reference form and automatically saved configuration |
| Preview/generate | Shared real tokenizer; ordinary 66 characters/41 tokens/1 chunk and long 270 characters/127 tokens/2 chunks |
| Final WAV | 48 kHz mono PCM16; ordinary 13.6 s and long 55.6 s; zero clipped samples; AudioSeal payload matched in each evaluated window |
| Detection | Ordinary marked-frame fraction 1.0; long approximately 0.9992; measured from final saved WAV bytes |
| Backup/restore | Restored settings/history retain all completed WAVs; audio is excluded from the backup archive |
| GPU-active cancellation | Owned inference PID observed on GPU, journal producing, 98% whole-device utilization at cancellation; 1 unfinished run quarantined, completed output unchanged; real generation and playback then succeeded |
| Restart | A new desktop process loaded the saved interpreter and four verified history records; selected real WAV playback succeeded |
| Lifecycle | Install, update, rollback, forced exit after relocating the old app, recovery, update and uninstall through the installer |
| Uninstall | Five WAV files, all 41 user-data files and an unlisted user memo retained byte-for-byte; owned app files removed from the active app folder |
| Responsiveness | Event-loop callbacks continued while setup and generation workers were busy; duplicate generation callback started one worker |

The packaging/display candidate completed installation, update, rollback,
forced update interruption, recovery and update again. Its desktop generated
the ordinary WAV with the same content hash as the accepted listening sample,
detected its watermark and played it. Old environment calibration was excluded;
a new successful receipt was used for subsequent estimates. A new desktop
process verified five history records and playback. Uninstall preserved six
WAV files, all 44 user-data files and the unlisted memo byte-for-byte.

Machine checks do not evaluate intelligibility. On 2026-09-13, the operator
accepted the short, ordinary and long listening samples. This is acceptance of
these private samples, not a guarantee for all texts or voices. Private
recordings, generated samples, voice labels, screenshots with user paths and
private evidence records are not shipped.

The full suite at `426732afa747ace3b475335ad68da4d9897eacd1` ran 266 cases:
257 passed, 9 skipped because the ordinary process could not create symlinks,
with no failures or errors. On 2026-09-13, the exact nine skipped test methods
were executed in one authorized elevated process against the fixed functional
commit `7e019bd3f8bae9e76ded8ca67c46cf2c74fe9936`: all nine passed, with zero
skips, failures or errors. These test methods and the exercised filesystem
boundary checks are unchanged in the packaging/display candidate. The additional
metadata test is included in its 257 passing cases. This is combined scoped
coverage from two runs; it is not a claim that the original unelevated run had
no skips. No host network or security settings were changed.

Actual inference child processes reached 7,860,310,016 bytes of peak working
set and 13,462,994,944 bytes of peak private committed memory. Sampling was
approximately once per second and followed worker descendants, including the
actual Python process behind the environment launcher. Whole-device GPU use
peaked at 7,253 MiB over 321 samples; it includes display and other processes.
The separate short run measured 4,424,197,120 bytes of peak PyTorch allocated
GPU memory. These quantities are different, and none establishes minimum VRAM.

The completed fresh setup occupied approximately 11.02 GiB across model assets,
environment, source archives, wheel cache and notices. Setup requires at least
16 GiB free before starting; WAVs and backup/retained application copies need
additional space. CPU inference, other GPUs/Python versions, other Windows
versions and actual network-disconnected operation remain unverified.

Publication requires the user's approval of the exact final commit, tag,
artifact hashes and release contents. Actual GitHub clone, Release download,
hash/link checks and observed Windows warnings follow that approval.
