#!/usr/bin/env python3
"""Build a deterministic source-only ZIP from one exact Git commit."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Iterable, Mapping
import zipfile

try:
    from clean_export import _is_reparse, is_allowlisted, load_allowlist, validate_allowlist
except ImportError:  # pragma: no cover
    from tools.clean_export import _is_reparse, is_allowlisted, load_allowlist, validate_allowlist
try:
    from dependency_closure import DependencyClosureError, load_lock, lock_digest, validate_lock
except ImportError:  # pragma: no cover
    from tools.dependency_closure import DependencyClosureError, load_lock, lock_digest, validate_lock
try:
    from generate_source_metadata import SourceMetadataError, _canonical, build_cyclonedx_document, build_notices, build_spdx_document, git_tree_files, resolve_commit
except ImportError:  # pragma: no cover
    from tools.generate_source_metadata import SourceMetadataError, _canonical, build_cyclonedx_document, build_notices, build_spdx_document, git_tree_files, resolve_commit
try:
    from scan_public_surface import scan_public_surface
except ImportError:  # pragma: no cover
    from tools.scan_public_surface import scan_public_surface


class SourceArchiveError(ValueError):
    """Raised when a source archive cannot be proven reproducible and safe."""


_DENY_EXTENSIONS = {".wav", ".mp3", ".flac", ".ogg", ".safetensors", ".ckpt", ".pt", ".pth", ".dll", ".pyd", ".so", ".dylib", ".exe", ".msi", ".db", ".sqlite", ".sqlite3", ".jsonl", ".onnx", ".bin", ".model", ".tflite", ".npy", ".npz", ".pickle", ".pkl"}
_DENY_DIRS = {".git", ".venv", "venv", "env", "models", "model", "runtime", "cache", "caches", "outputs", "output", "profiles", "profile", "state", "sessions", "session", "logs", "log", "receipts", "reports", "artifacts", "packages", "installers", "node_modules", "__pycache__", ".pytest_cache", ".mypy_cache", ".cache", "audio", "voices", "voice", "recordings", "user-data", "user_data", "userdata", "native", "site-packages", "build", "dist", "bin", "lib", "temp", "tmp"}
_ARCHIVE_EXTENSIONS = (".zip", ".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tbz2", ".tar.xz", ".txz")
_GENERATED = ("SBOM.cdx.json", "SBOM.spdx.json", "THIRD_PARTY_NOTICES.generated.md")
_LOCK_PATH = "config/dependency-closure.lock.json"


def _reparse(path: Path) -> bool:
    try:
        return _is_reparse(path)
    except Exception as exc:
        raise SourceArchiveError(f"cannot inspect reparse state for {path}: {exc}") from exc


def _safe_rel(path: str) -> str:
    normalized = path.replace("\\", "/")
    if not normalized or normalized.startswith(("/", "//")) or (len(normalized) > 1 and normalized[1] == ":") or any(part in {"", ".", ".."} for part in normalized.split("/")):
        raise SourceArchiveError(f"non-canonical/traversal archive path: {path!r}")
    return normalized


def _check_source_path(path: str, allowlist: Mapping[str, Any]) -> None:
    rel = _safe_rel(path)
    parts = rel.split("/")
    if not is_allowlisted(rel, dict(allowlist)):
        raise SourceArchiveError(f"Git tree path is not allowlisted: {rel}")
    if any(p.lower() in _DENY_DIRS for p in parts[:-1]):
        raise SourceArchiveError(f"forbidden source directory: {rel}")
    if Path(parts[-1]).suffix.lower() in _DENY_EXTENSIONS or parts[-1].lower() in {"pyvenv.cfg", "artifacts.lock.json", "playback.lock.json"}:
        raise SourceArchiveError(f"forbidden source payload: {rel}")
    if rel.lower().endswith(_ARCHIVE_EXTENSIONS):
        raise SourceArchiveError(f"nested archive is forbidden: {rel}")


def _validate_parent(path: Path) -> None:
    parent = path.parent
    if not parent.exists() or not parent.is_dir() or _reparse(parent):
        raise SourceArchiveError(f"archive destination parent must be an existing non-link directory: {parent}")
    current = parent
    while True:
        if current.exists() and _reparse(current):
            raise SourceArchiveError(f"archive destination ancestor is a reparse point: {current}")
        if current.parent == current:
            break
        current = current.parent


def _write_new(path: Path, data: bytes) -> None:
    if path.exists() or path.is_symlink() or (path.exists() and _reparse(path)):
        raise SourceArchiveError(f"refusing to overwrite staging file: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as fh:
        fh.write(data)
    os.chmod(path, 0o644)


def _digest(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            size += len(block)
            digest.update(block)
    return size, digest.hexdigest()


def _stage_is_safe_to_remove(stage: Path, parent: Path, identity: tuple[int, int] | None) -> bool:
    """Prove the disposable staging tree is still the original regular tree."""
    try:
        current = stage.lstat()
        if stage.parent != parent or _reparse(stage) or not stage.is_dir() or identity != (current.st_dev, current.st_ino):
            return False
        for directory, dirs, files in os.walk(stage, topdown=True, followlinks=False):
            directory_path = Path(directory)
            for name in [*dirs, *files]:
                child = directory_path / name
                if _reparse(child):
                    return False
        return True
    except Exception:
        return False


def _zip_from_stage(stage: Path, destination: Path, paths: list[str]) -> None:
    # ZIP_STORED avoids compressor-version drift and is appropriate for a
    # source package.  Every member has the same fixed DOS timestamp and mode.
    with zipfile.ZipFile(destination, "x", compression=zipfile.ZIP_STORED, strict_timestamps=True) as archive:
        for rel in sorted(paths):
            data = (stage / Path(*rel.split("/"))).read_bytes()
            info = zipfile.ZipInfo(rel, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_STORED
            info.create_system = 3
            info.external_attr = (stat.S_IFREG | 0o644) << 16
            info.extra = b""
            info.comment = b""
            archive.writestr(info, data)


def _run_archive_scan(archive: Path, root: Path, allowlist: Mapping[str, Any], private_policy: Path | None) -> None:
    # The existing scanner is called as a library, with generated metadata
    # added to the same positive allowlist and disclosure scope.
    policy = json.loads(json.dumps(dict(allowlist)))
    policy["source_paths"] = list(policy.get("source_paths", [])) + list(_GENERATED) + ["SOURCE_MANIFEST.json"]
    policy["disclosure_paths"] = list(policy.get("disclosure_paths", policy.get("required_disclosure_paths", []))) + list(policy.get("required_disclosure_paths", [])) + list(_GENERATED) + ["SOURCE_MANIFEST.json"]
    policy["required_disclosure_paths"] = list(policy.get("required_disclosure_paths", [])) + ["THIRD_PARTY_NOTICES.generated.md"]
    report = scan_public_surface(root, policy, archives=[archive], private_policy=private_policy, scan_worktree=False)
    if report.get("status") != "ok":
        raise SourceArchiveError(f"public surface scanner rejected archive: {json.dumps(report.get('findings', []), ensure_ascii=False, sort_keys=True)}")


def build_source_archive(
    repo_root: str | Path,
    commit: str,
    output: str | Path,
    lock: str | Path | Mapping[str, Any],
    allowlist: str | Path | Mapping[str, Any],
    *,
    audience: str = "INTERNAL_SOURCE_REVIEW",
    private_policy: str | Path | None = None,
) -> dict[str, Any]:
    """Build and atomically promote a source-only archive.

    The destination is created with an atomic hard-link promotion from a
    temporary ZIP.  This provides no-overwrite semantics even when another
    process races the final existence check.
    """
    if audience != "INTERNAL_SOURCE_REVIEW":
        raise SourceArchiveError("public or unspecified audiences are refused; use INTERNAL_SOURCE_REVIEW explicitly")
    if private_policy is None:
        raise SourceArchiveError("a private surface-scan policy is required for an internal source review archive")
    repo = Path(repo_root).absolute()
    destination = Path(output).absolute()
    private_policy_path = Path(private_policy).absolute()
    if not private_policy_path.is_file() or _reparse(private_policy_path):
        raise SourceArchiveError("private surface-scan policy must be an existing regular non-link file")
    if destination.suffix.lower() != ".zip":
        raise SourceArchiveError("source archive destination must have a .zip suffix")
    _validate_parent(destination)
    if destination.exists() or destination.is_symlink() or (destination.exists() and _reparse(destination)):
        raise SourceArchiveError(f"refusing to overwrite existing archive: {destination}")
    if _reparse(repo) or not repo.is_dir():
        raise SourceArchiveError("repository root must be a regular directory")
    policy = load_allowlist(allowlist) if not isinstance(allowlist, Mapping) else validate_allowlist(dict(allowlist))
    lock_obj = load_lock(lock) if not isinstance(lock, Mapping) else validate_lock(lock)
    if isinstance(lock, Mapping):
        lock_argument_bytes = None
    else:
        try:
            # .gitattributes pins this lock to LF; normalize a CRLF working
            # copy so a Windows checkout cannot create a false mismatch.
            lock_argument_bytes = Path(lock).read_bytes().replace(b"\r\n", b"\n")
        except OSError as exc:
            raise SourceArchiveError(f"cannot reread validated lock argument: {exc}") from exc
    resolved = resolve_commit(repo, commit)
    tree_id, source_details = git_tree_files(repo, resolved, policy)
    for fact in source_details:
        _check_source_path(fact["path"], policy)
    source_facts = [{k: fact[k] for k in ("path", "size", "sha256")} for fact in source_details]
    lock_fact = next((fact for fact in source_details if fact["path"] == _LOCK_PATH), None)
    if lock_fact is None:
        raise SourceArchiveError(f"exact commit must contain {_LOCK_PATH}")
    try:
        lock_blob = subprocess.run(["git", "-C", str(repo), "cat-file", "blob", lock_fact["object"]], stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        raise SourceArchiveError(f"cannot reread {_LOCK_PATH} from exact commit: {exc}") from exc
    if lock_blob.returncode:
        raise SourceArchiveError(f"cannot read {_LOCK_PATH} from exact commit")
    try:
        committed_lock = json.loads(lock_blob.stdout.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise SourceArchiveError(f"committed {_LOCK_PATH} is not valid JSON: {exc}") from exc
    if validate_lock(committed_lock) != lock_obj or lock_digest(committed_lock) != lock_digest(lock_obj) or (lock_argument_bytes is not None and lock_blob.stdout != lock_argument_bytes):
        raise SourceArchiveError(f"committed {_LOCK_PATH} does not exactly match the validated lock argument")
    parent = destination.parent
    stage: Path | None = None
    temporary_zip: Path | None = None
    stage_identity: tuple[int, int] | None = None
    try:
        stage = Path(tempfile.mkdtemp(prefix=".source-archive-", dir=parent))
        if _reparse(stage):
            raise SourceArchiveError("fresh staging directory is a reparse point")
        stage_info = stage.stat()
        stage_identity = (stage_info.st_dev, stage_info.st_ino)
        for fact in source_details:
            rel = fact["path"]
            target = stage.joinpath(*rel.split("/"))
            if target.exists() or target.is_symlink() or (target.exists() and _reparse(target)):
                raise SourceArchiveError(f"staging collision: {rel}")
            content = subprocess.run(["git", "-C", str(repo), "cat-file", "blob", fact["object"]], stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
            if content.returncode:
                raise SourceArchiveError(f"cannot read exact Git blob: {rel}")
            if hashlib.sha256(content.stdout).hexdigest() != fact["sha256"] or len(content.stdout) != fact["size"]:
                raise SourceArchiveError(f"Git blob changed during source extraction: {rel}")
            _write_new(target, content.stdout)
        spdx = build_spdx_document(repo, resolved, lock_obj, allowlist=policy)
        cyclonedx = build_cyclonedx_document(repo, resolved, lock_obj, allowlist=policy)
        notices = build_notices(lock_obj, commit=resolved)
        _write_new(stage / "SBOM.spdx.json", _canonical(spdx))
        _write_new(stage / "SBOM.cdx.json", _canonical(cyclonedx))
        _write_new(stage / "THIRD_PARTY_NOTICES.generated.md", notices.encode("utf-8"))
        generated = []
        for rel in _GENERATED:
            size, digest = _digest(stage / rel)
            generated.append({"path": rel, "size": size, "sha256": digest})
        manifest = {
            "schema_version": "neutral-source-manifest-v1",
            "audience": audience,
            "source_commit": resolved,
            "source_tree": tree_id,
            "lock_sha256": lock_digest(lock_obj),
            "files": source_facts,
            "generated": generated,
            "archive_files": sorted(source_facts + generated, key=lambda item: item["path"]),
        }
        _write_new(stage / "SOURCE_MANIFEST.json", _canonical(manifest))
        all_paths = [fact["path"] for fact in source_facts] + list(_GENERATED) + ["SOURCE_MANIFEST.json"]
        temporary_zip = stage / ".archive-output.zip"
        _zip_from_stage(stage, temporary_zip, all_paths)
        _run_archive_scan(temporary_zip, repo, policy, private_policy_path)
        try:
            os.link(temporary_zip, destination)
        except FileExistsError as exc:
            raise SourceArchiveError(f"refusing to overwrite archive created concurrently: {destination}") from exc
        except OSError as exc:
            raise SourceArchiveError(f"atomic no-overwrite promotion unavailable: {exc}") from exc
        temporary_zip.unlink()
        temporary_zip = None
        return {"schema_version": "neutral-source-archive-summary-v1", "status": "ok", "audience": audience, "source_commit": resolved, "source_tree": tree_id, "archive": destination.name, "file_count": len(source_facts), "archive_sha256": _digest(destination)[1]}
    except (OSError, subprocess.SubprocessError, SourceMetadataError, SourceArchiveError, DependencyClosureError) as exc:
        raise SourceArchiveError(str(exc)) from exc
    finally:
        if temporary_zip is not None:
            try:
                temporary_zip.unlink()
            except OSError:
                pass
        if stage is not None:
            # The staging path is disposable, but cleanup is destructive. A
            # swapped/reparse path is deliberately left behind for inspection.
            if _stage_is_safe_to_remove(stage, parent, stage_identity):
                shutil.rmtree(stage, ignore_errors=True)


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--lock", required=True)
    parser.add_argument("--allowlist", required=True)
    parser.add_argument("--audience", required=True)
    parser.add_argument("--private-policy", required=True)
    args = parser.parse_args(argv)
    try:
        result = build_source_archive(args.root, args.commit, args.output, args.lock, args.allowlist, audience=args.audience, private_policy=args.private_policy)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    except SourceArchiveError as exc:
        del exc
        print(json.dumps({"status": "error", "code": "SOURCE_ARCHIVE_REJECTED", "message": "Source archive construction failed."}, ensure_ascii=False, sort_keys=True))
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


build_archive = build_source_archive
