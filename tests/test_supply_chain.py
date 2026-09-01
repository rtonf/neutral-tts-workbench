"""Focused tests for the source-only dependency and archive contracts."""

from __future__ import annotations

import hashlib
import io
import json
import shutil
import subprocess
import tempfile
import unittest
import zipfile
from contextlib import redirect_stdout
from pathlib import Path

from tools.build_source_archive import SourceArchiveError, build_source_archive, main as build_archive_main
from tools.dependency_closure import DependencyClosureError, load_lock, main as dependency_main, validate_public_bundle
from tools.generate_source_metadata import SourceMetadataError, build_cyclonedx_document, build_spdx_document, generate_metadata, git_tree_files, main as metadata_main
from tools.verify_source_archive import SourceArchiveVerificationError, main as verify_archive_main, verify_source_archive


ROOT = Path(__file__).resolve().parents[1]
PRIVATE_POLICY = ROOT / ".private-compliance" / "public-scan-policy.json"
AUDIENCE = "INTERNAL_SOURCE_REVIEW"


class SupplyChainTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        # Build against a fresh committed candidate.  The parent integration
        # may add this lock and other owned files to required disclosures after
        # the repository's baseline HEAD, so using HEAD directly would create
        # a deliberately incomplete exact-commit fixture.
        cls.fixture_tmp = tempfile.TemporaryDirectory()
        cls.fixture = Path(cls.fixture_tmp.name) / "candidate"
        shutil.copytree(ROOT, cls.fixture, ignore=shutil.ignore_patterns(".git", ".private-compliance", ".staging", "__pycache__", ".pytest_cache"))
        subprocess.run(["git", "init", "-b", "main", str(cls.fixture)], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(cls.fixture), "config", "user.name", "Supply Chain Test"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(cls.fixture), "config", "user.email", "supply-chain-test@invalid"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(cls.fixture), "add", "-A"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(cls.fixture), "commit", "-m", "exact source candidate"], check=True, capture_output=True)
        cls.commit = subprocess.check_output(["git", "-C", str(cls.fixture), "rev-parse", "HEAD"], text=True).strip()
        cls.lock = cls.fixture / "config" / "dependency-closure.lock.json"
        cls.allowlist = cls.fixture / "compliance" / "source-asset-allowlist.json"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.fixture_tmp.cleanup()

    def _build(self, destination: Path) -> None:
        build_source_archive(self.fixture, self.commit, destination, self.lock, self.allowlist, audience=AUDIENCE, private_policy=PRIVATE_POLICY)

    def _verify(self, archive: Path) -> dict:
        return verify_source_archive(
            archive,
            repo_root=self.fixture,
            lock=self.lock,
            allowlist=self.allowlist,
            expected_commit=self.commit,
            audience=AUDIENCE,
            private_policy=PRIVATE_POLICY,
        )

    def _variant(self, *, remove_lock: bool = False, mismatch_lock: bool = False) -> tuple[Path, str]:
        variant = Path(self.fixture_tmp.name) / ("variant-" + str(len(list(Path(self.fixture_tmp.name).iterdir()))))
        shutil.copytree(self.fixture, variant, ignore=shutil.ignore_patterns(".git", "__pycache__", ".pytest_cache"))
        lock_path = variant / "config" / "dependency-closure.lock.json"
        if remove_lock:
            lock_path.unlink()
        if mismatch_lock:
            value = json.loads(lock_path.read_text(encoding="utf-8"))
            value["project"]["display_name"] = "Mismatched source candidate"
            lock_path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        subprocess.run(["git", "init", "-b", "main", str(variant)], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(variant), "config", "user.name", "Supply Chain Test"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(variant), "config", "user.email", "supply-chain-test@invalid"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(variant), "add", "-A"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(variant), "commit", "-m", "variant"], check=True, capture_output=True)
        commit = subprocess.check_output(["git", "-C", str(variant), "rev-parse", "HEAD"], text=True).strip()
        return variant, commit

    def test_lock_is_fail_closed_and_current_candidate_is_internal_only(self) -> None:
        lock = load_lock(self.lock)
        self.assertFalse(lock["project"]["public_distribution"])
        self.assertEqual(lock["source_candidate"]["audience"], "INTERNAL_SOURCE_REVIEW")
        with self.assertRaises(DependencyClosureError):
            validate_public_bundle(lock)
        external = [c for c in lock["components"] if c["component_id"] != "project-source"]
        self.assertTrue(external)
        self.assertFalse(any(c["bundle"] for c in external))

    def test_validator_rejects_unsafe_or_incomplete_supplied_facts(self) -> None:
        original = load_lock(self.lock)
        cases = [
            ("display_name", 17),
            ("kind", ""),
            ("sha256", "not-a-sha"),
            ("source_url", "relative/path"),
            ("wheel_url", "https" + chr(58) + "//user" + chr(58) + "password@example.com/wheel"),
        ]
        for field, value in cases:
            mutated = json.loads(json.dumps(original))
            component = next(item for item in mutated["components"] if item["component_id"] != "project-source")
            component[field] = value
            with self.subTest(field=field), self.assertRaises(DependencyClosureError):
                load_lock_from_object(mutated)
        mutated = json.loads(json.dumps(original))
        source = next(item for item in mutated["components"] if item["component_id"] == "project-source")
        source["source_commit_or_version"] = "HEAD"
        with self.assertRaises(DependencyClosureError):
            load_lock_from_object(mutated)

    def test_supply_chain_cli_errors_are_path_private(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            private_path = Path(directory) / "private-candidate.zip"
            private_path.write_bytes(b"sentinel")
            calls = [
                (dependency_main, ["--lock", str(self.lock), "--public"]),
                (metadata_main, ["--root", str(self.fixture), "--commit", self.commit, "--lock", str(self.lock), "--allowlist", str(self.allowlist), "--output-dir", directory]),
                (build_archive_main, ["--root", str(self.fixture), "--commit", self.commit, "--output", str(private_path), "--lock", str(self.lock), "--allowlist", str(self.allowlist), "--audience", AUDIENCE, "--private-policy", str(PRIVATE_POLICY)]),
                (verify_archive_main, ["--archive", str(Path(directory) / "missing.zip"), "--root", str(self.fixture), "--lock", str(self.lock), "--allowlist", str(self.allowlist), "--commit", self.commit, "--audience", AUDIENCE, "--private-policy", str(PRIVATE_POLICY)]),
            ]
            for main, argv in calls:
                output = io.StringIO()
                with self.subTest(main=main.__module__), redirect_stdout(output):
                    self.assertEqual(main(argv), 2)
                    payload = output.getvalue()
                    self.assertNotIn(str(self.fixture), payload)
                    self.assertNotIn(str(Path(directory)), payload)

    def test_spdx_has_exact_content_checksums_and_verification_code(self) -> None:
        lock = load_lock(self.lock)
        tree, facts = git_tree_files(self.fixture, self.commit, json.loads(self.allowlist.read_text(encoding="utf-8")))
        document = build_spdx_document(self.fixture, self.commit, lock, allowlist=json.loads(self.allowlist.read_text(encoding="utf-8")))
        cyclonedx = build_cyclonedx_document(self.fixture, self.commit, lock, allowlist=json.loads(self.allowlist.read_text(encoding="utf-8")))
        self.assertEqual(document["spdxVersion"], "SPDX-2.3")
        self.assertEqual(document["dataLicense"], "CC0-1.0")
        self.assertTrue(any(r["relationshipType"] == "DESCRIBES" for r in document["relationships"]))
        self.assertEqual(len(document["files"]), len(facts))
        expected = hashlib.sha1("".join(sorted(f["sha1"] for f in facts)).encode("ascii")).hexdigest()
        project = document["packages"][0]
        self.assertEqual(project["packageVerificationCode"]["packageVerificationCodeValue"], expected)
        for item in document["files"]:
            self.assertTrue(item["fileName"].startswith("./"))
            self.assertEqual({c["algorithm"] for c in item["checksums"]}, {"SHA1", "SHA256"})
            self.assertEqual(item["licenseInfoInFiles"], ["NOASSERTION"])
        self.assertEqual(cyclonedx["bomFormat"], "CycloneDX")
        self.assertEqual(cyclonedx["specVersion"], "1.7")
        self.assertEqual(cyclonedx["metadata"]["component"]["version"], self.commit)
        self.assertEqual(cyclonedx["compositions"][0]["aggregate"], "incomplete_first_party_only")
        self.assertEqual(len(cyclonedx["components"]), len(facts))
        self.assertEqual({item["name"] for item in cyclonedx["components"]}, {item["path"] for item in facts})
        self.assertEqual(tree, subprocess_tree(self.fixture, self.commit))

    def test_repeated_builds_are_byte_identical_and_no_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            first = Path(directory) / "one.zip"
            second = Path(directory) / "two.zip"
            self._build(first)
            self._build(second)
            self.assertEqual(first.read_bytes(), second.read_bytes())
            with self.assertRaises(SourceArchiveError):
                self._build(first)
            self.assertEqual(first.read_bytes(), second.read_bytes())

    def test_public_audience_and_failure_preserve_destination(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "candidate.zip"
            output.write_bytes(b"sentinel")
            with self.assertRaises(SourceArchiveError):
                build_source_archive(self.fixture, self.commit, output, self.lock, self.allowlist, audience="PUBLIC", private_policy=PRIVATE_POLICY)
            self.assertEqual(output.read_bytes(), b"sentinel")
            review = Path(directory) / "review.zip"
            self._build(review)
            with self.assertRaises(SourceArchiveVerificationError):
                verify_source_archive(review, repo_root=self.fixture, lock=self.lock, allowlist=self.allowlist, expected_commit=self.commit, audience="PUBLIC", private_policy=PRIVATE_POLICY)
            with self.assertRaises(SourceArchiveVerificationError):
                verify_source_archive(review, repo_root=self.fixture, lock=self.lock, allowlist=self.allowlist, expected_commit=self.commit, private_policy=PRIVATE_POLICY)

    def test_verifier_rejects_extra_nested_and_tampered_members(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            original = Path(directory) / "original.zip"
            self._build(original)
            self._verify(original)
            extra = Path(directory) / "extra.zip"
            extra.write_bytes(original.read_bytes())
            with zipfile.ZipFile(extra, "a") as archive:
                archive.writestr("extra.txt", b"extra")
            with self.assertRaises(SourceArchiveVerificationError):
                self._verify(extra)
            nested = Path(directory) / "nested.zip"
            nested.write_bytes(original.read_bytes())
            with zipfile.ZipFile(nested, "a") as archive:
                archive.writestr("nested.zip", b"PK\x03\x04")
            with self.assertRaises(SourceArchiveVerificationError):
                self._verify(nested)
            tampered = Path(directory) / "tampered.zip"
            tampered.write_bytes(original.read_bytes())
            with zipfile.ZipFile(tampered, "r") as source:
                entries = [(info.filename, source.read(info)) for info in source.infolist()]
            with zipfile.ZipFile(tampered, "w", compression=zipfile.ZIP_STORED) as target:
                for name, data in entries:
                    target.writestr(name, data + b"x" if name == "README.md" else data)
            with self.assertRaises(SourceArchiveVerificationError):
                self._verify(tampered)

    def test_verifier_rejects_traversal_and_public_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bad = Path(directory) / "bad.zip"
            with zipfile.ZipFile(bad, "w") as archive:
                archive.writestr("../SOURCE_MANIFEST.json", b"{}")
            with self.assertRaises(SourceArchiveVerificationError):
                self._verify(bad)

    def test_builder_requires_committed_lock_and_matching_lock(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            missing_root, missing_commit = self._variant(remove_lock=True)
            with self.assertRaises(SourceArchiveError):
                build_source_archive(missing_root, missing_commit, Path(directory) / "missing.zip", self.lock, missing_root / "compliance" / "source-asset-allowlist.json", audience="INTERNAL_SOURCE_REVIEW", private_policy=PRIVATE_POLICY)
            mismatch_root, mismatch_commit = self._variant(mismatch_lock=True)
            with self.assertRaises(SourceArchiveError):
                build_source_archive(mismatch_root, mismatch_commit, Path(directory) / "mismatch.zip", self.lock, mismatch_root / "compliance" / "source-asset-allowlist.json", audience="INTERNAL_SOURCE_REVIEW", private_policy=PRIVATE_POLICY)

    def test_verifier_rejects_zip_metadata_drift(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            original = Path(directory) / "original.zip"
            rewritten = Path(directory) / "rewritten.zip"
            self._build(original)
            with zipfile.ZipFile(original, "r") as source, zipfile.ZipFile(rewritten, "w", compression=zipfile.ZIP_STORED) as target:
                for info in source.infolist():
                    target.writestr(info.filename, source.read(info))
            with self.assertRaises(SourceArchiveVerificationError):
                self._verify(rewritten)

    def test_verifier_regenerates_and_rejects_consistently_rehashed_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            original = Path(directory) / "original.zip"
            tampered = Path(directory) / "tampered-metadata.zip"
            self._build(original)
            with zipfile.ZipFile(original, "r") as source:
                entries = {info.filename: source.read(info) for info in source.infolist()}
            sbom = json.loads(entries["SBOM.spdx.json"].decode("utf-8"))
            sbom["name"] = "tampered deterministic document"
            sbom_bytes = (json.dumps(sbom, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
            entries["SBOM.spdx.json"] = sbom_bytes
            manifest = json.loads(entries["SOURCE_MANIFEST.json"].decode("utf-8"))
            fact = {"path": "SBOM.spdx.json", "size": len(sbom_bytes), "sha256": hashlib.sha256(sbom_bytes).hexdigest()}
            manifest["generated"] = [fact if item["path"] == fact["path"] else item for item in manifest["generated"]]
            manifest["archive_files"] = [fact if item["path"] == fact["path"] else item for item in manifest["archive_files"]]
            entries["SOURCE_MANIFEST.json"] = (json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
            with zipfile.ZipFile(tampered, "w", compression=zipfile.ZIP_STORED) as target:
                for name in sorted(entries):
                    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                    info.compress_type = zipfile.ZIP_STORED
                    info.create_system = 3
                    info.external_attr = (0o100644) << 16
                    target.writestr(info, entries[name])
            with self.assertRaises(SourceArchiveVerificationError):
                self._verify(tampered)

    def test_standalone_metadata_requires_fresh_existing_output_parent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "metadata"
            result = generate_metadata(self.fixture, self.commit, self.lock, output, allowlist=json.loads(self.allowlist.read_text(encoding="utf-8")))
            self.assertEqual(result["files"], ["SBOM.cdx.json", "SBOM.spdx.json", "THIRD_PARTY_NOTICES.generated.md"])
            with self.assertRaises(SourceMetadataError):
                generate_metadata(self.fixture, self.commit, self.lock, output, allowlist=json.loads(self.allowlist.read_text(encoding="utf-8")))
            real_parent = Path(directory) / "real-parent"
            real_parent.mkdir()
            linked_parent = Path(directory) / "linked-parent"
            try:
                linked_parent.symlink_to(real_parent, target_is_directory=True)
            except (OSError, NotImplementedError):
                return
            with self.assertRaises(SourceMetadataError):
                generate_metadata(self.fixture, self.commit, self.lock, linked_parent / "metadata", allowlist=json.loads(self.allowlist.read_text(encoding="utf-8")))

    def test_reparse_source_destination_and_archive_are_rejected_when_supported(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source_alias = base / "source-link"
            parent_real = base / "real-parent"
            parent_alias = base / "parent-link"
            archive = base / "normal.zip"
            self._build(archive)
            try:
                source_alias.symlink_to(self.fixture, target_is_directory=True)
                parent_real.mkdir()
                parent_alias.symlink_to(parent_real, target_is_directory=True)
                archive_alias = base / "archive-link.zip"
                archive_alias.symlink_to(archive)
            except (OSError, NotImplementedError):
                self.skipTest("symlink/reparse creation is unavailable")
            with self.assertRaises(Exception):
                build_source_archive(source_alias, self.commit, base / "source-link-out.zip", self.lock, self.allowlist, audience="INTERNAL_SOURCE_REVIEW", private_policy=PRIVATE_POLICY)
            with self.assertRaises(Exception):
                build_source_archive(self.fixture, self.commit, parent_alias / "out.zip", self.lock, self.allowlist, audience="INTERNAL_SOURCE_REVIEW", private_policy=PRIVATE_POLICY)
            with self.assertRaises(SourceArchiveVerificationError):
                self._verify(archive_alias)


def load_lock_from_object(value: dict) -> dict:
    from tools.dependency_closure import validate_lock
    return validate_lock(value)


def subprocess_tree(root: Path, commit: str) -> str:
    import subprocess
    return subprocess.check_output(["git", "-C", str(root), "rev-parse", f"{commit}^{{tree}}"], text=True).strip()


if __name__ == "__main__":
    unittest.main()
