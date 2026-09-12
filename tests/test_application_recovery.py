from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from neutral_tts.cli import main


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class ApplicationRecoveryEntryTests(unittest.TestCase):
    def invoke(self, args: list[str]) -> tuple[int, dict[str, object]]:
        output = io.StringIO()
        with redirect_stdout(output):
            code = main(args)
        return code, json.loads(output.getvalue())

    @staticmethod
    def child_environment() -> dict[str, str]:
        environment = os.environ.copy()
        source = str(PROJECT_ROOT / "src")
        inherited = environment.get("PYTHONPATH")
        environment["PYTHONPATH"] = source if not inherited else source + os.pathsep + inherited
        return environment

    @staticmethod
    def common_args(root: Path) -> list[str]:
        return [
            "--text",
            "synthetic restart input",
            "--title",
            "restart",
            "--date",
            "20260906",
            "--time",
            "004500",
            "--output-dir",
            str(root / "output"),
            "--staging-dir",
            str(root / "recovery"),
        ]

    def test_normal_cli_generation_uses_completed_recovery_journal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            code, payload = self.invoke(["demo-run", *self.common_args(root)])

            self.assertEqual(code, 0)
            self.assertEqual(payload["outcome"]["operation"], "NEW_GENERATION")
            startup = payload["outcome"]["startup_recovery"]
            self.assertEqual(startup["operation"], "STARTUP_RECOVERY")
            self.assertFalse(startup["backend_invoked"])
            journals = list((root / "recovery" / "journals").glob("*.json"))
            self.assertEqual(len(journals), 1)
            self.assertEqual(json.loads(journals[0].read_text(encoding="utf-8"))["state"], "completed")
            self.assertEqual(len(list((root / "output").glob("*.wav"))), 1)

    def test_process_death_during_generation_is_reconciled_before_new_run(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            script = r'''
import os
import sys
import neutral_tts.cli as cli
from neutral_tts.demo_backend import DEMO_SILENCE_ONLY

class ExitDuringGeneration(DEMO_SILENCE_ONLY):
    def generate(self, request):
        super().generate(request)
        os._exit(91)

cli.DEMO_SILENCE_ONLY = ExitDuringGeneration
cli.main([
    "demo-run", "--text", "synthetic restart input", "--title", "restart",
    "--date", "20260906", "--time", "004500",
    "--output-dir", sys.argv[1], "--staging-dir", sys.argv[2],
])
'''
            interrupted = subprocess.run(
                [sys.executable, "-c", script, str(root / "output"), str(root / "recovery")],
                cwd=PROJECT_ROOT,
                env=self.child_environment(),
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
            self.assertEqual(interrupted.returncode, 91)
            self.assertFalse(list((root / "output").glob("*.wav")))

            code, payload = self.invoke(["demo-run", *self.common_args(root)])
            self.assertEqual(code, 0)
            startup = payload["outcome"]["startup_recovery"]
            self.assertEqual(startup["status"], "RECOVERED")
            self.assertEqual(startup["generation"]["quarantined_runs"], 1)
            self.assertFalse(startup["backend_invoked"])
            self.assertEqual(len(list((root / "output").glob("*.wav"))), 1)

    def test_process_death_after_completed_transition_keeps_one_output_on_startup(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            script = r'''
import os
import sys
import neutral_tts.application as application
import neutral_tts.cli as cli

RealRecoveryManager = application.RecoveryManager

def recovery_manager(recovery, output):
    def exit_after_completed(transition):
        if transition == "completed":
            os._exit(92)
    return RealRecoveryManager(recovery, output, fault_injector=exit_after_completed)

application.RecoveryManager = recovery_manager
cli.main([
    "demo-run", "--text", "synthetic restart input", "--title", "restart",
    "--date", "20260906", "--time", "004500",
    "--output-dir", sys.argv[1], "--staging-dir", sys.argv[2],
])
'''
            interrupted = subprocess.run(
                [sys.executable, "-c", script, str(root / "output"), str(root / "recovery")],
                cwd=PROJECT_ROOT,
                env=self.child_environment(),
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
            self.assertEqual(interrupted.returncode, 92)
            before = list((root / "output").glob("*.wav"))
            self.assertEqual(len(before), 1)
            before_bytes = before[0].read_bytes()

            code, payload = self.invoke(["demo-preview", *self.common_args(root)])
            self.assertEqual(code, 0)
            startup = payload["startup_recovery"]
            self.assertEqual(startup["status"], "VERIFIED")
            self.assertEqual(startup["generation"]["verified_completed_outputs"], 1)
            self.assertEqual(startup["generation"]["recovered_publications"], 0)
            self.assertFalse(startup["backend_invoked"])
            after = list((root / "output").glob("*.wav"))
            self.assertEqual(len(after), 1)
            self.assertEqual(after[0].read_bytes(), before_bytes)

    def test_linked_publication_is_completed_by_startup_without_backend_rerun(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            script = r'''
import os
import sys
import neutral_tts.application as application
import neutral_tts.cli as cli

RealRecoveryManager = application.RecoveryManager

def recovery_manager(recovery, output):
    def exit_after_link(transition):
        if transition == "publication_linked":
            os._exit(93)
    return RealRecoveryManager(recovery, output, fault_injector=exit_after_link)

application.RecoveryManager = recovery_manager
cli.main([
    "demo-run", "--text", "synthetic restart input", "--title", "restart",
    "--date", "20260906", "--time", "004500",
    "--output-dir", sys.argv[1], "--staging-dir", sys.argv[2],
])
'''
            interrupted = subprocess.run(
                [sys.executable, "-c", script, str(root / "output"), str(root / "recovery")],
                cwd=PROJECT_ROOT,
                env=self.child_environment(),
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
            self.assertEqual(interrupted.returncode, 93)
            before = list((root / "output").glob("*.wav"))
            self.assertEqual(len(before), 1)
            before_bytes = before[0].read_bytes()

            code, payload = self.invoke(["demo-preview", *self.common_args(root)])
            self.assertEqual(code, 0)
            startup = payload["startup_recovery"]
            self.assertEqual(startup["status"], "RECOVERED")
            self.assertEqual(startup["generation"]["recovered_publications"], 1)
            self.assertEqual(startup["generation"]["verified_completed_outputs"], 0)
            self.assertFalse(startup["backend_invoked"])
            after = list((root / "output").glob("*.wav"))
            self.assertEqual(len(after), 1)
            self.assertEqual(after[0].read_bytes(), before_bytes)

    def test_competing_ordinary_startup_is_rejected_until_owner_exits(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            marker = root / "backend-ready"
            release = root / "backend-release"
            script = r'''
import sys
import time
from pathlib import Path
import neutral_tts.cli as cli
from neutral_tts.demo_backend import DEMO_SILENCE_ONLY

class BlockingBackend(DEMO_SILENCE_ONLY):
    def generate(self, request):
        Path(sys.argv[3]).write_text("ready", encoding="utf-8")
        deadline = time.monotonic() + 15
        while not Path(sys.argv[4]).exists():
            if time.monotonic() >= deadline:
                raise RuntimeError("test release was not observed")
            time.sleep(0.02)
        return super().generate(request)

cli.DEMO_SILENCE_ONLY = BlockingBackend
raise SystemExit(cli.main([
    "demo-run", "--text", "synthetic restart input", "--title", "restart",
    "--date", "20260906", "--time", "004500",
    "--output-dir", sys.argv[1], "--staging-dir", sys.argv[2],
]))
'''
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    script,
                    str(root / "output"),
                    str(root / "recovery"),
                    str(marker),
                    str(release),
                ],
                cwd=PROJECT_ROOT,
                env=self.child_environment(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                deadline = time.monotonic() + 10
                while not marker.exists() and process.poll() is None:
                    if time.monotonic() >= deadline:
                        self.fail("generation owner did not reach the test hold point")
                    time.sleep(0.02)
                self.assertIsNone(process.poll())

                code, payload = self.invoke(["demo-preview", *self.common_args(root)])
                self.assertEqual(code, 2)
                self.assertEqual(payload["status"], "ERROR")
            finally:
                release.write_text("release", encoding="utf-8")
                try:
                    stdout, stderr = process.communicate(timeout=20)
                except subprocess.TimeoutExpired:
                    process.kill()
                    stdout, stderr = process.communicate(timeout=5)
            self.assertEqual(process.returncode, 0, msg=stdout + stderr)

            code, payload = self.invoke(["demo-preview", *self.common_args(root)])
            self.assertEqual(code, 0)
            self.assertEqual(payload["startup_recovery"]["status"], "VERIFIED")


if __name__ == "__main__":
    unittest.main()
