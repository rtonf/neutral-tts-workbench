from __future__ import annotations

import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("immutable_tree_manifest", ROOT / "tools" / "immutable_tree_manifest.py")
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class ImmutableTreeManifestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "nested").mkdir()
        (self.root / "z.txt").write_bytes(b"z")
        (self.root / "nested" / "a.txt").write_bytes(b"alpha")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_manifest_is_deterministic_and_sorted(self) -> None:
        first = MODULE.build_manifest(self.root)
        second = MODULE.build_manifest(self.root)
        self.assertEqual(first, second)
        self.assertEqual(first["directories"], ["nested"])
        self.assertEqual([item["path"] for item in first["files"]], ["nested/a.txt", "z.txt"])
        self.assertEqual(first["file_count"], 2)
        self.assertEqual(first["directory_count"], 1)
        self.assertEqual(first["total_bytes"], 6)
        self.assertEqual(
            first["algorithm"],
            "sorted relative-path<TAB>size<TAB>file-sha256 plus sorted directory paths; UTF-8; LF",
        )

    def test_summary_omits_full_records_but_keeps_aggregate(self) -> None:
        summary = MODULE.build_manifest(self.root, full_record=False)
        self.assertNotIn("files", summary)
        self.assertNotIn("directories", summary)
        self.assertEqual(len(summary["canonical_manifest_sha256"]), 64)

    def test_content_size_path_and_directory_changes_are_detected(self) -> None:
        baseline = MODULE.build_manifest(self.root)
        (self.root / "nested" / "a.txt").write_bytes(b"changed")
        content_changed = MODULE.build_manifest(self.root)
        self.assertNotEqual(
            baseline["canonical_manifest_sha256"],
            content_changed["canonical_manifest_sha256"],
        )
        self.assertNotEqual(baseline["files"], content_changed["files"])

        (self.root / "nested" / "a.txt").write_bytes(b"alpha-more")
        size_changed = MODULE.build_manifest(self.root)
        self.assertNotEqual(
            content_changed["canonical_manifest_sha256"],
            size_changed["canonical_manifest_sha256"],
        )

        (self.root / "nested" / "a.txt").rename(self.root / "nested" / "b.txt")
        path_changed = MODULE.build_manifest(self.root)
        self.assertNotEqual(
            size_changed["canonical_manifest_sha256"],
            path_changed["canonical_manifest_sha256"],
        )

        (self.root / "newdir").mkdir()
        directory_changed = MODULE.build_manifest(self.root)
        self.assertNotEqual(
            path_changed["canonical_manifest_sha256"],
            directory_changed["canonical_manifest_sha256"],
        )
        self.assertIn("newdir", directory_changed["directories"])

    def test_rejects_non_directory_root_and_symlink_when_supported(self) -> None:
        with self.assertRaises(MODULE.ManifestError):
            MODULE.build_manifest(self.root / "z.txt")
        with self.assertRaises(MODULE.ManifestError):
            MODULE.build_manifest(self.root / "missing")
        link = self.root / "link.txt"
        try:
            link.symlink_to(self.root / "z.txt")
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unavailable")
        with self.assertRaises(MODULE.ManifestError):
            MODULE.build_manifest(self.root)

    def test_cli_emits_json_to_stdout_without_writing(self) -> None:
        self.assertEqual(MODULE.main([str(self.root), "--summary"]), 0)
        parsed = json.loads(json.dumps(MODULE.build_manifest(self.root, full_record=False)))
        self.assertEqual(parsed["file_count"], 2)


if __name__ == "__main__":
    unittest.main()
