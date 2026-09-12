from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
import zipfile
from contextlib import redirect_stdout
from pathlib import Path

from neutral_tts.backup_restore import create_backup
from neutral_tts.backup_restore_cli import HOST_SCHEMA, main


class BackupRestoreCliTests(unittest.TestCase):
    @staticmethod
    def _source(root: Path, *, marker: str = "source") -> None:
        for category in ("settings", "history", "dictionary", "voice_profiles"):
            (root / category).mkdir(parents=True)
        (root / "settings" / "settings.json").write_text(
            json.dumps({"marker": marker}, sort_keys=True), encoding="utf-8"
        )
        (root / "history" / "runs.jsonl").write_text(
            json.dumps({"run": marker}, sort_keys=True) + "\n", encoding="utf-8"
        )
        (root / "dictionary" / "dictionary.json").write_text(
            json.dumps({"revision": marker}, sort_keys=True), encoding="utf-8"
        )
        (root / "voice_profiles" / "generic.json").write_text(
            json.dumps(
                {
                    "channels": 1,
                    "language": "ja",
                    "profile_id": "generic-standard",
                    "profile_kind": "generic",
                    "sample_rate": 48000,
                    "schema_version": "neutral-generic-voice-profile-v1",
                },
                separators=(",", ":"),
                sort_keys=True,
            ),
            encoding="utf-8",
        )

    @staticmethod
    def _supported_snapshot(root: Path) -> tuple[tuple[str, bytes], ...]:
        return tuple(
            sorted(
                (path.relative_to(root).as_posix(), path.read_bytes())
                for category in ("settings", "history", "dictionary", "voice_profiles")
                for path in (root / category).rglob("*")
                if path.is_file()
            )
        )

    @staticmethod
    def _whole_snapshot(root: Path) -> tuple[tuple[str, bytes], ...]:
        return tuple(
            sorted(
                (path.relative_to(root).as_posix(), path.read_bytes())
                for path in root.rglob("*")
                if path.is_file()
            )
        )

    @staticmethod
    def _invoke(
        args: list[str], *, capacity_probe=None
    ) -> tuple[int, dict[str, object], str]:
        output = io.StringIO()
        with redirect_stdout(output):
            code = main(args, capacity_probe=capacity_probe)
        raw = output.getvalue()
        return code, json.loads(raw), raw

    def test_explicit_backup_then_restore_to_empty_root_matches_supported_data(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            source = root / "synthetic-source"
            target = root / "empty-target"
            source.mkdir()
            target.mkdir()
            self._source(source)
            (source / "unmanaged.bin").write_bytes(b"not selected")
            archive = root / "bounded-backup.zip"
            safety = root / "empty-target-safety.zip"

            backup_code, backup_payload, backup_raw = self._invoke(
                ["backup", "--data-root", str(source), "--archive", str(archive)]
            )
            restore_code, restore_payload, restore_raw = self._invoke(
                [
                    "restore",
                    "--archive",
                    str(archive),
                    "--data-root",
                    str(target),
                    "--safety-backup",
                    str(safety),
                ],
                capacity_probe=lambda _directory: 1 << 40,
            )

            self.assertEqual((backup_code, restore_code), (0, 0))
            self.assertEqual(backup_payload["schema_version"], HOST_SCHEMA)
            self.assertEqual(backup_payload["result"]["terminal_state"], "COMPLETED")
            self.assertEqual(restore_payload["result"]["terminal_state"], "COMPLETED")
            self.assertEqual(
                restore_payload["result"]["restored"],
                backup_payload["result"]["archive"],
            )
            self.assertEqual(
                self._supported_snapshot(target), self._supported_snapshot(source)
            )
            self.assertFalse((target / "unmanaged.bin").exists())
            self.assertTrue(safety.is_file())
            self.assertNotIn(str(root), backup_raw)
            self.assertNotIn(str(root), restore_raw)

    def test_recover_command_reconciles_an_interrupted_existing_journal(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            source = root / "synthetic-source"
            live = root / "synthetic-live"
            source.mkdir()
            live.mkdir()
            self._source(source, marker="incoming")
            self._source(live, marker="original")
            before = self._whole_snapshot(live)
            archive = root / "bounded-backup.zip"
            safety = root / "interrupted-safety.zip"
            create_backup(source, archive)
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
            interrupted = subprocess.run(
                [sys.executable, "-c", script, str(archive), str(live), str(safety)],
                env=environment,
                check=False,
            )
            self.assertEqual(interrupted.returncode, 73)
            self.assertTrue((root / ".synthetic-live.restore-journal.json").is_file())

            code, payload, _raw = self._invoke(
                ["recover", "--data-root", str(live)]
            )
            self.assertEqual(code, 0)
            self.assertEqual(payload["result"]["terminal_state"], "RECOVERED")
            self.assertTrue(payload["result"]["reconciled"])
            self.assertEqual(self._whole_snapshot(live), before)
            self.assertFalse((root / ".synthetic-live.restore-journal.json").exists())

            code, payload, _raw = self._invoke(
                ["recover", "--data-root", str(live)]
            )
            self.assertEqual(code, 0)
            self.assertEqual(
                payload["result"]["terminal_state"], "NO_PENDING_RESTORE"
            )
            self.assertFalse(payload["result"]["reconciled"])

    def test_tampered_or_missing_archive_is_rejected_before_target_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            source = root / "synthetic-source"
            source.mkdir()
            self._source(source)
            archive = root / "bounded-backup.zip"
            create_backup(source, archive)
            with zipfile.ZipFile(archive, "r") as opened:
                infos = opened.infolist()
                contents = {info.filename: opened.read(info) for info in infos}
            contents["settings/settings.json"] = b'{"tampered":true}'
            with zipfile.ZipFile(archive, "w") as replaced:
                for info in infos:
                    replaced.writestr(info, contents[info.filename])

            for label, candidate in (
                ("tampered", archive),
                ("missing", root / "missing.zip"),
            ):
                with self.subTest(label=label):
                    target = root / f"{label}-target"
                    target.mkdir()
                    marker = target / "unmanaged.txt"
                    marker.write_bytes(b"unchanged")
                    safety = root / f"{label}-safety.zip"
                    before = self._whole_snapshot(target)
                    code, payload, raw = self._invoke(
                        [
                            "restore",
                            "--archive",
                            str(candidate),
                            "--data-root",
                            str(target),
                            "--safety-backup",
                            str(safety),
                        ],
                        capacity_probe=lambda _directory: 1 << 40,
                    )
                    self.assertEqual(code, 2)
                    self.assertEqual(payload["status"], "ERROR")
                    self.assertEqual(
                        payload["error"]["code"], "BACKUP_RESTORE_REJECTED"
                    )
                    self.assertEqual(self._whole_snapshot(target), before)
                    self.assertFalse(safety.exists())
                    self.assertNotIn(str(root), raw)

    def test_insufficient_capacity_is_rejected_before_restore_side_effects(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            source = root / "synthetic-source"
            target = root / "empty-target"
            source.mkdir()
            target.mkdir()
            self._source(source)
            archive = root / "bounded-backup.zip"
            safety = root / "capacity-safety.zip"
            create_backup(source, archive)

            code, payload, _raw = self._invoke(
                [
                    "restore",
                    "--archive",
                    str(archive),
                    "--data-root",
                    str(target),
                    "--safety-backup",
                    str(safety),
                ],
                capacity_probe=lambda _directory: 0,
            )

            self.assertEqual(code, 2)
            self.assertEqual(payload["status"], "ERROR")
            self.assertEqual(self._whole_snapshot(target), ())
            self.assertFalse(safety.exists())
            self.assertFalse((root / ".empty-target.restore-journal.json").exists())

    def test_target_link_replacement_attempt_is_rejected_without_external_write(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            source = root / "synthetic-source"
            target = root / "empty-target"
            external = root / "external-target"
            displaced = root / "displaced-target"
            source.mkdir()
            target.mkdir()
            external.mkdir()
            marker = external / "marker.txt"
            marker.write_bytes(b"external-unchanged")
            self._source(source)
            archive = root / "bounded-backup.zip"
            safety = root / "replacement-safety.zip"
            create_backup(source, archive)

            def replace_during_preflight(_directory: Path) -> int:
                target.rename(displaced)
                target.symlink_to(external, target_is_directory=True)
                return 1 << 40

            try:
                code, payload, _raw = self._invoke(
                    [
                        "restore",
                        "--archive",
                        str(archive),
                        "--data-root",
                        str(target),
                        "--safety-backup",
                        str(safety),
                    ],
                    capacity_probe=replace_during_preflight,
                )
                self.assertEqual(code, 2)
                self.assertEqual(payload["status"], "ERROR")
                self.assertEqual(marker.read_bytes(), b"external-unchanged")
                self.assertFalse(safety.exists())
                self.assertFalse((root / ".empty-target.restore-journal.json").exists())
            finally:
                if target.is_symlink():
                    target.unlink()
                if displaced.exists() and not target.exists():
                    displaced.rename(target)

    def test_valid_archive_swap_after_admission_is_rejected_before_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            source = root / "synthetic-source"
            replacement_source = root / "replacement-source"
            target = root / "empty-target"
            source.mkdir()
            replacement_source.mkdir()
            target.mkdir()
            self._source(source, marker="admitted")
            self._source(replacement_source, marker="replacement")
            archive = root / "bounded-backup.zip"
            replacement_archive = root / "replacement-backup.zip"
            safety = root / "swap-safety.zip"
            create_backup(source, archive)
            create_backup(replacement_source, replacement_archive)

            def replace_after_admission(_directory: Path) -> int:
                archive.unlink()
                replacement_archive.rename(archive)
                return 1 << 40

            code, payload, _raw = self._invoke(
                [
                    "restore",
                    "--archive",
                    str(archive),
                    "--data-root",
                    str(target),
                    "--safety-backup",
                    str(safety),
                ],
                capacity_probe=replace_after_admission,
            )

            self.assertEqual(code, 2)
            self.assertEqual(payload["status"], "ERROR")
            self.assertEqual(self._whole_snapshot(target), ())
            self.assertFalse(safety.exists())
            self.assertFalse((root / ".empty-target.restore-journal.json").exists())

    def test_existing_archive_or_safety_output_collision_is_no_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            source = root / "synthetic-source"
            target = root / "synthetic-target"
            source.mkdir()
            target.mkdir()
            self._source(source)
            self._source(target, marker="original")
            archive = root / "bounded-backup.zip"
            create_backup(source, archive)
            archive_before = archive.read_bytes()

            code, payload, _raw = self._invoke(
                ["backup", "--data-root", str(source), "--archive", str(archive)]
            )
            self.assertEqual(code, 2)
            self.assertEqual(payload["status"], "ERROR")
            self.assertEqual(archive.read_bytes(), archive_before)

            safety = root / "existing-safety.zip"
            safety.write_bytes(b"owned-output")
            target_before = self._whole_snapshot(target)
            code, payload, _raw = self._invoke(
                [
                    "restore",
                    "--archive",
                    str(archive),
                    "--data-root",
                    str(target),
                    "--safety-backup",
                    str(safety),
                ],
                capacity_probe=lambda _directory: 1 << 40,
            )
            self.assertEqual(code, 2)
            self.assertEqual(payload["status"], "ERROR")
            self.assertEqual(safety.read_bytes(), b"owned-output")
            self.assertEqual(self._whole_snapshot(target), target_before)
            self.assertFalse((root / ".synthetic-target.restore-journal.json").exists())


if __name__ == "__main__":
    unittest.main()
