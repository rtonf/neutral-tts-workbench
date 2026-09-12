from __future__ import annotations

import json
import unittest
from pathlib import Path

from tools.dependency_closure import load_lock


ROOT = Path(__file__).resolve().parents[1]


class DistributionLedgerTests(unittest.TestCase):
    def test_public_matrix_matches_dependency_decision_facts(self) -> None:
        lock = load_lock(ROOT / "config" / "dependency-closure.lock.json")
        matrix = json.loads((ROOT / "compliance" / "license-matrix.public.json").read_text(encoding="utf-8"))
        self.assertEqual(matrix["schema_version"], "neutral-license-matrix-v2")
        self.assertEqual(matrix["legal_conclusion"], "none")
        self.assertIsNone(matrix["decision_authority"])
        public_by_id = {item["component_id"]: item for item in matrix["components"]}
        lock_by_id = {item["component_id"]: item for item in lock["components"]}
        self.assertEqual(set(public_by_id), set(lock_by_id))
        required = {
            "approval_authority", "approval_status", "artifact_url", "bundled",
            "component_id", "display_name", "exact_version", "kind", "license_spdx",
            "license_status", "notice_required", "notice_status", "redistribution_status",
            "sha256", "source_url",
        }
        for component_id, public in public_by_id.items():
            with self.subTest(component_id=component_id):
                self.assertTrue(required.issubset(public))
                locked = lock_by_id[component_id]
                self.assertEqual(public["display_name"], locked["display_name"])
                self.assertEqual(public["kind"], locked["kind"])
                self.assertEqual(public["bundled"], locked["bundle"])
                self.assertEqual(public["exact_version"], locked["source_commit_or_version"])
                self.assertEqual(public["source_url"], locked["source_url"])
                self.assertEqual(public["artifact_url"], locked["wheel_url"])
                self.assertEqual(public["sha256"], locked["sha256"])
                for field in (
                    "approval_authority", "approval_status", "license_spdx", "license_status",
                    "notice_required", "notice_status", "redistribution_status",
                ):
                    self.assertEqual(public[field], locked[field])

    def test_source_license_does_not_approve_binary_or_external_payloads(self) -> None:
        ledger = json.loads(
            (ROOT / "compliance" / "distribution-gates.public.json").read_text(encoding="utf-8")
        )
        expected = {
            "codec", "installer", "model", "native_runtime", "playback", "signing",
            "source_license", "tokenizer",
        }
        gates = {item["gate_id"]: item for item in ledger["gates"]}
        self.assertEqual(set(gates), expected)
        self.assertEqual(ledger["release_decision"], "NO_GO_PUBLIC_BINARY")
        self.assertIsNone(ledger["decision_authority"])
        source = gates.pop("source_license")
        self.assertEqual(source["status"], "APPROVED")
        self.assertEqual(source["approval_authority"], "rtonf")
        self.assertIn("MIT License", (ROOT / "LICENSE").read_text(encoding="utf-8"))
        self.assertTrue((ROOT / source["approval_record"]).is_file())
        for gate in gates.values():
            self.assertNotIn(gate["status"], {"APPROVED", "PASSED"})
            self.assertIsNone(gate["approval_authority"])
            self.assertIsNone(gate["approval_record"])
            self.assertTrue(gate["required_evidence"])

    def test_all_contract_schemas_are_valid_json_objects(self) -> None:
        names = (
            "backup-manifest.schema.json",
            "clean-windows-acceptance.schema.json",
            "dependency-closure.schema.json",
            "distribution-gates.schema.json",
            "install-layout.schema.json",
            "license-matrix.schema.json",
            "provenance.schema.json",
            "release-candidate.schema.json",
            "signature-target.schema.json",
            "upgrade-rollback.schema.json",
        )
        for name in names:
            with self.subTest(name=name):
                value = json.loads((ROOT / "config" / name).read_text(encoding="utf-8"))
                self.assertEqual(value["$schema"], "https://json-schema.org/draft/2020-12/schema")
                self.assertEqual(value["type"], "object")


if __name__ == "__main__":
    unittest.main()
