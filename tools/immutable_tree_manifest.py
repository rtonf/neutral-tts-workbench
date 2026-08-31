"""Create a deterministic, read-only manifest for a directory tree.

The manifest is intentionally limited to filesystem facts needed for an
immutable before/after comparison.  It never writes to the inspected tree or
to an output file; the command-line interface emits JSON on stdout.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import sys
from pathlib import Path
from typing import Any


class ManifestError(RuntimeError):
    """Raised when a tree cannot be inspected safely and completely."""


def _reject_special(path: Path, info: os.stat_result, *, kind: str) -> None:
    """Reject links, reparse points, and non-regular filesystem objects."""
    if stat.S_ISLNK(info.st_mode) or (
        getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    ):
        raise ManifestError(f"{kind} is a symlink or reparse point: {path}")
    if kind == "file" and not stat.S_ISREG(info.st_mode):
        raise ManifestError(f"file is not regular: {path}")


def _sha256_file(path: Path, expected_size: int) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
    except OSError as exc:
        raise ManifestError(f"cannot read file: {path}: {exc}") from exc
    # Detect a file changing during inspection; this is essential for a useful
    # before/after comparison and also avoids silently accepting a partial read.
    try:
        final_size = path.stat().st_size
    except OSError as exc:
        raise ManifestError(f"cannot stat file after reading: {path}: {exc}") from exc
    if final_size != expected_size:
        raise ManifestError(f"file changed while reading: {path}")
    return digest.hexdigest()


def build_manifest(root: str | os.PathLike[str], *, full_record: bool = True) -> dict[str, Any]:
    """Return a deterministic manifest for *root* without following links."""
    root_path = Path(root)
    try:
        root_info = root_path.lstat()
    except OSError as exc:
        raise ManifestError(f"root does not exist or is inaccessible: {root_path}: {exc}") from exc
    _reject_special(root_path, root_info, kind="root")
    if not stat.S_ISDIR(root_info.st_mode):
        raise ManifestError(f"root is not an existing directory: {root_path}")

    files: list[dict[str, Any]] = []
    # The protected baseline intentionally excludes the root itself and records
    # only descendant directory paths, matching Get-ChildItem -Recurse.
    directories: set[str] = set()

    def visit(directory: Path, relative: str) -> None:
        try:
            entries = sorted(os.scandir(directory), key=lambda item: item.name)
        except OSError as exc:
            raise ManifestError(f"cannot inspect directory: {directory}: {exc}") from exc
        for entry in entries:
            child = Path(entry.path)
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError as exc:
                raise ManifestError(f"cannot stat tree entry: {child}: {exc}") from exc
            child_relative = f"{relative}/{entry.name}" if relative != "." else entry.name
            if entry.is_dir(follow_symlinks=False):
                _reject_special(child, info, kind="directory")
                directories.add(child_relative.replace(os.sep, "/"))
                visit(child, child_relative)
            else:
                _reject_special(child, info, kind="file")
                size = info.st_size
                files.append(
                    {
                        "path": child_relative.replace(os.sep, "/"),
                        "size_bytes": size,
                        "sha256": _sha256_file(child, size),
                    }
                )

    visit(root_path, ".")
    files.sort(key=lambda item: item["path"])
    directory_list = sorted(directories)
    file_lines = [
        f"{item['path']}\t{item['size_bytes']}\t{item['sha256']}"
        for item in files
    ]
    canonical_text = (
        "\n".join(file_lines)
        + "\n--DIRECTORIES--\n"
        + "\n".join(directory_list)
    )
    total_bytes = sum(int(item["size_bytes"]) for item in files)
    result: dict[str, Any] = {
        "schema_version": "immutable-tree-manifest-v1",
        "file_count": len(files),
        "directory_count": len(directory_list),
        "total_bytes": total_bytes,
        "canonical_manifest_sha256": hashlib.sha256(
            canonical_text.encode("utf-8")
        ).hexdigest(),
        "algorithm": (
            "sorted relative-path<TAB>size<TAB>file-sha256 plus sorted "
            "directory paths; UTF-8; LF"
        ),
    }
    if full_record:
        result["directories"] = directory_list
        result["files"] = files
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", help="exact existing directory to inspect")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--full-record", action="store_true", help="include sorted directory and file records")
    mode.add_argument("--summary", action="store_true", help="explicitly request the compact summary (default)")
    args = parser.parse_args(argv)
    try:
        manifest = build_manifest(args.root, full_record=args.full_record)
    except ManifestError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
