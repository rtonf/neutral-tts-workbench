import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from neutral_tts.environment_binding import environment_digest


class EnvironmentBindingTests(unittest.TestCase):
    def test_same_version_code_native_and_unrecorded_module_changes_invalidate(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            package = root / "example"
            package.mkdir()
            code = package / "__init__.py"
            code.write_bytes(b"VERSION = 1\n")
            binary = package / "native.dll"
            binary.write_bytes(b"synthetic native fixture")
            executable = root / "python.exe"
            executable.write_bytes(b"synthetic interpreter fixture")
            stdlib = root / "stdlib"
            stdlib.mkdir()
            class Distribution:
                version = "1.0"
                files = ["example/__init__.py", "example/native.dll"]
                def locate_file(self, value):
                    return root / value
            with patch("neutral_tts.environment_binding._host_identity", return_value="test-host") as host, patch("neutral_tts.environment_binding._installed_closure", return_value=[("example", Distribution())]), patch("sys.executable", str(executable)), patch("sys.base_prefix", str(root)), patch("sysconfig.get_path", return_value=str(stdlib)):
                original = environment_digest()
                code.touch()
                self.assertEqual(original, environment_digest())
                code.write_bytes(b"VERSION = 2\n")
                changed_code = environment_digest()
                self.assertNotEqual(original, changed_code)
                binary.write_bytes(b"changed native fixture")
                changed_native = environment_digest()
                self.assertNotEqual(changed_code, changed_native)
                (package / "unrecorded.py").write_bytes(b"VALUE = 3\n")
                added = environment_digest()
                self.assertNotEqual(changed_native, added)
                host.return_value = "changed-driver-or-device"
                self.assertNotEqual(added, environment_digest())
