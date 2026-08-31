import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from tools.clean_export import ExportError, build_manifest, export_clean, validate_allowlist


class CleanExportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "src").mkdir()
        self.allowlist = {
            "default": "deny",
            "source_paths": ["src/**/*.py"],
            "required_disclosure_paths": [],
            "always_deny_extensions": [],
            "always_deny_names": [],
        }
        (self.root / "src" / "main.py").write_text("print('synthetic')\n", encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def test_exact_hash_manifest_exports_only_allowlisted_files(self):
        manifest = build_manifest(self.root, self.allowlist)
        destination = self.root.parent / (self.root.name + "-staging")
        try:
            summary = export_clean(self.root, destination, manifest, self.allowlist)
            self.assertEqual(summary["status"], "ok")
            self.assertEqual((destination / "src" / "main.py").read_text(), "print('synthetic')\n")
        finally:
            import shutil
            shutil.rmtree(destination, ignore_errors=True)

    def test_traversal_is_rejected_before_destination_creation(self):
        digest = hashlib.sha256(b"x").hexdigest()
        manifest = {"schema_version": "neutral-clean-export-v1", "files": [{"path": "../secret.py", "size": 1, "sha256": digest}]}
        destination = self.root / "staging"
        with self.assertRaises(ExportError):
            export_clean(self.root, destination, manifest, self.allowlist)
        self.assertFalse(destination.exists())

    def test_hash_mismatch_is_rejected_before_destination_creation(self):
        manifest = build_manifest(self.root, self.allowlist)
        manifest["files"][0]["sha256"] = "0" * 64
        destination = self.root / "staging"
        with self.assertRaises(ExportError):
            export_clean(self.root, destination, manifest, self.allowlist)
        self.assertFalse(destination.exists())

    def test_preexisting_destination_is_never_overwritten(self):
        manifest = build_manifest(self.root, self.allowlist)
        destination = self.root / "staging"
        destination.mkdir()
        sentinel = destination / "sentinel"
        sentinel.write_text("keep", encoding="utf-8")
        with self.assertRaises(ExportError):
            export_clean(self.root, destination, manifest, self.allowlist)
        self.assertEqual(sentinel.read_text(), "keep")

    def test_invalid_allowlist_glob_is_rejected(self):
        invalid_patterns = ("", ".." + "/" + "*.py", "/" + "tmp/*.py", "C:" + "/" + "private/*.py", "src/" + "/" + "*.py", "src/." + "/" + "*.py")
        for pattern in invalid_patterns:
            with self.subTest(pattern=pattern):
                with self.assertRaises(ExportError):
                    validate_allowlist({"default": "deny", "source_paths": [pattern]})

    def test_symlink_directory_is_rejected_by_manifest_builder(self):
        link = self.root / "src" / "linked"
        try:
            link.symlink_to(self.root / "src", target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("directory symlinks unavailable")
        with self.assertRaises(ExportError):
            build_manifest(self.root, self.allowlist)


if __name__ == "__main__":
    unittest.main()
