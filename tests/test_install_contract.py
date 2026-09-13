from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import neutral_tts.install_contract as install_contract_module

from neutral_tts.install_contract import (
    InstallContractError,
    InstallIntegrityError,
    InstallPathError,
    InstallStateError,
    build_manifest,
    canonical_relative_path,
    create_test_layout,
    install_layout,
    load_manifest,
    reconcile_install_lifecycle,
    rollback_upgrade,
    uninstall_owned,
    upgrade_layout,
    verify_layout,
)


COMMIT_A = "1" * 40
TREE_A = "2" * 40
COMMIT_B = "3" * 40
TREE_B = "4" * 40


class InjectedFailure(RuntimeError):
    pass


class InstallContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).absolute()
        self.source_a = self.root / "source-a"
        self.source_b = self.root / "source-b"
        self.source_a.mkdir()
        self.source_b.mkdir()
        for source, marker in ((self.source_a, "one"), (self.source_b, "two")):
            (source / "src").mkdir()
            (source / "src" / "app.py").write_text(f"VALUE = {marker!r}\n", encoding="utf-8")
            (source / "README.md").write_text(f"source layout {marker}\n", encoding="utf-8")
        self.included = {"README.md": "documentation", "src/app.py": "source"}
        self.user_data = self.root / "user-data-root"
        self.user_data.mkdir()
        (self.user_data / "keep.txt").write_text("user owned\n", encoding="utf-8")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def make_layout(self, name: str, version: int) -> Path:
        source = self.source_a if version == 1 else self.source_b
        commit = COMMIT_A if version == 1 else COMMIT_B
        tree = TREE_A if version == 1 else TREE_B
        destination = self.root / name
        create_test_layout(
            source,
            destination,
            self.included,
            candidate_id=f"source-demo-v{version}",
            source_commit=commit,
            source_tree=tree,
            app_root_name="NeutralApp",
        )
        return destination

    def make_junction(self, link: Path, target: Path) -> None:
        if os.name != "nt":
            link.symlink_to(target, target_is_directory=True)
            return
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if result.returncode:
            self.skipTest("junction creation is unavailable")

    def test_relative_paths_reject_windows_aliases_and_traversal(self) -> None:
        self.assertEqual(canonical_relative_path("src/module.py"), "src/module.py")
        drive_path = "".join(("Z", ":", "/rooted.py"))
        unc_path = "".join(("/", "/", "host/share.py"))
        backslash_path = "".join(("src", chr(92), "module.py"))
        rejected = (
            "../module.py",
            "src/../module.py",
            "/rooted.py",
            drive_path,
            unc_path,
            backslash_path,
            "src/file.py:stream",
            "src/con.txt",
            "src/trailing. ",
            "src//module.py",
        )
        for value in rejected:
            with self.subTest(value=value), self.assertRaises(InstallPathError):
                canonical_relative_path(value)

    def test_manifest_is_deterministic_and_rejects_external_payloads(self) -> None:
        first = build_manifest(
            self.source_a,
            self.included,
            candidate_id="source-demo-v1",
            source_commit=COMMIT_A,
            source_tree=TREE_A,
            app_root_name="NeutralApp",
        )
        second = build_manifest(
            self.source_a,
            dict(reversed(tuple(self.included.items()))),
            candidate_id="source-demo-v1",
            source_commit=COMMIT_A,
            source_tree=TREE_A,
            app_root_name="NeutralApp",
        )
        self.assertEqual(first, second)
        (self.source_a / "native.exe").write_bytes(b"MZ\x00\x00")
        with self.assertRaises(InstallIntegrityError):
            build_manifest(
                self.source_a,
                {"native.exe": "source"},
                candidate_id="source-demo-v1",
                source_commit=COMMIT_A,
                source_tree=TREE_A,
                app_root_name="NeutralApp",
            )

    def test_layout_build_is_fresh_exact_and_no_overwrite(self) -> None:
        layout = self.make_layout("layout-v1", 1)
        manifest = verify_layout(layout)
        before = hashlib.sha256((layout / "src" / "app.py").read_bytes()).hexdigest()
        with self.assertRaises(InstallStateError):
            create_test_layout(
                self.source_b,
                layout,
                self.included,
                candidate_id="source-demo-v2",
                source_commit=COMMIT_B,
                source_tree=TREE_B,
                app_root_name="NeutralApp",
            )
        self.assertEqual(manifest["candidate_id"], "source-demo-v1")
        self.assertEqual(before, hashlib.sha256((layout / "src" / "app.py").read_bytes()).hexdigest())

    def test_staging_parent_reparse_race_writes_no_candidate_bytes_outside(self) -> None:
        destination = self.root / "raced-layout"
        outside = self.root / "outside-stage"
        outside.mkdir()
        real_make = install_contract_module.make_directories
        replaced = False

        def race(guard, directory):
            nonlocal replaced
            result = real_make(guard, directory)
            if directory.name == "src" and not replaced:
                replaced = True
                directory.rmdir()
                self.make_junction(directory, outside)
            return result

        with patch("neutral_tts.install_contract.make_directories", side_effect=race):
            with self.assertRaises(InstallPathError):
                create_test_layout(
                    self.source_a,
                    destination,
                    self.included,
                    candidate_id="source-race",
                    source_commit=COMMIT_A,
                    source_tree=TREE_A,
                    app_root_name="NeutralApp",
                )
        self.assertFalse((outside / "app.py").exists())

    def test_install_double_run_preserves_completed_output(self) -> None:
        layout = self.make_layout("layout-v1", 1)
        app = self.root / "NeutralApp"
        result = install_layout(layout, app, self.user_data)
        installed = (app / "src" / "app.py").read_bytes()
        self.assertEqual(result.candidate_id, "source-demo-v1")
        with self.assertRaises(InstallStateError):
            install_layout(layout, app, self.user_data)
        self.assertEqual((app / "src" / "app.py").read_bytes(), installed)
        self.assertEqual((self.user_data / "keep.txt").read_text(encoding="utf-8"), "user owned\n")

    def test_upgrade_retains_prior_tree_and_explicit_rollback_preserves_both(self) -> None:
        old_layout = self.make_layout("layout-v1", 1)
        new_layout = self.make_layout("layout-v2", 2)
        app = self.root / "NeutralApp"
        install_layout(old_layout, app, self.user_data)
        upgraded = upgrade_layout(new_layout, app, self.user_data)
        self.assertEqual(load_manifest(app)["candidate_id"], "source-demo-v2")
        self.assertIsNotNone(upgraded.retained_root)
        self.assertEqual(load_manifest(upgraded.retained_root)["candidate_id"], "source-demo-v1")
        rolled_back = rollback_upgrade(app, upgraded.retained_root, self.user_data)
        self.assertEqual(load_manifest(app)["candidate_id"], "source-demo-v1")
        self.assertEqual(load_manifest(rolled_back.retained_root)["candidate_id"], "source-demo-v2")
        self.assertTrue((self.user_data / "keep.txt").exists())

    def test_live_process_owner_blocks_reconcile_and_competing_upgrade(self) -> None:
        old_layout = self.make_layout("owner-old", 1)
        new_layout = self.make_layout("owner-new", 2)
        app = self.root / "NeutralApp"
        install_layout(old_layout, app, self.user_data)
        marker = self.root / "owner-ready"
        release = self.root / "owner-release"
        script = """
import os
import sys
import time
from pathlib import Path
from neutral_tts.install_contract import upgrade_layout

layout, app, user, marker, release = map(Path, sys.argv[1:])

def hold(stage):
    if stage != "upgrade.after_stage":
        return
    marker.write_text(str(os.getpid()), encoding="ascii")
    deadline = time.monotonic() + 15
    while not release.exists():
        if time.monotonic() >= deadline:
            raise RuntimeError("owner hold timed out")
        time.sleep(0.02)

upgrade_layout(layout, app, user, fault_hook=hold)
"""
        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
        process = subprocess.Popen(
            [
                sys.executable,
                "-B",
                "-c",
                script,
                str(new_layout),
                str(app),
                str(self.user_data),
                str(marker),
                str(release),
            ],
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            deadline = time.monotonic() + 10
            while not marker.exists() and process.poll() is None:
                if time.monotonic() >= deadline:
                    self.fail("upgrade owner did not reach the hold point")
                time.sleep(0.02)
            self.assertIsNone(process.poll())
            owner_path = self.root / ".NeutralApp.install-owner.json"
            owner = json.loads(owner_path.read_text(encoding="utf-8"))
            self.assertEqual(owner["pid"], process.pid)
            self.assertEqual(owner["operation"], "upgrade_layout")
            journal_path = self.root / ".NeutralApp.install-journal.json"
            frozen_journal = journal_path.read_bytes()
            with self.assertRaises(InstallStateError):
                reconcile_install_lifecycle(app, self.user_data)
            with self.assertRaises(InstallStateError):
                upgrade_layout(new_layout, app, self.user_data)
            retained = self.root / json.loads(frozen_journal)["retained_name"]
            raw_attempts = {
                "install": lambda: install_layout.__wrapped__(
                    new_layout, app, self.user_data
                ),
                "upgrade": lambda: upgrade_layout.__wrapped__(
                    new_layout, app, self.user_data
                ),
                "rollback": lambda: rollback_upgrade.__wrapped__(
                    app, retained, self.user_data
                ),
                "uninstall": lambda: uninstall_owned.__wrapped__(
                    app, self.user_data
                ),
            }
            for action, attempt in raw_attempts.items():
                with self.subTest(raw_action=action):
                    with self.assertRaisesRegex(
                        InstallStateError, "no process owner"
                    ):
                        attempt()
                    self.assertEqual(journal_path.read_bytes(), frozen_journal)
            self.assertEqual(verify_layout(app)["candidate_id"], "source-demo-v1")
            release.write_text("continue", encoding="ascii")
            stdout, stderr = process.communicate(timeout=10)
            self.assertEqual(process.returncode, 0, stdout + stderr)
            self.assertEqual(verify_layout(app)["candidate_id"], "source-demo-v2")
            rollback_roots = [
                path
                for path in self.root.iterdir()
                if path.name.startswith(".NeutralApp.rollback-") and path.is_dir()
            ]
            self.assertEqual(len(rollback_roots), 1)
            rollback_upgrade(app, rollback_roots[0], self.user_data)
            self.assertEqual(verify_layout(app)["candidate_id"], "source-demo-v1")
        finally:
            if process.poll() is None:
                release.touch(exist_ok=True)
                try:
                    process.communicate(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.communicate(timeout=3)
            else:
                if process.stdout is not None:
                    process.stdout.close()
                if process.stderr is not None:
                    process.stderr.close()

    def test_upgrade_faults_restore_prior_complete_layout(self) -> None:
        for transition in (
            "upgrade.after_stage",
            "upgrade.after_current_relocated",
            "upgrade.after_new_promoted",
        ):
            with self.subTest(transition=transition):
                case = self.root / transition.replace(".", "-")
                case.mkdir()
                user_data = case / "user-data"
                user_data.mkdir()
                (user_data / "keep.txt").write_text("keep", encoding="utf-8")
                app = case / "NeutralApp"
                old_layout = self.make_layout(f"old-{transition.replace('.', '-')}", 1)
                new_layout = self.make_layout(f"new-{transition.replace('.', '-')}", 2)
                install_layout(old_layout, app, user_data)

                def fail(stage: str) -> None:
                    if stage == transition:
                        raise InjectedFailure(stage)

                with self.assertRaises((InjectedFailure, InstallStateError)):
                    upgrade_layout(new_layout, app, user_data, fault_hook=fail)
                self.assertEqual(load_manifest(app)["candidate_id"], "source-demo-v1")
                self.assertEqual((user_data / "keep.txt").read_text(encoding="utf-8"), "keep")

    def test_process_death_during_upgrade_is_reconciled_to_prior_layout(self) -> None:
        old_layout = self.make_layout("process-old", 1)
        new_layout = self.make_layout("process-new", 2)
        app = self.root / "NeutralApp"
        install_layout(old_layout, app, self.user_data)
        script = (
            "import os,sys; from pathlib import Path; "
            "from neutral_tts.install_contract import upgrade_layout; "
            "hook=lambda stage: os._exit(81) if stage == "
            "'upgrade.after_current_relocated' else None; "
            "upgrade_layout(Path(sys.argv[1]),Path(sys.argv[2]),"
            "Path(sys.argv[3]),fault_hook=hook)"
        )
        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
        completed = subprocess.run(
            [sys.executable, "-c", script, str(new_layout), str(app), str(self.user_data)],
            env=environment,
            check=False,
        )
        self.assertEqual(completed.returncode, 81)
        journal_path = self.root / ".NeutralApp.install-journal.json"
        self.assertTrue(journal_path.is_file())
        interrupted = json.loads(journal_path.read_text(encoding="utf-8"))
        self.assertNotEqual(interrupted["owner"]["pid"], os.getpid())
        self.assertTrue(reconcile_install_lifecycle(app, self.user_data))
        self.assertEqual(verify_layout(app)["candidate_id"], "source-demo-v1")
        upgraded = upgrade_layout(new_layout, app, self.user_data)
        self.assertEqual(verify_layout(app)["candidate_id"], "source-demo-v2")
        rollback_upgrade(app, upgraded.retained_root, self.user_data)
        self.assertEqual(verify_layout(app)["candidate_id"], "source-demo-v1")

    def test_upgrade_relocated_root_identity_race_cannot_report_success(self) -> None:
        old_layout = self.make_layout("identity-old", 1)
        new_layout = self.make_layout("identity-new", 2)
        app = self.root / "NeutralApp"
        install_layout(old_layout, app, self.user_data)
        outside = self.root / "outside-upgrade"
        outside.mkdir()
        genuine = self.root / "genuine-old"
        real_rename = os.rename
        raced = False

        def race(source, destination):
            nonlocal raced
            source_path = Path(source)
            destination_path = Path(destination)
            if source_path == app and ".NeutralApp.rollback-" in destination_path.name and not raced:
                raced = True
                real_rename(app, genuine)
                self.make_junction(app, outside)
            return real_rename(source, destination)

        with patch("neutral_tts.install_contract.os.rename", side_effect=race):
            with self.assertRaises(InstallStateError):
                upgrade_layout(new_layout, app, self.user_data)
        self.assertEqual(verify_layout(genuine)["candidate_id"], "source-demo-v1")
        self.assertTrue((self.root / ".NeutralApp.install-journal.json").exists())

    def test_rollback_result_uses_restored_layout_file_count(self) -> None:
        old_layout = self.root / "one-file-old"
        create_test_layout(
            self.source_a,
            old_layout,
            {"README.md": "documentation"},
            candidate_id="source-one-file",
            source_commit=COMMIT_A,
            source_tree=TREE_A,
            app_root_name="NeutralApp",
        )
        new_layout = self.make_layout("two-file-new", 2)
        app = self.root / "NeutralApp"
        install_layout(old_layout, app, self.user_data)
        upgraded = upgrade_layout(new_layout, app, self.user_data)
        restored = rollback_upgrade(app, upgraded.retained_root, self.user_data)
        self.assertEqual(restored.file_count, 1)
        self.assertEqual(verify_layout(app)["file_count"], 1)

    def test_uninstall_moves_only_owned_files_and_preserves_user_data(self) -> None:
        layout = self.make_layout("layout-v1", 1)
        app = self.root / "NeutralApp"
        install_layout(layout, app, self.user_data)
        (app / "operator-note.txt").write_text("not owned", encoding="utf-8")
        result = uninstall_owned(app, self.user_data)
        self.assertTrue((app / "operator-note.txt").exists())
        self.assertFalse((app / "src" / "app.py").exists())
        self.assertIsNotNone(result.retained_root)
        self.assertEqual(verify_layout(result.retained_root)["candidate_id"], "source-demo-v1")
        self.assertEqual((self.user_data / "keep.txt").read_text(encoding="utf-8"), "user owned\n")

    def test_uninstall_fault_rolls_back_without_partial_removal(self) -> None:
        layout = self.make_layout("layout-v1", 1)
        app = self.root / "NeutralApp"
        install_layout(layout, app, self.user_data)

        def fail(stage: str) -> None:
            if stage == "uninstall.after_owned_relocated":
                raise InjectedFailure(stage)

        with self.assertRaises(InstallStateError):
            uninstall_owned(app, self.user_data, fault_hook=fail)
        self.assertEqual(verify_layout(app)["candidate_id"], "source-demo-v1")
        self.assertTrue((self.user_data / "keep.txt").exists())

    def test_process_death_during_uninstall_is_reconciled(self) -> None:
        layout = self.make_layout("process-uninstall", 1)
        app = self.root / "NeutralApp"
        install_layout(layout, app, self.user_data)
        script = (
            "import os,sys; from pathlib import Path; "
            "from neutral_tts.install_contract import uninstall_owned; "
            "hook=lambda stage: os._exit(82) if stage == "
            "'uninstall.after_owned_relocated' else None; "
            "uninstall_owned(Path(sys.argv[1]),Path(sys.argv[2]),fault_hook=hook)"
        )
        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
        completed = subprocess.run(
            [sys.executable, "-c", script, str(app), str(self.user_data)],
            env=environment,
            check=False,
        )
        self.assertEqual(completed.returncode, 82)
        self.assertTrue((self.root / ".NeutralApp.install-journal.json").is_file())
        self.assertTrue(reconcile_install_lifecycle(app, self.user_data))
        self.assertEqual(verify_layout(app)["candidate_id"], "source-demo-v1")

    def test_uninstall_rollback_reparse_race_fails_without_external_write(self) -> None:
        layout = self.make_layout("junction-uninstall", 1)
        app = self.root / "NeutralApp"
        install_layout(layout, app, self.user_data)
        outside = self.root / "outside-uninstall"
        outside.mkdir()
        calls = 0

        def race(stage: str) -> None:
            nonlocal calls
            if stage != "uninstall.after_owned_relocated":
                return
            calls += 1
            if calls == 2:
                source_parent = app / "src"
                source_parent.rmdir()
                self.make_junction(source_parent, outside)
                raise InjectedFailure(stage)

        with self.assertRaises(InstallStateError):
            uninstall_owned(app, self.user_data, fault_hook=race)
        self.assertFalse((outside / "app.py").exists())
        self.assertTrue((self.root / ".NeutralApp.install-journal.json").exists())

    def test_manifest_corruption_is_rejected_before_lifecycle_change(self) -> None:
        layout = self.make_layout("layout-v1", 1)
        manifest_path = layout / "OWNED_FILES.json"
        value = json.loads(manifest_path.read_text(encoding="utf-8"))
        value["files"][0]["sha256"] = "0" * 64
        manifest_path.write_text(
            json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        with self.assertRaises(InstallIntegrityError):
            verify_layout(layout)

    def test_symlink_payload_is_rejected_when_supported(self) -> None:
        link = self.source_a / "linked.py"
        try:
            os.symlink(self.source_a / "src" / "app.py", link)
        except (OSError, NotImplementedError):
            self.skipTest("symlink creation is unavailable on this host")
        with self.assertRaises(InstallContractError):
            build_manifest(
                self.source_a,
                {"linked.py": "source"},
                candidate_id="source-demo-v1",
                source_commit=COMMIT_A,
                source_tree=TREE_A,
                app_root_name="NeutralApp",
            )


if __name__ == "__main__":
    unittest.main()
