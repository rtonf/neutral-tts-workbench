#!/usr/bin/env python3
"""Strict, standard-library validation for the exact dependency closure.

The lock is deliberately more restrictive than an ordinary package lock.  It
describes things which were considered, including components that are *not*
bundled, and a public bundle is rejected unless every included external
component has independently verifiable source, artifact, version, hash, and
license facts.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from urllib.parse import urlparse
from pathlib import Path
from typing import Any, Mapping


class DependencyClosureError(ValueError):
    """Raised when a dependency closure is incomplete or unsafe."""


_SHA256 = re.compile(r"^[0-9a-fA-F]{64}$")
_PLACEHOLDER = re.compile(r"(?i)^(?:unknown|internal_only|not_approved|noassertion|none|null|todo|tbd|placeholder|unversioned|latest|head|main|master|pending|not_reviewed)$")
_FORBIDDEN_KIND = re.compile(r"(?i)(?:model|tokenizer|codec|checkpoint|runtime|native|voice)")
_EXTERNAL_APPROVED = {"APPROVED", "APPROVED_FOR_PUBLIC", "PUBLIC_APPROVED"}
_REQUIRED_COMPONENT_FIELDS = {
    "component_id", "display_name", "kind", "source_url", "wheel_url",
    "source_commit_or_version", "sha256", "license_spdx", "license_status",
    "redistribution_status", "notice_required", "notice_status",
    "approval_status", "approval_authority", "bundle", "bundle_reason",
}
_SPDX_IDENTIFIER = re.compile(r"^(?:NOASSERTION|[A-Za-z0-9][A-Za-z0-9.+-]*(?:-[A-Za-z0-9.+-]+)*)$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def _load_json(path: Path) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as fh:
            value = json.load(fh)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise DependencyClosureError(f"cannot read dependency closure {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise DependencyClosureError("dependency closure must be a JSON object")
    return value


def load_lock(path: str | Path) -> dict[str, Any]:
    return validate_lock(_load_json(Path(path)))


def load_schema(path: str | Path) -> dict[str, Any]:
    return _load_json(Path(path))


def _non_placeholder(value: Any, field: str, component: str) -> str:
    if not isinstance(value, str) or not value.strip() or _PLACEHOLDER.fullmatch(value.strip()):
        raise DependencyClosureError(f"{component}: {field} is missing, unpinned, or a placeholder")
    text = value.strip()
    if value != text or _CONTROL.search(text):
        raise DependencyClosureError(f"{component}: {field} must be canonical single-line text")
    return text


def _optional_text(value: Any, field: str, component: str) -> str | None:
    if value is None:
        return None
    return _non_placeholder(value, field, component)


def _status_text(value: Any, field: str, component: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip() or _CONTROL.search(value):
        raise DependencyClosureError(f"{component}: {field} must be canonical non-empty status text")
    return value


def _validate_url(value: Any, field: str, component: str) -> None:
    if value is None:
        return
    text = _non_placeholder(value, field, component)
    parsed = urlparse(text)
    if parsed.scheme.lower() not in {"https", "http"} or not parsed.netloc or getattr(parsed, "username") or getattr(parsed, "password"):
        raise DependencyClosureError(f"{component}: {field} must be an absolute HTTP(S) URL without credentials")
    if any(token in text.lower() for token in ("api_key", "access_token", "client_secret", "password", "passwd", "authorization", "bearer")):
        raise DependencyClosureError(f"{component}: {field} contains credential-like URL data")


def _validate_license(value: Any, component: str) -> None:
    if value is None:
        return
    text = _non_placeholder(value, "license_spdx", component) if str(value).upper() != "NOASSERTION" else value.strip()
    if not isinstance(text, str) or not _SPDX_IDENTIFIER.fullmatch(text):
        raise DependencyClosureError(f"{component}: license_spdx is not a valid SPDX identifier")


def _validate_component(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise DependencyClosureError("each dependency component must be an object")
    if set(raw) - _REQUIRED_COMPONENT_FIELDS:
        raise DependencyClosureError(f"component has unknown fields: {sorted(set(raw) - _REQUIRED_COMPONENT_FIELDS)}")
    missing = _REQUIRED_COMPONENT_FIELDS - set(raw)
    if missing:
        raise DependencyClosureError(f"component missing fields: {sorted(missing)}")
    cid = _non_placeholder(raw["component_id"], "component_id", "component")
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]*", cid):
        raise DependencyClosureError(f"{cid}: invalid component_id")
    result = dict(raw)
    result["component_id"] = cid
    for field in ("display_name", "kind"):
        result[field] = _non_placeholder(raw[field], field, cid)
    if not isinstance(raw["bundle"], bool):
        raise DependencyClosureError(f"{cid}: bundle must be boolean")
    for field in (
        "source_url", "wheel_url", "source_commit_or_version", "sha256", "license_spdx",
        "license_status", "redistribution_status", "notice_status", "approval_status",
        "approval_authority", "bundle_reason",
    ):
        value = raw[field]
        if value is not None and not isinstance(value, str):
            raise DependencyClosureError(f"{cid}: {field} must be a string or null")
    _validate_url(raw["source_url"], "source_url", cid)
    _validate_url(raw["wheel_url"], "wheel_url", cid)
    _validate_license(raw["license_spdx"], cid)
    for field in ("redistribution_status", "notice_status", "approval_status"):
        result[field] = _status_text(raw[field], field, cid)
    if raw["approval_authority"] is not None:
        result["approval_authority"] = _non_placeholder(
            raw["approval_authority"], "approval_authority", cid
        )
    for field in ("source_commit_or_version", "license_spdx", "license_status", "bundle_reason"):
        if cid == "project-source" and field in {"license_spdx", "license_status"}:
            if not isinstance(raw[field], str) or not raw[field].strip():
                raise DependencyClosureError(f"{cid}: {field} is missing")
        elif not raw["bundle"] and field in {"license_spdx", "license_status"}:
            # The internal closure intentionally records unresolved external
            # facts (NOASSERTION/NOT_APPROVED/etc.).  They remain forbidden by
            # validate_public_bundle, but are valid records for this lane.
            if raw[field] is not None and (not isinstance(raw[field], str) or not raw[field].strip()):
                raise DependencyClosureError(f"{cid}: {field} is missing")
        else:
            _optional_text(raw[field], field, cid)
    _non_placeholder(raw["bundle_reason"], "bundle_reason", cid)
    if raw["sha256"] is not None and (not isinstance(raw["sha256"], str) or not _SHA256.fullmatch(raw["sha256"])):
        raise DependencyClosureError(f"{cid}: sha256 must be an exact 64-character SHA-256 when supplied")
    if raw["notice_required"] is not None and not isinstance(raw["notice_required"], bool):
        raise DependencyClosureError(f"{cid}: notice_required must be boolean or null")
    if raw["bundle"] and cid != "project-source":
        # A bundled external artifact must be reproducibly identifiable.  URL
        # fields are intentionally both required: a source URL and the exact
        # wheel/archive URL are separate supply-chain facts.
        for field in ("source_url", "wheel_url", "source_commit_or_version", "license_spdx", "license_status", "bundle_reason"):
            _non_placeholder(raw[field], field, cid)
        digest = raw["sha256"]
        if not isinstance(digest, str) or not _SHA256.fullmatch(digest):
            raise DependencyClosureError(f"{cid}: bundled external component requires an exact SHA-256")
        if raw["license_spdx"].strip().upper() == "NOASSERTION":
            raise DependencyClosureError(f"{cid}: bundled external component has no asserted SPDX license")
        if raw["license_status"].strip().upper() not in _EXTERNAL_APPROVED:
            raise DependencyClosureError(f"{cid}: bundled external component is not license-approved")
        if raw["redistribution_status"].strip().upper() not in _EXTERNAL_APPROVED:
            raise DependencyClosureError(f"{cid}: bundled external component is not redistribution-approved")
        if raw["approval_status"].strip().upper() not in _EXTERNAL_APPROVED:
            raise DependencyClosureError(f"{cid}: bundled external component lacks approval authority")
    elif raw["bundle"] and cid == "project-source":
        if not isinstance(raw["bundle_reason"], str) or not raw["bundle_reason"].strip():
            raise DependencyClosureError("project-source requires a bundle reason")
    return result


def validate_lock(lock: Mapping[str, Any], *, public: bool = False, audience: str | None = None) -> dict[str, Any]:
    """Validate a lock and optionally apply the public-bundle gate.

    The returned object is a deep JSON copy so callers can safely use it while
    producing deterministic metadata without mutating the loaded lock.
    """
    if not isinstance(lock, Mapping):
        raise DependencyClosureError("dependency closure must be an object")
    required = {"schema_version", "baseline_commit", "source_commit_policy", "project", "policy", "components", "source_candidate"}
    if set(lock) != required:
        raise DependencyClosureError(f"dependency closure fields must be exactly {sorted(required)}")
    if lock["schema_version"] != "neutral-dependency-closure-v1":
        raise DependencyClosureError("unsupported dependency closure schema")
    if not isinstance(lock["baseline_commit"], str) or not re.fullmatch(r"[0-9a-f]{40}", lock["baseline_commit"]):
        raise DependencyClosureError("baseline_commit must be an exact 40-character Git commit")
    if lock["source_commit_policy"] != "RESOLVE_EXACT_COMMIT_ARGUMENT_AT_BUILD":
        raise DependencyClosureError("source_commit_policy must resolve the exact build commit argument")
    project = lock["project"]
    if not isinstance(project, Mapping) or set(project) != {
        "approval_authority", "approval_status", "display_name", "id", "license_spdx",
        "license_status", "notice_required", "notice_status", "public_distribution",
        "redistribution_status",
    }:
        raise DependencyClosureError("project fields are incomplete or unknown")
    for f in ("id", "display_name", "license_spdx", "license_status"):
        _non_placeholder(project[f], f, "project") if f != "license_spdx" else _validate_license(project[f], "project")
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]*", project["id"]):
        raise DependencyClosureError("project.id is invalid")
    if project["public_distribution"] is not False:
        raise DependencyClosureError("project.public_distribution must remain false for this source-only lane")
    for field in ("redistribution_status", "notice_status", "approval_status"):
        _status_text(project[field], field, "project")
    if project["notice_required"] is not None and not isinstance(project["notice_required"], bool):
        raise DependencyClosureError("project.notice_required must be boolean or null")
    if project["approval_authority"] is not None:
        _non_placeholder(project["approval_authority"], "approval_authority", "project")
    policy = lock["policy"]
    if not isinstance(policy, Mapping) or set(policy) != {"allowed_audiences", "public_bundle_requires_approval", "source_only"}:
        raise DependencyClosureError("policy fields are incomplete or unknown")
    if not isinstance(policy["allowed_audiences"], list) or not policy["allowed_audiences"] or not all(isinstance(x, str) and x for x in policy["allowed_audiences"]):
        raise DependencyClosureError("policy.allowed_audiences must be a non-empty string list")
    if not isinstance(policy["public_bundle_requires_approval"], bool) or not isinstance(policy["source_only"], bool):
        raise DependencyClosureError("policy boolean fields are invalid")
    if policy["allowed_audiences"] != ["INTERNAL_SOURCE_REVIEW"] or policy["public_bundle_requires_approval"] is not True or policy["source_only"] is not True:
        raise DependencyClosureError("dependency closure policy must remain the internal source-only policy")
    candidate = lock["source_candidate"]
    if not isinstance(candidate, Mapping) or set(candidate) != {"audience", "public_distribution", "reason"}:
        raise DependencyClosureError("source_candidate fields are incomplete or unknown")
    if candidate["audience"] != "INTERNAL_SOURCE_REVIEW" or candidate["public_distribution"] is not False:
        raise DependencyClosureError("source_candidate must be an internal, non-public candidate")
    _non_placeholder(candidate["reason"], "reason", "source_candidate")
    components = lock["components"]
    if not isinstance(components, list) or not components:
        raise DependencyClosureError("components must be a non-empty list")
    checked: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in components:
        component = _validate_component(raw)
        if component["component_id"] in seen:
            raise DependencyClosureError(f"duplicate component_id: {component['component_id']}")
        seen.add(component["component_id"])
        checked.append(component)
    project_sources = [item for item in checked if item["component_id"] == "project-source"]
    if len(project_sources) != 1:
        raise DependencyClosureError("dependency closure must contain exactly one project-source component")
    project_source = project_sources[0]
    if project_source["kind"] != "source" or project_source["bundle"] is not True or project_source["source_url"] is not None or project_source["wheel_url"] is not None or project_source["sha256"] is not None or project_source["source_commit_or_version"] != "RESOLVED_AT_BUILD_FROM_EXACT_COMMIT_ARGUMENT":
        raise DependencyClosureError("project-source must be the exact source-only build-time component")
    if project_source["license_spdx"] != project["license_spdx"] or project_source["license_status"] != project["license_status"]:
        raise DependencyClosureError("project-source license facts must match project facts")
    for field in (
        "redistribution_status", "notice_required", "notice_status", "approval_status",
        "approval_authority",
    ):
        if project_source[field] != project[field]:
            raise DependencyClosureError(f"project-source {field} must match project facts")
    result = json.loads(json.dumps(lock, ensure_ascii=False))
    result["components"] = checked
    if public:
        validate_public_bundle(result, audience=audience)
    return result


def validate_public_bundle(lock: Mapping[str, Any], *, audience: str | None = "PUBLIC") -> dict[str, Any]:
    """Apply the fail-closed public redistribution gate to a validated lock."""
    checked = validate_lock(lock, public=False)
    if audience not in {"PUBLIC", "PUBLIC_BUNDLE"}:
        raise DependencyClosureError("public bundle validation requires a public audience")
    project = checked["project"]
    if project["public_distribution"] is not True:
        raise DependencyClosureError("public distribution is explicitly blocked by the lock")
    if str(project["license_spdx"]).upper() in {"NOASSERTION", "UNKNOWN"} or str(project["license_status"]).upper() not in _EXTERNAL_APPROVED:
        raise DependencyClosureError("project source license is not approved for public distribution")
    if str(project["redistribution_status"]).upper() not in _EXTERNAL_APPROVED or str(project["approval_status"]).upper() not in _EXTERNAL_APPROVED:
        raise DependencyClosureError("project source redistribution authority is not approved")
    if audience not in checked["policy"]["allowed_audiences"]:
        raise DependencyClosureError(f"audience {audience} is not permitted by the dependency closure policy")
    if checked["policy"]["source_only"]:
        # Source-only is compatible with a public source release in principle,
        # but only after the explicit project and dependency approvals above.
        pass
    for component in checked["components"]:
        cid = component["component_id"]
        if cid == "project-source":
            continue
        kind = str(component["kind"])
        status = str(component["license_status"] or "").upper()
        if _FORBIDDEN_KIND.search(kind):
            raise DependencyClosureError(f"{cid}: model/tokenizer/codec/checkpoint/runtime/native/voice components are forbidden in a public bundle")
        if status not in _EXTERNAL_APPROVED:
            raise DependencyClosureError(f"{cid}: license status {status or 'MISSING'} is not approved")
        if str(component["redistribution_status"]).upper() not in _EXTERNAL_APPROVED or str(component["approval_status"]).upper() not in _EXTERNAL_APPROVED:
            raise DependencyClosureError(f"{cid}: redistribution authority is not approved")
        if not component["bundle"]:
            # An unbundled dependency may be documented in a source project,
            # but it cannot be part of a claim that the public closure is exact.
            raise DependencyClosureError(f"{cid}: external dependency is unbundled and therefore not an exact public closure")
        _validate_component(component)
    return checked


def lock_digest(lock: Mapping[str, Any]) -> str:
    """Return the canonical SHA-256 used by metadata provenance."""
    checked = validate_lock(lock)
    encoded = json.dumps(checked, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lock", required=True)
    parser.add_argument("--public", action="store_true")
    args = parser.parse_args(argv)
    try:
        value = load_lock(args.lock)
        if args.public:
            validate_public_bundle(value)
        print(json.dumps({"status": "ok", "lock_sha256": lock_digest(value)}, sort_keys=True))
    except DependencyClosureError as exc:
        del exc
        print(json.dumps({"status": "error", "code": "DEPENDENCY_CLOSURE_REJECTED", "message": "Dependency closure validation failed."}, sort_keys=True))
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


validate_dependency_closure = validate_lock
validate_for_public_bundle = validate_public_bundle
