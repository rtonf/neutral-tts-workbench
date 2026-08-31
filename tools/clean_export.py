#!/usr/bin/env python3
"""Construct a clean source staging tree from an exact, hash-pinned manifest.

This module intentionally uses only the Python standard library.  The public
API is small, but the command line interface is useful in CI as well::

    python tools/clean_export.py --source-root . --destination-root .staging/x \
        --manifest export-manifest.json --allowlist compliance/source-asset-allowlist.json

The exporter is positive-allowlist based: it never copies a directory as a
whole and never removes anything.  A destination must not exist before the
operation starts.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
from pathlib import Path, PurePosixPath
from typing import Any, Iterable


class ExportError(ValueError):
    """Raised when an export cannot be proven safe."""


_SHA256 = re.compile(r"^[0-9a-fA-F]{64}$")


def _norm_rel(value: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ExportError("manifest path must be a non-empty string")
    value = value.replace("\\", "/")
    # Any absolute, drive, UNC, or traversal spelling is rejected, including
    # forms that pathlib on the host would otherwise normalize away.
    if value.startswith(("/", "//")) or re.match(r"^[A-Za-z]:", value):
        raise ExportError(f"absolute or drive path is forbidden: {value!r}")
    parts = value.split("/")
    if any(p in ("", ".", "..") for p in parts):
        raise ExportError(f"non-canonical or traversal path: {value!r}")
    return "/".join(parts)


def _is_reparse(path: Path) -> bool:
    """Return true for links and Windows reparse points without following them."""
    try:
        if path.is_symlink():
            return True
        mode = path.lstat().st_mode
    except (OSError, ValueError) as exc:
        raise ExportError(f"cannot inspect {path}: {exc}") from exc
    if stat.S_ISLNK(mode):
        return True
    if os.name == "nt":
        try:
            import ctypes
            attrs = ctypes.windll.kernel32.GetFileAttributesW(str(path))
            if attrs == 0xFFFFFFFF:
                raise OSError(ctypes.get_last_error(), "GetFileAttributesW failed")
            return bool(attrs & 0x400)  # FILE_ATTRIBUTE_REPARSE_POINT
        except (AttributeError, OSError) as exc:
            # A missing ctypes API is not proof of safety.
            if isinstance(exc, OSError):
                raise ExportError(f"cannot inspect reparse state for {path}: {exc}") from exc
    return False


def _within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _validate_destination_ancestors(destination: Path) -> None:
    """Ensure creating destination cannot traverse an existing link/reparse point."""
    current = destination.parent
    while True:
        if current.exists() or current.is_symlink():
            if _is_reparse(current):
                raise ExportError(f"destination parent is a symlink/reparse point: {current}")
        parent = current.parent
        if parent == current:
            return
        current = parent


def _validate_source_ancestors(source: Path) -> None:
    """Check every existing source ancestor; no link may hide above the root."""
    current = source
    while True:
        if current.exists() or current.is_symlink():
            if _is_reparse(current):
                raise ExportError(f"source ancestor is a symlink/reparse point: {current}")
        parent = current.parent
        if parent == current:
            return
        current = parent


def _validate_glob_pattern(pattern: Any) -> str:
    if not isinstance(pattern, str) or not pattern or "\x00" in pattern:
        raise ExportError("allowlist glob must be a non-empty string")
    normalized = pattern.replace("\\", "/")
    if normalized.startswith("/") or normalized.startswith("//") or re.match(r"^[A-Za-z]:", normalized):
        raise ExportError(f"allowlist glob must be relative: {pattern!r}")
    components = normalized.split("/")
    if any(component in ("", ".", "..") for component in components):
        raise ExportError(f"allowlist glob has empty/dot/traversal component: {pattern!r}")
    return normalized


def _pattern_regex(pattern: str) -> re.Pattern[str]:
    """Translate the allowlist's POSIX glob, with ** matching zero directories."""
    pattern = pattern.replace("\\", "/")
    bits: list[str] = []
    i = 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            bits.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("/**", i):
            bits.append("(?:/.*)?")
            i += 3
        elif pattern[i] == "*":
            bits.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            bits.append("[^/]")
            i += 1
        else:
            bits.append(re.escape(pattern[i]))
            i += 1
    return re.compile("^" + "".join(bits) + "$", re.IGNORECASE if os.name == "nt" else 0)


def load_allowlist(path: os.PathLike[str] | str) -> dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            value = json.load(fh)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ExportError(f"cannot read allowlist: {exc}") from exc
    if not isinstance(value, dict) or value.get("default") != "deny":
        raise ExportError("allowlist must be an object with default=deny")
    paths = value.get("source_paths")
    if not isinstance(paths, list) or not all(isinstance(p, str) and p for p in paths):
        raise ExportError("allowlist source_paths must be a non-empty string list")
    return validate_allowlist(value)


def validate_allowlist(value: Any) -> dict[str, Any]:
    """Validate an already-loaded allowlist using the same fail-closed rules."""
    if not isinstance(value, dict) or value.get("default") != "deny":
        raise ExportError("allowlist must be an object with default=deny")
    paths = value.get("source_paths")
    if not isinstance(paths, list) or not paths or not all(isinstance(p, str) and p for p in paths):
        raise ExportError("allowlist source_paths must be a non-empty string list")
    value["source_paths"] = [_validate_glob_pattern(p) for p in paths]
    return value


def is_allowlisted(relpath: str, allowlist: dict[str, Any]) -> bool:
    relpath = _norm_rel(relpath)
    return any(_pattern_regex(pattern).match(relpath) for pattern in allowlist.get("source_paths", []))


def _read_manifest(path: os.PathLike[str] | str) -> dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            manifest = json.load(fh)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ExportError(f"cannot read manifest: {exc}") from exc
    if not isinstance(manifest, dict):
        raise ExportError("manifest must be a JSON object")
    required = {"schema_version", "files"}
    if not required.issubset(manifest):
        raise ExportError("manifest requires schema_version and files")
    if manifest["schema_version"] != "neutral-clean-export-v1":
        raise ExportError("unsupported manifest schema")
    if set(manifest) - {"schema_version", "source_root", "files"}:
        raise ExportError("manifest contains unknown fields")
    if not isinstance(manifest["files"], list):
        raise ExportError("manifest files must be a list")
    seen: set[str] = set()
    for entry in manifest["files"]:
        if not isinstance(entry, dict) or set(entry) != {"path", "size", "sha256"}:
            raise ExportError("each manifest file must contain exactly path, size, sha256")
        rel = _norm_rel(entry["path"])
        if rel in seen:
            raise ExportError(f"duplicate manifest path: {rel}")
        seen.add(rel)
        if not isinstance(entry["size"], int) or entry["size"] < 0:
            raise ExportError(f"invalid size for {rel}")
        if not isinstance(entry["sha256"], str) or not _SHA256.fullmatch(entry["sha256"]):
            raise ExportError(f"invalid sha256 for {rel}")
        entry["path"] = rel
    return manifest


def _safe_source_file(root: Path, rel: str) -> Path:
    candidate = root.joinpath(*rel.split("/"))
    if not _within(candidate, root):
        raise ExportError(f"manifest path escapes source root: {rel}")
    if _is_reparse(candidate):
        raise ExportError(f"symlink/reparse input is forbidden: {rel}")
    try:
        if not candidate.is_file() or not stat.S_ISREG(candidate.stat().st_mode):
            raise ExportError(f"manifest input is not a regular file: {rel}")
    except OSError as exc:
        raise ExportError(f"cannot inspect source file {rel}: {exc}") from exc
    # Check each parent without following links.  This catches a linked
    # directory even if the final file itself appears regular.
    current = candidate.parent
    while current != root:
        if _is_reparse(current):
            raise ExportError(f"symlink/reparse parent is forbidden: {rel}")
        current = current.parent
    return candidate


def _digest(path: Path) -> tuple[int, str]:
    size = 0
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as fh:
            while True:
                block = fh.read(1024 * 1024)
                if not block:
                    break
                size += len(block)
                digest.update(block)
    except (OSError, IOError) as exc:
        raise ExportError(f"cannot read source {path}: {exc}") from exc
    return size, digest.hexdigest()


def export_clean(
    source_root: os.PathLike[str] | str,
    destination_root: os.PathLike[str] | str,
    manifest: os.PathLike[str] | str | dict[str, Any],
    allowlist: os.PathLike[str] | str | dict[str, Any],
) -> dict[str, Any]:
    """Export exact manifest files into a newly-created destination.

    Returns a machine-readable summary and raises :class:`ExportError` before
    creating the destination whenever validation fails.
    """
    source = Path(source_root).absolute()
    destination = Path(destination_root).absolute()
    _validate_source_ancestors(source)
    if _is_reparse(source) or not source.is_dir():
        raise ExportError("source root must be an existing non-link directory")
    if destination.exists() or destination.is_symlink():
        raise ExportError("destination staging root must be previously absent")
    _validate_destination_ancestors(destination)
    if _within(destination, source) or _within(source, destination):
        raise ExportError("source and destination must not contain one another")
    if isinstance(allowlist, dict):
        policy = validate_allowlist(allowlist)
    else:
        policy = load_allowlist(allowlist)
    if isinstance(manifest, dict):
        # Validate through the same strict shape used for files read from disk.
        tmp = manifest
        required = {"schema_version", "files"}
        if not required.issubset(tmp) or tmp.get("schema_version") != "neutral-clean-export-v1":
            raise ExportError("invalid manifest")
        if set(tmp) - {"schema_version", "source_root", "files"}:
            raise ExportError("manifest contains unknown fields")
        manifest_obj = json.loads(json.dumps(tmp))
        # Inline equivalent of _read_manifest validation via a temporary-free path.
        entries = manifest_obj.get("files")
        if not isinstance(entries, list):
            raise ExportError("manifest files must be a list")
        seen: set[str] = set()
        for entry in entries:
            if not isinstance(entry, dict) or set(entry) != {"path", "size", "sha256"}:
                raise ExportError("each manifest file must contain exactly path, size, sha256")
            rel = _norm_rel(entry["path"])
            if rel in seen or not isinstance(entry["size"], int) or entry["size"] < 0 or not isinstance(entry["sha256"], str) or not _SHA256.fullmatch(entry["sha256"]):
                raise ExportError(f"invalid or duplicate manifest entry: {rel}")
            entry["path"] = rel
            seen.add(rel)
    else:
        manifest_obj = _read_manifest(manifest)
    entries = manifest_obj["files"]
    if not all(is_allowlisted(e["path"], policy) for e in entries):
        bad = next(e["path"] for e in entries if not is_allowlisted(e["path"], policy))
        raise ExportError(f"manifest path is not allowlisted: {bad}")
    expected_source = manifest_obj.get("source_root")
    if expected_source is not None and Path(expected_source).absolute() != source:
        raise ExportError("manifest source_root does not match requested source root")

    # Validate every input and hash before creating a destination.  This keeps
    # a bad manifest from leaving a partial staging tree behind.
    validated: list[tuple[dict[str, Any], Path]] = []
    for entry in entries:
        source_file = _safe_source_file(source, entry["path"])
        size, digest = _digest(source_file)
        if size != entry["size"] or digest.lower() != entry["sha256"].lower():
            raise ExportError(f"size or SHA-256 mismatch: {entry['path']}")
        validated.append((entry, source_file))

    try:
        destination.mkdir(parents=True, exist_ok=False)
    except OSError as exc:
        raise ExportError(f"cannot create staging root: {exc}") from exc
    copied = 0
    try:
        for entry, source_file in validated:
            target = destination.joinpath(*entry["path"].split("/"))
            if not _within(target, destination):
                raise ExportError(f"destination path escapes staging root: {entry['path']}")
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists() or target.is_symlink():
                raise ExportError(f"refusing to overwrite destination: {entry['path']}")
            # xb provides no-overwrite semantics if a competing process races us.
            with open(target, "xb") as out, open(source_file, "rb") as inp:
                while True:
                    block = inp.read(1024 * 1024)
                    if not block:
                        break
                    out.write(block)
            copied_size, copied_digest = _digest(target)
            if copied_size != entry["size"] or copied_digest.lower() != entry["sha256"].lower():
                raise ExportError(f"copied file changed during export: {entry['path']}")
            copied += 1
    except (OSError, ExportError) as exc:
        raise ExportError(f"export failed after {copied} files: {exc}") from exc
    return {
        "schema_version": "neutral-clean-export-summary-v1",
        "status": "ok",
        "source_root": str(source),
        "destination_root": str(destination),
        "files_expected": len(entries),
        "files_copied": copied,
    }


def build_manifest(source_root: os.PathLike[str] | str, allowlist: os.PathLike[str] | str | dict[str, Any]) -> dict[str, Any]:
    """Create an exact manifest from regular, non-link allowlisted files."""
    source = Path(source_root).absolute()
    _validate_source_ancestors(source)
    if _is_reparse(source) or not source.is_dir():
        raise ExportError("source root must be an existing non-link directory")
    policy = allowlist if isinstance(allowlist, dict) else load_allowlist(allowlist)
    if isinstance(allowlist, dict):
        policy = validate_allowlist(policy)
    found: list[dict[str, Any]] = []
    def walk_error(exc: OSError) -> None:
        raise ExportError(f"cannot inspect source directory: {exc}") from exc

    for current, dirs, files in os.walk(source, topdown=True, followlinks=False, onerror=walk_error):
        current_path = Path(current)
        dirs[:] = sorted(dirs)
        files = sorted(files)
        kept_dirs: list[str] = []
        for d in dirs:
            child = current_path / d
            if _is_reparse(child):
                raise ExportError(f"symlink/reparse input is forbidden: {child}")
            if d != ".git":
                kept_dirs.append(d)
        dirs[:] = kept_dirs
        for name in files:
            candidate = current_path / name
            if _is_reparse(candidate):
                raise ExportError(f"symlink/reparse input is forbidden: {candidate}")
            rel = candidate.relative_to(source).as_posix()
            if not is_allowlisted(rel, policy):
                continue
            regular = _safe_source_file(source, rel)
            size, digest = _digest(regular)
            found.append({"path": rel, "size": size, "sha256": digest})
    return {"schema_version": "neutral-clean-export-v1", "source_root": str(source), "files": found}


# Clear aliases make the API discoverable for callers that use the command's
# noun first, while keeping export_clean as the canonical implementation.
clean_export = export_clean
create_manifest = build_manifest


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", required=True)
    parser.add_argument("--destination-root", required=True)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--allowlist", required=True)
    args = parser.parse_args(argv)
    try:
        summary = export_clean(args.source_root, args.destination_root, args.manifest, args.allowlist)
    except ExportError as exc:
        print(json.dumps({"schema_version": "neutral-clean-export-summary-v1", "status": "error", "error": str(exc)}, ensure_ascii=False, sort_keys=True))
        return 2
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
