from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from neutral_tts.install_contract import verify_layout
from tools.build_install_layout import main


class InstallLayoutToolTests(unittest.TestCase):
    def test_cli_builds_demo_only_layout_without_printing_local_paths(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name).absolute()
            source = root / "source"
            source.mkdir()
            (source / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
            spec = root / "spec.json"
            spec.write_text(
                json.dumps(
                    {
                        "schema_version": "neutral-test-layout-input-v1",
                        "candidate_id": "source-demo-v1",
                        "app_root_name": "NeutralApp",
                        "source": {"commit": "a" * 40, "tree": "b" * 40},
                        "files": [{"path": "module.py", "role": "source"}],
                    }
                ),
                encoding="utf-8",
            )
            destination = root / "layout"
            output = io.StringIO()
            with redirect_stdout(output):
                status = main(
                    [
                        "--source-root", str(source),
                        "--destination-root", str(destination),
                        "--spec", str(spec),
                    ]
                )
            payload = json.loads(output.getvalue())
            self.assertEqual(status, 0)
            self.assertEqual(payload["verification_scope"], "DEMO_SOURCE_ONLY")
            self.assertEqual(payload["external_payload_count"], 0)
            self.assertNotIn(str(root), output.getvalue())
            self.assertEqual(verify_layout(destination)["candidate_id"], "source-demo-v1")

    def test_cli_rejects_an_inexact_spec_with_a_fixed_error(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name).absolute()
            source = root / "source"
            source.mkdir()
            spec = root / "spec.json"
            spec.write_text('{"schema_version":"unsupported"}', encoding="utf-8")
            output = io.StringIO()
            with redirect_stdout(output):
                status = main(
                    [
                        "--source-root", str(source),
                        "--destination-root", str(root / "layout"),
                        "--spec", str(spec),
                    ]
                )
            payload = json.loads(output.getvalue())
            self.assertEqual(status, 2)
            self.assertEqual(payload["code"], "SOURCE_LAYOUT_REJECTED")
            self.assertNotIn(str(root), output.getvalue())


if __name__ == "__main__":
    unittest.main()
