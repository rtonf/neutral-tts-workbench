#!/usr/bin/env python3
"""Generate deterministic SPDX 2.3, CycloneDX 1.7, and notices for a Git tree."""

from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import re
import subprocess
import uuid
from pathlib import Path
from typing import Any, Iterable, Mapping

try:
    from dependency_closure import DependencyClosureError, load_lock, lock_digest, validate_lock
except ImportError:  # pragma: no cover - package/module invocation convenience
    from tools.dependency_closure import DependencyClosureError, load_lock, lock_digest, validate_lock
try:
    from clean_export import _is_reparse, _norm_rel, is_allowlisted, load_allowlist
except ImportError:  # pragma: no cover
    from tools.clean_export import _is_reparse, _norm_rel, is_allowlisted, load_allowlist


class SourceMetadataError(ValueError):
    """Raised when exact Git metadata cannot be produced safely."""


def _git(root: Path, *args: str, check: bool = True) -> bytes:
    try:
        process = subprocess.run(["git", "-C", str(root), *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        raise SourceMetadataError(f"Git inspection unavailable: {exc}") from exc
    if check and process.returncode:
        detail = process.stderr.decode("utf-8", "replace").strip()
        raise SourceMetadataError(f"Git inspection failed: {detail or process.returncode}")
    return process.stdout


def resolve_commit(root: str | Path, commit: str) -> str:
    repo = Path(root).absolute()
    current = repo
    while True:
        if current.exists() and _is_reparse(current):
            raise SourceMetadataError(f"repository ancestor is a reparse point: {current}")
        if current.parent == current:
            break
        current = current.parent
    if _is_reparse(repo) or not repo.is_dir() or _is_reparse(repo / ".git"):
        raise SourceMetadataError("repository root or .git is absent/reparse")
    try:
        resolved = _git(repo, "rev-parse", "--verify", f"{commit}^{{commit}}").decode("ascii").strip()
    except UnicodeError as exc:
        raise SourceMetadataError("Git returned a non-ASCII commit id") from exc
    if not re.fullmatch(r"[0-9a-f]{40}", resolved):
        raise SourceMetadataError("Git did not resolve an exact commit")
    return resolved


def _tree_and_files(root: Path, commit: str, allowlist: Mapping[str, Any] | None = None) -> tuple[str, list[dict[str, Any]]]:
    raw_tree = _git(root, "ls-tree", "-r", "--full-tree", "-z", commit)
    entries: list[dict[str, Any]] = []
    seen: set[str] = set()
    for record in raw_tree.split(b"\x00"):
        if not record:
            continue
        try:
            meta, raw_path = record.split(b"\t", 1)
            mode, kind, object_id = meta.decode("ascii").split(" ", 2)
            path = _norm_rel(raw_path.decode("utf-8", "strict"))
        except (ValueError, UnicodeError) as exc:
            raise SourceMetadataError(f"malformed Git tree record: {exc}") from exc
        if path in seen:
            raise SourceMetadataError(f"duplicate Git tree path: {path}")
        seen.add(path)
        if mode == "120000" or kind != "blob":
            raise SourceMetadataError(f"Git symlink, submodule, or non-blob is forbidden: {path}")
        if not re.fullmatch(r"[0-9a-f]{40}", object_id):
            raise SourceMetadataError(f"Git returned an invalid blob id: {path}")
        if allowlist is not None and not is_allowlisted(path, dict(allowlist)):
            raise SourceMetadataError(f"Git tree path is not allowlisted: {path}")
        content = _git(root, "cat-file", "blob", object_id)
        entries.append({"path": path, "size": len(content), "sha1": hashlib.sha1(content).hexdigest(), "sha256": hashlib.sha256(content).hexdigest(), "mode": mode, "object": object_id})
    entries.sort(key=lambda item: item["path"])
    tree = _git(root, "rev-parse", f"{commit}^{{tree}}").decode("ascii").strip()
    if not re.fullmatch(r"[0-9a-f]{40}", tree):
        raise SourceMetadataError("Git did not return an exact tree id")
    return tree, entries


def git_tree_files(root: str | Path, commit: str, allowlist: Mapping[str, Any] | None = None) -> tuple[str, list[dict[str, Any]]]:
    """Return the exact tree id and sorted regular-file facts for *commit*."""
    resolved = resolve_commit(root, commit)
    tree, files = _tree_and_files(Path(root).absolute(), resolved, allowlist)
    return tree, files


def _commit_created(root: Path, commit: str) -> str:
    value = _git(root, "show", "-s", "--format=%cI", commit).decode("ascii", "strict").strip()
    try:
        parsed = _dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SourceMetadataError(f"invalid Git commit timestamp: {value!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_dt.timezone.utc)
    return parsed.astimezone(_dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _canonical(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def _spdx_id(prefix: str, value: str) -> str:
    return f"SPDXRef-{prefix}-{hashlib.sha256(value.encode('utf-8')).hexdigest()[:24]}"


def _cdx_ref(prefix: str, value: str) -> str:
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
    return f"urn:neutral-tts:cyclonedx:{prefix}:{digest}"


def _cdx_license(value: str | None) -> list[dict[str, dict[str, str]]]:
    if value and value.upper() != "NOASSERTION":
        return [{"license": {"id": value}}]
    return [{"license": {"name": "NOASSERTION"}}]


def _validate_output_parent(target: Path) -> None:
    parent = target.parent
    if not parent.exists() or not parent.is_dir():
        raise SourceMetadataError(f"metadata output parent must be an existing directory: {parent}")
    current = parent
    while True:
        try:
            if _is_reparse(current):
                raise SourceMetadataError(f"metadata output ancestor is a reparse point: {current}")
        except SourceMetadataError:
            raise
        except Exception as exc:
            raise SourceMetadataError(f"cannot inspect metadata output ancestor {current}: {exc}") from exc
        if current.parent == current:
            break
        current = current.parent


def _write_fresh(path: Path, data: bytes) -> None:
    try:
        if path.exists() or path.is_symlink():
            raise SourceMetadataError(f"refusing to overwrite metadata file: {path.name}")
        with path.open("xb") as stream:
            stream.write(data)
    except SourceMetadataError:
        raise
    except OSError as exc:
        raise SourceMetadataError(f"cannot write metadata file {path.name}: {exc}") from exc


def _coerce_lock(lock: Mapping[str, Any] | str | Path) -> dict[str, Any]:
    return load_lock(lock) if not isinstance(lock, Mapping) else validate_lock(lock)


def build_spdx_document(root: str | Path, commit: str, lock: Mapping[str, Any] | str | Path, *, allowlist: Mapping[str, Any] | None = None) -> dict[str, Any]:
    repo = Path(root).absolute()
    checked = _coerce_lock(lock)
    resolved = resolve_commit(repo, commit)
    tree, files = _tree_and_files(repo, resolved, allowlist)
    created = _commit_created(repo, resolved)
    namespace_digest = hashlib.sha256(f"{resolved}\0{tree}\0{lock_digest(checked)}".encode("ascii")).hexdigest()
    document_uuid = uuid.uuid5(uuid.NAMESPACE_URL, f"urn:neutral-tts:source:{namespace_digest}")
    project = checked["project"]
    project_id = _spdx_id("Package", project["id"])
    packages: list[dict[str, Any]] = [{
        "SPDXID": project_id,
        "name": project["display_name"],
        "versionInfo": resolved,
        "downloadLocation": "NOASSERTION",
        "filesAnalyzed": True,
        "licenseConcluded": project["license_spdx"],
        "licenseDeclared": project["license_spdx"],
        "copyrightText": "NOASSERTION",
    }]
    relationships: list[dict[str, str]] = [{"spdxElementId": "SPDXRef-DOCUMENT", "relationshipType": "DESCRIBES", "relatedSpdxElement": project_id}]
    for component in sorted(checked["components"], key=lambda item: item["component_id"]):
        if component["component_id"] == "project-source" or not component["bundle"]:
            continue
        package_id = _spdx_id("Package", component["component_id"])
        location = component["source_url"] or component["wheel_url"] or "NOASSERTION"
        version = component["source_commit_or_version"] or "NOASSERTION"
        license_id = component["license_spdx"] or "NOASSERTION"
        packages.append({
            "SPDXID": package_id,
            "name": component["display_name"],
            "versionInfo": version,
            "downloadLocation": location,
            "filesAnalyzed": False,
            "licenseConcluded": license_id,
            "licenseDeclared": license_id,
            "copyrightText": "NOASSERTION",
        })
        relationships.append({"spdxElementId": project_id, "relationshipType": "DEPENDS_ON", "relatedSpdxElement": package_id})
    spdx_files: list[dict[str, Any]] = []
    contains: list[dict[str, str]] = []
    for entry in files:
        file_id = _spdx_id("File", entry["path"])
        spdx_files.append({
            "SPDXID": file_id,
            "fileName": "./" + entry["path"],
            "checksums": [{"algorithm": "SHA1", "checksumValue": entry["sha1"]}, {"algorithm": "SHA256", "checksumValue": entry["sha256"]}],
            "licenseConcluded": "NOASSERTION",
            "licenseInfoInFiles": ["NOASSERTION"],
            "copyrightText": "NOASSERTION",
        })
        contains.append({"spdxElementId": project_id, "relationshipType": "CONTAINS", "relatedSpdxElement": file_id})
    relationships[0:0] = contains
    verification_code = hashlib.sha1("".join(sorted(entry["sha1"].lower() for entry in files)).encode("ascii")).hexdigest()
    packages[0]["packageVerificationCode"] = {"packageVerificationCodeValue": verification_code}
    document: dict[str, Any] = {
        "spdxVersion": "SPDX-2.3",
        "dataLicense": "CC0-1.0",
        "SPDXID": "SPDXRef-DOCUMENT",
        "name": f"{project['display_name']} source tree {resolved[:12]}",
        "documentNamespace": f"urn:uuid:{document_uuid}",
        "creationInfo": {"created": created, "creators": ["Tool: neutral-source-metadata/1.0"]},
        "documentDescribes": [project_id],
        "packages": packages,
        "files": spdx_files,
        "relationships": relationships,
    }
    return document


def build_cyclonedx_document(root: str | Path, commit: str, lock: Mapping[str, Any] | str | Path, *, allowlist: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Return a deterministic CycloneDX 1.7 SBOM for included source files.

    Excluded dependency-closure records intentionally remain in the generated
    notices rather than being represented as bundled CycloneDX components.
    """
    repo = Path(root).absolute()
    checked = _coerce_lock(lock)
    resolved = resolve_commit(repo, commit)
    tree, files = _tree_and_files(repo, resolved, allowlist)
    created = _commit_created(repo, resolved)
    provenance = hashlib.sha256(f"{resolved}\0{tree}\0{lock_digest(checked)}\0cyclonedx-1.7".encode("ascii")).hexdigest()
    serial = uuid.uuid5(uuid.NAMESPACE_URL, f"urn:neutral-tts:cyclonedx:{provenance}")
    project = checked["project"]
    project_ref = _cdx_ref("application", project["id"] + "\0" + resolved)
    components: list[dict[str, Any]] = []
    file_refs: list[str] = []
    for entry in files:
        file_ref = _cdx_ref("file", entry["path"])
        file_refs.append(file_ref)
        components.append({
            "type": "file",
            "bom-ref": file_ref,
            "name": entry["path"],
            "hashes": [
                {"alg": "SHA-1", "content": entry["sha1"]},
                {"alg": "SHA-256", "content": entry["sha256"]},
            ],
            "licenses": _cdx_license(None),
        })
    return {
        "$schema": "https://cyclonedx.org/schema/bom-1.7.schema.json",
        "bomFormat": "CycloneDX",
        "specVersion": "1.7",
        "serialNumber": f"urn:uuid:{serial}",
        "version": 1,
        "metadata": {
            "timestamp": created,
            "lifecycles": [{"phase": "build"}],
            "tools": {"components": [{"type": "application", "name": "neutral-source-metadata", "version": "1.0"}]},
            "component": {
                "type": "application",
                "bom-ref": project_ref,
                "name": project["display_name"],
                "version": resolved,
                "licenses": _cdx_license(project["license_spdx"]),
            },
        },
        "components": components,
        "dependencies": [{"ref": project_ref, "dependsOn": file_refs}],
        "compositions": [{"aggregate": "incomplete_first_party_only", "assemblies": [project_ref]}],
    }


def build_notices(lock: Mapping[str, Any] | str | Path, *, commit: str) -> str:
    checked = _coerce_lock(lock)
    lines = [
        "# Third-party notices (source-only metadata)",
        "",
        "This deterministic notice was generated from dependency-closure.lock.json.",
        f"Exact source commit: `{commit}`",
        "",
        "No third-party, model, tokenizer, codec, runtime, native, or voice payload is bundled by the current lock.",
        "The entries below are closure records and do not assert redistribution permission.",
        "",
    ]
    for component in sorted(checked["components"], key=lambda item: item["component_id"]):
        if component["component_id"] == "project-source":
            continue
        lines.extend([
            f"## {component['display_name']}",
            "",
            f"- Component ID: `{component['component_id']}`",
            f"- Kind: `{component['kind']}`",
            f"- License SPDX: `{component['license_spdx'] or 'NOASSERTION'}`",
            f"- License status: `{component['license_status'] or 'MISSING'}`",
            f"- Redistribution status: `{component['redistribution_status']}`",
            f"- Notice required: `{str(component['notice_required']).lower() if component['notice_required'] is not None else 'unknown'}`",
            f"- Notice status: `{component['notice_status']}`",
            f"- Approval status: `{component['approval_status']}`",
            f"- Approval authority: `{component['approval_authority'] or 'NONE_RECORDED'}`",
            f"- Bundled: `{str(component['bundle']).lower()}`",
            f"- Source URL: `{component['source_url'] or 'NOASSERTION'}`",
            f"- Wheel URL: `{component['wheel_url'] or 'NOASSERTION'}`",
            f"- Source commit/version: `{component['source_commit_or_version'] or 'NOASSERTION'}`",
            f"- SHA-256: `{component['sha256'] or 'NOASSERTION'}`",
            f"- Decision: {component['bundle_reason'] or 'NOASSERTION'}",
            "",
        ])
    return "\n".join(lines)


def generate_metadata(root: str | Path, commit: str, lock: Mapping[str, Any] | str | Path, output_dir: str | Path, *, allowlist: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Write deterministic SPDX, CycloneDX, and notice metadata."""
    target = Path(output_dir).absolute()
    if target.exists() or target.is_symlink():
        raise SourceMetadataError(f"metadata output directory must be fresh: {target}")
    _validate_output_parent(target)
    resolved = resolve_commit(root, commit)
    spdx = build_spdx_document(root, resolved, lock, allowlist=allowlist)
    cyclonedx = build_cyclonedx_document(root, resolved, lock, allowlist=allowlist)
    notices = build_notices(lock, commit=resolved)
    try:
        target.mkdir(exist_ok=False)
    except OSError as exc:
        raise SourceMetadataError(f"cannot create fresh metadata output directory: {exc}") from exc
    try:
        if _is_reparse(target):
            raise SourceMetadataError(f"metadata output directory is a reparse point: {target}")
    except SourceMetadataError:
        raise
    except Exception as exc:
        raise SourceMetadataError(f"cannot inspect metadata output directory: {exc}") from exc
    _write_fresh(target / "SBOM.spdx.json", _canonical(spdx))
    _write_fresh(target / "SBOM.cdx.json", _canonical(cyclonedx))
    _write_fresh(target / "THIRD_PARTY_NOTICES.generated.md", notices.encode("utf-8"))
    return {"commit": resolved, "files": ["SBOM.cdx.json", "SBOM.spdx.json", "THIRD_PARTY_NOTICES.generated.md"]}


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--lock", required=True)
    parser.add_argument("--allowlist")
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args(argv)
    try:
        allowlist = load_allowlist(args.allowlist) if args.allowlist else None
        result = generate_metadata(args.root, args.commit, load_lock(args.lock), args.output_dir, allowlist=allowlist)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    except (DependencyClosureError, SourceMetadataError) as exc:
        del exc
        print(json.dumps({"status": "error", "code": "SOURCE_METADATA_REJECTED", "message": "Source metadata generation failed."}, ensure_ascii=False, sort_keys=True))
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


# Stable noun-first aliases for CI callers.
generate_source_metadata = generate_metadata
