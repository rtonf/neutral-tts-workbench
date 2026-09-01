from __future__ import annotations

import copy
import hashlib
import json
import unittest

from neutral_tts.release_contract import (
    ReleaseContractError,
    build_release_candidate,
    pending_clean_windows_acceptance,
    serialize_release_candidate,
    validate_clean_windows_acceptance,
    validate_release_candidate,
)


class ReleaseContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.artifacts = {
            "dependency_lock_sha256": "1" * 64,
            "install_layout_manifest_sha256": "2" * 64,
            "sbom_cyclonedx_sha256": "3" * 64,
            "sbom_spdx_sha256": "4" * 64,
            "source_archive_sha256": "5" * 64,
        }
        self.candidate = build_release_candidate(
            candidate_id="source-demo-v1",
            source_commit="a" * 40,
            source_tree="b" * 40,
            artifacts=self.artifacts,
        )

    def test_candidate_is_deterministic_unsigned_and_pending(self) -> None:
        repeated = build_release_candidate(
            candidate_id="source-demo-v1",
            source_commit="a" * 40,
            source_tree="b" * 40,
            artifacts=dict(reversed(tuple(self.artifacts.items()))),
        )
        self.assertEqual(self.candidate, repeated)
        self.assertEqual(self.candidate["signature_target"]["signature_status"], "UNSIGNED")
        self.assertIsNone(self.candidate["signature_target"]["signature"])
        self.assertEqual(self.candidate["clean_windows_acceptance"]["overall_status"], "PENDING")
        self.assertEqual(self.candidate["release_decision"], "NO_GO_PUBLIC_BINARY")
        encoded = serialize_release_candidate(self.candidate)
        self.assertEqual(encoded, serialize_release_candidate(json.loads(encoded)))

    def test_signature_target_binds_every_signable_fact(self) -> None:
        changed = copy.deepcopy(self.candidate)
        changed["artifacts"]["source_archive_sha256"] = "6" * 64
        with self.assertRaises(ReleaseContractError):
            validate_release_candidate(changed)

    def test_fake_signature_and_acceptance_claims_are_rejected(self) -> None:
        signed = copy.deepcopy(self.candidate)
        signed["signature_target"]["signature_status"] = "SIGNED"
        signed["signature_target"]["signature"] = "fabricated"
        with self.assertRaises(ReleaseContractError):
            validate_release_candidate(signed)
        accepted = copy.deepcopy(self.candidate)
        accepted["clean_windows_acceptance"]["overall_status"] = "PASSED"
        with self.assertRaises(ReleaseContractError):
            validate_release_candidate(accepted)

    def test_clean_windows_status_requires_per_check_evidence(self) -> None:
        value = pending_clean_windows_acceptance("source-demo-v1")
        evidence = hashlib.sha256(b"synthetic external evidence reference").hexdigest()
        for check in value["checks"]:
            check["status"] = "PASSED"
            check["evidence_sha256"] = evidence
        value["overall_status"] = "PASSED"
        with self.assertRaises(ReleaseContractError):
            validate_clean_windows_acceptance(value)
        value["checks"][0]["evidence_sha256"] = None
        with self.assertRaises(ReleaseContractError):
            validate_clean_windows_acceptance(value)

    def test_candidate_validator_refuses_even_evidence_backed_acceptance(self) -> None:
        value = copy.deepcopy(self.candidate)
        evidence = "7" * 64
        for check in value["clean_windows_acceptance"]["checks"]:
            check["status"] = "PASSED"
            check["evidence_sha256"] = evidence
        value["clean_windows_acceptance"]["overall_status"] = "PASSED"
        with self.assertRaises(ReleaseContractError):
            validate_release_candidate(value)

    def test_incomplete_or_unknown_artifact_set_is_rejected(self) -> None:
        incomplete = dict(self.artifacts)
        incomplete.pop("sbom_spdx_sha256")
        with self.assertRaises(ReleaseContractError):
            build_release_candidate(
                candidate_id="source-demo-v1",
                source_commit="a" * 40,
                source_tree="b" * 40,
                artifacts=incomplete,
            )


if __name__ == "__main__":
    unittest.main()
