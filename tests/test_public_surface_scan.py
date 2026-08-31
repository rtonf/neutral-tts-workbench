import io
import subprocess
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from tools.scan_public_surface import ScanError, scan_archive, scan_public_surface


class PublicSurfaceScanTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "src").mkdir()
        (self.root / "docs").mkdir()
        (self.root / "docs" / "NOTICE.md").write_text("required notice\n", encoding="utf-8")
        self.allowlist = {
            "default": "deny",
            "source_paths": ["src/**/*.py", "docs/*.md"],
            "required_disclosure_paths": ["docs/NOTICE.md"],
            "always_deny_extensions": [".wav", ".bin"],
            "always_deny_names": [],
        }

    def tearDown(self):
        self.tmp.cleanup()

    def test_unclassified_and_synthetic_private_content_fail(self):
        (self.root / "src" / "safe.py").write_text("ok\n", encoding="utf-8")
        (self.root / "extra.txt").write_text("not allowlisted\n", encoding="utf-8")
        (self.root / "src" / "leak.py").write_text("private-name\n", encoding="utf-8")
        report = scan_public_surface(self.root, self.allowlist, private_policy={"private_literals": ["private-name"]})
        codes = {item["code"] for item in report["findings"]}
        self.assertEqual(report["status"], "failed")
        self.assertIn("unclassified", codes)
        self.assertIn("private_literal", codes)

    def test_disclosure_name_is_scoped_to_notice(self):
        (self.root / "docs" / "NOTICE.md").write_text("AllowedVendor\n", encoding="utf-8")
        (self.root / "src" / "safe.py").write_text("AllowedVendor\n", encoding="utf-8")
        policy = {**self.allowlist, "third_party_names": ["AllowedVendor"]}
        report = scan_public_surface(self.root, policy)
        self.assertTrue(any(x["code"] == "disclosure_scope" for x in report["findings"]))
        self.assertFalse(any(x["code"] == "disclosure_scope" and x["path"] == "docs/NOTICE.md" for x in report["findings"]))

    def test_disclosure_only_literal_policy_key_is_scoped(self):
        (self.root / "docs" / "NOTICE.md").write_text("ExactVendor\n", encoding="utf-8")
        (self.root / "src" / "safe.py").write_text("ExactVendor\n", encoding="utf-8")
        policy = {**self.allowlist, "disclosure_only_literals": ["ExactVendor"]}
        report = scan_public_surface(self.root, policy)
        self.assertTrue(any(x["code"] == "disclosure_scope" and x["path"] == "src/safe.py" for x in report["findings"]))
        self.assertFalse(any(x["code"] == "disclosure_scope" and x["path"] == "docs/NOTICE.md" for x in report["findings"]))

    def test_nested_git_is_not_exempt(self):
        nested = self.root / "src" / ".git"
        nested.mkdir()
        (nested / "HEAD").write_text("private control\n", encoding="utf-8")
        report = scan_public_surface(self.root, self.allowlist)
        self.assertTrue(any(x["code"] == "unclassified" and x["path"] == "src/.git/HEAD" for x in report["findings"]))

    def test_empty_nested_git_directory_is_not_exempt(self):
        (self.root / "src" / ".git").mkdir()
        report = scan_public_surface(self.root, self.allowlist)
        self.assertTrue(any(x["code"] == "unclassified" and x["path"] == "src/.git" for x in report["findings"]))

    def test_git_only_requires_git_inspection(self):
        with self.assertRaises(ScanError):
            scan_public_surface(self.root, self.allowlist, scan_worktree=False)

    def test_required_disclosure_is_enforced(self):
        (self.root / "docs" / "NOTICE.md").unlink()
        report = scan_public_surface(self.root, self.allowlist)
        self.assertTrue(any(item["code"] == "missing_required_disclosure" for item in report["findings"]))

        archive = self.root / "missing-notice.zip"
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("src/a.py", b"safe")
        findings = scan_archive(archive, self.allowlist)
        self.assertTrue(any(item["code"] == "missing_required_disclosure" for item in findings))

    def test_git_only_ignores_untracked_private_worktree_file(self):
        (self.root / "private.txt").write_text("untracked private\n", encoding="utf-8")
        with patch("tools.scan_public_surface._scan_git", return_value=[]):
            report = scan_public_surface(self.root, self.allowlist, scan_worktree=False, git_history=True)
        self.assertEqual(report["status"], "ok")

    def test_git_history_scans_commit_metadata(self):
        try:
            subprocess.run(["git", "init", "-b", "main", str(self.root)], check=True, capture_output=True)
        except (FileNotFoundError, subprocess.CalledProcessError):
            self.skipTest("Git unavailable")
        subprocess.run(["git", "-C", str(self.root), "config", "user.name", "Test"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(self.root), "config", "user.email", "test@invalid"], check=True, capture_output=True)
        (self.root / "src" / "safe.py").write_text("safe\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.root), "add", "src/safe.py", "docs/NOTICE.md"], check=True, capture_output=True)
        private_path = "C" + ":" + chr(92) + "Users" + chr(92) + "person"
        subprocess.run(["git", "-C", str(self.root), "commit", "-m", "leak " + private_path], check=True, capture_output=True)
        report = scan_public_surface(self.root, self.allowlist, scan_worktree=False, git_history=True)
        self.assertTrue(any(item["code"] == "local_path" and "commit-metadata" in item["path"] for item in report["findings"]))

    def test_binary_ascii_and_utf16_strings_are_scanned(self):
        windows_path = bytes([67, 58, 92]) + b"Users" + bytes([92]) + b"tester" + bytes([92]) + b"secret.txt"
        (self.root / "src" / "payload.py").write_bytes(b"\x00" + windows_path)
        report = scan_public_surface(self.root, self.allowlist)
        self.assertTrue(any(x["code"] == "local_path" for x in report["findings"]))
        local_path = "X:" + "/local/hidden"
        (self.root / "src" / "payload.py").write_bytes(local_path.encode("utf-16-le"))
        report = scan_public_surface(self.root, self.allowlist)
        self.assertTrue(any(x["code"] == "local_path" for x in report["findings"]))

    def test_zip_and_tar_members_are_inspected(self):
        archive = self.root / "candidate.zip"
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("src/a.py", bytes([67, 58, 92]) + b"Users" + bytes([92]) + b"someone" + bytes([92]) + b"private")
            zf.writestr("nested.zip", b"not allowed")
        findings = scan_archive(archive, self.allowlist)
        codes = {x["code"] for x in findings}
        self.assertIn("local_path", codes)
        self.assertIn("nested_archive", codes)

        notice = self.root / "notice.zip"
        with zipfile.ZipFile(notice, "w") as zf:
            zf.writestr("docs/NOTICE.md", b"ArchiveVendor")
            zf.writestr("src/a.py", b"ArchiveVendor")
        disclosure_policy = {**self.allowlist, "disclosure_only_literals": ["ArchiveVendor"]}
        scoped = scan_archive(notice, disclosure_policy)
        self.assertTrue(any(x["code"] == "disclosure_scope" and "src/a.py" in x["path"] for x in scoped))
        self.assertFalse(any(x["code"] == "disclosure_scope" and "docs/NOTICE.md" in x["path"] for x in scoped))

        tar_path = self.root / "candidate.tar"
        with tarfile.open(tar_path, "w") as tf:
            payload = b"secret"
            info = tarfile.TarInfo("src/a.py")
            info.size = len(payload)
            tf.addfile(info, io.BytesIO(payload))
        self.assertIsInstance(scan_archive(tar_path, self.allowlist), list)

    def test_unsupported_archive_fails_closed(self):
        bad = self.root / "candidate.rar"
        bad.write_bytes(b"archive")
        with self.assertRaises(ScanError):
            scan_archive(bad, self.allowlist)


if __name__ == "__main__":
    unittest.main()
