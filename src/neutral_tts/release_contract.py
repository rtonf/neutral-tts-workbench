"""Deterministic source-candidate provenance and release-boundary schemas.

These records describe what a future signer and clean-device verifier would
need.  They deliberately emit only an unsigned, internal, source-only NO-GO
state and cannot manufacture a signature or acceptance result.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from typing import Any, Final


RELEASE_CANDIDATE_SCHEMA: Final = "neutral-release-candidate-v1"
PROVENANCE_SCHEMA: Final = "neutral-source-provenance-v1"
SIGNATURE_TARGET_SCHEMA: Final = "neutral-signature-target-v1"
UPGRADE_ROLLBACK_SCHEMA: Final = "neutral-upgrade-rollback-contract-v1"
CLEAN_WINDOWS_SCHEMA: Final = "neutral-clean-windows-acceptance-v1"

_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_GIT_OBJECT = re.compile(r"[0-9a-f]{40}\Z")
_CANDIDATE = re.compile(r"[a-z0-9](?:[a-z0-9.-]{0,94}[a-z0-9])?\Z")
_ARTIFACT_KEYS = (
    "dependency_lock_sha256",
    "install_layout_manifest_sha256",
    "sbom_cyclonedx_sha256",
    "sbom_spdx_sha256",
    "source_archive_sha256",
)
_ACCEPTANCE_CHECKS = (
    "demo_workflow",
    "no_external_payload",
    "test_layout_integrity",
    "uninstall_owned_only",
    "upgrade_rollback",
    "user_data_preserved",
)


class ReleaseContractError(ValueError):
    """A release-foundation record is incomplete, forged, or overclaims status."""


def _canonical(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def _candidate_id(value: object) -> str:
    if not isinstance(value, str) or not _CANDIDATE.fullmatch(value):
        raise ReleaseContractError("candidate identifier is invalid")
    return value


def _git_object(value: object, label: str) -> str:
    if not isinstance(value, str) or not _GIT_OBJECT.fullmatch(value):
        raise ReleaseContractError(f"{label} is not an exact Git object")
    return value


def _sha256(value: object, label: str) -> str:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise ReleaseContractError(f"{label} is not an exact SHA-256 value")
    return value


def _artifacts(value: object) -> dict[str, str]:
    if not isinstance(value, Mapping) or set(value) != set(_ARTIFACT_KEYS):
        raise ReleaseContractError("artifact digest set is not exact")
    return {name: _sha256(value[name], name) for name in _ARTIFACT_KEYS}


def build_provenance(
    candidate_id: str,
    source_commit: str,
    source_tree: str,
    artifacts: Mapping[str, str],
) -> dict[str, Any]:
    """Build a deterministic first-party source-contract provenance record."""

    exact_artifacts = _artifacts(artifacts)
    return {
        "schema_version": PROVENANCE_SCHEMA,
        "candidate_id": _candidate_id(candidate_id),
        "build_kind": "SOURCE_CONTRACT_ONLY",
        "source": {
            "commit": _git_object(source_commit, "source commit"),
            "tree": _git_object(source_tree, "source tree"),
        },
        "materials": [
            {"name": name, "sha256": exact_artifacts[name]}
            for name in _ARTIFACT_KEYS
        ],
        "network_used": False,
        "external_payloads": [],
        "claims_slsa_conformance": False,
    }


def upgrade_rollback_contract() -> dict[str, Any]:
    """Return the only currently supported lifecycle transition contract."""

    return {
        "schema_version": UPGRADE_ROLLBACK_SCHEMA,
        "source_layout_required": True,
        "owned_files_manifest_required": True,
        "fresh_staging_required": True,
        "no_follow_guarded_staging_required": True,
        "durable_phase_journal_required": True,
        "startup_reconciliation_required": True,
        "overwrite_existing_root": False,
        "retain_prior_complete_layout": True,
        "automatic_rollback_on_failure": True,
        "rollback_overwrites_current": False,
        "implicit_user_data_migration": False,
        "implicit_user_data_delete": False,
    }


def pending_clean_windows_acceptance(candidate_id: str) -> dict[str, Any]:
    """Create a pending template; only external physical execution can fill it."""

    return {
        "schema_version": CLEAN_WINDOWS_SCHEMA,
        "candidate_id": _candidate_id(candidate_id),
        "environment": {
            "physical_device_required": True,
            "clean_snapshot_required": True,
            "offline_required": True,
            "bundled_runtime_expected": False,
        },
        "checks": [
            {"name": name, "status": "PENDING", "evidence_sha256": None}
            for name in _ACCEPTANCE_CHECKS
        ],
        "overall_status": "PENDING",
    }


def validate_clean_windows_acceptance(value: object) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {
        "candidate_id", "checks", "environment", "overall_status", "schema_version"
    }:
        raise ReleaseContractError("clean Windows acceptance shape is not exact")
    if value.get("schema_version") != CLEAN_WINDOWS_SCHEMA:
        raise ReleaseContractError("clean Windows acceptance schema is unsupported")
    _candidate_id(value.get("candidate_id"))
    environment = value.get("environment")
    if environment != {
        "physical_device_required": True,
        "clean_snapshot_required": True,
        "offline_required": True,
        "bundled_runtime_expected": False,
    }:
        raise ReleaseContractError("clean Windows environment contract was weakened")
    checks = value.get("checks")
    if not isinstance(checks, list) or len(checks) != len(_ACCEPTANCE_CHECKS):
        raise ReleaseContractError("clean Windows acceptance check set is incomplete")
    statuses: list[str] = []
    observed_names: list[str] = []
    for check in checks:
        if not isinstance(check, dict) or set(check) != {"evidence_sha256", "name", "status"}:
            raise ReleaseContractError("clean Windows acceptance check shape is invalid")
        name = check.get("name")
        status = check.get("status")
        evidence = check.get("evidence_sha256")
        if name not in _ACCEPTANCE_CHECKS or name in observed_names:
            raise ReleaseContractError("clean Windows acceptance check is unknown or duplicated")
        # This module has no trusted physical-device verifier or authority key.
        # Consequently it validates templates only; accepting a digest-shaped
        # PASSED/FAILED claim here would let any caller forge external evidence.
        if status != "PENDING" or evidence is not None:
            raise ReleaseContractError(
                "clean Windows results require an external trusted verifier"
            )
        observed_names.append(name)
        statuses.append(status)
    if observed_names != sorted(_ACCEPTANCE_CHECKS):
        raise ReleaseContractError("clean Windows acceptance checks are not canonical")
    if value.get("overall_status") != "PENDING":
        raise ReleaseContractError("clean Windows overall status does not match its checks")
    return value


def _signable_payload(candidate: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "artifacts": candidate["artifacts"],
        "audience": candidate["audience"],
        "candidate_id": candidate["candidate_id"],
        "clean_windows_acceptance": candidate["clean_windows_acceptance"],
        "provenance": candidate["provenance"],
        "release_decision": candidate["release_decision"],
        "schema_version": candidate["schema_version"],
        "source": candidate["source"],
        "upgrade_rollback": candidate["upgrade_rollback"],
    }


def build_release_candidate(
    *,
    candidate_id: str,
    source_commit: str,
    source_tree: str,
    artifacts: Mapping[str, str],
) -> dict[str, Any]:
    """Build an unsigned internal candidate record without acceptance claims."""

    identifier = _candidate_id(candidate_id)
    commit = _git_object(source_commit, "source commit")
    tree = _git_object(source_tree, "source tree")
    exact_artifacts = _artifacts(artifacts)
    candidate: dict[str, Any] = {
        "schema_version": RELEASE_CANDIDATE_SCHEMA,
        "candidate_id": identifier,
        "audience": "INTERNAL_SOURCE_REVIEW",
        "release_decision": "NO_GO_PUBLIC_BINARY",
        "source": {"commit": commit, "tree": tree},
        "artifacts": exact_artifacts,
        "provenance": build_provenance(identifier, commit, tree, exact_artifacts),
        "upgrade_rollback": upgrade_rollback_contract(),
        "clean_windows_acceptance": pending_clean_windows_acceptance(identifier),
    }
    candidate["signature_target"] = {
        "schema_version": SIGNATURE_TARGET_SCHEMA,
        "target_format": "CANONICAL_JSON_SHA256",
        "payload_sha256": hashlib.sha256(_canonical(_signable_payload(candidate))).hexdigest(),
        "signature_status": "UNSIGNED",
        "signature_algorithm": None,
        "signature": None,
        "certificate_identity": None,
    }
    return validate_release_candidate(candidate)


def validate_release_candidate(value: object) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {
        "artifacts", "audience", "candidate_id", "clean_windows_acceptance",
        "provenance", "release_decision", "schema_version", "signature_target",
        "source", "upgrade_rollback",
    }:
        raise ReleaseContractError("release candidate shape is not exact")
    if value.get("schema_version") != RELEASE_CANDIDATE_SCHEMA:
        raise ReleaseContractError("release candidate schema is unsupported")
    identifier = _candidate_id(value.get("candidate_id"))
    if value.get("audience") != "INTERNAL_SOURCE_REVIEW" or value.get("release_decision") != "NO_GO_PUBLIC_BINARY":
        raise ReleaseContractError("release candidate exceeds the permitted audience or decision")
    source = value.get("source")
    if not isinstance(source, dict) or set(source) != {"commit", "tree"}:
        raise ReleaseContractError("release source identity is invalid")
    commit = _git_object(source.get("commit"), "source commit")
    tree = _git_object(source.get("tree"), "source tree")
    artifacts = _artifacts(value.get("artifacts"))
    expected_provenance = build_provenance(identifier, commit, tree, artifacts)
    if value.get("provenance") != expected_provenance:
        raise ReleaseContractError("release provenance does not match candidate materials")
    if value.get("upgrade_rollback") != upgrade_rollback_contract():
        raise ReleaseContractError("upgrade and rollback contract was weakened")
    acceptance = validate_clean_windows_acceptance(value.get("clean_windows_acceptance"))
    if acceptance["candidate_id"] != identifier or acceptance["overall_status"] != "PENDING":
        raise ReleaseContractError("unchecked candidate cannot claim clean Windows acceptance")
    signature = value.get("signature_target")
    if not isinstance(signature, dict) or set(signature) != {
        "certificate_identity", "payload_sha256", "schema_version", "signature",
        "signature_algorithm", "signature_status", "target_format",
    }:
        raise ReleaseContractError("signature target shape is invalid")
    expected_payload = hashlib.sha256(_canonical(_signable_payload(value))).hexdigest()
    if signature != {
        "schema_version": SIGNATURE_TARGET_SCHEMA,
        "target_format": "CANONICAL_JSON_SHA256",
        "payload_sha256": expected_payload,
        "signature_status": "UNSIGNED",
        "signature_algorithm": None,
        "signature": None,
        "certificate_identity": None,
    }:
        raise ReleaseContractError("signature target is forged, stale, or overclaims signing")
    return value


def serialize_release_candidate(value: object) -> bytes:
    return _canonical(validate_release_candidate(value))


__all__ = [
    "CLEAN_WINDOWS_SCHEMA", "PROVENANCE_SCHEMA", "RELEASE_CANDIDATE_SCHEMA",
    "SIGNATURE_TARGET_SCHEMA", "UPGRADE_ROLLBACK_SCHEMA", "ReleaseContractError",
    "build_provenance", "build_release_candidate", "pending_clean_windows_acceptance",
    "serialize_release_candidate", "upgrade_rollback_contract",
    "validate_clean_windows_acceptance", "validate_release_candidate",
]
