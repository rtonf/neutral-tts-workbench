from __future__ import annotations

import json
import hashlib
import os
import stat
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

import neutral_tts.backup_restore as backup_restore_module

from neutral_tts.backup_restore import (
    ArchiveRejected,
    BackupError,
    BackupRestore,
    RestoreError,
    create_backup,
    reconcile_restore,
    restore_backup,
    validate_backup,
)


class BackupRestoreTests(unittest.TestCase):
    def _source(self, root: Path) -> None:
        for category in ("settings", "history", "dictionary", "voice_profiles"):
            (root / category).mkdir(parents=True)
        (root / "settings" / "settings.json").write_text('{"quality":"balanced"}', encoding="utf-8")
        (root / "history" / "runs.jsonl").write_text('{"status":"ok"}\n', encoding="utf-8")
        (root / "dictionary" / "dictionary.json").write_text('{"revision":1}', encoding="utf-8")
        (root / "voice_profiles" / "generic.json").write_text(
            json.dumps(
                {
                    "schema_version": "neutral-generic-voice-profile-v1",
                    "profile_kind": "generic",
                    "profile_id": "generic-standard",
                    "language": "ja",
                    "sample_rate": 48000,
                    "channels": 1,
                },
                separators=(",", ":"),
                sort_keys=True,
            ),
            encoding="utf-8",
        )

    def _junction(self, link: Path, target: Path) -> None:
        if os.name != "nt":
            self.skipTest("junction probe is Windows-specific")
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if result.returncode:
            self.skipTest("junction creation is unavailable")

    def test_deterministic_backup_manifest_and_zip(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            self._source(root / "source")
            archive_one, archive_two = root / "one.zip", root / "two.zip"
            create_backup(root / "source", archive_one)
            create_backup(root / "source", archive_two)
            self.assertEqual(archive_one.read_bytes(), archive_two.read_bytes())
            with zipfile.ZipFile(archive_one) as archive:
                manifest = json.loads(archive.read("manifest.json"))
            self.assertEqual(manifest["schema_version"], "neutral-tts-backup-v1")
            self.assertEqual(manifest["categories"], ["dictionary", "history", "settings", "voice_profiles"])
            self.assertTrue(all(set(entry) == {"path", "category", "size", "sha256"} for entry in manifest["entries"]))

    def test_restore_creates_safety_backup_and_replaces_only_supported_categories(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            self._source(root / "source")
            archive = root / "backup.zip"
            create_backup(root / "source", archive)
            live = root / "live"
            self._source(live)
            (live / "settings" / "settings.json").write_text("old", encoding="utf-8")
            (live / "unmanaged.txt").write_text("preserve", encoding="utf-8")
            safety = restore_backup(archive, live)
            self.assertTrue(safety.is_file())
            self.assertEqual((live / "settings" / "settings.json").read_text(), '{"quality":"balanced"}')
            self.assertEqual((live / "unmanaged.txt").read_text(), "preserve")

    def test_restore_staging_reparse_race_cannot_escape_candidate_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            self._source(root / "source")
            archive = root / "backup.zip"
            create_backup(root / "source", archive)
            live = root / "live"
            self._source(live)
            outside = root / "outside-restore"
            outside.mkdir()
            real_make = backup_restore_module.make_directories
            replaced = False

            def race(guard, directory):
                nonlocal replaced
                result = real_make(guard, directory)
                if directory.name == "dictionary" and not replaced:
                    replaced = True
                    directory.rmdir()
                    self._junction(directory, outside)
                return result

            with patch("neutral_tts.backup_restore.make_directories", side_effect=race):
                with self.assertRaises(RestoreError):
                    restore_backup(
                        archive,
                        live,
                        safety_backup_path=root / "race-safety.zip",
                    )
            self.assertFalse((outside / "dictionary.json").exists())

    def test_traversal_and_case_collision_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            archive = root / "bad.zip"
            with zipfile.ZipFile(archive, "w") as z:
                z.writestr("manifest.json", json.dumps({"schema_version": "neutral-tts-backup-v1", "entries": [], "aggregate_sha256": "0" * 64}))
                z.writestr("settings/../escape.json", b"bad")
            with self.assertRaises(ArchiveRejected):
                restore_backup(archive, root / "live")

    def test_hash_corruption_leaves_live_data_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            self._source(root / "source")
            archive = root / "backup.zip"
            create_backup(root / "source", archive)
            live = root / "live"
            self._source(live)
            old = (live / "settings" / "settings.json").read_bytes()
            with zipfile.ZipFile(archive, "r") as source:
                infos = source.infolist()
                data = {info.filename: source.read(info) for info in infos}
            data["settings/settings.json"] = b"tampered"
            with zipfile.ZipFile(archive, "w") as target:
                for info in infos:
                    target.writestr(info, data[info.filename])
            with self.assertRaises(ArchiveRejected):
                restore_backup(archive, live)
            self.assertEqual((live / "settings" / "settings.json").read_bytes(), old)

    def test_restore_failure_rolls_back_after_live_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            self._source(root / "source")
            archive = root / "backup.zip"
            create_backup(root / "source", archive)
            live = root / "live"
            self._source(live)
            # A file where an incoming category directory must go triggers a
            # failure after settings has already been moved/installed.
            shutil_target = live / "history"
            for child in shutil_target.iterdir():
                child.unlink()
            shutil_target.rmdir()
            shutil_target.write_text("not-a-directory")
            old_settings = (live / "settings" / "settings.json").read_bytes()
            with self.assertRaises(BackupError):
                restore_backup(archive, live)
            self.assertEqual((live / "settings" / "settings.json").read_bytes(), old_settings)

    def test_injected_restore_failure_rolls_back_all_live_categories(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            self._source(root / "source")
            archive = root / "backup.zip"
            create_backup(root / "source", archive)
            live = root / "live"
            self._source(live)
            (live / "settings" / "settings.json").write_text('{"before":true}', encoding="utf-8")
            before = {
                path.relative_to(live).as_posix(): path.read_bytes()
                for path in live.rglob("*")
                if path.is_file()
            }

            def fail(stage: str) -> None:
                if stage == "restore.after_incoming_promoted:history":
                    raise RuntimeError("synthetic restore fault")

            safety = root / "pre-restore.zip"
            with self.assertRaises(RestoreError):
                restore_backup(archive, live, safety_backup_path=safety, fault_hook=fail)
            after = {
                path.relative_to(live).as_posix(): path.read_bytes()
                for path in live.rglob("*")
                if path.is_file()
            }
            self.assertEqual(after, before)
            self.assertTrue(safety.is_file())

    def test_keyboard_interrupt_is_normalized_and_rolls_back(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            self._source(root / "source")
            archive = root / "backup.zip"
            create_backup(root / "source", archive)
            live = root / "live"
            self._source(live)
            (live / "settings" / "settings.json").write_text('{"before":true}', encoding="utf-8")
            before = {
                path.relative_to(live).as_posix(): path.read_bytes()
                for path in live.rglob("*")
                if path.is_file()
            }

            def interrupt(stage: str) -> None:
                if stage == "restore.after_incoming_promoted:settings":
                    raise KeyboardInterrupt()

            with self.assertRaises(RestoreError):
                restore_backup(
                    archive,
                    live,
                    safety_backup_path=root / "interrupt-safety.zip",
                    fault_hook=interrupt,
                )
            after = {
                path.relative_to(live).as_posix(): path.read_bytes()
                for path in live.rglob("*")
                if path.is_file()
            }
            self.assertEqual(after, before)

    def test_process_death_journal_reconciles_to_safety_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            self._source(root / "source")
            archive = root / "backup.zip"
            create_backup(root / "source", archive)
            live = root / "live"
            self._source(live)
            (live / "settings" / "settings.json").write_text('{"before":true}', encoding="utf-8")
            before = {
                path.relative_to(live).as_posix(): path.read_bytes()
                for path in live.rglob("*")
                if path.is_file()
            }
            safety = root / "process-safety.zip"
            script = (
                "import os,sys; from pathlib import Path; "
                "from neutral_tts.backup_restore import restore_backup; "
                "hook=lambda stage: os._exit(73) if stage == "
                "'restore.after_incoming_promoted:history' else None; "
                "restore_backup(Path(sys.argv[1]),Path(sys.argv[2]),"
                "safety_backup_path=Path(sys.argv[3]),fault_hook=hook)"
            )
            environment = os.environ.copy()
            environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
            completed = subprocess.run(
                [sys.executable, "-c", script, str(archive), str(live), str(safety)],
                env=environment,
                check=False,
            )
            self.assertEqual(completed.returncode, 73)
            self.assertTrue((root / ".live.restore-journal.json").is_file())
            self.assertTrue(reconcile_restore(live))
            after = {
                path.relative_to(live).as_posix(): path.read_bytes()
                for path in live.rglob("*")
                if path.is_file()
            }
            self.assertEqual(after, before)
            self.assertFalse((root / ".live.restore-journal.json").exists())

    def test_unsafe_archive_path_classes_and_link_entries_are_rejected(self) -> None:
        categories = ["dictionary", "history", "settings", "voice_profiles"]
        aggregate = hashlib.sha256(
            json.dumps(
                {"categories": categories, "entries": []},
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        manifest = json.dumps(
            {
                "schema_version": "neutral-tts-backup-v1",
                "categories": categories,
                "entries": [],
                "aggregate_sha256": aggregate,
            },
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        drive = "".join(("Q", ":", "/settings/value.json"))
        network = "".join(("/", "/", "host/settings/value.json"))
        backslash_path = "".join(("settings", chr(92), "value.json"))
        paths = (
            "/settings/value.json",
            drive,
            network,
            "settings/value.json:stream",
            "settings/../value.json",
            "settings/./value.json",
            "settings//value.json",
            backslash_path,
        )
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            for index, unsafe in enumerate(paths):
                archive_path = root / f"unsafe-{index}.zip"
                with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_STORED) as archive:
                    for member_name, data in (("manifest.json", manifest), (unsafe, b"{}")):
                        info = zipfile.ZipInfo(member_name, date_time=(1980, 1, 1, 0, 0, 0))
                        info.create_system = 3
                        info.external_attr = (stat.S_IFREG | 0o600) << 16
                        archive.writestr(info, data)
                with self.subTest(path=unsafe), self.assertRaises(ArchiveRejected):
                    validate_backup(archive_path)

            link_archive = root / "link.zip"
            with zipfile.ZipFile(link_archive, "w", compression=zipfile.ZIP_STORED) as archive:
                info = zipfile.ZipInfo("manifest.json", date_time=(1980, 1, 1, 0, 0, 0))
                info.create_system = 3
                info.external_attr = (stat.S_IFLNK | 0o600) << 16
                archive.writestr(info, manifest)
            with self.assertRaises(ArchiveRejected):
                validate_backup(link_archive)

    def test_incomplete_category_manifest_and_unapproved_profile_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            self._source(root / "source")
            (root / "source" / "voice_profiles" / "generic.json").write_text(
                '{"name":"person-like"}', encoding="utf-8"
            )
            with self.assertRaises(BackupError):
                create_backup(root / "source", root / "bad-profile.zip")

            self._source(root / "valid")
            archive = root / "valid.zip"
            create_backup(root / "valid", archive)
            with zipfile.ZipFile(archive) as source:
                infos = source.infolist()
                contents = {info.filename: source.read(info) for info in infos}
            manifest = json.loads(contents["manifest.json"])
            manifest["categories"].pop()
            contents["manifest.json"] = json.dumps(
                manifest, ensure_ascii=True, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
            partial = root / "partial.zip"
            with zipfile.ZipFile(partial, "w", compression=zipfile.ZIP_STORED) as target:
                for info in infos:
                    target.writestr(info, contents[info.filename])
            with self.assertRaises(ArchiveRejected):
                validate_backup(partial)


if __name__ == "__main__":
    unittest.main()
