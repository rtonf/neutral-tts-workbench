"""Private reference selection with separate recorded permissions and byte copies."""
from __future__ import annotations

import hashlib
import io
import os
import uuid
import wave
from datetime import datetime, timezone
from pathlib import Path

from .cli import _safe_directory
from .fs_safety import DirectoryGuard, assert_safe_descendant, open_exclusive_binary
from .local_runtime import MAX_REFERENCE_BYTES, _absolute, _read
from .workbench import Workbench, WorkbenchError, read_private, write_private


SCOPES = ("recording_use", "voice_replication", "generated_audio_use")


def inspect_reference(data):
    """Inspect bounded WAV bytes, without loading inference or audio libraries."""
    if not 0 < len(data) <= MAX_REFERENCE_BYTES:
        raise WorkbenchError("Reference audio exceeds its byte limit.")
    with wave.open(io.BytesIO(data), "rb") as wav:
        rate, frames = wav.getframerate(), wav.getnframes()
        if wav.getnchannels() != 1 or wav.getsampwidth() != 2 or rate not in {44100, 48000}:
            raise WorkbenchError("Use mono PCM16 reference WAV at 44.1 or 48 kHz.")
        if not 0 < frames <= 30 * rate or len(wav.readframes(frames)) != 2 * frames:
            raise WorkbenchError("Reference WAV must be complete and at most 30 seconds.")
    return {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data),
            "sample_rate": rate, "frames": frames, "channels": 1, "sample_width": 2}


def prepare_references(data_root, *, reference_paths, permissions, acknowledged, evidence_path=""):
    """Save actual operator statements; never infer permission to publish audio.

    Registration is not asset license approval. A completed record contains no
    invented reviewed:true fields. A later runtime setup must also bind the
    reviewed engine/assets and the requested use to the recorded scope.
    """
    if type(reference_paths) is not list or not 1 <= len(reference_paths) <= 4:
        raise WorkbenchError("Select one to four reference WAV files.")
    if type(permissions) is not dict or set(permissions) != set(SCOPES):
        raise WorkbenchError("Three separate permission statements are required.")
    if type(acknowledged) is not dict or set(acknowledged) != set(SCOPES) or any(v is not True for v in acknowledged.values()):
        raise WorkbenchError("Confirm recording use, voice replication and generated audio use separately.")
    if any(type(v) is not str or not 1 <= len(v.strip()) <= 4096 for v in permissions.values()):
        raise WorkbenchError("Describe each permission and its limits.")
    paths = [_absolute(p) for p in reference_paths]
    if len(set(paths)) != len(paths):
        raise WorkbenchError("The reference selection contains duplicates.")
    workbench = Workbench(Path(data_root))
    with workbench.owned():
        admitted = []
        for path in paths:
            data = _read(path, limit=MAX_REFERENCE_BYTES)
            admitted.append((path, data, inspect_reference(data)))
        evidence = None
        if evidence_path:
            proof = _absolute(evidence_path)
            evidence = _read(proof, limit=256 * 1024)
        # New copies live outside backup categories; a restored backup cannot
        # silently replace a voice selection. A single pointer admits a complete
        # selection only after all its files and source checks succeed.
        area = _safe_directory(str(workbench.root / "local-voice"), label="private voice storage")
        selection = _safe_directory(str(area / uuid.uuid4().hex), label="private voice selection")
        copied = []
        with DirectoryGuard(selection) as guard:
            for index, (path, data, metadata) in enumerate(admitted, 1):
                target = selection / f"reference-{index:02d}.wav"
                with open_exclusive_binary(guard, target) as stream:
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                _read(target, limit=MAX_REFERENCE_BYTES, digest=metadata["sha256"])
                _read(path, limit=MAX_REFERENCE_BYTES, digest=metadata["sha256"])
                copied.append(dict(metadata, path=str(target), source_path=str(path)))
            attached = None
            if evidence is not None:
                target = selection / "permission-evidence.bin"
                with open_exclusive_binary(guard, target) as stream:
                    stream.write(evidence)
                    stream.flush()
                    os.fsync(stream.fileno())
                attached = {"path": str(target), "sha256": hashlib.sha256(evidence).hexdigest()}
            record = {
                "schema_version": "neutral-reference-selection-v1",
                "recorded_at": datetime.now(timezone.utc).isoformat(),
                "basis": "operator_statement", "permissions": permissions,
                "acknowledged": acknowledged, "attached_evidence": attached,
                "references": copied, "public_distribution_granted": False,
            }
            record_path = selection / "selection.json"
            write_private(record_path, record)
            digest = hashlib.sha256(_read(record_path, limit=256 * 1024)).hexdigest()
            assert_safe_descendant(guard, record_path)
            write_private(area / "active.json", {"path": str(record_path), "sha256": digest})
        return {"selection_file": str(record_path), "references": copied,
                "permissions": permissions, "inference_ready": False}


def current_references(data_root):
    workbench = Workbench(Path(data_root))
    with workbench.owned():
        return _current_references_owned(workbench)


def _current_references_owned(workbench):
        pointer = workbench.root / "local-voice" / "active.json"
        if not pointer.exists():
            return {"configured": False}
        binding = read_private(pointer)
        path = _absolute(binding["path"])
        area = pointer.parent
        if area not in path.parents:
            raise WorkbenchError("The voice selection is outside its private storage.")
        from .local_runtime import _object
        record = _object(_read(path, limit=256 * 1024, digest=binding["sha256"]))
        if record.get("schema_version") != "neutral-reference-selection-v1":
            raise WorkbenchError("The saved voice selection has an unsupported schema.")
        attached = record.get("attached_evidence")
        if attached:
            proof = _absolute(attached["path"])
            if proof.parent != path.parent:
                raise WorkbenchError("Permission evidence is outside its selection.")
            _read(proof, limit=256 * 1024, digest=attached["sha256"])
        for reference in record["references"]:
            target = _absolute(reference["path"])
            if target.parent != path.parent:
                raise WorkbenchError("The reference copy is outside its selection.")
            inspect_reference(_read(target, limit=MAX_REFERENCE_BYTES, digest=reference["sha256"]))
        return {"configured": True, "selection_file": str(path),
                "references": record["references"], "permissions": record["permissions"],
                "inference_ready": False}
