from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

import neutral_tts.application as application_module
from neutral_tts.cli import DEMO_TOKENIZER_IDENTITY, main


class CliE2ETests(unittest.TestCase):
    def invoke(self, args: list[str]) -> tuple[int, dict[str, object]]:
        output = io.StringIO()
        with redirect_stdout(output):
            code = main(args)
        return code, json.loads(output.getvalue())

    def test_preview_and_demo_run_are_machine_readable_and_labeled(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            args = ["--text", "raw private text", "--output-dir", str(root / "out"), "--staging-dir", str(root / "stage")]
            code, preview = self.invoke(["preview", *args])
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
                    code, payload = self.invoke(["preview", *args])

                self.assertEqual(code, 2)
                self.assertEqual(payload["status"], "ERROR")
                self.assertEqual(
                    payload["error"],
                    {
                        "code": "WORKFLOW_REJECTED",
                        "message": "The request could not be completed.",
                    },
                )
                retry_code, retry_payload = self.invoke(["preview", *args])
                self.assertEqual(retry_code, 0)
                self.assertEqual(retry_payload["status"], "READY")

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
                    "preview",
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
