#!/usr/bin/env python3
"""Fail-closed scanner for the neutral source public surface.

The scanner reports every failure as a JSON finding and exits non-zero.  It
does not rely on Git's ignore rules: ignored and untracked files are scanned
like any other worktree file.  ``.git`` is skipped in the worktree walk and is
examined separately only when ``--git-commit`` or ``--git-history`` is used.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import stat
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path
from typing import Any, Iterable

try:
    from clean_export import _is_reparse, _norm_rel, is_allowlisted, load_allowlist, validate_allowlist
except ImportError:  # direct import when invoked as a package
    from tools.clean_export import _is_reparse, _norm_rel, is_allowlisted, load_allowlist, validate_allowlist


class ScanError(ValueError):
    """Raised for an invalid scan request or an unreadable scan target."""


_ASCII = re.compile(rb"[\x20-\x7e]{4,}")
_UTF16LE = re.compile(rb"(?:[\x20-\x7e]\x00){4,}")
_UTF16BE = re.compile(rb"(?:\x00[\x20-\x7e]){4,}")
_BACKSLASH = chr(92)
_DRIVE_PATH = re.compile(
    r"(?i)(?<![A-Za-z0-9])[A-Z]" + ":" + "[" + re.escape("/" + _BACKSLASH) + r"][^\s\"']*"
)
_UNC_PATH = re.compile(re.escape(_BACKSLASH * 2) + r"[^\s\"']+")
_UNIX_PRIVATE_PATH = re.compile(r"(?<![\w])/(?:Users|home|private|var/folders|tmp|mnt|workspace)/[^\s\"']*")
_CREDENTIAL = re.compile(r"(?i)\b(?:api[_ -]?key|access[_ -]?token|client[_ -]?secret|password|passwd|private[_ -]?key|authorization|bearer|secret)\b\s*[:=]\s*['\"]?[^\s,'\"]{4,}")
_DENY_EXTENSIONS = {
    ".wav", ".mp3", ".flac", ".ogg", ".safetensors", ".ckpt", ".pt", ".pth", ".egg-link",
    ".dll", ".pyd", ".so", ".dylib", ".exe", ".msi", ".db", ".sqlite", ".sqlite3", ".jsonl",
    ".onnx", ".bin", ".model", ".tflite", ".npy", ".npz", ".pickle", ".pkl",
}
_DENY_NAMES = {"pyvenv.cfg", "artifacts.lock.json", "playback.lock.json"}
_DENY_DIRS = {
    ".venv", "venv", "env", "models", "model", "runtime", "cache", "caches", "outputs", "output",
    "profiles", "profile", "state", "sessions", "session", "logs", "log", "receipts", "reports",
    "artifacts", "packages", "installers", "node_modules", "__pycache__", ".pytest_cache", ".mypy_cache",
    ".cache", "audio", "voices", "voice", "recordings", "user-data", "user_data", "userdata",
    "native", "site-packages", "build", "dist", "bin", "lib", "temp", "tmp",
}
_ARCHIVE_EXTENSIONS = (".zip", ".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tbz2", ".tar.xz", ".txz")


def _finding(code: str, path: str, detail: str = "", **extra: Any) -> dict[str, Any]:
    item = {"code": code, "path": path}
    if detail:
        item["detail"] = detail
    item.update(extra)
    return item


def _policy_values(policy: dict[str, Any], names: tuple[str, ...]) -> list[str]:
    values: list[str] = []
    for key in names:
        raw = policy.get(key, [])
        if isinstance(raw, str):
            values.append(raw)
        elif isinstance(raw, list) and all(isinstance(x, str) for x in raw):
            values.extend(raw)
    return values


def _private_rules(policy: dict[str, Any]) -> tuple[list[str], list[re.Pattern[str]], list[str]]:
    literals = _policy_values(policy, ("private_literals", "forbidden_literals", "private_policy_literals"))
    regexes: list[re.Pattern[str]] = []
    for value in _policy_values(policy, ("private_regexes", "forbidden_regexes", "private_policy_regexes")):
        try:
            regexes.append(re.compile(value))
        except re.error as exc:
            raise ScanError(f"invalid private policy regex: {exc}") from exc
    disclosures = _policy_values(policy, ("third_party_names", "exact_third_party_names", "disclosure_names", "disclosure_only_literals"))
    disclosure_obj = policy.get("third_party_disclosures")
    if isinstance(disclosure_obj, dict):
        disclosures.extend(_policy_values(disclosure_obj, ("names", "exact_names")))
    nested = policy.get("private_scan_policy")
    if isinstance(nested, dict):
        a, b, c = _private_rules(nested)
        literals.extend(a)
        regexes.extend(b)
        disclosures.extend(c)
    return literals, regexes, disclosures


def _disclosure_paths(policy: dict[str, Any]) -> list[str]:
    paths = policy.get("disclosure_paths", policy.get("allowed_disclosure_paths", policy.get("required_disclosure_paths", [])))
    if not isinstance(paths, list) or not all(isinstance(x, str) for x in paths):
        raise ScanError("required_disclosure_paths must be a string list")
    return paths


def _required_disclosures(policy: dict[str, Any]) -> tuple[str, ...]:
    paths = policy.get("required_disclosure_paths", [])
    if not isinstance(paths, list) or any(not isinstance(path, str) or not path for path in paths):
        raise ScanError("required_disclosure_paths must be a string list")
    normalized: list[str] = []
    for path in paths:
        try:
            normalized.append(_norm_rel(path.replace(_BACKSLASH, "/")))
        except ValueError as exc:
            raise ScanError(f"invalid required disclosure path: {path}") from exc
    return tuple(dict.fromkeys(normalized))


def _missing_disclosure_findings(
    seen: set[str], policy: dict[str, Any], source_label: str
) -> list[dict[str, Any]]:
    return [
        _finding(
            "missing_required_disclosure",
            f"{source_label}!{required}" if source_label else required,
            "required public disclosure is absent from this scanned surface",
        )
        for required in _required_disclosures(policy)
        if required not in seen
    ]


def _is_disclosure(rel: str, policy: dict[str, Any]) -> bool:
    return any(fnmatch.fnmatchcase(rel, p.replace(_BACKSLASH, "/")) for p in _disclosure_paths(policy))


def _archive_name(name: str) -> bool:
    low = name.lower()
    return any(low.endswith(ext) for ext in _ARCHIVE_EXTENSIONS)


def _decode_strings(data: bytes) -> list[tuple[str, str]]:
    """Return printable ASCII/UTF-16 strings found in arbitrary bytes."""
    result: list[tuple[str, str]] = []
    for match in _ASCII.finditer(data):
        result.append(("ascii", match.group().decode("ascii", "strict")))
    for match in _UTF16LE.finditer(data):
        try:
            result.append(("utf16le", match.group().decode("utf-16-le", "strict")))
        except UnicodeDecodeError:
            pass
    for match in _UTF16BE.finditer(data):
        try:
            result.append(("utf16be", match.group().decode("utf-16-be", "strict")))
        except UnicodeDecodeError:
            pass
    return result


def _content_findings(path: str, data: bytes, policy: dict[str, Any], *, binary: bool = False, logical_path: str | None = None) -> list[dict[str, Any]]:
    literals, regexes, disclosures = _private_rules(policy)
    # Labels such as archive.zip!docs/NOTICE.md and git:<commit>:docs/NOTICE.md
    # are useful diagnostics, but disclosure policy is always based on the
    # logical relative path inside the candidate tree/member.
    disclosure = _is_disclosure(logical_path or path, policy)
    # Binary strings are checked as strings too.  For text, strict UTF-8 gives
    # readable diagnostics; undecodable content is still inspected via strings.
    text = data.decode("utf-8", "replace")
    strings = _decode_strings(data) if binary or b"\x00" in data or "�" in text else [("utf8", text)]
    findings: list[dict[str, Any]] = []
    for kind, value in strings:
        for match in (_DRIVE_PATH, _UNC_PATH, _UNIX_PRIVATE_PATH):
            if match.search(value):
                findings.append(_finding("local_path", path, "local absolute or UNC path in content", encoding=kind))
                break
        if _CREDENTIAL.search(value):
            findings.append(_finding("credential_like", path, "credential-like assignment in content", encoding=kind))
        for literal in literals:
            if literal and literal in value:
                findings.append(_finding("private_literal", path, "private-policy literal matched", value=literal, encoding=kind))
        for expression in regexes:
            if expression.search(value):
                findings.append(_finding("private_regex", path, "private-policy regex matched", pattern=expression.pattern, encoding=kind))
        for name in disclosures:
            if name and name in value and not disclosure:
                findings.append(_finding("disclosure_scope", path, "third-party disclosure name outside disclosure path", value=name, encoding=kind))
    return findings


def _read_bytes(path: Path) -> bytes:
    try:
        with open(path, "rb") as fh:
            return fh.read()
    except (OSError, IOError) as exc:
        raise ScanError(f"unreadable file {path}: {exc}") from exc


def _check_path_policy(rel: str, policy: dict[str, Any]) -> list[dict[str, Any]]:
    rel = rel.replace(_BACKSLASH, "/")
    parts = rel.split("/")
    name = parts[-1].lower()
    findings: list[dict[str, Any]] = []
    if not is_allowlisted(rel, policy):
        findings.append(_finding("unclassified", rel, "path is not in the positive allowlist"))
    if name in {x.lower() for x in policy.get("always_deny_names", [])} or name in _DENY_NAMES:
        findings.append(_finding("denied_name", rel, "disallowed filename"))
    if Path(name).suffix.lower() in _DENY_EXTENSIONS or Path(name).suffix.lower() in {str(x).lower() for x in policy.get("always_deny_extensions", [])}:
        findings.append(_finding("denied_extension", rel, "disallowed payload extension"))
    for component in parts[:-1]:
        if component.lower() in _DENY_DIRS:
            findings.append(_finding("denied_category", rel, f"disallowed directory category: {component}"))
    if _archive_name(name):
        findings.append(_finding("archive_requires_explicit_scan", rel, "archive must be scanned through archive mode"))
    return findings


def _scan_archive_member(name: str, data: bytes, policy: dict[str, Any], findings: list[dict[str, Any]], source_label: str) -> None:
    member = name.replace(_BACKSLASH, "/")
    try:
        normalized = _norm_rel(member)
    except (ValueError, ScanError) as exc:
        findings.append(_finding("archive_member_path", f"{source_label}!{member}", str(exc)))
        return
    if _archive_name(normalized):
        findings.append(_finding("nested_archive", f"{source_label}!{normalized}", "nested archives are forbidden"))
    findings.extend(_check_path_policy(normalized, policy))
    findings.extend(_content_findings(f"{source_label}!{normalized}", data, policy, binary=(b"\x00" in data), logical_path=normalized))


def scan_archive(path: os.PathLike[str] | str, policy: dict[str, Any]) -> list[dict[str, Any]]:
    """Scan a zip or tar archive, rejecting links, unreadable entries, and nesting."""
    archive = Path(path)
    if _is_reparse(archive) or not archive.is_file():
        raise ScanError(f"archive is not a regular non-link file: {archive}")
    label = str(archive)
    findings: list[dict[str, Any]] = []
    seen: set[str] = set()
    low = archive.name.lower()
    try:
        if low.endswith(".zip"):
            with zipfile.ZipFile(archive) as zf:
                for info in zf.infolist():
                    if info.is_dir():
                        continue
                    mode = (info.external_attr >> 16) & 0o170000
                    if mode == stat.S_IFLNK:
                        findings.append(_finding("archive_link", f"{label}!{info.filename}", "archive symlink is forbidden"))
                        continue
                    try:
                        data = zf.read(info)
                    except (OSError, RuntimeError, zipfile.BadZipFile, KeyError) as exc:
                        raise ScanError(f"unreadable archive member {info.filename}: {exc}") from exc
                    try:
                        seen.add(_norm_rel(info.filename.replace(_BACKSLASH, "/")))
                    except ValueError:
                        pass
                    _scan_archive_member(info.filename, data, policy, findings, label)
        elif low.endswith((".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tbz2", ".tar.xz", ".txz")):
            with tarfile.open(archive, mode="r:*") as tf:
                for info in tf.getmembers():
                    if info.isdir():
                        continue
                    if info.issym() or info.islnk() or not info.isfile():
                        findings.append(_finding("archive_link", f"{label}!{info.name}", "archive links and special members are forbidden"))
                        continue
                    extracted = tf.extractfile(info)
                    if extracted is None:
                        raise ScanError(f"unreadable archive member {info.name}")
                    try:
                        seen.add(_norm_rel(info.name.replace(_BACKSLASH, "/")))
                    except ValueError:
                        pass
                    _scan_archive_member(info.name, extracted.read(), policy, findings, label)
        else:
            raise ScanError(f"unsupported archive format: {archive}")
    except (OSError, EOFError, tarfile.TarError, zipfile.BadZipFile) as exc:
        raise ScanError(f"cannot scan archive {archive}: {exc}") from exc
    findings.extend(_missing_disclosure_findings(seen, policy, label))
    return findings


def _git(*args: str, cwd: Path, check: bool = True) -> subprocess.CompletedProcess[bytes]:
    try:
        result = subprocess.run(["git", "-C", str(cwd), *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        raise ScanError(f"Git inspection unavailable: {exc}") from exc
    if check and result.returncode:
        detail = result.stderr.decode("utf-8", "replace").strip()
        raise ScanError(f"Git inspection failed: {detail or result.returncode}")
    return result


def _scan_git(root: Path, policy: dict[str, Any], commit: str | None, history: bool) -> list[dict[str, Any]]:
    if not (root / ".git").exists() or _is_reparse(root / ".git"):
        raise ScanError("Git inspection requested but .git is absent or unsafe")
    target = commit or "HEAD"
    verified = _git("rev-parse", "--verify", f"{target}^{{commit}}", cwd=root)
    commit_id = verified.stdout.decode("ascii", "strict").strip()
    findings: list[dict[str, Any]] = []

    def scan_commit(commit_hash: str) -> None:
        commit_object = _git("cat-file", "commit", commit_hash, cwd=root).stdout
        findings.extend(
            _content_findings(
                f"git:{commit_hash}:commit-metadata",
                commit_object,
                policy,
                binary=False,
            )
        )
        raw = _git("ls-tree", "-r", "-z", commit_hash, cwd=root).stdout
        seen: set[str] = set()
        for record in raw.split(b"\x00"):
            if not record:
                continue
            try:
                meta, rel_bytes = record.split(b"\t", 1)
                mode, kind, _object = meta.decode("ascii").split(" ", 2)
                rel = rel_bytes.decode("utf-8", "strict")
            except (ValueError, UnicodeError) as exc:
                raise ScanError(f"malformed Git tree record: {exc}") from exc
            try:
                rel = _norm_rel(rel)
            except ValueError as exc:
                findings.append(_finding("git_path", rel, str(exc)))
                continue
            if mode == "120000" or kind == "commit":
                findings.append(_finding("git_link", rel, "Git symlink or submodule is forbidden"))
                continue
            seen.add(rel)
            findings.extend(_check_path_policy(rel, policy))
            shown = _git("show", f"{commit_hash}:{rel}", cwd=root).stdout
            findings.extend(_content_findings(f"git:{commit_hash}:{rel}", shown, policy, binary=(b"\x00" in shown), logical_path=rel))
        findings.extend(_missing_disclosure_findings(seen, policy, f"git:{commit_hash}"))

    scan_commit(commit_id)
    if history:
        commits = _git("rev-list", commit_id, cwd=root).stdout.decode("ascii", "strict").splitlines()
        for old_commit in commits[1:]:
            scan_commit(old_commit)
    return findings


def scan_public_surface(
    root: os.PathLike[str] | str,
    allowlist: os.PathLike[str] | str | dict[str, Any],
    *,
    archives: Iterable[os.PathLike[str] | str] | None = None,
    git_commit: str | None = None,
    git_history: bool = False,
    private_policy: os.PathLike[str] | str | dict[str, Any] | None = None,
    scan_worktree: bool = True,
) -> dict[str, Any]:
    """Scan a worktree and optional independently supplied archives/Git state."""
    base = Path(root).absolute()
    if _is_reparse(base) or not base.is_dir():
        raise ScanError("scan root must be an existing non-link directory")
    policy = validate_allowlist(allowlist) if isinstance(allowlist, dict) else load_allowlist(allowlist)
    if private_policy is not None:
        if isinstance(private_policy, dict):
            policy = {**policy, **private_policy}
        else:
            try:
                with open(private_policy, "r", encoding="utf-8") as fh:
                    private_obj = json.load(fh)
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                raise ScanError(f"cannot read private scan policy: {exc}") from exc
            if not isinstance(private_obj, dict):
                raise ScanError("private scan policy must be an object")
            policy = {**policy, **private_obj}
    findings: list[dict[str, Any]] = []
    visited: set[Path] = set()
    if not scan_worktree and git_commit is None and not git_history and not archives:
        raise ScanError("a disabled worktree scan requires Git or archive inspection")
    worktree_seen: set[str] = set()

    def walk_error(exc: OSError) -> None:
        raise ScanError(f"cannot inspect worktree directory: {exc}") from exc

    if scan_worktree:
      for current, dirs, files in os.walk(base, topdown=True, followlinks=False, onerror=walk_error):
        current_path = Path(current)
        try:
            current_path.relative_to(base)
        except ValueError as exc:
            raise ScanError(f"walk escaped scan root: {current_path}") from exc
        dirs[:] = sorted(dirs)
        kept_dirs: list[str] = []
        for name in dirs:
            child = current_path / name
            # Only repository-root .git metadata is exempt from ordinary
            # scanning. A nested .git is an ordinary unclassified payload.
            if current_path == base and name == ".git":
                continue
            if _is_reparse(child):
                rel = child.relative_to(base).as_posix()
                findings.append(_finding("link_or_reparse", rel, "symlink/reparse directory"))
                continue
            if name == ".git" and current_path != base and not is_allowlisted(child.relative_to(base).as_posix(), policy):
                findings.append(_finding("unclassified", child.relative_to(base).as_posix(), "nested Git metadata is not exempt"))
            if name.lower() in _DENY_DIRS:
                rel = child.relative_to(base).as_posix()
                findings.append(_finding("denied_category", rel, f"disallowed directory category: {name}"))
            kept_dirs.append(name)
        dirs[:] = kept_dirs
        for name in sorted(files):
            # A linked worktree may represent Git metadata as a .git file;
            # only a root-level .git file is metadata; nested ones are public
            # surface inputs and therefore fail unless explicitly allowlisted.
            if current_path == base and name == ".git":
                continue
            candidate = current_path / name
            rel = candidate.relative_to(base).as_posix()
            if _is_reparse(candidate):
                findings.append(_finding("link_or_reparse", rel, "symlink/reparse file"))
                continue
            try:
                st = candidate.stat()
            except OSError as exc:
                raise ScanError(f"unreadable file {candidate}: {exc}") from exc
            if not stat.S_ISREG(st.st_mode):
                findings.append(_finding("unsupported_file", rel, "not a regular file"))
                continue
            worktree_seen.add(rel)
            findings.extend(_check_path_policy(rel, policy))
            data = _read_bytes(candidate)
            binary = b"\x00" in data or not _looks_text(data)
            findings.extend(_content_findings(rel, data, policy, binary=binary))
            visited.add(candidate)
        findings.extend(_missing_disclosure_findings(worktree_seen, policy, ""))
    if archives:
        for archive in archives:
            findings.extend(scan_archive(archive, policy))
    if git_commit is not None or git_history:
        findings.extend(_scan_git(base, policy, git_commit, git_history))
    return {
        "schema_version": "neutral-public-surface-report-v1",
        "status": "ok" if not findings else "failed",
        "root": str(base),
        "findings": findings,
        "finding_count": len(findings),
    }


# Short aliases are intentionally kept for programmatic callers and older CI
# wrappers; both still return the same machine-readable report.
scan = scan_public_surface


def _looks_text(data: bytes) -> bool:
    if not data:
        return True
    sample = data[:65536]
    if b"\x00" in sample:
        return False
    try:
        sample.decode("utf-8", "strict")
        return True
    except UnicodeDecodeError:
        return False


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True)
    parser.add_argument("--allowlist", required=True)
    parser.add_argument("--archive", action="append", default=[])
    parser.add_argument("--git-commit")
    parser.add_argument("--git-history", action="store_true")
    parser.add_argument("--git-only", action="store_true", help="scan only the selected Git commit/history, not worktree files")
    parser.add_argument("--archive-only", action="store_true", help="scan only explicitly supplied archives, not worktree files")
    parser.add_argument("--private-policy")
    args = parser.parse_args(argv)
    try:
        if args.git_only and args.archive_only:
            raise ScanError("--git-only and --archive-only are mutually exclusive")
        if args.archive_only and not args.archive:
            raise ScanError("--archive-only requires --archive")
        report = scan_public_surface(args.root, args.allowlist, archives=args.archive, git_commit=args.git_commit, git_history=args.git_history, private_policy=args.private_policy, scan_worktree=not (args.git_only or args.archive_only))
    except ScanError as exc:
        report = {"schema_version": "neutral-public-surface-report-v1", "status": "error", "finding_count": 1, "findings": [_finding("scan_error", str(args.root), str(exc))]}
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
        return 2
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0 if report["status"] == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
