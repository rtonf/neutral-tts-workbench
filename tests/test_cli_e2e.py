from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

import neutral_tts.application as application_module
from neutral_tts.cli import DEMO_TOKENIZER_IDENTITY, main


class CliE2ETests(unittest.TestCase):
    def test_private_text_file_and_standard_input_preview_without_echo(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            source = root / "input.txt"
            source.write_text("private synthetic input", encoding="utf-8-sig")
            common = ["--output-dir", str(root / "output"), "--staging-dir", str(root / "stage")]
            for value in (str(source), "-"):
                with mock.patch("sys.stdin", io.StringIO("private synthetic input")):
                    code, payload = self.invoke(["demo-preview", "--text-file", value, *common])
                self.assertEqual(code, 0)
                self.assertNotIn("private synthetic input", repr(payload))
                self.assertNotIn(str(root), repr(payload))

    def test_oversized_or_conflicting_input_rejects_before_host_or_root_creation(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            for options in (["--text", "x" * 4097], ["--text", "x", "--text-file", "-"], ["--text", "  "]):
                code, _ = self.invoke(["real-run", "--runtime-config", str(root / "runtime.json"),
                                      "--output-dir", str(root / "out"), "--staging-dir", str(root / "stage"), *options])
                self.assertEqual(code, 2)
                self.assertFalse((root / "out").exists())
                self.assertFalse((root / "stage").exists())

    def test_invalid_command_does_not_echo_private_argument(self):
        output, errors = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            code = main(["private-invalid-command"])
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(output.getvalue())["error"]["code"], "INVALID_ARGUMENTS")
        self.assertNotIn("private-invalid-command", output.getvalue() + errors.getvalue())

    @staticmethod
    def snapshot_tree(
        root: Path,
    ) -> tuple[tuple[str, ...], tuple[tuple[str, bytes], ...]]:
        paths = tuple(root.rglob("*"))
        directories = tuple(
            sorted(path.relative_to(root).as_posix() for path in paths if path.is_dir())
        )
        files = tuple(
            sorted(
                (path.relative_to(root).as_posix(), path.read_bytes())
                for path in paths
                if path.is_file()
            )
        )
        return directories, files

    def invoke(self, args: list[str]) -> tuple[int, dict[str, object]]:
        output = io.StringIO()
        with redirect_stdout(output):
            code = main(args)
        return code, json.loads(output.getvalue())

    def test_preview_and_demo_run_are_machine_readable_and_labeled(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            args = ["--text", "raw private text", "--output-dir", str(root / "out"), "--staging-dir", str(root / "stage")]
            code, preview = self.invoke(["demo-preview", *args])
            self.assertEqual(code, 0)
            self.assertEqual(preview["status"], "READY")
            self.assertEqual(preview["startup_recovery"]["operation"], "STARTUP_RECOVERY")
            self.assertFalse(preview["startup_recovery"]["backend_invoked"])
            self.assertNotIn("raw private text", repr(preview))
            code, result = self.invoke(["demo-run", *args])
            self.assertEqual(code, 0)
            self.assertEqual(result["status"], "SUCCEEDED")
            self.assertEqual(result["outcome"]["operation"], "NEW_GENERATION")
            self.assertFalse(result["demo"]["speech"])
            self.assertEqual(result["demo"]["tokenizer_identity"], DEMO_TOKENIZER_IDENTITY)
            self.assertIn("filename", result["preflight"])
            self.assertIn("duration_seconds", result["preflight"])
            self.assertIn("storage_mib", result["preflight"])
            self.assertIn("staging_mib", result["preflight"])
            self.assertTrue(list((root / "out").glob("*.wav")))

    def test_preview_rejects_root_replacement_before_owner_acquisition(self):
        for role in ("out", "stage"):
            with self.subTest(role=role), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                target = root / role
                retained = root / f"original-{role}"
                args = [
                    "--text",
                    "synthetic input",
                    "--output-dir",
                    str(root / "out"),
                    "--staging-dir",
                    str(root / "stage"),
                ]
                real_owner = application_module._RecoveryOwner

                def replace_before_owner(recovery_directory, *, expected_identity=None):
                    target.rename(retained)
                    target.mkdir()
                    return real_owner(
                        recovery_directory,
                        expected_identity=expected_identity,
                    )

                with mock.patch.object(
                    application_module,
                    "_RecoveryOwner",
                    side_effect=replace_before_owner,
                ):
                    code, payload = self.invoke(["demo-preview", *args])

                self.assertEqual(code, 2)
                self.assertEqual(payload["status"], "ERROR")
                self.assertEqual(
                    payload["error"],
                    {
                        "code": "WORKFLOW_REJECTED",
                        "message": "The request could not be completed.",
                    },
                )
                retry_code, retry_payload = self.invoke(["demo-preview", *args])
                self.assertEqual(retry_code, 0)
                self.assertEqual(retry_payload["status"], "READY")

    def test_optional_lifecycle_options_reject_before_cli_side_effects(self):
        for command in ("demo-preview", "demo-run"):
            for configuration, lifecycle_args in (
                ("restore", ("--restore-live-data-root", "{target}")),
                (
                    "install-pair",
                    (
                        "--install-app-root",
                        "{target}",
                        "--install-user-data-root",
                        "{external}",
                    ),
                ),
                ("install-app-only", ("--install-app-root", "{target}")),
                ("install-user-only", ("--install-user-data-root", "{external}")),
            ):
                with (
                    self.subTest(command=command, configuration=configuration),
                    tempfile.TemporaryDirectory() as temporary,
                ):
                    root = Path(temporary)
                    target = root / "lifecycle-target"
                    external = root / "external-data"
                    target.mkdir()
                    external.mkdir()
                    restore_journal = root / ".lifecycle-target.restore-journal.json"
                    install_journal = root / ".lifecycle-target.install-journal.json"
                    restore_journal.write_bytes(b"existing-restore-journal")
                    install_journal.write_bytes(b"existing-install-journal")
                    (external / "marker").write_bytes(b"external-unchanged")
                    rendered_lifecycle_args = [
                        value.format(target=target, external=external)
                        for value in lifecycle_args
                    ]
                    output = root / "new-output"
                    staging = root / "new-staging"
                    args = [
                        command,
                        "--text",
                        "synthetic input",
                        "--output-dir",
                        str(output),
                        "--staging-dir",
                        str(staging),
                        *rendered_lifecycle_args,
                    ]
                    before = self.snapshot_tree(root)

                    code, payload = self.invoke(args)

                    self.assertEqual(code, 2)
                    self.assertEqual(payload["status"], "ERROR")
                    self.assertEqual(
                        payload["error"],
                        {
                            "code": "UNSUPPORTED_LIFECYCLE_INTEGRATION",
                            "message": (
                                "Automatic restore/install lifecycle integration "
                                "is not supported by this application host."
                            ),
                        },
                    )
                    self.assertEqual(self.snapshot_tree(root), before)
                    self.assertFalse(output.exists())
                    self.assertFalse(staging.exists())
                    self.assertEqual(
                        restore_journal.read_bytes(), b"existing-restore-journal"
                    )
                    self.assertEqual(
                        install_journal.read_bytes(), b"existing-install-journal"
                    )
                    self.assertEqual(
                        (external / "marker").read_bytes(), b"external-unchanged"
                    )

    def test_error_payload_uses_fixed_privacy_safe_message(self):
        drive_path = "C:" + "/secret/path.wav"
        output_path = "C:" + "/secret/path"
        staging_path = "C:" + "/secret/stage"
        with mock.patch(
            "neutral_tts.cli._build_service",
            side_effect=RuntimeError("raw private text " + drive_path),
        ):
            code, payload = self.invoke(
                [
                    "demo-preview",
                    "--text",
                    "raw private text",
                    "--output-dir",
                    output_path,
                    "--staging-dir",
                    staging_path,
                ]
            )
        self.assertEqual(code, 2)
        self.assertEqual(payload["error"], {"code": "WORKFLOW_REJECTED", "message": "The request could not be completed."})
        self.assertNotIn("raw private text", repr(payload))
        self.assertNotIn("secret", repr(payload))


if __name__ == "__main__":
    unittest.main()
