"""Verified same-byte playback for completed history records."""

from __future__ import annotations

import hashlib
import io
import os
import stat
import wave
from collections.abc import Callable
from pathlib import Path

from .history import HistoryRecord


class PlaybackError(RuntimeError):
    """A history output cannot be verified or played."""


def _verified_wave_bytes(record: HistoryRecord) -> bytes:
    if not isinstance(record, HistoryRecord):
        raise TypeError("playback requires a HistoryRecord")
    path = record.output_path
    if not isinstance(path, Path) or not path.is_absolute():
        raise PlaybackError("playback output identity is invalid")
    try:
        before = path.stat()
        linked = path.lstat()
        if (
            path.is_symlink()
            or bool(getattr(linked, "st_file_attributes", 0) & 0x400)
            or not stat.S_ISREG(before.st_mode)
            or before.st_size != record.output_size
        ):
            raise PlaybackError("playback output is not a verified regular file")
        with path.open("rb") as handle:
            opened = os.fstat(handle.fileno())
            data = handle.read(record.output_size + 1)
        after = path.stat()
    except PlaybackError:
        raise
    except OSError as exc:
        raise PlaybackError("playback output is unavailable") from exc
    identities = {
        (item.st_dev, item.st_ino, item.st_size, item.st_mtime_ns)
        for item in (before, opened, after)
    }
    if len(identities) != 1 or len(data) != record.output_size:
        raise PlaybackError("playback output changed during verification")
    if hashlib.sha256(data).hexdigest() != record.output_sha256:
        raise PlaybackError("playback output hash changed")
    try:
        with wave.open(io.BytesIO(data), "rb") as wav:
            facts = (
                wav.getnchannels(),
                wav.getsampwidth(),
                wav.getframerate(),
                wav.getnframes(),
                wav.getcomptype(),
            )
            pcm = wav.readframes(wav.getnframes())
            if wav.readframes(1):
                raise PlaybackError("playback WAV has an ambiguous frame extent")
    except PlaybackError:
        raise
    except (EOFError, wave.Error) as exc:
        raise PlaybackError("playback output is not a valid WAV") from exc
    channels, width, rate, frames, compression = facts
    if (
        channels != 1
        or width != 2
        or rate <= 0
        or frames <= 0
        or compression != "NONE"
        or len(pcm) != frames * 2
    ):
        raise PlaybackError("playback output is not non-empty mono PCM16")
    return data


class WinsoundPlayback:
    """Play already-verified WAV bytes synchronously on Windows."""

    @staticmethod
    def _windows_player(data: bytes) -> None:
        if os.name != "nt":
            raise PlaybackError("Windows playback is unavailable on this host")
        import winsound

        # Synchronous playback is the default. SND_SYNC is not exported by the
        # supported Python 3.11-3.13 winsound modules.
        winsound.PlaySound(data, winsound.SND_MEMORY)

    def play(
        self,
        record: HistoryRecord,
        *,
        player: Callable[[bytes], None] | None = None,
    ) -> None:
        data = _verified_wave_bytes(record)
        selected = self._windows_player if player is None else player
        if not callable(selected):
            raise TypeError("player must be callable")
        try:
            selected(data)
        except PlaybackError:
            raise
        except Exception as exc:
            raise PlaybackError("verified playback failed") from exc


__all__ = ["PlaybackError", "WinsoundPlayback"]
