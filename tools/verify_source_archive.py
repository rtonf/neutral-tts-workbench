#!/usr/bin/env python3
"""Independently verify a deterministic source-only archive manifest."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import stat
import zipfile
from pathlib import Path
from typing import Any, Iterable, Mapping

try:
    from clean_export import _is_reparse, load_allowlist, validate_allowlist
except ImportError:  # pragma: no cover
    from tools.clean_export import _is_reparse, load_allowlist, validate_allowlist
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


class SourceArchiveVerificationError(ValueError):
    """Raised when archive bytes do not match their exact source manifest."""


_SHA256 = re.compile(r"^[0-9a-fA-F]{64}$")
_ARCHIVE_EXTENSIONS = (".zip", ".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tbz2", ".tar.xz", ".txz")
_DENY_EXTENSIONS = {".wav", ".mp3", ".flac", ".ogg", ".safetensors", ".ckpt", ".pt", ".pth", ".dll", ".pyd", ".so", ".dylib", ".exe", ".msi", ".db", ".sqlite", ".sqlite3", ".jsonl", ".onnx", ".bin", ".model", ".tflite", ".npy", ".npz", ".pickle", ".pkl"}
_DENY_DIRS = {".git", ".venv", "venv", "env", "models", "model", "runtime", "cache", "caches", "outputs", "output", "profiles", "profile", "state", "sessions", "session", "logs", "log", "receipts", "reports", "artifacts", "packages", "installers", "node_modules", "__pycache__", ".pytest_cache", ".mypy_cache", ".cache", "audio", "voices", "voice", "recordings", "user-data", "user_data", "userdata", "native", "site-packages", "build", "dist", "bin", "lib", "temp", "tmp"}
_GENERATED = {"SBOM.cdx.json", "SBOM.spdx.json", "THIRD_PARTY_NOTICES.generated.md"}
_LOCK_PATH = "config/dependency-closure.lock.json"


def _reparse(path: Path) -> bool:
    try:
        return _is_reparse(path)
    except Exception as exc:
        raise SourceArchiveVerificationError(f"cannot inspect archive reparse state: {exc}") from exc


def _safe_rel(value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise SourceArchiveVerificationError("archive path must be a non-empty string")
    value = value.replace("\\", "/")
    if value.startswith(("/", "//")) or (len(value) > 1 and value[1] == ":") or any(p in {"", ".", ".."} for p in value.split("/")):
        raise SourceArchiveVerificationError(f"traversal or non-canonical archive path: {value!r}")
    return value


def _facts(value: Any, label: str) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise SourceArchiveVerificationError(f"{label} must be a list")
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entry in value:
        if not isinstance(entry, dict) or set(entry) != {"path", "size", "sha256"}:
            raise SourceArchiveVerificationError(f"{label} contains an invalid file fact")
        path = _safe_rel(entry["path"])
        if path in seen:
            raise SourceArchiveVerificationError(f"duplicate manifest path: {path}")
        seen.add(path)
        if not isinstance(entry["size"], int) or entry["size"] < 0 or not isinstance(entry["sha256"], str) or not _SHA256.fullmatch(entry["sha256"]):
            raise SourceArchiveVerificationError(f"invalid size/hash for {path}")
        result.append({"path": path, "size": entry["size"], "sha256": entry["sha256"].lower()})
    if result != sorted(result, key=lambda item: item["path"]):
        raise SourceArchiveVerificationError(f"{label} must be sorted by canonical path")
    return result


def _path_policy(path: str) -> None:
    lower = path.lower()
    if path == ".git" or any(part.lower() in _DENY_DIRS for part in path.split("/")[:-1]):
        raise SourceArchiveVerificationError(f"forbidden archive directory: {path}")
    if Path(path).suffix.lower() in _DENY_EXTENSIONS or lower.endswith(_ARCHIVE_EXTENSIONS):
        raise SourceArchiveVerificationError(f"forbidden archive payload or nested archive: {path}")


def _canonical_json(data: bytes, label: str) -> Any:
    try:
        value = json.loads(data.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise SourceArchiveVerificationError(f"{label} is not valid UTF-8 JSON: {exc}") from exc
    if _canonical(value) != data:
        raise SourceArchiveVerificationError(f"{label} is not canonical deterministic JSON")
    return value


def _scanner_policy(allowlist: Mapping[str, Any] | None, names: list[str]) -> dict[str, Any]:
    if allowlist is None:
        return {"default": "deny", "source_paths": names, "required_disclosure_paths": []}
    policy = json.loads(json.dumps(dict(allowlist)))
    policy["source_paths"] = list(policy.get("source_paths", [])) + ["SOURCE_MANIFEST.json", "SBOM.cdx.json", "SBOM.spdx.json", "THIRD_PARTY_NOTICES.generated.md"]
    policy["disclosure_paths"] = list(policy.get("disclosure_paths", policy.get("required_disclosure_paths", []))) + list(policy.get("required_disclosure_paths", [])) + ["SOURCE_MANIFEST.json", "SBOM.cdx.json", "SBOM.spdx.json", "THIRD_PARTY_NOTICES.generated.md"]
    policy["required_disclosure_paths"] = list(policy.get("required_disclosure_paths", [])) + ["THIRD_PARTY_NOTICES.generated.md"]
    return policy


def verify_source_archive(
    archive: str | Path,
    *,
    repo_root: str | Path | None = None,
    lock: str | Path | Mapping[str, Any] | None = None,
    allowlist: str | Path | Mapping[str, Any] | None = None,
    expected_commit: str | None = None,
    audience: str | None = None,
    private_policy: str | Path | None = None,
) -> dict[str, Any]:
    """Verify ZIP structure, bytes, manifest, Git tree, SBOM, and scanner output."""
    if repo_root is None or lock is None or allowlist is None or expected_commit is None or private_policy is None:
        raise SourceArchiveVerificationError("exact verification requires repository, lock, allowlist, expected commit, and private scan policy")
    if audience != "INTERNAL_SOURCE_REVIEW":
        raise SourceArchiveVerificationError("audience must be exactly INTERNAL_SOURCE_REVIEW")
    if not re.fullmatch(r"[0-9a-f]{40}", expected_commit):
        raise SourceArchiveVerificationError("expected commit must be an exact 40-character Git commit")
    path = Path(archive).absolute()
    current = path
    while True:
        if current.exists() and _reparse(current):
            raise SourceArchiveVerificationError("archive or an archive ancestor is a reparse point")
        if current.parent == current:
            break
        current = current.parent
    if not path.is_file():
        raise SourceArchiveVerificationError("archive must be an existing regular non-link file")
    try:
        zf = zipfile.ZipFile(path, "r")
    except (OSError, zipfile.BadZipFile) as exc:
        raise SourceArchiveVerificationError(f"cannot open archive: {exc}") from exc
    with zf:
        if zf.comment:
            raise SourceArchiveVerificationError("archive comment is not allowed")
        infos = zf.infolist()
        if not infos:
            raise SourceArchiveVerificationError("archive is empty")
        members: dict[str, bytes] = {}
        member_order: list[str] = []
        for info in infos:
            if info.is_dir():
                raise SourceArchiveVerificationError(f"directory member is not allowed: {info.filename}")
            mode = (info.external_attr >> 16) & 0o170000
            if mode != stat.S_IFREG or ((info.external_attr >> 16) & 0o777) != 0o644:
                raise SourceArchiveVerificationError(f"archive link or special member: {info.filename}")
            if info.date_time != (1980, 1, 1, 0, 0, 0) or info.compress_type != zipfile.ZIP_STORED or info.extra or info.comment:
                raise SourceArchiveVerificationError(f"archive member has non-deterministic ZIP metadata: {info.filename}")
            rel = _safe_rel(info.filename)
            if rel in members:
                raise SourceArchiveVerificationError(f"duplicate archive member: {rel}")
            if rel.lower().endswith(_ARCHIVE_EXTENSIONS) or (info.filename != rel):
                raise SourceArchiveVerificationError(f"nested/non-canonical archive member: {rel}")
            data = zf.read(info)
            if data.startswith(b"PK\x03\x04") or (len(data) >= 262 and data[257:262] == b"ustar"):
                raise SourceArchiveVerificationError(f"nested archive signature: {rel}")
            _path_policy(rel)
            members[rel] = data
            member_order.append(rel)
        if member_order != sorted(member_order):
            raise SourceArchiveVerificationError("archive members are not in deterministic path order")
    if "SOURCE_MANIFEST.json" not in members:
        raise SourceArchiveVerificationError("SOURCE_MANIFEST.json is missing")
    manifest = _canonical_json(members["SOURCE_MANIFEST.json"], "SOURCE_MANIFEST.json")
    required = {"schema_version", "audience", "source_commit", "source_tree", "lock_sha256", "files", "generated", "archive_files"}
    if not isinstance(manifest, dict) or set(manifest) != required or manifest["schema_version"] != "neutral-source-manifest-v1":
        raise SourceArchiveVerificationError("SOURCE_MANIFEST.json has an invalid exact shape")
    if manifest["audience"] != "INTERNAL_SOURCE_REVIEW" or audience != manifest["audience"]:
        raise SourceArchiveVerificationError("public audience is refused by source-only archive policy")
    commit = manifest["source_commit"]
    if not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise SourceArchiveVerificationError("manifest source_commit is not an exact Git commit")
    if expected_commit is not None and expected_commit != commit:
        raise SourceArchiveVerificationError("manifest commit does not match expected commit")
    if not isinstance(manifest["source_tree"], str) or not re.fullmatch(r"[0-9a-f]{40}", manifest["source_tree"]):
        raise SourceArchiveVerificationError("manifest source_tree is not an exact Git tree")
    if not isinstance(manifest["lock_sha256"], str) or not _SHA256.fullmatch(manifest["lock_sha256"]):
        raise SourceArchiveVerificationError("manifest lock_sha256 is invalid")
    source = _facts(manifest["files"], "files")
    generated = _facts(manifest["generated"], "generated")
    if {fact["path"] for fact in generated} != _GENERATED:
        raise SourceArchiveVerificationError("generated metadata set is incomplete or contains extras")
    archive_facts = _facts(manifest["archive_files"], "archive_files")
    expected_facts = sorted(source + generated, key=lambda item: item["path"])
    if archive_facts != expected_facts:
        raise SourceArchiveVerificationError("archive_files does not exactly equal source plus generated facts")
    if set(members) != {fact["path"] for fact in archive_facts} | {"SOURCE_MANIFEST.json"}:
        raise SourceArchiveVerificationError("archive has missing or extra members")
    for fact in archive_facts:
        data = members[fact["path"]]
        if len(data) != fact["size"] or hashlib.sha256(data).hexdigest() != fact["sha256"]:
            raise SourceArchiveVerificationError(f"hash/size drift: {fact['path']}")
    lock_obj: dict[str, Any] | None = None
    if lock is not None:
        try:
            lock_obj = load_lock(lock) if not isinstance(lock, Mapping) else validate_lock(lock)
            lock_argument_bytes = None if isinstance(lock, Mapping) else Path(lock).read_bytes().replace(b"\r\n", b"\n")
        except (OSError, DependencyClosureError) as exc:
            raise SourceArchiveVerificationError(f"cannot read validated lock argument: {exc}") from exc
        if manifest["lock_sha256"] != lock_digest(lock_obj):
            raise SourceArchiveVerificationError("manifest lock digest does not match validated lock")
        committed_lock_fact = next((fact for fact in source if fact["path"] == _LOCK_PATH), None)
        if committed_lock_fact is None:
            raise SourceArchiveVerificationError(f"exact archive commit is missing {_LOCK_PATH}")
        try:
            committed_lock = json.loads(members[_LOCK_PATH].decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise SourceArchiveVerificationError(f"archive {_LOCK_PATH} is not valid JSON: {exc}") from exc
        if validate_lock(committed_lock) != lock_obj or lock_digest(committed_lock) != lock_digest(lock_obj) or (lock_argument_bytes is not None and members[_LOCK_PATH] != lock_argument_bytes):
            raise SourceArchiveVerificationError(f"archive {_LOCK_PATH} does not match the validated lock")
    allow_obj: dict[str, Any] | None = None
    repo: Path | None = None
    if repo_root is not None:
        repo = Path(repo_root).absolute()
        try:
            allow_obj = load_allowlist(allowlist) if allowlist is not None and not isinstance(allowlist, Mapping) else (validate_allowlist(dict(allowlist)) if isinstance(allowlist, Mapping) else None)
        except Exception as exc:
            raise SourceArchiveVerificationError(f"cannot read source allowlist: {exc}") from exc
        resolved = resolve_commit(repo, commit)
        if resolved != commit:
            raise SourceArchiveVerificationError("manifest commit cannot be resolved in repository")
        tree, git_facts = git_tree_files(repo, commit, allow_obj)
        git_facts = [{k: fact[k] for k in ("path", "size", "sha256")} for fact in git_facts]
        if tree != manifest["source_tree"] or git_facts != source:
            raise SourceArchiveVerificationError("archive source manifest does not exactly match Git commit/tree")
        if lock_obj is not None and allow_obj is not None:
            try:
                expected_spdx = _canonical(build_spdx_document(repo, commit, lock_obj, allowlist=allow_obj))
                expected_cyclonedx = _canonical(build_cyclonedx_document(repo, commit, lock_obj, allowlist=allow_obj))
                expected_notices = build_notices(lock_obj, commit=commit).encode("utf-8")
            except Exception as exc:
                raise SourceArchiveVerificationError(f"cannot regenerate deterministic source metadata: {exc}") from exc
            if members["SBOM.spdx.json"] != expected_spdx:
                raise SourceArchiveVerificationError("SBOM.spdx.json does not match deterministic Git+lock regeneration")
            if members["SBOM.cdx.json"] != expected_cyclonedx:
                raise SourceArchiveVerificationError("SBOM.cdx.json does not match deterministic Git+lock regeneration")
            if members["THIRD_PARTY_NOTICES.generated.md"] != expected_notices:
                raise SourceArchiveVerificationError("generated third-party notices do not match deterministic Git+lock regeneration")
    names = list(members)
    scan_allow = _scanner_policy(load_allowlist(allowlist) if allowlist is not None and not isinstance(allowlist, Mapping) else validate_allowlist(dict(allowlist)) if isinstance(allowlist, Mapping) else None, names)
    scan_root = Path(repo_root).absolute() if repo_root is not None else path.parent
    report = scan_public_surface(scan_root, scan_allow, archives=[path], private_policy=private_policy, scan_worktree=False)
    if report.get("status") != "ok":
        raise SourceArchiveVerificationError(f"public surface scanner rejected archive: {json.dumps(report.get('findings', []), ensure_ascii=False, sort_keys=True)}")
    sbom = _canonical_json(members["SBOM.spdx.json"], "SBOM.spdx.json")
    if not isinstance(sbom, dict) or sbom.get("spdxVersion") != "SPDX-2.3" or sbom.get("dataLicense") != "CC0-1.0" or sbom.get("SPDXID") != "SPDXRef-DOCUMENT":
        raise SourceArchiveVerificationError("SBOM is not SPDX 2.3")
    if not isinstance(sbom.get("packages"), list) or not isinstance(sbom.get("files"), list) or not isinstance(sbom.get("relationships"), list):
        raise SourceArchiveVerificationError("SBOM package/file/relationship sections are incomplete")
    if not any(item.get("relationshipType") == "DESCRIBES" and item.get("spdxElementId") == "SPDXRef-DOCUMENT" for item in sbom["relationships"]):
        raise SourceArchiveVerificationError("SBOM lacks explicit SPDX document DESCRIBES relationship")
    described = [item.get("relatedSpdxElement") for item in sbom["relationships"] if item.get("relationshipType") == "DESCRIBES" and item.get("spdxElementId") == "SPDXRef-DOCUMENT"]
    packages = {item.get("SPDXID"): item for item in sbom["packages"] if isinstance(item, dict)}
    if len(described) != 1 or described[0] not in packages:
        raise SourceArchiveVerificationError("SBOM must describe exactly one project package")
    project_package = packages[described[0]]
    code = project_package.get("packageVerificationCode", {}).get("packageVerificationCodeValue") if isinstance(project_package.get("packageVerificationCode"), dict) else None
    if not isinstance(code, str) or not re.fullmatch(r"[0-9a-fA-F]{40}", code):
        raise SourceArchiveVerificationError("SBOM project packageVerificationCode is missing or invalid")
    sbom_files: dict[str, dict[str, Any]] = {}
    for item in sbom["files"]:
        if not isinstance(item, dict) or not isinstance(item.get("fileName"), str) or not item["fileName"].startswith("./"):
            raise SourceArchiveVerificationError("SBOM fileName must be package-root relative with ./ prefix")
        rel = _safe_rel(item["fileName"][2:])
        if rel in sbom_files:
            raise SourceArchiveVerificationError(f"duplicate SBOM file: {rel}")
        checksums = item.get("checksums")
        if not isinstance(checksums, list):
            raise SourceArchiveVerificationError(f"SBOM checksums missing: {rel}")
        sha1 = [x.get("checksumValue", "").lower() for x in checksums if isinstance(x, dict) and x.get("algorithm") == "SHA1"]
        sha256 = [x.get("checksumValue", "").lower() for x in checksums if isinstance(x, dict) and x.get("algorithm") == "SHA256"]
        if len(sha1) != 1 or len(sha256) != 1 or not re.fullmatch(r"[0-9a-f]{40}", sha1[0]) or not re.fullmatch(r"[0-9a-f]{64}", sha256[0]):
            raise SourceArchiveVerificationError(f"SBOM requires exact SHA1 and SHA256 checksums: {rel}")
        if item.get("licenseConcluded") != "NOASSERTION" or item.get("licenseInfoInFiles") != ["NOASSERTION"]:
            raise SourceArchiveVerificationError(f"SBOM source license facts are incomplete: {rel}")
        if rel not in {fact["path"] for fact in source} or sha256[0] != next(f["sha256"] for f in source if f["path"] == rel):
            raise SourceArchiveVerificationError(f"SBOM file does not match source manifest: {rel}")
        if sha1[0] != hashlib.sha1(members[rel]).hexdigest():
            raise SourceArchiveVerificationError(f"SBOM SHA1 drift: {rel}")
        sbom_files[rel] = item
    if set(sbom_files) != {fact["path"] for fact in source}:
        raise SourceArchiveVerificationError("SBOM file set does not exactly match source manifest")
    expected_code = hashlib.sha1("".join(sorted(hashlib.sha1(members[fact["path"]]).hexdigest() for fact in source)).encode("ascii")).hexdigest()
    if code.lower() != expected_code:
        raise SourceArchiveVerificationError("SBOM packageVerificationCode drift")
    cdx = _canonical_json(members["SBOM.cdx.json"], "SBOM.cdx.json")
    if not isinstance(cdx, dict) or cdx.get("$schema") != "https://cyclonedx.org/schema/bom-1.7.schema.json" or cdx.get("bomFormat") != "CycloneDX" or cdx.get("specVersion") != "1.7" or cdx.get("version") != 1:
        raise SourceArchiveVerificationError("CycloneDX SBOM is not exact CycloneDX 1.7 JSON")
    if not isinstance(cdx.get("serialNumber"), str) or not re.fullmatch(r"urn:uuid:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", cdx["serialNumber"]):
        raise SourceArchiveVerificationError("CycloneDX serialNumber is missing or invalid")
    metadata = cdx.get("metadata")
    subject = metadata.get("component") if isinstance(metadata, dict) else None
    if not isinstance(subject, dict) or subject.get("type") != "application" or subject.get("version") != commit or not isinstance(subject.get("bom-ref"), str):
        raise SourceArchiveVerificationError("CycloneDX metadata subject does not bind the exact source commit")
    cdx_files: dict[str, dict[str, Any]] = {}
    source_by_path = {fact["path"]: fact for fact in source}
    cdx_components = cdx.get("components")
    if not isinstance(cdx_components, list):
        raise SourceArchiveVerificationError("CycloneDX components are missing")
    for item in cdx_components:
        if not isinstance(item, dict) or item.get("type") != "file" or not isinstance(item.get("name"), str) or not isinstance(item.get("bom-ref"), str):
            raise SourceArchiveVerificationError("CycloneDX components must be uniquely referenced source files")
        rel = _safe_rel(item["name"])
        if rel in cdx_files:
            raise SourceArchiveVerificationError(f"duplicate CycloneDX source file: {rel}")
        hashes = item.get("hashes")
        sha1 = [x.get("content", "").lower() for x in hashes if isinstance(x, dict) and x.get("alg") == "SHA-1"] if isinstance(hashes, list) else []
        sha256 = [x.get("content", "").lower() for x in hashes if isinstance(x, dict) and x.get("alg") == "SHA-256"] if isinstance(hashes, list) else []
        if rel not in source_by_path or len(sha1) != 1 or len(sha256) != 1 or sha1[0] != hashlib.sha1(members[rel]).hexdigest() or sha256[0] != source_by_path[rel]["sha256"]:
            raise SourceArchiveVerificationError(f"CycloneDX source hash drift: {rel}")
        cdx_files[rel] = item
    if set(cdx_files) != set(source_by_path):
        raise SourceArchiveVerificationError("CycloneDX component set does not exactly match included source")
    dependencies = cdx.get("dependencies")
    if not isinstance(dependencies, list) or dependencies != [{"ref": subject["bom-ref"], "dependsOn": [cdx_files[rel]["bom-ref"] for rel in sorted(cdx_files)]}]:
        raise SourceArchiveVerificationError("CycloneDX dependency graph does not exactly assemble the source files")
    return {"schema_version": "neutral-source-archive-verification-v1", "status": "ok", "archive": path.name, "source_commit": commit, "source_tree": manifest["source_tree"], "member_count": len(members), "archive_sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", required=True)
    parser.add_argument("--root", required=True)
    parser.add_argument("--lock", required=True)
    parser.add_argument("--allowlist", required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--audience", required=True)
    parser.add_argument("--private-policy", required=True)
    args = parser.parse_args(argv)
    try:
        result = verify_source_archive(args.archive, repo_root=args.root, lock=args.lock, allowlist=args.allowlist, expected_commit=args.commit, audience=args.audience, private_policy=args.private_policy)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    except (SourceArchiveVerificationError, SourceMetadataError, DependencyClosureError, OSError, zipfile.BadZipFile) as exc:
        del exc
        print(json.dumps({"status": "error", "code": "SOURCE_ARCHIVE_VERIFICATION_REJECTED", "message": "Source archive verification failed."}, ensure_ascii=False, sort_keys=True))
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


verify_archive = verify_source_archive
