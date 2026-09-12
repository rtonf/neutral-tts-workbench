from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
import zipfile
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

import neutral_tts.backup_restore as backup_restore_module
import neutral_tts.backup_restore_cli as backup_restore_cli_module
from neutral_tts.backup_restore import (
    RESTORE_CAPACITY_RESERVE_BYTES,
    RestoreError,
    create_backup,
    validate_backup,
)
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

    @staticmethod
    def _subprocess_environment() -> dict[str, str]:
        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(
            Path(__file__).resolve().parents[1] / "src"
        )
        return environment

    def _wait_for_file(
        self, marker: Path, process: subprocess.Popen[str], *, timeout: float = 10.0
    ) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if marker.is_file():
                return
            if process.poll() is not None:
                stdout, stderr = process.communicate()
                self.fail(
                    f"child exited before marker: {process.returncode}; "
                    f"stdout={stdout!r}; stderr={stderr!r}"
                )
            time.sleep(0.02)
        self.fail("child did not reach the requested restore fault stage")

    def _crash_restore(
        self,
        archive: Path,
        live: Path,
        safety: Path,
        stage: str,
        *,
        exit_code: int = 73,
    ) -> subprocess.CompletedProcess[str]:
        script = (
            "import os,sys\n"
            "from pathlib import Path\n"
            "from neutral_tts.backup_restore import restore_backup\n"
            "stage_to_stop=sys.argv[4]\n"
            "exit_code=int(sys.argv[5])\n"
            "def hook(stage):\n"
            "    if stage == stage_to_stop:\n"
            "        os._exit(exit_code)\n"
            "restore_backup(Path(sys.argv[1]),Path(sys.argv[2]),"
            "safety_backup_path=Path(sys.argv[3]),fault_hook=hook)\n"
        )
        return subprocess.run(
            [
                sys.executable,
                "-c",
                script,
                str(archive),
                str(live),
                str(safety),
                stage,
                str(exit_code),
            ],
            env=self._subprocess_environment(),
            check=False,
            capture_output=True,
            text=True,
        )

    def _crash_reconcile(
        self, live: Path, stage: str, *, exit_code: int = 74
    ) -> subprocess.CompletedProcess[str]:
        script = (
            "import os,sys\n"
            "from pathlib import Path\n"
            "from neutral_tts.backup_restore import reconcile_restore\n"
            "stage_to_stop=sys.argv[2]\n"
            "exit_code=int(sys.argv[3])\n"
            "def hook(stage):\n"
            "    if stage == stage_to_stop:\n"
            "        os._exit(exit_code)\n"
            "reconcile_restore(Path(sys.argv[1]),fault_hook=hook)\n"
        )
        return subprocess.run(
            [sys.executable, "-c", script, str(live), stage, str(exit_code)],
            env=self._subprocess_environment(),
            check=False,
            capture_output=True,
            text=True,
        )

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
            for key in (
                "aggregate_sha256",
                "categories",
                "entry_count",
                "expanded_bytes",
                "schema_version",
            ):
                self.assertEqual(
                    restore_payload["result"]["restored"][key],
                    backup_payload["result"]["archive"][key],
                )
            self.assertEqual(
                self._supported_snapshot(target), self._supported_snapshot(source)
            )
            self.assertFalse((target / "unmanaged.bin").exists())
            self.assertTrue(safety.is_file())
            self.assertEqual(
                backup_payload["result"]["archive"]["archive_sha256"],
                hashlib.sha256(archive.read_bytes()).hexdigest(),
            )
            self.assertEqual(
                backup_payload["result"]["archive"]["archive_size"],
                archive.stat().st_size,
            )
            self.assertEqual(
                restore_payload["result"]["safety_backup"]["archive_sha256"],
                hashlib.sha256(safety.read_bytes()).hexdigest(),
            )
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
            self.assertEqual(payload["result"]["terminal_state"], "ROLLED_BACK")
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

            real_restore = backup_restore_cli_module.restore_backup_result

            def replace_after_admission(*args, **kwargs):
                archive.unlink()
                replacement_archive.rename(archive)
                return real_restore(*args, **kwargs)

            with patch.object(
                backup_restore_cli_module,
                "restore_backup_result",
                side_effect=replace_after_admission,
            ):
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
            self.assertEqual(self._whole_snapshot(target), ())
            self.assertFalse(safety.exists())
            self.assertFalse((root / ".empty-target.restore-journal.json").exists())

    def test_same_root_restore_and_recover_are_busy_while_real_process_owns_root(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            source = root / "synthetic-source"
            live = root / "synthetic-live"
            source.mkdir()
            live.mkdir()
            self._source(source, marker="incoming")
            self._source(live, marker="original")
            archive = root / "incoming.zip"
            safety = root / "active-safety.zip"
            second_safety = root / "second-safety.zip"
            marker = root / "owner-held.marker"
            release = root / "release.marker"
            create_backup(source, archive)
            script = (
                "import sys,time\n"
                "from pathlib import Path\n"
                "from neutral_tts.backup_restore import restore_backup\n"
                "marker=Path(sys.argv[4])\n"
                "release=Path(sys.argv[5])\n"
                "def hook(stage):\n"
                "    if stage == 'restore.after_incoming_promoted:history':\n"
                "        marker.write_text('held',encoding='utf-8')\n"
                "        while not release.exists():\n"
                "            time.sleep(0.01)\n"
                "restore_backup(Path(sys.argv[1]),Path(sys.argv[2]),"
                "safety_backup_path=Path(sys.argv[3]),fault_hook=hook)\n"
            )
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    script,
                    str(archive),
                    str(live),
                    str(safety),
                    str(marker),
                    str(release),
                ],
                env=self._subprocess_environment(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            stdout = ""
            stderr = ""
            try:
                self._wait_for_file(marker, process)
                self.assertTrue(
                    (root / ".synthetic-live.restore-journal.json").is_file()
                )
                for command in (
                    ["recover", "--data-root", str(live)],
                    [
                        "restore",
                        "--archive",
                        str(archive),
                        "--data-root",
                        str(live),
                        "--safety-backup",
                        str(second_safety),
                    ],
                ):
                    with self.subTest(command=command[0]):
                        code, payload, raw = self._invoke(
                            command, capacity_probe=lambda _directory: 1 << 40
                        )
                        self.assertEqual(code, 2)
                        self.assertEqual(payload["status"], "ERROR")
                        self.assertEqual(
                            payload["error"]["code"], "BACKUP_RESTORE_BUSY"
                        )
                        self.assertNotIn(str(root), raw)
                self.assertFalse(second_safety.exists())
            finally:
                release.write_text("continue", encoding="utf-8")
                try:
                    stdout, stderr = process.communicate(timeout=20)
                except subprocess.TimeoutExpired:
                    process.kill()
                    stdout, stderr = process.communicate()
            self.assertEqual(
                process.returncode,
                0,
                msg=f"stdout={stdout!r}; stderr={stderr!r}",
            )
            self.assertEqual(
                self._supported_snapshot(live), self._supported_snapshot(source)
            )
            self.assertFalse(
                (root / ".synthetic-live.restore-journal.json").exists()
            )
            code, payload, _raw = self._invoke(
                ["recover", "--data-root", str(live)]
            )
            self.assertEqual(code, 0)
            self.assertEqual(
                payload["result"]["terminal_state"], "NO_PENDING_RESTORE"
            )

    def test_completed_journal_survives_process_death_and_recovers_as_committed(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            source = root / "synthetic-source"
            live = root / "synthetic-live"
            source.mkdir()
            live.mkdir()
            self._source(source, marker="incoming")
            self._source(live, marker="original")
            archive = root / "incoming.zip"
            safety = root / "commit-safety.zip"
            create_backup(source, archive)

            crashed = self._crash_restore(
                archive,
                live,
                safety,
                "restore.after_completed_journal",
            )
            self.assertEqual(
                crashed.returncode,
                73,
                msg=f"stdout={crashed.stdout!r}; stderr={crashed.stderr!r}",
            )
            journal = root / ".synthetic-live.restore-journal.json"
            self.assertTrue(journal.is_file())
            self.assertEqual(
                self._supported_snapshot(live), self._supported_snapshot(source)
            )

            code, payload, _raw = self._invoke(
                ["recover", "--data-root", str(live)]
            )
            self.assertEqual(code, 0)
            self.assertEqual(payload["status"], "SUCCEEDED")
            self.assertEqual(payload["result"]["terminal_state"], "COMPLETED")
            self.assertTrue(payload["result"]["reconciled"])
            self.assertEqual(
                self._supported_snapshot(live), self._supported_snapshot(source)
            )
            self.assertFalse(journal.exists())

            code, payload, _raw = self._invoke(
                ["recover", "--data-root", str(live)]
            )
            self.assertEqual(code, 0)
            self.assertEqual(
                payload["result"]["terminal_state"], "NO_PENDING_RESTORE"
            )

    def test_reconcile_rename_crashes_are_idempotently_recovered(self) -> None:
        stages = tuple(
            f"reconcile.{operation}:{category}"
            for operation in ("after_new_reverted", "after_old_restored")
            for category in ("dictionary", "history", "settings", "voice_profiles")
        )
        for reconcile_stage in stages:
            with self.subTest(stage=reconcile_stage), tempfile.TemporaryDirectory() as name:
                root = Path(name)
                source = root / "synthetic-source"
                live = root / "synthetic-live"
                source.mkdir()
                live.mkdir()
                self._source(source, marker="incoming")
                self._source(live, marker="original")
                before = self._supported_snapshot(live)
                archive = root / "incoming.zip"
                safety = root / "reconcile-safety.zip"
                create_backup(source, archive)
                interrupted = self._crash_restore(
                    archive,
                    live,
                    safety,
                    "restore.after_incoming_promoted:voice_profiles",
                )
                self.assertEqual(
                    interrupted.returncode,
                    73,
                    msg=(
                        f"stdout={interrupted.stdout!r}; "
                        f"stderr={interrupted.stderr!r}"
                    ),
                )
                journal = root / ".synthetic-live.restore-journal.json"
                self.assertTrue(journal.is_file())

                crashed = self._crash_reconcile(live, reconcile_stage)
                self.assertEqual(
                    crashed.returncode,
                    74,
                    msg=f"stdout={crashed.stdout!r}; stderr={crashed.stderr!r}",
                )
                self.assertTrue(journal.is_file())

                code, payload, _raw = self._invoke(
                    ["recover", "--data-root", str(live)]
                )
                self.assertEqual(code, 0)
                self.assertEqual(payload["status"], "SUCCEEDED")
                self.assertEqual(
                    payload["result"]["terminal_state"], "ROLLED_BACK"
                )
                self.assertEqual(self._supported_snapshot(live), before)
                self.assertFalse(journal.exists())

                code, payload, _raw = self._invoke(
                    ["recover", "--data-root", str(live)]
                )
                self.assertEqual(code, 0)
                self.assertEqual(
                    payload["result"]["terminal_state"], "NO_PENDING_RESTORE"
                )

    def test_restore_death_between_rename_and_journal_preserves_original_tree(self) -> None:
        # Kill a real child immediately after the filesystem rename, before
        # the normal after-stage hook or journal update can run.
        script = (
            "import os,sys\n"
            "from pathlib import Path\n"
            "from neutral_tts.backup_restore import restore_backup\n"
            "live=Path(sys.argv[2]); category=sys.argv[4]; direction=sys.argv[5]\n"
            "rename=os.rename\n"
            "def interrupted_rename(source, target, *args, **kwargs):\n"
            "    result=rename(source,target,*args,**kwargs)\n"
            "    selected=Path(source if direction == 'out' else target)\n"
            "    if selected == live/category:\n"
            "        os._exit(75)\n"
            "    return result\n"
            "os.rename=interrupted_rename\n"
            "restore_backup(Path(sys.argv[1]),live,safety_backup_path=Path(sys.argv[3]))\n"
        )
        for category in ("dictionary", "history", "settings", "voice_profiles"):
            for direction, populated in (("out", True), ("in", True), ("in", False)):
                with self.subTest(category=category, direction=direction, populated=populated):
                    with tempfile.TemporaryDirectory() as name:
                        root = Path(name)
                        source, live = root / "source", root / "live"
                        source.mkdir()
                        live.mkdir()
                        self._source(source, marker="incoming")
                        if populated:
                            self._source(live, marker="original")
                        (live / "unmanaged.txt").write_text("preserve", encoding="utf-8")
                        before = self._whole_snapshot(live)
                        directories = sorted(p.name for p in live.iterdir() if p.is_dir())
                        archive, safety = root / "incoming.zip", root / "safety.zip"
                        create_backup(source, archive)
                        child = subprocess.run(
                            [sys.executable, "-c", script, str(archive), str(live),
                             str(safety), category, direction],
                            env=self._subprocess_environment(), capture_output=True,
                            text=True, check=False, timeout=20,
                        )
                        self.assertEqual(child.returncode, 75, child.stderr)
                        code, payload, _ = self._invoke(["recover", "--data-root", str(live)])
                        self.assertEqual(code, 0)
                        self.assertEqual(payload["result"]["terminal_state"], "ROLLED_BACK")
                        self.assertEqual(self._whole_snapshot(live), before)
                        self.assertEqual(
                            sorted(p.name for p in live.iterdir() if p.is_dir()), directories
                        )
                        code, payload, _ = self._invoke(["recover", "--data-root", str(live)])
                        self.assertEqual(code, 0)
                        self.assertEqual(payload["result"]["terminal_state"], "NO_PENDING_RESTORE")

    def test_backup_publication_swap_is_rejected_without_deleting_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            source = root / "synthetic-source"
            replacement_source = root / "replacement-source"
            source.mkdir()
            replacement_source.mkdir()
            self._source(source, marker="intended")
            self._source(replacement_source, marker="replacement")
            replacement_archive = root / "replacement.zip"
            output = root / "published.zip"
            create_backup(replacement_source, replacement_archive)
            replacement_manifest = validate_backup(replacement_archive)
            replacement_bytes = replacement_archive.read_bytes()
            real_published_artifact = backup_restore_module._published_artifact

            def swap_after_link(archive_path, manifest):
                archive_path.unlink()
                replacement_archive.rename(archive_path)
                return real_published_artifact(archive_path, manifest)

            with patch.object(
                backup_restore_module,
                "_published_artifact",
                side_effect=swap_after_link,
            ):
                code, payload, raw = self._invoke(
                    [
                        "backup",
                        "--data-root",
                        str(source),
                        "--archive",
                        str(output),
                    ]
                )

            self.assertEqual(code, 2)
            self.assertEqual(payload["status"], "ERROR")
            self.assertEqual(
                payload["error"]["code"], "BACKUP_RESTORE_REJECTED"
            )
            self.assertNotIn(str(root), raw)
            self.assertTrue(output.is_file())
            self.assertEqual(output.read_bytes(), replacement_bytes)
            self.assertEqual(validate_backup(output), replacement_manifest)

    def test_completed_cleanup_failure_is_incomplete_then_recoverable(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            source = root / "synthetic-source"
            live = root / "synthetic-live"
            source.mkdir()
            live.mkdir()
            self._source(source, marker="incoming")
            self._source(live, marker="original")
            archive = root / "incoming.zip"
            safety = root / "cleanup-safety.zip"
            create_backup(source, archive)

            with patch.object(
                backup_restore_module.shutil,
                "rmtree",
                side_effect=OSError("synthetic cleanup refusal"),
            ):
                code, payload, raw = self._invoke(
                    [
                        "restore",
                        "--archive",
                        str(archive),
                        "--data-root",
                        str(live),
                        "--safety-backup",
                        str(safety),
                    ],
                    capacity_probe=lambda _directory: 1 << 40,
                )

            journal = root / ".synthetic-live.restore-journal.json"
            self.assertEqual(code, 3)
            self.assertEqual(payload["status"], "INCOMPLETE")
            self.assertEqual(
                payload["result"]["terminal_state"],
                "COMPLETED_CLEANUP_PENDING",
            )
            self.assertNotIn(str(root), raw)
            self.assertEqual(
                self._supported_snapshot(live), self._supported_snapshot(source)
            )
            self.assertTrue(safety.is_file())
            self.assertTrue(journal.is_file())

            code, payload, _raw = self._invoke(
                ["recover", "--data-root", str(live)]
            )
            self.assertEqual(code, 0)
            self.assertEqual(payload["status"], "SUCCEEDED")
            self.assertEqual(payload["result"]["terminal_state"], "COMPLETED")
            self.assertEqual(
                self._supported_snapshot(live), self._supported_snapshot(source)
            )
            self.assertFalse(journal.exists())

    def test_unknown_terminal_integrity_is_not_reported_as_success(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            source = root / "synthetic-source"
            live = root / "synthetic-live"
            source.mkdir()
            live.mkdir()
            self._source(source, marker="incoming")
            self._source(live, marker="original")
            archive = root / "incoming.zip"
            safety = root / "integrity-safety.zip"
            create_backup(source, archive)
            real_verify = backup_restore_module._verify_live_matches_manifest
            calls = 0

            def fail_terminal_verification(*args, **kwargs):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise RestoreError("synthetic terminal verification failure")
                return real_verify(*args, **kwargs)

            with patch.object(
                backup_restore_module,
                "_verify_live_matches_manifest",
                side_effect=fail_terminal_verification,
            ):
                code, payload, raw = self._invoke(
                    [
                        "restore",
                        "--archive",
                        str(archive),
                        "--data-root",
                        str(live),
                        "--safety-backup",
                        str(safety),
                    ],
                    capacity_probe=lambda _directory: 1 << 40,
                )

            journal = root / ".synthetic-live.restore-journal.json"
            self.assertEqual(code, 3)
            self.assertEqual(payload["status"], "INCOMPLETE")
            self.assertEqual(
                payload["result"]["terminal_state"],
                "COMPLETED_INTEGRITY_UNVERIFIED",
            )
            self.assertNotIn(str(root), raw)
            self.assertTrue(journal.is_file())
            self.assertEqual(
                self._supported_snapshot(live), self._supported_snapshot(source)
            )

            code, payload, _raw = self._invoke(
                ["recover", "--data-root", str(live)]
            )
            self.assertEqual(code, 0)
            self.assertEqual(payload["result"]["terminal_state"], "COMPLETED")
            self.assertFalse(journal.exists())

    def test_capacity_is_checked_for_separate_workspace_and_safety_volumes(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            source = root / "synthetic-source"
            live = root / "synthetic-live"
            safety_directory = root / "synthetic-safety-volume"
            source.mkdir()
            live.mkdir()
            safety_directory.mkdir()
            self._source(source, marker="incoming")
            self._source(live, marker="original")
            before = self._supported_snapshot(live)
            archive = root / "incoming.zip"
            safety = safety_directory / "capacity-safety.zip"
            create_backup(source, archive)
            probed: list[Path] = []

            def device(directory: Path) -> int:
                if directory == live.parent:
                    return 101
                if directory == safety_directory:
                    return 202
                raise AssertionError(f"unexpected capacity directory: {directory}")

            def capacity(directory: Path) -> int:
                probed.append(directory)
                if directory == live.parent:
                    return 1 << 40
                if directory == safety_directory:
                    return RESTORE_CAPACITY_RESERVE_BYTES - 1
                raise AssertionError(f"unexpected capacity probe: {directory}")

            with patch.object(
                backup_restore_module, "_capacity_device", side_effect=device
            ):
                code, payload, _raw = self._invoke(
                    [
                        "restore",
                        "--archive",
                        str(archive),
                        "--data-root",
                        str(live),
                        "--safety-backup",
                        str(safety),
                    ],
                    capacity_probe=capacity,
                )

            self.assertEqual(code, 2)
            self.assertEqual(payload["status"], "ERROR")
            self.assertEqual(probed, [live.parent, safety_directory])
            self.assertEqual(self._supported_snapshot(live), before)
            self.assertFalse(safety.exists())
            self.assertFalse(
                (root / ".synthetic-live.restore-journal.json").exists()
            )

    def test_same_volume_capacity_includes_incoming_and_safety_snapshots(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            source = root / "synthetic-source"
            live = root / "synthetic-live"
            source.mkdir()
            live.mkdir()
            self._source(source, marker="incoming")
            self._source(live, marker="original")
            before = self._supported_snapshot(live)
            archive = root / "incoming.zip"
            safety = root / "capacity-safety.zip"
            create_backup(source, archive)
            incoming_bytes = sum(
                entry.size for entry in validate_backup(archive).entries
            )
            observed: list[Path] = []

            def capacity(directory: Path) -> int:
                observed.append(directory)
                return incoming_bytes + RESTORE_CAPACITY_RESERVE_BYTES

            code, payload, _raw = self._invoke(
                [
                    "restore",
                    "--archive",
                    str(archive),
                    "--data-root",
                    str(live),
                    "--safety-backup",
                    str(safety),
                ],
                capacity_probe=capacity,
            )

            self.assertEqual(code, 2)
            self.assertEqual(payload["status"], "ERROR")
            self.assertEqual(observed, [live.parent])
            self.assertEqual(self._supported_snapshot(live), before)
            self.assertFalse(safety.exists())

    def test_malformed_cli_never_echoes_private_arguments_to_stdout_or_stderr(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            private_value = str(Path(name) / "private-user-tree" / "secret.zip")
            cases = (
                ["restore", "--archive", private_value],
                [private_value],
            )
            for arguments in cases:
                with self.subTest(arguments=arguments[0]):
                    stdout = io.StringIO()
                    stderr = io.StringIO()
                    with redirect_stdout(stdout), redirect_stderr(stderr):
                        code = main(arguments)
                    raw_stdout = stdout.getvalue()
                    raw_stderr = stderr.getvalue()
                    payload = json.loads(raw_stdout)
                    self.assertEqual(code, 2)
                    self.assertEqual(payload["status"], "ERROR")
                    self.assertEqual(
                        payload["error"]["code"], "BACKUP_RESTORE_REJECTED"
                    )
                    self.assertEqual(raw_stderr, "")
                    self.assertNotIn(private_value, raw_stdout)
                    self.assertNotIn(private_value, raw_stderr)

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
